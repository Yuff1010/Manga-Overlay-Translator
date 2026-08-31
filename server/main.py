"""OCRTranslator 本地服务：接收图片 → OCR → LLM 翻译 → 返回文本块坐标和译文。"""
import base64
import io
import os

import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel

from bubble import bubble_boxes, in_bubble
from ocr import detect_lang, pool_status, run_ocr
from translator import list_profiles, pos_meta, translate_texts

_HERE = os.path.dirname(os.path.abspath(__file__))
_USERSCRIPT_PATH = os.path.join(_HERE, "..", "userscript", "ocr-translator.user.js")

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
    # OCR：缺依赖、GPU 库缺失（cudnn）、模型损坏等运行期错误 → 500，但把可读信息
    # 透传给前端。注意 Paddle 抛的 PreconditionNotMetError 不是 RuntimeError，
    # 只接 RuntimeError 会让 C++ 堆栈裸抛出去（实测 cudnn 缺失时就是这样）。
    # Paddle 的异常自带一大段多行 C++ 建议，str(e) 全塞进 detail 会让响应变得
    # 又长又难看，所以只取异常类型名 + 首行。
    try:
        lang = detect_lang(arr) if req.lang == "auto" else req.lang
        blocks = run_ocr(arr, lang)
    except Exception as e:
        # Paddle 的异常信息首行常是 "In user code:"，真正的错误名（如
        # PreconditionNotMetError: cudnn64_8.dll ...）在后续行里，所以挑
        # 含 "Error" 的那一行给前端；都没有就退回首行。
        lines = [l.strip() for l in str(e).splitlines() if l.strip()]
        key = next((l for l in lines if "Error" in l), lines[0] if lines else "")
        msg = f"{type(e).__name__}: {key[:200]}" if key else type(e).__name__
        print(f"[main] OCR 失败：{msg}")
        raise HTTPException(status_code=500, detail=f"OCR 失败: {msg}")

    # 翻译失败时 translate_texts 原样返回，这里对比一下好让前端知道，
    # 否则覆盖层显示原文，看起来和翻译成功没区别
    translated = False
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    error = ""
    if blocks:
        texts = [b["text"] for b in blocks]
        # 位置上下文：块的相对位置随请求进 prompt，让模型按页面布局还原
        # 阅读顺序（日漫竖排右→左），而不是按 OCR 吐出的顺序硬翻
        metas = [
            pos_meta(b["x"], b["y"], b["w"], b["h"], img.width, img.height)
            for b in blocks
        ]
        try:
            out, dialog, usage, error = translate_texts(
                texts, req.target, req.profile, req.model, metas=metas
            )
        except ValueError as e:      # 供应商编号非法
            raise HTTPException(status_code=400, detail=str(e))
        translated = out != texts
        # 气泡增强（可选）：气泡内的文本强制算对白，即使 LLM 判成噪声也保留；
        # 气泡外的沿用 LLM 判断——旁白天然在气泡外，不能拿气泡当过滤依据。
        # 没有气泡模型时 boxes 为空，in_bubble 恒 False，退回纯 LLM 判断。
        boxes = bubble_boxes(arr)
        # dialogue 标记随块返回，前端据此在"仅对白/全部文字"之间即时切换，
        # 不必为换个范围重新翻译一遍
        for b, t, d in zip(blocks, out, dialog):
            b["translation"] = t
            b["dialogue"] = bool(d) or in_bubble(b, boxes)
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


