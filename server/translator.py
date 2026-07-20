"""调用 OpenAI 兼容的 LLM API 整批翻译（保持上下文连贯）。"""
import json
import os
import time

import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
API_KEY = os.getenv("LLM_API_KEY", "")
MODEL = os.getenv("LLM_MODEL", "deepseek-chat")

# 推理型模型（如 deepseek-v4-flash）默认会为每句台词消耗上千 reasoning token，
# 翻译这种简单任务上 97% 的时间都花在"思考"上（实测 30s → 1.5s）。
# 该参数是 DeepSeek 系专有的，其他接口不认识会返回 400 —— 那就自动退回不带它重试。
_supports_no_thinking = True

SYSTEM_PROMPT = (
    "你是专业漫画翻译。将 JSON 数组中的每句台词翻译成{target}，"
    "口语化、简短、符合漫画语气。台词来自同一页漫画，注意上下文连贯。"
    "OCR 可能有少量错字，按语境纠正。"
    "只返回与输入等长的 JSON 字符串数组，不要输出任何其他内容。"
)


def _post(texts, target: str, no_thinking: bool):
    body = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT.format(target=target)},
            {"role": "user", "content": json.dumps(texts, ensure_ascii=False)},
        ],
        "temperature": 0.3,
    }
    if no_thinking:
        body["thinking"] = {"type": "disabled"}
    return requests.post(
        f"{BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json=body,
        timeout=90,
    )


RETRIES = 3       # 偶发失败（限流、超时、返回条数对不上）重试几次
CHUNK = 25        # 单次最多翻多少条，太长模型容易少返回几条


def _translate_once(texts, target: str):
    """翻一批。成功返回等长列表，失败返回 None。"""
    global _supports_no_thinking
    resp = _post(texts, target, _supports_no_thinking)
    if resp.status_code == 400 and _supports_no_thinking:
        # 接口不支持 thinking 参数 → 记住并退回普通请求
        _supports_no_thinking = False
        print("[translator] 接口不支持 thinking 参数，已退回普通模式")
        resp = _post(texts, target, False)
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"].strip()
    result = json.loads(_strip_fence(content))
    if isinstance(result, list) and len(result) == len(texts):
        return [str(t) for t in result]
    got = len(result) if isinstance(result, list) else "非数组"
    raise ValueError(f"返回条数不符：期望 {len(texts)}，实际 {got}")


def _translate_chunk(texts, target: str):
    """带重试。全部失败才原样返回，保证接口不中断。"""
    for i in range(RETRIES):
        try:
            return _translate_once(texts, target)
        except Exception as e:  # noqa: BLE001
            print(f"[translator] 第 {i + 1}/{RETRIES} 次失败: {e}")
            if i + 1 < RETRIES:
                time.sleep(1.5 * (i + 1))   # 退避，避开限流
    return texts


def translate_texts(texts, target: str = "中文"):
    """整页台词批量翻译。分批进行，某批失败不影响其余批次。"""
    if not API_KEY:
        return [f"[未配置 LLM_API_KEY] {t}" for t in texts]
    out = []
    for i in range(0, len(texts), CHUNK):
        out.extend(_translate_chunk(texts[i : i + CHUNK], target))
    return out


def _strip_fence(s: str) -> str:
    """去掉模型可能输出的 ```json ... ``` 围栏。"""
    if s.startswith("```"):
        s = s.split("\n", 1)[-1]
        s = s.rsplit("```", 1)[0]
    return s.strip()
