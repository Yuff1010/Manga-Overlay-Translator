"""OCR：识别文字行并把同一气泡内的多行合并为一个文本块。

超高的条漫（webtoon，常见 760x15000）不能整张送进 PaddleOCR：
检测器会把最长边缩到 DET_LIMIT，15000px 高的图被压掉十几倍，文字只剩 1~2 像素。
因此这里切片逐块识别，再把 y 坐标还原回原图。

切片高度不写死，而是由图片宽度推导（思路参考 manga-image-translator 的
det_rearrange_forward）：只要切片最长边不超过 DET_LIMIT，检测器就不会下采样，
文字能以原始分辨率进入模型。
"""
import math
import queue
import threading

DET_LIMIT = 960     # PaddleOCR 检测器输入上限（det_limit_side_len 默认值）
TILE_OVERLAP = 200  # 切片重叠，避免正好从一行字中间切断
POOL_SIZE = 3       # 每种语言的实例数
CPU_THREADS = 5     # 每实例线程数

# POOL_SIZE × CPU_THREADS 要贴着物理核心数，别超额订阅：
# 16 核实测（4 路并发、真实条漫图，数值为 20 张图的总耗时）
#   1 实例 ×10 线程  72s      3 实例 ×10 线程  83s  ← 线程抢核，反而更慢
#   2 实例 × 8 线程  77s      4 实例 × 4 线程  78s
#   3 实例 × 5 线程  65s  ← 最优，但相对单实例也只快约 1.1 倍
# 结论：CPU 已接近饱和，靠堆实例榨不出多少了，要提速得换 GPU。


def _tile_height(w: int) -> int:
    """切片高度：让切片最长边恰好顶到 DET_LIMIT，缩放比为 1。

    图片本身就比 DET_LIMIT 宽时，无论怎么切都要按宽度缩放，
    此时取正方形切片——在同样的缩放比下块数最少。
    """
    return max(DET_LIMIT, w)


def _is_v3() -> bool:
    """paddleocr 3.x 换了 API：predict() 取代 ocr()，返回结构也不同。"""
    try:
        import paddleocr

        return int(str(getattr(paddleocr, "__version__", "2")).split(".")[0]) >= 3
    except Exception:  # noqa: BLE001
        return False


def _new_ocr(lang: str):
    """新建一个 PaddleOCR 实例（首次调用会下载模型）。"""
    try:
        from paddleocr import PaddleOCR
    except ImportError as e:
        raise RuntimeError(
            "未安装 paddleocr，请先执行: pip install -r requirements.txt"
        ) from e
    if _is_v3():
        # 文档方向矫正和去扭曲是给扫描件用的，漫画用不上，关掉省时间
        # Paddle 3.3.x + PP-OCRv6 在部分 Windows CPU 环境的 oneDNN 执行器
        # 会报 ConvertPirAttribute2RuntimeAttribute；关闭 MKL-DNN 走稳定路径。
        return PaddleOCR(
            lang=lang,
            use_textline_orientation=True,
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            enable_mkldnn=False,
            cpu_threads=CPU_THREADS,
        )
    return PaddleOCR(
        use_angle_cls=True, lang=lang, show_log=False, cpu_threads=CPU_THREADS
    )


def _predict(inst, img):
    """跑一次识别，统一返回 [(四点框, 文本, 置信度), ...]，抹平两代 API 差异。"""
    if _is_v3():
        out = []
        for r in inst.predict(img):
            polys = r.get("rec_polys")
            if polys is None:
                polys = r.get("dt_polys") or []
            for poly, text, score in zip(polys, r["rec_texts"], r["rec_scores"]):
                out.append((poly, text, score))
        return out
    out = []
    for page in inst.ocr(img, cls=True) or []:
        for box, (text, score) in page or []:
            out.append((box, text, score))
    return out


