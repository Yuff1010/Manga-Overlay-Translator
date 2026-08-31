"""调用 OpenAI 兼容的 LLM API 整批翻译（保持上下文连贯）。"""
import json
import os
import threading
import time
from collections import OrderedDict

import requests
from dotenv import load_dotenv

load_dotenv()

# .env 的 mtime 缓存：超过这个时间就重新读一遍配置，省得改了 .env 还要重启服务。
# getmtime 在文件缺失时抛 OSError，做一层兜底；失败就退回"每次都重载"。
_ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
_env_mtime = 0.0
_profiles = []

def _load_profiles():
    """读取 .env 里的若干组供应商配置。

    第一组用无后缀的变量名（LLM_BASE_URL 等），只配这一组时行为和以前一致；
    额外的组用数字后缀 LLM_2_*、LLM_3_*，遇到没配 BASE_URL 的编号就停。
    """
    out = []
    n = 1
    while True:
        p = "LLM_" if n == 1 else f"LLM_{n}_"
        base = os.getenv(p + "BASE_URL", "")
        if not base:
            if n == 1:  # 完全没配时给个默认，保持旧行为
                base = "https://api.deepseek.com/v1"
            else:
                break
        # MODEL 本是单数、MODELS 是列表，但两个都按逗号拆——
        # 把多个模型写进 MODEL 是很自然的笔误，没必要让它静默失败
        names = f"{os.getenv(p + 'MODEL', '')},{os.getenv(p + 'MODELS', '')}"
        models = []
        for m in names.split(","):
            m = m.strip()
            if m and m not in models:
                models.append(m)
        if not models:
            models = ["deepseek-chat"]
        out.append({
            "id": str(n),
            "name": os.getenv(p + "NAME", "") or f"供应商{n}",
            "base_url": base.rstrip("/"),
            "api_key": os.getenv(p + "API_KEY", ""),
            "models": models,
        })
        n += 1
    return out


def get_profiles():
    """返回当前供应商配置列表。.env 变了会自动重载，不用重启服务。

    用模块级 _profiles + mtime 比对实现惰性重载：每次调用先看 .env 的 mtime，
    没变就直接返回缓存；变了才重新 _load_profiles()。load_dotenv() 必须重跑，
    否则 os.getenv 读到的还是启动时缓存的旧值。
    """
    global _profiles, _env_mtime
    try:
        m = os.path.getmtime(_ENV_FILE)
    except OSError:
        m = 0.0
    if m != _env_mtime or not _profiles:
        if m:
            load_dotenv(override=True)   # 重新读盘覆盖 os.environ
        _profiles = _load_profiles()
        _env_mtime = m
    return _profiles


def list_profiles():
    """供 /config 用。刻意不返回 api_key——它不该离开服务端；
    只回一个 has_key 布尔，让状态页能如实显示是否配过密钥。"""
    return [
        {
            "id": p["id"],
            "name": p["name"],
            "models": p["models"],
            "has_key": bool(p["api_key"]),
        }
        for p in get_profiles()
    ]


def get_profile(pid: str = ""):
    profs = get_profiles()
    if not pid:
        return profs[0]
    for p in profs:
        if p["id"] == pid:
            return p
    raise ValueError(f"没有编号为 {pid} 的供应商配置，可用：{[p['id'] for p in profs]}")


# 推理型模型（如 deepseek-v4-flash）默认会为每句台词消耗上千 reasoning token，
# 翻译这种简单任务上 97% 的时间都花在"思考"上（实测 30s → 1.5s）。
# 该参数是 DeepSeek 系专有的，其他接口不认识会返回 400 —— 那就自动退回不带它重试。
# 必须按供应商分开记：否则 OpenAI 返回一次 400，会把 DeepSeek 的这项优化也一起关掉。
_no_thinking = {}

