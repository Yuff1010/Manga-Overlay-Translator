"""可选增强：用气泡分割模型判断 OCR 文本块是否落在对话气泡内。

这是「软信号」——气泡内的文本强制保留（对白），气泡外的交给 LLM 判断
（可能是旁白，也可能是音效/水印）。因为旁白天然在气泡外，绝不能拿
「气泡外」当过滤依据，否则会误杀旁白。

整体是可选的：模型文件或 onnxruntime 缺失时，所有函数安全降级为「无气泡」，
翻译链路退回纯 A+B（垃圾过滤 + LLM 分类），不影响服务。

模型：kitsumed/yolov8m_seg-speech-bubble（YOLOv8-seg），放在 models/bubble.onnx。
"""
import math
import os
import threading

import numpy as np

MODEL_PATH = os.getenv(
    "BUBBLE_MODEL", os.path.join(os.path.dirname(__file__), "models", "bubble.onnx")
)
CONF = 0.3          # 气泡置信度阈值
NMS_IOU = 0.45
INPUT = 640         # YOLOv8 输入边长
# 长条漫要滑窗多次，CPU 上每窗 ~0.2s，累积可观。而条漫气泡本就稀少
# （对白多是无框旁白），增量价值低——超过这么多窗就跳过，退回纯 A+B。
# 想在条漫上也启用的，配 GPU 版 onnxruntime 后把这个调大。
MAX_TILES = int(os.getenv("BUBBLE_MAX_TILES", "3"))

_sess = None
_tried = False
_lock = threading.Lock()
_failed = False


def _session():
    """懒加载并缓存 onnxruntime 会话。模型/依赖缺失时返回 None 并只提示一次。"""
    global _sess, _tried
    if _tried:
        return _sess
    with _lock:
        if _tried:
            return _sess
        _tried = True
        if not os.path.exists(MODEL_PATH):
            print(f"[bubble] 未找到气泡模型 {MODEL_PATH}，气泡增强关闭")
            return None
        try:
            import onnxruntime as ort
        except ImportError:
            print("[bubble] 未安装 onnxruntime，气泡增强关闭")
            return None
        try:
            prov = ort.get_available_providers()
            use = ["CUDAExecutionProvider", "CPUExecutionProvider"] \
                if "CUDAExecutionProvider" in prov else ["CPUExecutionProvider"]
            _sess = ort.InferenceSession(MODEL_PATH, providers=use)
        except Exception as e:  # 模型损坏/格式不兼容不能拖垮翻译主链路
            print(f"[bubble] 无法加载气泡模型，增强关闭：{e}")
            return None
        print(f"[bubble] 气泡增强已启用（{use[0]}）")
        return _sess


def _letterbox(im):
    """等比缩放并居中填充到 INPUT×INPUT，返回图和还原参数。"""
    import cv2

    h, w = im.shape[:2]
    r = min(INPUT / w, INPUT / h)
    nw, nh = int(round(w * r)), int(round(h * r))
    canvas = np.full((INPUT, INPUT, 3), 114, np.uint8)
    dx, dy = (INPUT - nw) // 2, (INPUT - nh) // 2
    canvas[dy : dy + nh, dx : dx + nw] = cv2.resize(im, (nw, nh))
    return canvas, r, dx, dy


def _nms(boxes, scores):
    idx = scores.argsort()[::-1]
    keep = []
    while idx.size:
        i = idx[0]
        keep.append(i)
        if idx.size == 1:
            break
        xx1 = np.maximum(boxes[i, 0], boxes[idx[1:], 0])
        yy1 = np.maximum(boxes[i, 1], boxes[idx[1:], 1])
        xx2 = np.minimum(boxes[i, 2], boxes[idx[1:], 2])
        yy2 = np.minimum(boxes[i, 3], boxes[idx[1:], 3])
        inter = np.clip(xx2 - xx1, 0, None) * np.clip(yy2 - yy1, 0, None)
        a1 = (boxes[i, 2] - boxes[i, 0]) * (boxes[i, 3] - boxes[i, 1])
        a2 = (boxes[idx[1:], 2] - boxes[idx[1:], 0]) * (boxes[idx[1:], 3] - boxes[idx[1:], 1])
        iou = inter / (a1 + a2 - inter + 1e-6)
        idx = idx[1:][iou < NMS_IOU]
    return keep


def _detect_one(sess, name, tile, y_off):
    """在一张（尺寸接近方形的）图上检测气泡，返回原图坐标的框。"""
    cv_img, r, dx, dy = _letterbox(tile)
    x = cv_img[:, :, ::-1].transpose(2, 0, 1)[None].astype(np.float32) / 255.0
    out0 = sess.run(None, {name: x})[0][0]          # (37, 8400)
    return _decode(out0, r, dx, dy, y_off)


def bubble_boxes(img):
    """安全检测对话气泡；可选模块的任何故障都降级为空结果。"""
    global _failed
    if _failed:
        return []
    try:
        return _bubble_boxes(img)
    except Exception as e:  # 推理输入/输出不兼容时仍须返回 OCR 与译文
        _failed = True
        print(f"[bubble] 气泡推理失败，增强关闭：{e}")
        return []


def _bubble_boxes(img):
    """检测对话气泡，返回 [(x1,y1,x2,y2), ...]（原图像素）。无模型时返回 []。

    长条漫按方窗滑动，所有窗口打包成一个 batch 一次推理——省掉逐窗的
    Python/调度开销；普通页整张一次过。
    """
    sess = _session()
    if sess is None:
        return []
    name = sess.get_inputs()[0].name
    h, w = img.shape[:2]
    if h <= w * 1.6:
        return _detect_one(sess, name, img, 0)

    if math.ceil(h / w) > MAX_TILES:      # 太长 → 气泡增强不划算，交给纯 A+B
        return []
    batch, params = [], []
    for top in range(0, h, w):
        tile = img[top : top + w]
        if tile.shape[0] < 40:
            continue
        cv_img, r, dx, dy = _letterbox(tile)
        batch.append(cv_img[:, :, ::-1].transpose(2, 0, 1))
        params.append((r, dx, dy, top))
    if not batch:
        return []
    x = np.stack(batch).astype(np.float32) / 255.0
    outs = sess.run(None, {name: x})[0]              # (N, 37, 8400)
    boxes = []
    for out0, (r, dx, dy, top) in zip(outs, params):
        boxes.extend(_decode(out0, r, dx, dy, top))
    return boxes


def _decode(out0, r, dx, dy, y_off):
    """把单张的检测输出解成原图坐标的气泡框。"""
    p = out0.T
    score = p[:, 4]
    m = score > CONF
    if not m.any():
        return []
    p, score = p[m], score[m]
    cx, cy, bw, bh = p[:, 0], p[:, 1], p[:, 2], p[:, 3]
    bx = np.stack([cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2], 1)
    out = []
    for i in _nms(bx, score):
        x1, y1, x2, y2 = bx[i]
        out.append((
            (x1 - dx) / r, (y1 - dy) / r + y_off,
            (x2 - dx) / r, (y2 - dy) / r + y_off,
        ))
    return out


def in_bubble(block, boxes) -> bool:
    """文本块中心是否落在某个气泡框内。"""
    cx = block["x"] + block["w"] / 2
    cy = block["y"] + block["h"] / 2
    return any(x1 <= cx <= x2 and y1 <= cy <= y2 for x1, y1, x2, y2 in boxes)