class _Pool:
    """PaddleOCR 实例池。

    实例本身不是线程安全的，而前端并发请求会落到 FastAPI 的线程池里。
    与其加锁把推理串行掉，不如备几个实例并行跑——CPU 还有约六成余量。
    实例按需创建，只用一路时就只建一个，不白占内存。
    """

    def __init__(self, lang):
        self.lang = lang
        self.free = queue.Queue()
        self.made = 0
        self.lock = threading.Lock()

    def acquire(self):
        try:
            return self.free.get_nowait()
        except queue.Empty:
            pass
        with self.lock:
            if self.made < POOL_SIZE:
                self.made += 1
                return _new_ocr(self.lang)
        return self.free.get()      # 都在忙，等一个空闲的

    def release(self, inst):
        self.free.put(inst)


_pools = {}
_pools_lock = threading.Lock()


def _get_pool(lang: str) -> _Pool:
    with _pools_lock:
        if lang not in _pools:
            _pools[lang] = _Pool(lang)
        return _pools[lang]


def _ocr_lines(img, lang: str):
    """对单张（尺寸正常的）图识别，返回文字行列表。"""
    pool = _get_pool(lang)
    inst = pool.acquire()
    try:
        result = _predict(inst, img)
    finally:
        pool.release(inst)
    lines = []
    for box, text, conf in result:
        if conf < 0.5 or not text.strip():
            continue
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        lines.append(
            {
                "x": min(xs),
                "y": min(ys),
                "w": max(xs) - min(xs),
                "h": max(ys) - min(ys),
                "text": text.strip(),
                "conf": conf,
            }
        )
    return lines


# 自动检测时试跑的候选模型。日/韩/中文模型同时也认拉丁字母，
# 而 es 模型带重音符号和倒问号，覆盖西语这类拉丁语系。
DETECT_CANDIDATES = ["es", "japan", "korean", "ch"]


def _script(ch: str) -> str:
    """判断单个字符属于哪种文字。"""
    o = ord(ch)
    if 0xAC00 <= o <= 0xD7AF or 0x1100 <= o <= 0x11FF:
        return "hangul"
    if 0x3040 <= o <= 0x30FF:
        return "kana"
    if 0x4E00 <= o <= 0x9FFF:
        return "cjk"
    if ch.isascii() and ch.isalpha():
        return "latin"
    return ""


# 每个候选模型只为"它该产出的文字种类"计分。
# 谚文/假名极具辨识度，见到就基本能定性；拉丁字母几乎所有模型都会吐，
# 权重必须压低，否则错误模型输出的拉丁乱码会把分数拉高。
_WEIGHTS = {
    "korean": {"hangul": 3.0, "latin": 0.3},
    "japan": {"kana": 3.0, "cjk": 1.5, "latin": 0.3},
    "ch": {"cjk": 2.0, "latin": 0.3},
    "es": {"latin": 1.0},
}


def detect_lang(img) -> str:
    """在少量切片上试跑候选模型，按识别出的文字种类打分。

    不能只比置信度——模型认错时照样给得出 0.6 的乱码，
    分数会被噪声主导。看"有没有产出该语言特有的字符"才靠谱。
    """
    h, w = img.shape[:2]
    tile_h = _tile_height(w)
    if h <= tile_h:
        samples = [img]
    else:
        # 取中部几处采样：首尾常是标题图和站点水印，不代表正文语言
        samples = [
            img[int((h - tile_h) * f) : int((h - tile_h) * f) + tile_h]
            for f in (0.3, 0.5, 0.7)
        ]

    best, best_score = DETECT_CANDIDATES[0], -1.0
    for lang in DETECT_CANDIDATES:
        w = _WEIGHTS[lang]
        score = sum(
            ln["conf"] * sum(w.get(_script(c), 0.0) for c in ln["text"])
            for s in samples
            for ln in _ocr_lines(s, lang)
        )
        if score > best_score:
            best, best_score = lang, score
    return best


