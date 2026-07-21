"""OCRTranslator 本地服务：接收图片 → OCR → LLM 翻译 → 返回文本块坐标和译文。"""
import base64
import io

import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel

from ocr import detect_lang, run_ocr
from translator import list_profiles, translate_texts

app = FastAPI(title="OCRTranslator")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class TranslateRequest(BaseModel):
    image: str            # base64，可带 data:image/... 前缀
    lang: str = "japan"   # PaddleOCR 语言: japan / korean / es / en / ch，或 auto 自动检测
    target: str = "中文"  # 目标语言
    profile: str = ""     # LLM 供应商编号，空 = 用 .env 里的第一组
    model: str = ""       # 模型名，空 = 用该供应商的默认模型


@app.post("/translate")
def translate(req: TranslateRequest):
    # 解码图片：坏数据 → 400，让前端拿到可读原因而非裸 500
    try:
        raw = base64.b64decode(req.image.split(",")[-1])
        img = Image.open(io.BytesIO(raw)).convert("RGB")
    except (ValueError, UnidentifiedImageError) as e:
        raise HTTPException(status_code=400, detail=f"无法解析图片: {e}")

    arr = np.array(img)
    # OCR：缺依赖等运行期错误 → 500，但把可读信息透传给前端
    try:
        lang = detect_lang(arr) if req.lang == "auto" else req.lang
        blocks = run_ocr(arr, lang)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    # 翻译失败时 translate_texts 原样返回，这里对比一下好让前端知道，
    # 否则覆盖层显示原文，看起来和翻译成功没区别
    translated = False
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    error = ""
    if blocks:
        texts = [b["text"] for b in blocks]
        try:
            out, usage, error = translate_texts(
                texts, req.target, req.profile, req.model
            )
        except ValueError as e:      # 供应商编号非法
            raise HTTPException(status_code=400, detail=str(e))
        translated = out != texts
        for b, t in zip(blocks, out):
            b["translation"] = t
    return {
        "width": img.width,
        "height": img.height,
        "blocks": blocks,
        "lang": lang,            # 自动检测时前端可复用，避免每张都重新检测
        "translated": translated,
        "usage": usage,
        "error": error,          # 翻译失败的具体原因，前端直接显示，省得去翻日志
    }


@app.get("/ping")
def ping():
    return {"ok": True}


@app.get("/config")
def config():
    """供面板填充供应商/模型下拉框。只回名称和模型名，api_key 不出服务端。"""
    profiles = list_profiles()
    return {
        "profiles": profiles,
        "default": {
            "profile": profiles[0]["id"] if profiles else "",
            "model": profiles[0]["models"][0] if profiles else "",
        },
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8765)