# 状态页 HTML。后端渲染初始状态，前端 JS 再用 fetch 刷新（模型会热加载）。
# 纯静态字符串，不引外部 CDN——本地工具不该依赖外网。
_STATUS_HTML = """<!DOCTYPE html>
<html lang="zh">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>OCRTranslator 状态</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font:14px/1.6 system-ui,-apple-system,"Microsoft YaHei",sans-serif;background:#1a1a2e;color:#e0e0e0;padding:24px;max-width:720px;margin:0 auto}
h1{font-size:20px;margin-bottom:16px;color:#7ec8e3}
.card{background:#16213e;border-radius:8px;padding:16px;margin-bottom:14px;border:1px solid #233}
.card h2{font-size:15px;color:#9ad;margin-bottom:10px}
.row{display:flex;justify-content:space-between;align-items:center;padding:4px 0;gap:12px}
.row .label{color:#8ab;flex:none}
.row .val{color:#e0e0e0;text-align:right;word-break:break-all}
.ok{color:#6fcf97}.warn{color:#f2c94c}.err{color:#eb5757}.info{color:#a0a0b0}
code{background:#0d1b2a;padding:2px 6px;border-radius:4px;font-size:13px}
.btn{display:inline-block;background:#3a6ea5;color:#fff;padding:7px 16px;border-radius:6px;text-decoration:none;font-size:13px;border:none;cursor:pointer}
.btn:hover{background:#4d82c4}.btn.green{background:#2d7d46}.btn.green:hover{background:#3a9c5c}
.tips{margin-top:8px;padding:10px;background:#0d1b2a;border-radius:6px;font-size:13px;color:#8ab}
ul{padding-left:18px;margin:6px 0}.tips li{margin:3px 0}
</style></head>
<body>
<h1>OCRTranslator 本地状态</h1>
<div class="card"><h2>后端</h2>
  <div class="row"><span class="label">服务地址</span><code>http://127.0.0.1:8765</code></div>
  <div class="row"><span class="label">状态</span><span id="be-status" class="info">检测中…</span></div>
  <div class="row"><span class="label">运行设备</span><span id="device-status" class="info">检测中…</span></div>
  <div class="row"><span class="label">已加载 OCR 模型</span><span id="ocr-models" class="info">—</span></div>
</div>
<div class="card"><h2>LLM 配置</h2><div id="llm-box" class="info">读取中…</div></div>
<div class="card"><h2>userscript 安装</h2>
  <p style="margin-bottom:10px" class="info">在装了 Tampermonkey 的浏览器里点下面的链接一键安装：</p>
  <a class="btn green" href="/userscript">安装 userscript（Tampermonkey）</a>
  <button class="btn" id="copy-btn" style="margin-left:8px">复制完整代码</button>
  <span id="copy-msg" class="info" style="margin-left:8px"></span>
</div>
<div class="card"><h2>常见自查</h2><ul>
  <li class="info">面板打不开 / 模型列表为空 → 确认本页后端状态是绿色的"运行中"。</li>
  <li class="info">翻译全是原文（淡黄）→ 检查 LLM 配置区有没有"未配置 API_KEY"。</li>
  <li class="info">速度莫名慢 → 看「运行设备」：显示 CPU 说明当前 paddle 是 CPU 版，换 GPU 环境启动可加速。</li>
  <li class="info">首次翻译某语言很慢 → PaddleOCR 第一次要下载模型（几百 MB），属正常。</li>
  <li class="info">韩语 OCR 不稳定是已知限制，日语/英语较可靠。</li>
  <li class="info">改了 <code>.env</code> 不用重启，下次请求自动重载。</li>
</ul></div>
<script>
fetch('/ping').then(r=>r.json()).then(d=>{
  const s=document.getElementById('be-status');
  if(d&&d.ok){s.textContent='运行中';s.className='ok';}
  else{s.textContent='异常';s.className='err';}
}).catch(()=>{const s=document.getElementById('be-status');s.textContent='无响应';s.className='err';});

fetch('/config').then(r=>r.json()).then(cfg=>{
  const box=document.getElementById('llm-box');
  const ps=cfg.profiles||[];
  if(!ps.length){box.innerHTML='<span class="warn">未配置任何供应商，请编辑 server/.env</span>';return;}
  const rows=ps.map(p=>{
    const models=(p.models||[]).join(', ')||'(无模型)';
    const ok=p.has_key!==false;
    const state=(ok?'已配置':'未配置 API_KEY')+' · '+models;
    return '<div class="row"><span class="label">'+p.name+' (供应商 '+p.id+')</span><span class="val '+(ok?'ok':'warn')+'">'+state+'</span></div>';
  });
  box.innerHTML=rows.join('');
}).catch(()=>{document.getElementById('llm-box').innerHTML='<span class="err">无法读取 /config</span>';});

function refreshOcr(){
  fetch('/ocr-status').then(r=>r.json()).then(d=>{
    const el=document.getElementById('ocr-models');
    const langs=d.models||{};
    const keys=Object.keys(langs);
    if(!keys.length){el.textContent='尚未使用（首次翻译时下载）';el.className='warn';}
    else{el.textContent=keys.map(k=>k+' ('+langs[k]+' 实例)').join(', ');el.className='ok';}
    // 设备如实展示：用户以为在用 GPU、实际 paddle 是 CPU 版，是"速度莫名慢"的最常见原因
    const dv=document.getElementById('device-status');
    if(dv){
      if(d.device==='gpu'){dv.textContent='GPU（加速中）';dv.className='ok';}
      else if(d.device==='cpu'){dv.textContent='CPU（未启用 GPU 加速）';dv.className='warn';}
      else{dv.textContent='未知';dv.className='info';}
    }
  }).catch(()=>{document.getElementById('ocr-models').textContent='未知';});
}
refreshOcr();

document.getElementById('copy-btn').onclick=function(){
  fetch('/userscript').then(r=>r.text()).then(t=>{
    navigator.clipboard.writeText(t).then(()=>{const m=document.getElementById('copy-msg');m.textContent='已复制到剪贴板';m.className='ok';setTimeout(()=>m.textContent='',2000);}).catch(()=>{const m=document.getElementById('copy-msg');m.textContent='复制失败，请用安装链接';m.className='err';});
  });
};
</script>
</body></html>"""


@app.get("/", response_class=HTMLResponse)
def status_page():
    """给人看的本地状态页：后端是否就绪、OCR 模型状态、LLM 配置、userscript 入口。"""
    return _STATUS_HTML


@app.get("/ocr-status")
def ocr_status():
    """供状态页 JS 刷新：device 是 paddle 实际运行设备，models 是已加载的
    OCR 语言模型及实例数（pool_status 已含这两个键，直接透传）。"""
    return pool_status()


@app.get("/userscript")
def userscript_source():
    """返回 userscript 源码。浏览器装了 Tampermonkey 时会识别 .user.js 头并提示安装。"""
    try:
        with open(_USERSCRIPT_PATH, encoding="utf-8") as f:
            return HTMLResponse(f.read(), media_type="text/javascript")
    except OSError:
        raise HTTPException(status_code=404, detail="userscript 文件未找到")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8765)
