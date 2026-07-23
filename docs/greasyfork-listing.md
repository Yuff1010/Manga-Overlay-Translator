# Greasy Fork 发布材料

发布到 [Greasy Fork](https://greasyfork.org/) 用的文案与步骤。
脚本本体在 [`userscript/ocr-translator.user.js`](../userscript/ocr-translator.user.js)。

---

## 发布步骤

1. 注册 / 登录 <https://greasyfork.org/>
2. 右上角 **发布脚本** → **发布你编写的脚本**
3. 把 `userscript/ocr-translator.user.js` 的**全部内容**粘进代码框
   （元数据块里已包含 `@name` `@version` `@license` 等必需项，无需另填）
4. 在 **附加信息 / Additional info** 里粘贴下面的《描述文案》
5. 语言选 **中文**，再用 “添加附加信息” 增加 **English** 版本
6. 提交

之后每次更新：编辑脚本 → 粘贴新代码 → **提升 `@version`**（Tampermonkey 靠它判断更新）。

> 脚本里**不要**写 `@updateURL` / `@downloadURL`。Greasy Fork 自己托管更新，
> 手动指向别处会与之冲突。

---

## 描述文案（中文）

**在你正在看的漫画页面上，直接把外文对白翻成中文，覆盖在原文位置。**

⚠️ **本脚本需要配合本机运行的后端服务使用。只安装脚本无法工作。**
后端负责 OCR 识别与调用 LLM 翻译，安装说明见 GitHub 仓库。

### 特点

- **就地覆盖**：译文盖在原文气泡上，逐帧跟随图片位置，滚动缩放都不错位
- **随时看原文**：按 `` ` `` 键或点击译文块即可切回原图，译文不会丢失
- **只翻对白**：自动识别并跳过音效、水印、页码等画面噪声（可切换为翻译全部文字）
- **广泛兼容**：支持 `<img>`、`<canvas>`、CSS 背景图、懒加载、shadow DOM、同源 iframe
- **长条漫友好**：自动切片识别，避免整图缩放导致的丢字
- **多语言**：源语言支持自动检测 / 日 / 韩 / 英 / 西 / 中；界面支持 5 种语言
- **模型自选**：任何 OpenAI 兼容接口（DeepSeek、OpenAI、通义千问、本地 Ollama 等），面板可切换
- **成本透明**：面板实时显示本轮消耗的输入 / 输出 token

### 使用前提

1. 在本机安装并启动后端服务（Python + PaddleOCR）
2. 配置一个 OpenAI 兼容的 LLM 接口密钥（密钥只保存在本地后端，不进浏览器）

完整安装说明：<https://github.com/Yuff1010/Manga-Translator>

### 隐私说明

- API 密钥只存在本地 `server/.env`，脚本不读取、不上传密钥
- 后端默认只监听 `127.0.0.1`
- 图片仅发送至你本机的服务；OCR 出的文本会发往你自己配置的 LLM 接口

请仅在你有权访问的页面上使用本工具。

---

## 描述文案（English）

**Translate manga dialogue in place, right on the page you are reading.**

⚠️ **This script requires a companion backend running on your own machine.
Installing the script alone will not work.** The backend performs OCR and calls
your LLM provider. See the GitHub repository for setup.

### Features

- **In-place overlay** — translations sit on the original bubbles and follow the
  image every frame, so scrolling and resizing never break alignment
- **Original always available** — press `` ` `` or click a block to see the artwork
  underneath; nothing is discarded
- **Dialogue only** — sound effects, watermarks and page numbers are detected and
  skipped by default (switchable to translate all text)
- **Broad site support** — `<img>`, `<canvas>`, CSS backgrounds, lazy loading,
  shadow DOM, same-origin iframes
- **Webtoon friendly** — very tall strips are tiled automatically so text is not
  lost to downscaling
- **Languages** — source: auto-detect / Japanese / Korean / English / Spanish /
  Chinese; UI available in 5 languages
- **Your choice of model** — any OpenAI-compatible endpoint (DeepSeek, OpenAI,
  Qwen, local Ollama…), switchable from the panel
- **Transparent cost** — live input / output token counters in the panel

### Requirements

1. Run the backend locally (Python + PaddleOCR)
2. Configure an OpenAI-compatible LLM endpoint

Setup guide: <https://github.com/Yuff1010/Manga-Translator>

### Privacy

- Your API key stays in the local `server/.env`; the script never reads or sends it
- The backend listens on `127.0.0.1` only
- Images go only to your own machine; recognised text goes to the LLM endpoint you configured

Please only use this tool on pages you are entitled to access.
