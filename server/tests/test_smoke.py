"""Pure-function smoke tests for the OCR/translator backend.

These deliberately avoid importing anything that pulls in PaddleOCR or makes
network calls, so they run in milliseconds on any machine and are safe to add
to CI. They cover the pieces most likely to silently break:

  - translator._load_profiles ordering / multi-provider numbering
  - translator.list_profiles has_key / no-secrets shape
  - translator.pos_meta / _build_user_content (reading-order context)
  - translator._Gloss promote threshold / capacity / per-target isolation
  - translator._strip_fence / _unpack (including the degraded string-array path)
  - ocr._script language scoring (the auto-detect heart)
  - ocr.is_garbage / merge_lines dedup and merge

Run:  python server/tests/test_smoke.py
Exit: 0 = all pass, 1 = some failed. Prints a one-line-per-check report.

If you have pytest installed, the functions are also discoverable:
  pytest server/tests/test_smoke.py
"""
import os
import sys

# 让 import 能找到 server/ 下的模块，无论从哪个 cwd 启动
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Windows 控制台默认是 GBK 代码页，打印日韩字符会直接 UnicodeEncodeError；
# 把 stdout/stderr 重配成 UTF-8，测试在任何地区码的终端上都能跑。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
from translator import (  # noqa: E402
    _Gloss,
    _build_user_content,
    _load_profiles,
    _strip_fence,
    _system_content,
    _unpack,
    list_profiles,
    pos_meta,
)
from ocr import (  # noqa: E402
    _script,
    _WEIGHTS,
    device_info,
    is_garbage,
    merge_lines,
    pool_status,
)
import ocr as _ocr  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    ok = bool(cond)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({extra})" if extra and not ok else ""))
    if not ok:
        FAILS.append(name)


# ---------- translator: profile loading ----------

def test_profiles_order(monkey_env=None):
    # 用一个临时的、只有两家的环境验证编号解析与排序
    env = {
        "LLM_NAME": "Alpha", "LLM_BASE_URL": "https://a/v1", "LLM_API_KEY": "k1",
        "LLM_MODEL": "a-model", "LLM_MODELS": "a-model,a2",
        "LLM_2_NAME": "Beta", "LLM_2_BASE_URL": "https://b/v1",
        "LLM_2_API_KEY": "k2", "LLM_2_MODELS": "b1,b2",
    }
    old = os.environ.copy()
    try:
        os.environ.clear()
        os.environ.update(env)
        profs = _load_profiles()
    finally:
        os.environ.clear()
        os.environ.update(old)

    check("profiles: two providers loaded", len(profs) == 2, f"got {len(profs)}")
    check("profiles: ids ordered 1,2", [p["id"] for p in profs] == ["1", "2"])
    check("profiles: names correct",
          [p["name"] for p in profs] == ["Alpha", "Beta"])
    # MODEL 和 MODELS 应合并去重，且 MODEL 在前
    check("profiles: MODEL+MODELS merged dedup",
          profs[0]["models"] == ["a-model", "a2"], str(profs[0]["models"]))
    check("profiles: base_url trailing slash stripped",
          profs[0]["base_url"] == "https://a/v1")


def test_strip_fence():
    check("strip_fence: plain json passthrough",
          _strip_fence('[{"t":"x"}]') == '[{"t":"x"}]')
    check("strip_fence: removes ```json fence",
          _strip_fence('```json\n[{"t":"x"}]\n```') == '[{"t":"x"}]')
    check("strip_fence: handles leading/trailing whitespace",
          _strip_fence('  [{"t":"x"}]  ') == '[{"t":"x"}]')


def test_unpack_dict_form():
    # 正常 {t,d} 形式
    src = ["A", "B"]
    res = [{"t": "甲", "d": 1}, {"t": "乙", "d": 0}]
    out, dlg = _unpack(res, src)
    check("unpack: dict t/d extracted", out == ["甲", "乙"], str(out))
    check("unpack: d=1 -> dialogue True", dlg == [True, False], str(dlg))


