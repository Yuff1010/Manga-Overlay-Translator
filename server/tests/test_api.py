"""API-layer tests for main.py endpoints (direct call, no HTTP server).

These verify the contract the userscript depends on: response shapes, the
400/500 error conversions, the no-key fallback path, and that /config never
leaks secrets. OCR/translation/bubble are replaced with in-memory stubs, so
the tests run in milliseconds and never load PaddleOCR or touch the network.

Approach: call the endpoint functions directly and catch HTTPException —
requirements.txt has no httpx, so FastAPI's TestClient would add a dependency
for little gain (routing is a one-line decorator; the logic lives in the
functions). Same check()/run_all() harness as test_smoke.py.

Run:  python server/tests/test_api.py
Exit: 0 = all pass, 1 = some failed.

If you have pytest installed, the functions are also discoverable:
  pytest server/tests/test_api.py
"""
import base64
import io
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from fastapi import HTTPException  # noqa: E402

import main  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    ok = bool(cond)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({extra})" if extra and not ok else ""))
    if not ok:
        FAILS.append(name)


# ---------- helpers ----------

def png_data_url(width=40, height=30, color=(200, 30, 30)):
    """生成一张小 PNG 的 data URL，当合法图片用。"""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def call_translate(req):
    """直调 translate 端点。返回 (响应, HTTPException)；二选一。"""
    try:
        return main.translate(req), None
    except HTTPException as e:
        return None, e


def patched(main_attrs, bubble=None, translator=None):
    """临时替换 main 命名空间里的依赖桩，用完恢复。

    main_attrs: {"run_ocr": stub, ...} 直接换成 main 模块里的同名符号
    """
    saved = {k: getattr(main, k) for k in main_attrs}
    for k, v in main_attrs.items():
        setattr(main, k, v)
    return saved


def restore(saved):
    for k, v in saved.items():
        setattr(main, k, v)


# ---------- /ping ----------

def test_ping():
    check("ping: returns ok", main.ping() == {"ok": True})


# ---------- /ocr-status ----------

def test_ocr_status_shape():
    # pool_status 现在自带 device/models 两键；端点必须直接透传，
    # 再包一层 "models" 会让状态页 JS 读到双重嵌套（历史 bug，回归锁定）
    saved = patched({"pool_status": lambda: {"device": "gpu", "models": {"japan": 2}}})
    try:
        res = main.ocr_status()
    finally:
        restore(saved)
    check("ocr-status: device/models at top level",
          res == {"device": "gpu", "models": {"japan": 2}}, str(res))


# ---------- /config ----------

def test_config_shape_and_no_secrets():
    fake = [
        {"id": "1", "name": "Alpha", "models": ["m1", "m2"], "has_key": True},
        {"id": "2", "name": "Beta", "models": ["b1"], "has_key": False},
    ]
    saved = patched({"list_profiles": lambda: fake})
    try:
        cfg = main.config()
    finally:
        restore(saved)
    check("config: profiles passed through", len(cfg["profiles"]) == 2)
    check("config: default is first profile/model",
          cfg["default"] == {"profile": "1", "model": "m1"}, str(cfg["default"]))
    for p in cfg["profiles"]:
        check(f"config: profile keys exact (id={p['id']})",
              set(p) == {"id", "name", "models", "has_key"}, str(sorted(p)))

    # 空配置：default 退空串而不是 IndexError
    saved = patched({"list_profiles": lambda: []})
    try:
        cfg0 = main.config()
    finally:
        restore(saved)
    check("config: empty profiles -> empty default",
          cfg0["profiles"] == [] and cfg0["default"] == {"profile": "", "model": ""})


# ---------- /translate: 4xx conversions ----------

def test_translate_bad_image_400():
    # 合法 base64 但不是图片 → 400 而非裸 500
    req = main.TranslateRequest(image=base64.b64encode(b"not an image").decode())
    _, err = call_translate(req)
    check("translate: non-image -> 400",
          err is not None and err.status_code == 400
          and "无法解析图片" in err.detail, str(getattr(err, "detail", "")))
    # 完全坏的 base64（binascii.Error 是 ValueError 子类）→ 同样 400
    req2 = main.TranslateRequest(image="!!!not-base64!!!")
    _, err2 = call_translate(req2)
    check("translate: bad base64 -> 400",
          err2 is not None and err2.status_code == 400)


def test_translate_invalid_profile_400():
    def bad_profile(*a, **k):
        raise ValueError("没有编号为 9 的供应商配置，可用：['1']")

    saved = patched({
        "run_ocr": lambda arr, lang: [{"x": 1, "y": 1, "w": 8, "h": 8, "text": "hi"}],
        "detect_lang": lambda arr: "en",
        "translate_texts": bad_profile,
        "bubble_boxes": lambda arr: [],
    })
    try:
        req = main.TranslateRequest(image=png_data_url(), lang="en")
        _, err = call_translate(req)
    finally:
        restore(saved)
    check("translate: invalid profile -> 400",
          err is not None and err.status_code == 400 and "9" in err.detail,
          str(getattr(err, "detail", "")))