def run_ocr(img, lang: str = "japan"):
    """返回 [{x, y, w, h, text}, ...]，坐标为原图像素。过高的图自动切片识别。"""
    h, w = img.shape[:2]
    tile_h = _tile_height(w)
    if h <= tile_h:                 # 一块装得下，不必切
        return merge_lines(_ocr_lines(img, lang))

    # 块数按重叠后的有效步长算，再把步长均分——首块贴顶、末块贴底，重叠分布均匀
    n = max(2, math.ceil((h - TILE_OVERLAP) / (tile_h - TILE_OVERLAP)))
    step = (h - tile_h) / (n - 1)
    lines = []
    for i in range(n):
        top = round(i * step)
        for ln in _ocr_lines(img[top : top + tile_h], lang):
            ln["y"] += top          # 切片内坐标 → 原图坐标
            lines.append(ln)
    return merge_lines(_dedupe(lines))


def _dedupe(lines):
    """去掉重叠区里被识别两次的重复行（文本相同且位置接近）。"""
    out = []
    for ln in lines:
        if not any(
            o["text"] == ln["text"]
            and abs(o["y"] - ln["y"]) < ln["h"] * 0.5
            and abs(o["x"] - ln["x"]) < max(ln["w"], 1) * 0.5
            for o in out
        ):
            out.append(ln)
    return out


def _close(a, b) -> bool:
    """判断两行是否属于同一气泡：间距小于行高的 0.8 倍。"""
    gap_y = max(a["y"], b["y"]) - min(a["y"] + a["h"], b["y"] + b["h"])
    gap_x = max(a["x"], b["x"]) - min(a["x"] + a["w"], b["x"] + b["w"])
    ref = min(max(a["h"], b["h"]), max(a["w"], b["w"]))
    return gap_y < ref * 0.8 and gap_x < ref * 0.8


def is_garbage(text: str) -> bool:
    """判断是不是 OCR 垃圾——纯符号、页码、单个字母这类，任何模式下都没用。

    只做"这压根不是文字"这一层判断。水印、音效属于"是文字但可能不想要"，
    交给翻译阶段按语义判定，因为那需要理解内容，规则做不到。
    """
    s = text.strip()
    if not s:
        return True
    letters = sum(1 for c in s if c.isalpha())
    if letters == 0:
        return True          # '%'、'...'、'10' 这类纯符号或纯数字
    # 单个拉丁字母基本是误识别；但日文的「え」「あ」、中文单字都是正经台词，
    # 不能一刀切按长度砍
    if len(s) == 1 and s.isascii():
        return True
    return False


def merge_lines(lines):
    """把相邻的行聚成块，返回块的外接矩形和拼接文本。"""
    # 效果音/艺术字常被误识别成「框住整片区域、却只认出一两个字符」的巨框。
    # 这种框物理上罩住周围多个气泡，会把它们全部链成一块——合并前先剔除。
    # 只打击「内容是噪声 且 框很大」的，小噪声框（%、单字母）留给后面按语义处理，
    # 免得误伤可能是句子片段的小框。
    lines = [
        ln for ln in lines
        if not (is_garbage(ln["text"]) and max(ln["w"], ln["h"]) > 120)
    ]
    groups = []
    for ln in sorted(lines, key=lambda l: (l["y"], l["x"])):
        merged = None
        for g in groups:
            if any(_close(ln, m) for m in g):
                if merged is None:
                    g.append(ln)
                    merged = g
                else:  # ln 同时挨着两个组 → 组合并
                    merged.extend(g)
                    g.clear()
        if merged is None:
            groups.append([ln])
    out = []
    for g in groups:
        if not g:
            continue
        x1 = min(m["x"] for m in g)
        y1 = min(m["y"] for m in g)
        x2 = max(m["x"] + m["w"] for m in g)
        y2 = max(m["y"] + m["h"] for m in g)
        text = " ".join(m["text"] for m in sorted(g, key=lambda m: (m["y"], m["x"])))
        # 合并之后再判垃圾：单看一行像噪声，拼进块里可能是完整句子的一部分
        if is_garbage(text):
            continue
        out.append(
            {
                "x": round(x1),
                "y": round(y1),
                "w": round(x2 - x1),
                "h": round(y2 - y1),
                "text": text,
            }
        )
    return out