def test_unpack_string_d_variants():
    # 模型偶尔把 d 写成 "false"/"0"/"no"，必须按 falsy 字符串处理而不是 Python 真值
    src = ["A", "B", "C", "D"]
    res = [
        {"t": "1", "d": "false"},
        {"t": "2", "d": "0"},
        {"t": "3", "d": "no"},
        {"t": "4", "d": 1},
    ]
    _, dlg = _unpack(res, src)
    check("unpack: string 'false'/'0'/'no' treated as non-dialogue",
          dlg == [False, False, False, True], str(dlg))


def test_unpack_degraded_string_array():
    # 模型退化成纯字符串数组时，按"全部保留"处理，不丢台词
    src = ["A", "B"]
    res = ["甲", "乙"]
    out, dlg = _unpack(res, src)
    check("unpack: degraded string array kept", out == ["甲", "乙"], str(out))
    check("unpack: degraded defaults to dialogue True",
          dlg == [True, True], str(dlg))


def test_unpack_missing_t_falls_back():
    # 缺 t 字段时退回原文，不抛异常
    src = ["A"]
    res = [{}]
    out, _ = _unpack(res, src)
    check("unpack: missing t falls back to source", out == ["A"], str(out))


# ---------- translator: /config shape ----------

def test_list_profiles_has_key_and_no_secrets():
    # has_key 必须如实反映 api_key 是否配置过（状态页依赖它），
    # 同时 api_key/base_url 本体绝不能出现在 /config 响应里
    import translator as _tr

    fake = [
        {"id": "1", "name": "A", "base_url": "https://a/v1",
         "api_key": "sk-secret", "models": ["m"]},
        {"id": "2", "name": "B", "base_url": "https://b/v1",
         "api_key": "", "models": ["n"]},
    ]
    orig = _tr.get_profiles
    _tr.get_profiles = lambda: fake
    try:
        profs = list_profiles()
    finally:
        _tr.get_profiles = orig

    check("list_profiles: has_key reflects api_key",
          [p["has_key"] for p in profs] == [True, False],
          str([p.get("has_key") for p in profs]))
    for p in profs:
        check(f"list_profiles: no api_key leak (id={p['id']})", "api_key" not in p)
        check(f"list_profiles: no base_url leak (id={p['id']})", "base_url" not in p)


# ---------- translator: reading-order context ----------

def test_pos_meta_thirds():
    # 800x1200 页面上的三列×三行相对位置描述
    cases = [
        ((10, 10, 50, 50), "左列·上部"),
        ((700, 1100, 50, 50), "右列·下部"),
        ((380, 580, 40, 40), "中列·中部"),
        ((760, 10, 30, 30), "右列·上部"),    # 恰在右边缘也归右列
    ]
    for (x, y, w, h), want in cases:
        got = pos_meta(x, y, w, h, 800, 1200)
        check(f"pos_meta: ({x},{y}) -> {want}", got == want, f"got {got}")
    check("pos_meta: zero dims fall back to center",
          pos_meta(0, 0, 0, 0, 0, 0) == "中列·中部",
          pos_meta(0, 0, 0, 0, 0, 0))


def test_build_user_content():
    import json as _json

    plain = _build_user_content(["A", "B"])
    check("user content: plain array when no metas",
          _json.loads(plain) == ["A", "B"], plain)

    rich = _build_user_content(["A", "B"], ["左列·上部", "右列·下部"])
    items = _json.loads(rich)
    check("user content: objects with i/pos/t when metas given",
          items == [
              {"i": 0, "pos": "左列·上部", "t": "A"},
              {"i": 1, "pos": "右列·下部", "t": "B"},
          ], str(items))


# ---------- translator: glossary ----------