def test_translate_ocr_error_500():
    def boom(arr, lang):
        raise RuntimeError("In user code:\nPreconditionNotMetError: cudnn missing")

    saved = patched({"run_ocr": boom, "detect_lang": lambda arr: "en"})
    try:
        req = main.TranslateRequest(image=png_data_url(), lang="en")
        _, err = call_translate(req)
    finally:
        restore(saved)
    check("translate: OCR failure -> 500",
          err is not None and err.status_code == 500, str(getattr(err, "status_code", "")))
    # detail 应挑出含 "Error" 的那行，而不是 "In user code:"
    check("translate: 500 detail picks the Error line",
          err is not None and "PreconditionNotMetError" in err.detail
          and "In user code" not in err.detail, str(getattr(err, "detail", "")))


# ---------- /translate: happy & fallback paths ----------

def test_translate_no_key_fallback():
    # 无 key：translate_texts 原样返回原文，translated=False，错误随响应带回
    blocks = [{"x": 1, "y": 1, "w": 8, "h": 8, "text": "hello"}]

    def fake_tt(texts, target, profile_id, model, metas=None):
        return list(texts), [True], {"prompt_tokens": 0, "completion_tokens": 0}, \
            "Alpha：未配置 API_KEY"

    saved = patched({
        "run_ocr": lambda arr, lang: [dict(b) for b in blocks],
        "detect_lang": lambda arr: "en",
        "translate_texts": fake_tt,
        "bubble_boxes": lambda arr: [],
    })
    try:
        req = main.TranslateRequest(image=png_data_url(), lang="en")
        res, err = call_translate(req)
    finally:
        restore(saved)
    check("translate: no-key returns 200 (fallback, not error)", err is None and res)
    if not res:
        return
    check("translate: translated=False when texts unchanged",
          res["translated"] is False)
    check("translate: blocks keep original as translation",
          res["blocks"][0]["translation"] == "hello")
    check("translate: error surfaced in response",
          "未配置 API_KEY" in res["error"], res["error"])


def test_translate_ok_fields_and_bubble():
    blocks = [
        {"x": 1, "y": 1, "w": 8, "h": 8, "text": "hello"},
        {"x": 20, "y": 1, "w": 8, "h": 8, "text": "BANG"},
    ]
    captured = {}

    def fake_tt(texts, target, profile_id, model, metas=None):
        captured["metas"] = metas
        return ["你好", "轰"], [True, False], \
            {"prompt_tokens": 7, "completion_tokens": 5}, ""

    saved = patched({
        "run_ocr": lambda arr, lang: [dict(b) for b in blocks],
        "detect_lang": lambda arr: "en",
        "translate_texts": fake_tt,
        # 气泡只罩住第二块的中心 (20+4, 1+4) → 第二块被强制算对白
        "bubble_boxes": lambda arr: [(18, 0, 30, 10)],
    })
    try:
        req = main.TranslateRequest(image=png_data_url(width=40, height=30), lang="en")
        res, err = call_translate(req)
    finally:
        restore(saved)
    check("translate: ok path returns 200", err is None and res)
    if not res:
        return
    check("translate: response has all fields",
          set(res) == {"width", "height", "blocks", "lang", "translated", "usage", "error"},
          str(sorted(res)))
    check("translate: image dims echoed", (res["width"], res["height"]) == (40, 30))
    check("translate: lang echoed", res["lang"] == "en")
    check("translate: translated=True on real change", res["translated"] is True)
    check("translate: usage echoed",
          res["usage"] == {"prompt_tokens": 7, "completion_tokens": 5}, str(res["usage"]))
    check("translate: block translations attached",
          [b["translation"] for b in res["blocks"]] == ["你好", "轰"])
    check("translate: dialogue flags applied",
          [b["dialogue"] for b in res["blocks"]] == [True, True],   # 第二块靠气泡强制
          str([b["dialogue"] for b in res["blocks"]]))
    # 位置上下文：main 应把每块的相对位置描述传给 translate_texts
    m = captured.get("metas")
    check("translate: metas passed through to translator",
          isinstance(m, list) and len(m) == 2 and all(isinstance(s, str) and s for s in m),
          str(m))


def test_translate_no_blocks():
    # OCR 一个字都没认出来：不该调 translate_texts，响应仍是完整形状
    called = {"n": 0}

    def fake_tt(*a, **k):
        called["n"] += 1
        return [], [], {"prompt_tokens": 0, "completion_tokens": 0}, ""

    saved = patched({
        "run_ocr": lambda arr, lang: [],
        "detect_lang": lambda arr: "en",
        "translate_texts": fake_tt,
        "bubble_boxes": lambda arr: [],
    })
    try:
        req = main.TranslateRequest(image=png_data_url(), lang="en")
        res, err = call_translate(req)
    finally:
        restore(saved)
    check("translate: no blocks -> 200 with empty blocks",
          err is None and res and res["blocks"] == [])
    check("translate: translator not called when no blocks", called["n"] == 0)


def run_all():
    print("=" * 60)
    print("OCRTranslator API-layer tests")
    print("=" * 60)
    test_ping()
    test_ocr_status_shape()
    print("-" * 40)
    test_config_shape_and_no_secrets()
    print("-" * 40)
    test_translate_bad_image_400()
    test_translate_invalid_profile_400()
    test_translate_ocr_error_500()
    print("-" * 40)
    test_translate_no_key_fallback()
    test_translate_ok_fields_and_bubble()
    test_translate_no_blocks()
    print("=" * 60)
    if FAILS:
        print(f"RESULT: {len(FAILS)} FAILED -> {FAILS}")
        return 1
    print("RESULT: ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(run_all())
