# Greasy Fork 发布指南

> ⚠️ **本文件是给维护者看的操作步骤，不要粘贴到 Greasy Fork 的描述框里。**
> 要粘贴的描述文案是另外两个文件：
> - 中文：[`greasyfork-description-zh.md`](greasyfork-description-zh.md)
> - English：[`greasyfork-description-en.md`](greasyfork-description-en.md)
>
> 那两个文件的**全文**就是描述内容，可以整份复制。

---

## 首次发布

1. 注册 / 登录 <https://greasyfork.org/>
2. 右上角 **发布脚本** → **发布你编写的脚本**
3. **脚本类型**选「公开用户脚本」
4. **代码**框：粘贴 [`userscript/ocr-translator.user.js`](../userscript/ocr-translator.user.js) 的全部内容
   （元数据块里已含 `@name` `@version` `@license` 等必需项，无需另填）
5. **附加信息**：格式选 `Markdown`，粘贴 `greasyfork-description-zh.md` 的**全文**
6. 点「添加本地化的附加信息」，语言选 **English**，粘贴 `greasyfork-description-en.md` 的**全文**
7. 提交

## 后续更新

编辑脚本 → 粘贴新代码 → **确保 `@version` 比线上版本高**。
Tampermonkey 靠这个号判断是否提示用户更新，忘了升号用户就收不到更新。

## 注意

- 脚本里**不要**写 `@updateURL` / `@downloadURL`。Greasy Fork 自行托管更新，
  手动指向别处会与之冲突。
- 描述里保留「需要本地后端」的醒目提示。这是本工具与普通脚本最大的不同，
  漏掉会导致用户装完发现不能用。
- 发布拿到脚本页 URL 后，回填到 README 的「用户脚本安装」章节。