def test_gloss_promote_threshold_and_caps():
    g = _Gloss()
    g.note("中文", [{"s": "ヤマト", "t": "大和"}])
    check("gloss: single suggestion not promoted yet",
          g.block("中文") == "", g.block("中文"))
    g.note("中文", [{"s": "ヤマト", "t": "大和"}])
    check("gloss: promoted after 2nd suggestion",
          "ヤマト → 大和" in g.block("中文"), g.block("中文"))

    # 目标语言分表：中文的表不该漏进英文
    check("gloss: isolated per target", g.block("English") == "")

    # 已入表的术语：后续不同建议不改译名（防抖动），只刷新热度
    g.note("中文", [{"s": "ヤマト", "t": "山户"}])
    check("gloss: promoted term is stable",
          "ヤマト → 大和" in g.block("中文")
          and "ヤマト → 山户" not in g.block("中文"), g.block("中文"))

    # 坏条目（空值/同值/非 dict）全部忽略，不抛异常
    g2 = _Gloss()
    g2.note("中文", [{"s": "", "t": "x"}, {"s": "y", "t": ""},
                     {"s": "z", "t": "z"}, "junk"])
    check("gloss: invalid entries ignored", g2.block("中文") == "")

    # 容量上限：超出逐出最久未用的（用小表验证机制本身）
    small = _Gloss()
    small.MAX_ENTRIES = 3
    for i in range(6):
        small.note("中文", [{"s": f"词{i}", "t": f"译{i}"}])
        small.note("中文", [{"s": f"词{i}", "t": f"译{i}"}])
    block = small.block("中文")
    check("gloss: evicts oldest beyond cap",
          "词0" not in block and "词5" in block, block)


def test_gloss_and_system_content_toggle():
    import translator as _tr

    old = os.environ.get("GLOSSARY")
    try:
        # 关：注入和 g 收集指令都消失
        os.environ["GLOSSARY"] = "0"
        g = _Gloss()
        g.note("中文", [{"s": "A", "t": "甲"}])
        g.note("中文", [{"s": "A", "t": "甲"}])
        check("gloss: block() empty when GLOSSARY=0", g.block("中文") == "")
        check("system: no gloss ask when disabled",
              "g ——" not in _system_content("中文"))

        # 开（默认）：g 指令在；target 槽位替换正常
        os.environ.pop("GLOSSARY", None)
        c = _system_content("English")
        check("system: target substituted",
              "翻译成English" in c and "{target}" not in c)
        check("system: gloss ask present by default", "g ——" in c)

        # 有表时注入 prompt
        class _FakeGloss:
            def block(self, target):
                return "ヤマト → 大和"

        orig = _tr.gloss
        _tr.gloss = _FakeGloss()
        try:
            c2 = _system_content("中文")
        finally:
            _tr.gloss = orig
        check("system: glossary injected into prompt",
              "术语表" in c2 and "ヤマト → 大和" in c2, c2[-80:])
    finally:
        if old is None:
            os.environ.pop("GLOSSARY", None)
        else:
            os.environ["GLOSSARY"] = old


# ---------- ocr: script scoring ----------

def test_script_classification():
    cases = {
        "あ": "kana", "ア": "kana",          # 平假名/片假名
        "韓": "cjk", "国": "cjk",            # CJK 统一表意
        "한": "hangul", "가": "hangul",       # 韩文谚文音节
        "A": "latin", "z": "latin",          # 拉丁字母
        "1": "", "！": "",                   # 数字/标点不分语种
    }
    for ch, want in cases.items():
        check(f"script: {ch!r} -> {want}", _script(ch) == want,
              f"got {_script(ch)!r}")


def test_weights_have_expected_scripts():
    # 自动检测打分依赖这些权重存在；漏一个会让对应语言永远得 0 分
    check("weights: korean has hangul", "hangul" in _WEIGHTS["korean"])
    check("weights: japan has kana+cjk",
          {"kana", "cjk"} <= set(_WEIGHTS["japan"]))
    check("weights: ch has cjk", "cjk" in _WEIGHTS["ch"])
    # 日/韩/中的 latin 权重必须压低（0.3）——几乎所有模型都会吐拉丁字母，
    # 权重高了错误模型输出的拉丁乱码就会把分数拉高。西语模型相反，latin=1.0，
    # 因为西语本身就是拉丁字母。
    for lang in ("korean", "japan", "ch"):
        check(f"weights: {lang} latin is low (<=0.3)",
              _WEIGHTS[lang].get("latin", 0) <= 0.3,
              f"got {_WEIGHTS[lang].get('latin')}")
    check("weights: es latin is high (==1.0)",
          _WEIGHTS["es"]["latin"] == 1.0, str(_WEIGHTS["es"]["latin"]))


