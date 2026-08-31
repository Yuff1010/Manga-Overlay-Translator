"""OCR model/strategy benchmark on synthetic pages (no copyrighted content).

Opt-in like the slow fixture test — it loads PaddleOCR and may download models:

    OCRT_SLOW=1 python server/tests/bench_ocr.py          (bash)
    set OCRT_SLOW=1 && python server/tests/bench_ocr.py   (Windows cmd)

What it measures, per combination (default models / light det / low-conf
retry on-off, and Korean det variants):
  - chars recognized that appear in the ground truth (recall proxy)
  - edit-distance accuracy of the best-matching block per ground-truth line
  - wall time

Pages are generated with PIL in Japanese / Korean / English using Windows
system fonts, at several font sizes (small text is where Korean is known to
be flaky). This follows the "measure before shipping" rule from
docs/optimization-roadmap.md (section 15A lesson).

The script prints a table; there is no pass/fail. Use the numbers to decide
whether OCR_DET_MODEL / OCR_RETRY_LOW_CONF should be recommended or defaults.
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

if os.getenv("OCRT_SLOW") != "1":
    print("SKIP: set OCRT_SLOW=1 to run the OCR benchmark")
    sys.exit(0)

try:
    import paddleocr  # noqa: F401
except ImportError:
    print("SKIP: paddleocr not installed in this environment")
    sys.exit(0)

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

import numpy as np  # noqa: E402

import ocr  # noqa: E402


def _font(candidates, size):
    for name in candidates:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


FONTS = {
    "japan": ["Yu Gothic.ttf", "msgothic.ttc", "Meiryo.ttc"],
    "korean": ["malgun.ttf", "malgunsl.ttf", "Gulim.ttc"],
    "en": ["arial.ttf", "Arial.ttf", "DejaVuSans.ttf"],
}

# 每种语言一页：多行台词，字号混排（小字号专挑韩语的已知弱点）
PAGE = {
    "japan": [
        "今日はとても良い天気だ",
        "そうだね、散歩に行こう",
        "怪物が現れた！",
        "逃げろ！早く！",
        "何が起こっているの？",
    ],
    "korean": [
        "오늘 날씨가 정말 좋다",
        "그래, 산책하러 가자",
        "괴물이 나타났다!",
        "도망쳐! 빨리!",
        "무슨 일이 일어나는 거야?",
    ],
    "en": [
        "The weather is nice today",
        "Let's go for a walk",
        "A monster appeared!",
        "Run! Quickly!",
        "What is happening?",
    ],
}
SIZES = [56, 40, 28, 22, 18]   # 与台词一一对应，18px 是小字号压力档


def make_page(lang):
    lines = PAGE[lang]
    img = Image.new("RGB", (800, 700), "white")
    d = ImageDraw.Draw(img)
    for i, (text, size) in enumerate(zip(lines, SIZES)):
        f = _font(FONTS[lang], size)
        d.text((60, 30 + i * 130), text, fill="black", font=f)
    return np.array(img), lines


def edit_distance(a, b):
    """标准 Levenshtein，用于块文本和 ground truth 的接近程度。"""
    if not a or not b:
        return max(len(a), len(b))
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def score(blocks, truths):
    """每个 ground truth 行找编辑距离最近的块，算字符级命中率和整体召回。"""
    hit_chars = 0
    total_chars = 0
    acc_sum = 0.0
    for t in truths:
        best_ed, best_txt = None, ""
        for b in blocks:
            ed = edit_distance(b["text"], t)
            if best_ed is None or ed < best_ed:
                best_ed, best_txt = ed, b["text"]
        total_chars += len(t)
        if best_ed is not None:
            hit_chars += max(0, len(t) - best_ed)
            acc_sum += 1 - best_ed / max(len(t), len(best_txt), 1)
    recall = hit_chars / total_chars if total_chars else 0.0
    acc = acc_sum / len(truths) if truths else 0.0
    return recall, acc


def run_combo(name, langs, env_over):
    """env_over 里的环境变量在本次组合内生效；结束后还原并清空实例池。"""
    saved_env = {k: os.environ.get(k) for k in env_over}
    saved_pools = dict(ocr._pools)
    saved_dev = ocr._device["v"]
    os.environ.update(env_over)
    ocr._pools.clear()
    try:
        rows = []
        t0 = time.perf_counter()
        for lang in langs:
            img, truths = make_page(lang)
            t1 = time.perf_counter()
            blocks = ocr.run_ocr(img, lang)
            dt = time.perf_counter() - t1
            recall, acc = score(blocks, truths)
            rows.append((lang, recall, acc, dt, len(blocks)))
        total = time.perf_counter() - t0
        print(f"\n== {name}  (env={env_over or 'default'})  total {total:.2f}s")
        print(f"{'lang':8} {'recall':>7} {'acc':>7} {'time':>7} {'blocks':>6}")
        for lang, recall, acc, dt, n in rows:
            print(f"{lang:8} {recall:7.2f} {acc:7.2f} {dt:6.2f}s {n:6d}")
        return rows
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        ocr._pools.clear()
        ocr._pools.update(saved_pools)
        ocr._device["v"] = saved_dev


def main():
    import paddleocr

    # 排除外部污染：默认组合必须在干净环境变量下测
    for k in ("OCR_DET_MODEL", "OCR_REC_MODEL", "OCR_RETRY_LOW_CONF"):
        os.environ.pop(k, None)
    print(f"paddleocr {paddleocr.__version__}, device probe: {ocr.device_info()}")
    langs = ["japan", "korean", "en"]
    run_combo("default (server det / medium rec)", langs, {})
    run_combo("low-conf retry ON", langs, {"OCR_RETRY_LOW_CONF": "1"})
    # 轻量检测模型：v6 语言（japan/en）与 v5 语言（korean）各有对应的小模型，
    # 且 OCR_DET_MODEL 是进程级全局，只能分开组合跑
    run_combo("light det PP-OCRv6_small_det [japan,en]", ["japan", "en"],
              {"OCR_DET_MODEL": "PP-OCRv6_small_det"})
    run_combo("tiny det PP-OCRv6_tiny_det [japan]", ["japan"],
              {"OCR_DET_MODEL": "PP-OCRv6_tiny_det"})
    run_combo("light det PP-OCRv5_mobile_det [korean]", ["korean"],
              {"OCR_DET_MODEL": "PP-OCRv5_mobile_det"})
    run_combo("light det + retry [korean]", ["korean"],
              {"OCR_DET_MODEL": "PP-OCRv5_mobile_det", "OCR_RETRY_LOW_CONF": "1"})
    run_combo("light rec PP-OCRv6_small_rec [japan]", ["japan"],
              {"OCR_REC_MODEL": "PP-OCRv6_small_rec"})


if __name__ == "__main__":
    main()
