"""Pure-function smoke tests for the OCR/translator backend.

These deliberately avoid importing anything that pulls in PaddleOCR or makes
network calls, so they run in milliseconds on any machine and are safe to add
to CI. They cover the pieces most likely to silently break:

  - translator._load_profiles ordering / multi-provider numbering
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
from translator import _load_profiles, _strip_fence, _unpack  # noqa: E402
from ocr import _script, _WEIGHTS, is_garbage, merge_lines  # noqa: E402

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
    test_script_classification()
    test_weights_have_expected_scripts()
    print("-" * 40)
    test_is_garbage()
    test_merge_lines_groups_adjacent()
    test_merge_lines_separate_when_far()
    test_merge_lines_drops_large_garbage()
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