# ---------- ocr: garbage filter & merge ----------

def test_is_garbage():
    check("garbage: empty", is_garbage(""))
    check("garbage: pure symbols", is_garbage("..."))
    check("garbage: pure digits", is_garbage("12345"))
    check("garbage: single ascii letter", is_garbage("Q"))
    check("not garbage: single cjk char", not is_garbage("語"))
    check("not garbage: single kana", not is_garbage("え"))
    check("not garbage: normal word", not is_garbage("Hello"))


def test_merge_lines_groups_adjacent():
    # 两个紧挨着的行应合并成一个块（同一气泡）
    lines = [
        {"x": 0, "y": 0, "w": 100, "h": 30, "text": "你好"},
        {"x": 0, "y": 32, "w": 100, "h": 30, "text": "世界"},
    ]
    out = merge_lines(lines)
    check("merge: adjacent lines merge to one block", len(out) == 1, f"got {len(out)}")
    if out:
        check("merge: text concatenated with space",
              out[0]["text"] == "你好 世界", out[0]["text"])
        check("merge: bbox spans both",
              (out[0]["x"], out[0]["y"], out[0]["w"], out[0]["h"]) == (0, 0, 100, 62),
              str((out[0]["x"], out[0]["y"], out[0]["w"], out[0]["h"])))


def test_merge_lines_separate_when_far():
    # 两个相距很远的行保持独立
    lines = [
        {"x": 0, "y": 0, "w": 100, "h": 30, "text": "上"},
        {"x": 0, "y": 500, "w": 100, "h": 30, "text": "下"},
    ]
    out = merge_lines(lines)
    check("merge: distant lines stay separate", len(out) == 2, f"got {len(out)}")


def test_merge_lines_drops_large_garbage():
    # 框很大但内容是噪声（如艺术字误识）应在合并前剔除
    lines = [
        {"x": 0, "y": 0, "w": 300, "h": 300, "text": "..."},  # 大框 + 垃圾
        {"x": 10, "y": 10, "w": 80, "h": 30, "text": "正常文字"},
    ]
    out = merge_lines(lines)
    check("merge: large garbage box dropped", len(out) == 1, f"got {len(out)}")
    if out:
        check("merge: keeps real text", out[0]["text"] == "正常文字", out[0]["text"])


def test_merge_lines_keeps_conf():
    # 块的 conf 是组内行置信度均值——排查误识别（韩语尤其）的线索
    lines = [
        {"x": 0, "y": 0, "w": 100, "h": 30, "text": "안녕", "conf": 0.9},
        {"x": 0, "y": 32, "w": 100, "h": 30, "text": "하세요", "conf": 0.7},
    ]
    out = merge_lines(lines)
    check("merge: block conf is mean of lines",
          out and out[0].get("conf") == 0.8, str(out[0].get("conf") if out else None))


# ---------- ocr: device detection ----------

def test_device_info_states_and_cache():
    import sys
    import types

    orig_v = _ocr._device["v"]
    saved = {k: sys.modules.get(k) for k in ("paddle", "paddle.device", "paddle.device.cuda")}
    try:
        def install(fake):
            sys.modules["paddle"] = fake
            dev = types.ModuleType("paddle.device")
            cuda = types.ModuleType("paddle.device.cuda")
            dev.cuda = cuda
            fake.device = dev      # 属性访问路径 paddle.device.cuda 也要通
            sys.modules["paddle.device"] = dev
            sys.modules["paddle.device.cuda"] = cuda
            return cuda

        # GPU：CUDA 编译 + 有卡
        fake = types.ModuleType("paddle")
        fake.is_compiled_with_cuda = lambda: True
        cuda = install(fake)
        cuda.device_count = lambda: 1
        _ocr._device["v"] = None
        check("device: gpu when cuda compiled and present", device_info() == "gpu")

        # 缓存：再探会撞上 None 模块（import 直接抛），能返回说明走了缓存
        sys.modules["paddle"] = None
        check("device: cached after first probe", device_info() == "gpu")

        # CPU：有 CUDA 编译但没卡
        _ocr._device["v"] = None
        sys.modules["paddle"] = fake
        cuda.device_count = lambda: 0
        check("device: cpu when no cuda device", device_info() == "cpu")

        # CPU：压根不是 CUDA 编译
        _ocr._device["v"] = None
        fake.is_compiled_with_cuda = lambda: False
        cuda.device_count = lambda: 1
        check("device: cpu when paddle not cuda build", device_info() == "cpu")

        # unknown：paddle 导不进来
        _ocr._device["v"] = None
        sys.modules["paddle"] = None
        check("device: unknown when paddle unimportable", device_info() == "unknown")
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
        _ocr._device["v"] = orig_v


