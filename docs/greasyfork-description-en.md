Translate manga dialogue in place, right on the page you are reading.

> ⚠️ **This script requires a companion backend running on your own machine.
> Installing the script alone will not work.** The backend performs OCR and calls
> your LLM provider. See the repository link below for setup.

## Features

- **In-place overlay** — translations sit on the original bubbles and follow the image every frame, so scrolling and resizing never break alignment
- **Original always available** — press `` ` `` or click a block to see the artwork underneath; nothing is discarded
- **Dialogue only** — sound effects, watermarks and page numbers are detected and skipped by default (switchable to translate all text)
- **Broad site support** — `<img>`, `<canvas>`, CSS backgrounds, lazy loading, shadow DOM, same-origin iframes
- **Webtoon friendly** — very tall strips are tiled automatically so text is not lost to downscaling
- **Languages** — source: auto-detect / Japanese / Korean / English / Spanish / Chinese; UI available in 5 languages
- **Your choice of model** — any OpenAI-compatible endpoint (DeepSeek, OpenAI, Qwen, local Ollama…), switchable from the panel
- **Transparent cost** — live input / output token counters in the panel

## Requirements

1. Run the backend locally (Python + PaddleOCR)
2. Configure an OpenAI-compatible LLM endpoint

Setup guide: https://github.com/Yuff1010/Manga-Overlay-Translator

## Privacy

- Your API key stays in the local `server/.env`; the script never reads or sends it
- The backend listens on `127.0.0.1` only
- Images go only to your own machine; recognised text goes to the LLM endpoint you configured

Please only use this tool on pages you are entitled to access.