SYSTEM_PROMPT = (
    "你是专业漫画翻译。输入是同一页漫画里 OCR 出来的文本数组，"
    "每项是字符串，或形如 {{\"i\": 序号, \"pos\": \"右列·上部\", \"t\": 文本}} 的对象。\n"
    "pos 是该块在页面上的大致位置（列×上下）。判断阅读顺序时参考它："
    "日漫竖排从右往左读，韩漫条漫从上往下。\n"
    "对每一项输出一个对象 {{\"t\": 译文, \"d\": 1或0}}：\n"
    "t —— 翻译成{target}，口语化、简短、符合漫画语气；"
    "同页台词注意上下文连贯，OCR 可能有错字，按语境纠正。\n"
    "d —— 这一项是不是人物对白或旁白正文。"
    "是则填 1；音效拟声词、站点水印网址、页码、残缺乱码填 0。\n"
    "只返回与输入等长的 JSON 数组，不要输出任何其他内容。"
)

# 术语表收集指令单独拼在 system 消息尾部：GLOSSARY=0 时不出现，
# 省得既不收集又让模型白白输出 g 字段。
GLOSS_ASK = (
    "g —— 可选。对重要专名（角色名/地名/招式名等），"
    '在对应输出项加 "g": {"s": 原文, "t": 译名"}；其余省略 g。'
)


def pos_meta(x, y, w, h, img_w, img_h):
    """块中心相对页面的位置描述，如「右列·上部」。

    刻意脱敏成三列×三行：原始像素坐标对模型没有意义还占 token，
    相对位置才是还原阅读顺序（竖排右→左）所需的信息。
    """
    def third(v, names):
        return names[min(2, int(v * 3))]

    cx = (x + w / 2) / img_w if img_w else 0.5
    cy = (y + h / 2) / img_h if img_h else 0.5
    return f"{third(cx, ('左列', '中列', '右列'))}·{third(cy, ('上部', '中部', '下部'))}"


def _gloss_enabled():
    """GLOSSARY=0 关闭术语表（收集与注入都停）。默认开。"""
    return os.getenv("GLOSSARY", "1").strip().lower() not in {"0", "false", "no", "off"}


class _Gloss:
    """跨页术语表（进程内存）。

    模型在输出项里附带 "g": {"s","t"} 建议译名；同一原文被建议 PROMOTE_N 次
    才晋升入表——单次 OCR 错认或翻译抖动不会被固化传播。之后注入 system
    prompt 让译名跨页一致。只存内存：重启即清空，而一次阅读会话 ≈ 一部
    漫画，正好是需要的生命周期。
    """

    PROMOTE_N = 2       # 晋升阈值
    MAX_ENTRIES = 300   # 每个目标语言的表上限，超出逐出最久未用
    INJECT_MAX = 60     # prompt 里最多注入多少条，控制 token

    def __init__(self):
        self._lock = threading.Lock()
        self._pending = {}   # target -> {src: {"t": dst, "n": 次数}}
        self._table = {}     # target -> OrderedDict[src] = dst

    def note(self, target, entries):
        for e in entries:
            if not isinstance(e, dict):
                continue
            s = str(e.get("s") or "").strip()
            t = str(e.get("t") or "").strip()
            if not s or not t or s == t:
                continue
            with self._lock:
                tab = self._table.setdefault(target, OrderedDict())
                if s in tab:
                    tab.move_to_end(s)     # 已在表中：不改译名，只刷新热度
                    continue
                pend = self._pending.setdefault(target, {})
                rec = pend.get(s) or {"t": t, "n": 0}
                rec["n"] += 1
                pend[s] = rec
                # 两次建议译名不一致时以最近一次为准；配合晋升阈值，
                # 偶发抖动到不了 PROMOTE_N，进不了表
                rec["t"] = t
                if rec["n"] >= self.PROMOTE_N:
                    tab[s] = t
                    pend.pop(s, None)
                    while len(tab) > self.MAX_ENTRIES:
                        tab.popitem(last=False)

    def block(self, target):
        """拼进 system prompt 的术语表文本；无表或被关闭时返回空。"""
        if not _gloss_enabled():
            return ""
        with self._lock:
            tab = self._table.get(target) or {}
            items = list(tab.items())[-self.INJECT_MAX:]
        return "\n".join(f"{s} → {t}" for s, t in items)


gloss = _Gloss()