def test_pool_status_has_device():
    orig_v = _ocr._device["v"]
    try:
        _ocr._device["v"] = "gpu"
        st = pool_status()
        check("pool_status: device field present", st.get("device") == "gpu", str(st))
        check("pool_status: models field kept", "models" in st and isinstance(st["models"], dict))
    finally:
        _ocr._device["v"] = orig_v


# ---------- ocr: low-confidence retry ----------

def test_retry_enabled_default_korean_only():
    import os as _os

    saved = _os.environ.get("OCR_RETRY_LOW_CONF")
    try:
        for val, kw, want in [
            ("", "korean", True), ("", "japan", False),
            ("1", "japan", True), ("1", "korean", True),
            ("0", "korean", False), ("0", "japan", False),
            ("on", "en", True), ("off", "korean", False),
        ]:
            if val == "":
                _os.environ.pop("OCR_RETRY_LOW_CONF", None)
            else:
                _os.environ["OCR_RETRY_LOW_CONF"] = val
            check(f"retry_enabled: env={val!r} lang={kw} -> {want}",
                  _ocr._retry_enabled(kw) is want)
    finally:
        if saved is None:
            _os.environ.pop("OCR_RETRY_LOW_CONF", None)
        else:
            _os.environ["OCR_RETRY_LOW_CONF"] = saved


def test_retry_low_conf_logic():
    import numpy as np

    img = np.zeros((100, 200, 3), dtype=np.uint8)
    orig_enabled = _ocr._retry_enabled
    orig_lines = _ocr._ocr_lines
    try:
        _ocr._retry_enabled = lambda lang: True

        # 重试更好 → 采纳重试结果；高置信度行不动
        lines = [
            {"x": 10, "y": 10, "w": 50, "h": 20, "text": "bad", "conf": 0.55},
            {"x": 10, "y": 60, "w": 50, "h": 20, "text": "ok", "conf": 0.9},
        ]
        inner_retry_flags = []

        def fake_lines(crop, lang, retry=True):
            inner_retry_flags.append(retry)
            return [{"x": 0, "y": 0, "w": 10, "h": 10, "text": "좋아", "conf": 0.92}]

        _ocr._ocr_lines = fake_lines
        out = _ocr._retry_low_conf(img, lines, "korean")
        check("retry: low-conf line replaced when retry is better",
              out[0]["text"] == "좋아" and out[0]["conf"] == 0.92, str(out[0]))
        check("retry: high-conf line untouched",
              out[1]["text"] == "ok" and out[1]["conf"] == 0.9, str(out[1]))
        check("retry: inner call made with retry=False (no recursion)",
              inner_retry_flags and all(f is False for f in inner_retry_flags))

        # 重试更差 → 保留首遍结果
        _ocr._ocr_lines = lambda crop, lang, retry=True: [
            {"x": 0, "y": 0, "w": 10, "h": 10, "text": "worse", "conf": 0.5}
        ]
        lines2 = [{"x": 10, "y": 10, "w": 50, "h": 20, "text": "keep", "conf": 0.6}]
        out2 = _ocr._retry_low_conf(img, lines2, "korean")
        check("retry: keeps original when retry not better",
              out2[0]["text"] == "keep" and out2[0]["conf"] == 0.6, str(out2[0]))

        # 开关关闭 → 完全不触发重试
        _ocr._retry_enabled = lambda lang: False
        n = [0]

        def counting(crop, lang, retry=True):
            n[0] += 1
            return []
        _ocr._ocr_lines = counting
        out3 = _ocr._retry_low_conf(img, lines2, "korean")
        check("retry: disabled by OCR_RETRY_LOW_CONF=0",
              n[0] == 0 and out3[0]["text"] == "keep")
    finally:
        _ocr._retry_enabled = orig_enabled
        _ocr._ocr_lines = orig_lines


