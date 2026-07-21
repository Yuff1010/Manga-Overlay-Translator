"""调用 OpenAI 兼容的 LLM API 整批翻译（保持上下文连贯）。"""
import json
import os
import time

import requests
from dotenv import load_dotenv

load_dotenv()

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


PROFILES = _load_profiles()


def list_profiles():
    """供 /config 用。刻意不返回 api_key——它不该离开服务端。"""
    return [{"id": p["id"], "name": p["name"], "models": p["models"]} for p in PROFILES]


def get_profile(pid: str = ""):
    if not pid:
        return PROFILES[0]
    for p in PROFILES:
        if p["id"] == pid:
            return p
    raise ValueError(f"没有编号为 {pid} 的供应商配置，可用：{[p['id'] for p in PROFILES]}")


# 推理型模型（如 deepseek-v4-flash）默认会为每句台词消耗上千 reasoning token，
# 翻译这种简单任务上 97% 的时间都花在"思考"上（实测 30s → 1.5s）。
# 该参数是 DeepSeek 系专有的，其他接口不认识会返回 400 —— 那就自动退回不带它重试。
# 必须按供应商分开记：否则 OpenAI 返回一次 400，会把 DeepSeek 的这项优化也一起关掉。
_no_thinking = {}

SYSTEM_PROMPT = (
    "你是专业漫画翻译。将 JSON 数组中的每句台词翻译成{target}，"
    "口语化、简短、符合漫画语气。台词来自同一页漫画，注意上下文连贯。"
    "OCR 可能有少量错字，按语境纠正。"
    "只返回与输入等长的 JSON 字符串数组，不要输出任何其他内容。"
)


def _post(texts, target: str, prof, model: str, no_thinking: bool):
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT.format(target=target)},
            {"role": "user", "content": json.dumps(texts, ensure_ascii=False)},
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


def _translate_once(texts, target: str, prof, model: str, usage: dict):
    """翻一批。成功返回等长列表；token 用量累加进 usage。"""
    pid = prof["id"]
    nt = _no_thinking.get(pid, True)
    resp = _post(texts, target, prof, model, nt)
    if resp.status_code == 400 and nt:
        # 该接口不认识 thinking 参数 → 只对这个供应商记下，退回普通请求
        _no_thinking[pid] = False
        print(f"[translator] {prof['name']} 不支持 thinking 参数，已退回普通模式")
        resp = _post(texts, target, prof, model, False)
    resp.raise_for_status()
    body = resp.json()
    u = body.get("usage") or {}
    # 失败重试也算钱，所以拿到响应就累加，不等成功与否
    usage["prompt_tokens"] += int(u.get("prompt_tokens") or 0)
    usage["completion_tokens"] += int(u.get("completion_tokens") or 0)

    content = body["choices"][0]["message"]["content"].strip()
    result = json.loads(_strip_fence(content))
    if isinstance(result, list) and len(result) == len(texts):
        return [str(t) for t in result]
    got = len(result) if isinstance(result, list) else "非数组"
    raise ValueError(f"返回条数不符：期望 {len(texts)}，实际 {got}")


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


def _translate_chunk(texts, target: str, prof, model: str, usage: dict):
    """带重试。全部失败才原样返回，保证接口不中断。返回 (译文, 错误说明)。"""
    err = ""
    for i in range(RETRIES):
        try:
            return _translate_once(texts, target, prof, model, usage), ""
        except Exception as e:  # noqa: BLE001
            fatal = _fatal_reason(e)
            if fatal:
                print(f"[translator] {prof['name']} {fatal}，不再重试")
                return texts, f"{prof['name']}：{fatal}"
            err = str(e)[:120]
            print(f"[translator] 第 {i + 1}/{RETRIES} 次失败: {e}")
            if i + 1 < RETRIES:
                time.sleep(1.5 * (i + 1))   # 退避，避开限流
    return texts, f"{prof['name']}：{err}"


def translate_texts(texts, target: str = "中文", profile_id: str = "", model: str = ""):
    """整页台词批量翻译，分批进行，某批失败不影响其余批次。

    返回 (译文列表, usage, 错误说明)。usage 用返回值传出而不是记在模块变量上——
    前端 4 路并发时模块级变量会被互相覆盖。
    """
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    prof = get_profile(profile_id)          # 编号非法会抛 ValueError，由上层转成 400
    model = model or prof["models"][0]
    if not prof["api_key"]:
        return ([f"[未配置 {prof['name']} 的 API_KEY] {t}" for t in texts],
                usage, f"{prof['name']}：未配置 API_KEY")
    out, err = [], ""
    for i in range(0, len(texts), CHUNK):
        part, e = _translate_chunk(texts[i : i + CHUNK], target, prof, model, usage)
        out.extend(part)
        err = err or e          # 记住第一个错误，够定位问题了
    return out, usage, err


def _strip_fence(s: str) -> str:
    """去掉模型可能输出的 ```json ... ``` 围栏。"""
    if s.startswith("```"):
        s = s.split("\n", 1)[-1]
        s = s.rsplit("```", 1)[0]
    return s.strip()