def _system_content(target):
    """system 消息：基础 prompt + 可选的 g 收集指令 + 可选的术语表。"""
    c = SYSTEM_PROMPT.format(target=target)
    if _gloss_enabled():
        c += "\n" + GLOSS_ASK
        g = gloss.block(target)
        if g:
            c += "\n术语表（固定译名，必须沿用）：\n" + g
    return c


def _build_user_content(texts, metas=None):
    """user 消息。有 metas（与 texts 等长的位置描述列表）时带上序号和
    位置帮模型还原阅读顺序；没有就保持旧的纯字符串数组。"""
    if metas is None:
        return json.dumps(texts, ensure_ascii=False)
    items = [{"i": i, "pos": pos, "t": t} for i, (t, pos) in enumerate(zip(texts, metas))]
    return json.dumps(items, ensure_ascii=False)


def _note_gloss(target, result):
    """从模型输出里提取 "g" 术语建议记入表。任何异常都不影响翻译主链路。"""
    try:
        if not _gloss_enabled():
            return
        entries = [
            item["g"]
            for item in result
            if isinstance(item, dict) and isinstance(item.get("g"), dict)
        ]
        if entries:
            gloss.note(target, entries)
    except Exception as e:  # noqa: BLE001
        print(f"[translator] 记录术语建议失败（忽略）: {e}")


def _post(texts, target: str, prof, model: str, no_thinking: bool, metas=None):
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": _system_content(target)},
            {"role": "user", "content": _build_user_content(texts, metas)},
        ],
        "temperature": 0.3,
    }
    if no_thinking:
        # 各家关推理模式的参数名不同：DeepSeek 用 thinking，DashScope(Qwen) 用
        # enable_thinking。一起发，不认识的一方通常直接忽略；真返回 400 的话
        # 外层会记下这个供应商并退回不带参数重试。
        body["thinking"] = {"type": "disabled"}
        body["enable_thinking"] = False
    return requests.post(
        f"{prof['base_url']}/chat/completions",
        headers={"Authorization": f"Bearer {prof['api_key']}"},
        json=body,
        timeout=90,
    )


RETRIES = 3       # 偶发失败（限流、超时、返回条数对不上）重试几次
CHUNK = 25        # 单次最多翻多少条，太长模型容易少返回几条


def _translate_once(texts, target: str, prof, model: str, usage: dict, metas=None):
    """翻一批。成功返回等长列表；token 用量累加进 usage。"""
    pid = prof["id"]
    nt = _no_thinking.get(pid, True)
    resp = _post(texts, target, prof, model, nt, metas)
    if resp.status_code == 400 and nt:
        # 该接口不认识 thinking 参数 → 只对这个供应商记下，退回普通请求
        _no_thinking[pid] = False
        print(f"[translator] {prof['name']} 不支持 thinking 参数，已退回普通模式")
        resp = _post(texts, target, prof, model, False, metas)
    resp.raise_for_status()
    try:
        body = resp.json()
    except ValueError:
        # 中转/代理偶尔返回 HTML 错误页或空 body；不拦的话 json.loads 抛出的
        # 原始信息很难看，且会被外层当成可重试错误白白重试三轮
        raise ValueError(f"返回不是合法 JSON（HTTP {resp.status_code}）：{resp.text[:120]}")
    u = body.get("usage") or {}
    # 失败重试也算钱，所以拿到响应就累加，不等成功与否
    usage["prompt_tokens"] += int(u.get("prompt_tokens") or 0)
    usage["completion_tokens"] += int(u.get("completion_tokens") or 0)

    choices = body.get("choices") or []
    if not choices:
        # 出错时有些接口返回 {"error": ...} 却带 200，直接 KeyError 会很迷惑
        raise ValueError(f"响应缺少 choices 字段：{str(body)[:120]}")
    content = (choices[0].get("message") or {}).get("content") or ""
    content = content.strip()
    if not content:
        raise ValueError("模型返回了空内容")
    result = json.loads(_strip_fence(content))
    if not isinstance(result, list) or len(result) != len(texts):
        got = len(result) if isinstance(result, list) else "非数组"
        raise ValueError(f"返回条数不符：期望 {len(texts)}，实际 {got}")
    _note_gloss(target, result)
    return _unpack(result, texts)