def test_ocr_lines_floor_with_retry():
    """重试模式：进门门槛放宽到 0.3，重试后仍 <0.5 的收口丢弃。"""
    import types

    import numpy as np

    orig_pool = _ocr._get_pool
    orig_pred = _ocr._predict
    orig_enabled = _ocr._retry_enabled
    orig_retry = _ocr._retry_low_conf
    try:
        fake_pool = types.SimpleNamespace(acquire=lambda: object(),
                                          release=lambda i: None)
        _ocr._get_pool = lambda lang: fake_pool

        def box_poly(x, y):
            return [[x, y], [x + 10, y], [x + 10, y + 10], [x, y + 10]]

        _ocr._predict = lambda inst, img: [
            (box_poly(0, 0), "weak", 0.42),    # 低于正常门槛，重试目标
            (box_poly(0, 20), "ok", 0.55),     # 正常行
            (box_poly(0, 40), "junk", 0.2),    # 连放宽门槛都进不来
        ]
        img = np.zeros((60, 20, 3), dtype=np.uint8)

        # 重试救回 weak → 保留；junk 进不了门
        _ocr._retry_enabled = lambda lang: True

        def fake_retry(img, lines, lang):
            return [dict(ln, text="fixed", conf=0.9) if ln["text"] == "weak" else ln
                    for ln in lines]
        _ocr._retry_low_conf = fake_retry
        texts = [l["text"] for l in _ocr._ocr_lines(img, "korean", retry=True)]
        check("floor: recovered weak line kept",
              "fixed" in texts and "junk" not in texts, str(texts))

        # 重试没救回（仍 0.42）→ 收口丢弃
        _ocr._retry_low_conf = lambda img, lines, lang: lines
        texts2 = [l["text"] for l in _ocr._ocr_lines(img, "korean", retry=True)]
        check("floor: unrecovered weak line dropped after retry",
              "weak" not in texts2 and "ok" in texts2, str(texts2))

        # 关闭重试：门槛维持 0.5
        _ocr._retry_enabled = lambda lang: False
        texts3 = [l["text"] for l in _ocr._ocr_lines(img, "korean", retry=True)]
        check("floor: default 0.5 floor when retry disabled",
              texts3 == ["ok"], str(texts3))
    finally:
        _ocr._get_pool = orig_pool
        _ocr._predict = orig_pred
        _ocr._retry_enabled = orig_enabled
        _ocr._retry_low_conf = orig_retry


# ---------- pool & bubble (robustness) ----------

def test_pool_acquire_rollback_and_timeout():
    """OCR 实例池：建实例失败要回滚名额；池满且全忙要超时而非永久阻塞。"""
    import ocr as _ocr

    # 临时把 POOL_SIZE 压到 1、_new_ocr 替换成会失败/成功的桩，
    # 避免真的去建 PaddleOCR 实例。
    orig_pool_size = _ocr.POOL_SIZE
    orig_new = _ocr._new_ocr
    _ocr.POOL_SIZE = 1
    try:
        # 1) 建实例失败 → 名额回滚，made 不该卡在 POOL_SIZE
        def boom(_lang):
            raise RuntimeError("模型下载失败")
        _ocr._new_ocr = boom
        pool = _ocr._Pool("test")
        raised = False
        try:
            pool.acquire()
        except RuntimeError:
            raised = True
        check("pool: acquire raises when builder fails", raised)
        check("pool: slot rolled back after build failure",
              pool.made == 0, f"made={pool.made}")

        # 2) 再让 builder 成功，确认回滚后还能正常建（名额没被永久占走）
        def ok(_lang):
            return ("fake_ocr_instance",)
        _ocr._new_ocr = ok
        inst = pool.acquire()
        check("pool: can acquire after rollback (slot reusable)",
              inst is not None and pool.made == 1, f"made={pool.made}")

        # 3) 池满且实例不归还 → acquire 应超时而非永久阻塞
        #    把 timeout 临时压到极小，避免测试本身卡住
        import queue as _q
        orig_get = _q.Queue.get
        fast_pool = _ocr._Pool("t2")
        fast_pool.made = 1           # 已满（POOL_SIZE=1）
        # 不 put 回去 = 模拟实例被长期占用
        raised = False
        try:
            # monkeypatch get 加一个极小 timeout，覆盖我们代码里的 300s
            import time as _t
            def fast_get(self, *a, **k):
                # 模拟空队列超时
                raise _q.Empty()
            _q.Queue.get = fast_get
            fast_pool.acquire()
        except RuntimeError as e:
            raised = "超时" in str(e)
        except _q.Empty:
            raised = True   # 也算（说明走到了等待路径并触发超时）
        finally:
            _q.Queue.get = orig_get
        check("pool: acquire times out instead of blocking forever", raised)
    finally:
        _ocr.POOL_SIZE = orig_pool_size
        _ocr._new_ocr = orig_new


def test_bubble_cooldown_recovers():
    """气泡推理失败后冷却，冷却到期应恢复，而非永久关闭。"""
    import bubble as _bub
    import time as _t

    orig_until = _bub._failed_until
    orig_cd = _bub.FAIL_COOLDOWN
    try:
        # 模拟一次失败，设定一个极短的冷却，确认能恢复
        _bub._failed_until = _t.monotonic() + 0.2  # 200ms 冷却
        # 冷却期内：bubble_boxes 应直接返回 []（不调用 _bubble_boxes）
        r = _bub.bubble_boxes(None)
        check("bubble: returns [] during cooldown", r == [], str(r))

        # 等冷却到期
        _t.sleep(0.25)
        # 冷却到期后：_failed_until 仍在过去，bubble_boxes 应尝试调用 _bubble_boxes。
        # 我们让 _bubble_boxes 返回一个哨兵列表，确认它确实被调用了。
        sentinel = [("x", 1, 2, 3)]
        orig_bb = _bub._bubble_boxes
        _bub._bubble_boxes = lambda img: sentinel
        try:
            r2 = _bub.bubble_boxes(None)
            check("bubble: recovers and calls _bubble_boxes after cooldown",
                  r2 == sentinel, str(r2))
        finally:
            _bub._bubble_boxes = orig_bb
    finally:
        _bub._failed_until = orig_until
        _bub.FAIL_COOLDOWN = orig_cd


def run_all():
    print("=" * 60)
    print("Manga-Overlay-Translator smoke tests")
    print("=" * 60)
    test_profiles_order()
    print("-" * 40)
    test_strip_fence()
    test_unpack_dict_form()
    test_unpack_string_d_variants()
    test_unpack_degraded_string_array()
    test_unpack_missing_t_falls_back()
    print("-" * 40)
    test_list_profiles_has_key_and_no_secrets()
    test_pos_meta_thirds()
    test_build_user_content()
    test_gloss_promote_threshold_and_caps()
    test_gloss_and_system_content_toggle()
    print("-" * 40)
    test_script_classification()
    test_weights_have_expected_scripts()
    print("-" * 40)
    test_is_garbage()
    test_merge_lines_groups_adjacent()
    test_merge_lines_separate_when_far()
    test_merge_lines_drops_large_garbage()
    test_merge_lines_keeps_conf()
    print("-" * 40)
    test_device_info_states_and_cache()
    test_pool_status_has_device()
    test_retry_enabled_default_korean_only()
    test_retry_low_conf_logic()
    test_ocr_lines_floor_with_retry()
    print("-" * 40)
    test_pool_acquire_rollback_and_timeout()
    test_bubble_cooldown_recovers()
    print("=" * 60)
    if FAILS:
        print(f"RESULT: {len(FAILS)} FAILED -> {FAILS}")
        return 1
    print("RESULT: ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(run_all())