def _unpack(result, texts):
    """把模型返回拆成 (译文, 是否对白)。

    正常返回 {"t":..., "d":...}；但模型偶尔会退化成纯字符串数组，
    那时按"全部保留"处理，宁可多显示也不要凭空吞掉台词。
    """
    out, dialog = [], []
    for item, src in zip(result, texts):
        if isinstance(item, dict):
            out.append(str(item.get("t", "") or src))
            flag = item.get("d", 1)
            # 兼容模型偶尔把数字/布尔值写成字符串；"0" 不能按 Python 真值算 True
            dialog.append(str(flag).strip().lower() not in {"0", "false", "no", "none", ""})
        else:
            out.append(str(item))
            dialog.append(True)
    return out, dialog


# 这些状态码重试多少次都一样：密钥无效、无权限、地址或模型名不对。
# 不快速失败的话，每张图都要白等三轮退避（约 4.5 秒）并刷满日志。
_FATAL = {401, 403, 404}


def _fatal_reason(e):
    """返回可读的致命原因；不是致命错误就返回空。"""
    resp = getattr(e, "response", None)
    code = getattr(resp, "status_code", None)
    if code not in _FATAL:
        return ""
    return {
        401: "密钥无效或已过期（401）",
        403: "密钥无权限（403）",
        404: "接口地址或模型名不存在（404）",
    }[code]


def _translate_chunk(texts, target: str, prof, model: str, usage: dict, metas=None):
    """带重试。返回 (译文, 是否对白, 错误说明)。全部失败则原样返回原文。"""
    err = ""
    keep_all = [True] * len(texts)      # 兜底时不做过滤，免得把台词也吞了
    for i in range(RETRIES):
        try:
            out, dialog = _translate_once(texts, target, prof, model, usage, metas)
            return out, dialog, ""
        except Exception as e:  # noqa: BLE001
            fatal = _fatal_reason(e)
            if fatal:
                print(f"[translator] {prof['name']} {fatal}，不再重试")
                return texts, keep_all, f"{prof['name']}：{fatal}"
            err = str(e)[:120]
            print(f"[translator] 第 {i + 1}/{RETRIES} 次失败: {e}")
            if i + 1 < RETRIES:
                time.sleep(1.5 * (i + 1))   # 退避，避开限流
    return texts, keep_all, f"{prof['name']}：{err}"


def translate_texts(texts, target: str = "中文", profile_id: str = "", model: str = "",
                    metas=None):
    """整页台词批量翻译，分批进行，某批失败不影响其余批次。

    metas 是与 texts 等长的位置描述列表（pos_meta 的输出），随批一起切片，
    帮模型按页面位置还原阅读顺序；长度不齐时忽略，退回无位置模式。

    返回 (译文列表, 是否对白列表, usage, 错误说明)。usage 用返回值传出而不是
    记在模块变量上——前端 4 路并发时模块级变量会被互相覆盖。
    """
    if metas is not None and len(metas) != len(texts):
        metas = None
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    prof = get_profile(profile_id)          # 编号非法会抛 ValueError，由上层转成 400
    model = model or prof["models"][0]
    if not prof["api_key"]:
        return (list(texts),
                [True] * len(texts), usage, f"{prof['name']}：未配置 API_KEY")
    out, dialog, err = [], [], ""
    for i in range(0, len(texts), CHUNK):
        part, dlg, e = _translate_chunk(
            texts[i : i + CHUNK], target, prof, model, usage,
            metas[i : i + CHUNK] if metas else None,
        )
        out.extend(part)
        dialog.extend(dlg)
        err = err or e          # 记住第一个错误，够定位问题了
    return out, dialog, usage, err


def _strip_fence(s: str) -> str:
    """去掉模型可能输出的 ```json ... ``` 围栏。"""
    if s.startswith("```"):
        s = s.split("\n", 1)[-1]
        s = s.rsplit("```", 1)[0]
    return s.strip()
