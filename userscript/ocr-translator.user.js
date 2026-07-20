// ==UserScript==
// @name         漫画图片翻译 (OCRTranslator)
// @namespace    ocr-translator
// @version      0.5.1
// @description  识别网页漫画图片中的外文，并在原文位置覆盖显示译文（需本地服务）
// @match        *://*/*
// @grant        GM_xmlhttpRequest
// @grant        GM_registerMenuCommand
// @grant        GM_getValue
// @grant        GM_setValue
// @connect      127.0.0.1
// @connect      *
// @run-at       document-idle
// ==/UserScript==

(function () {
  'use strict';

  const SERVER = 'http://127.0.0.1:8765';
  const MIN_SIZE = 250;        // 短边小于此值的图片跳过（忽略图标、头像）
  const MAX_RATIO = 2.5;       // 宽/高超过此值视为横幅或 logo，不是漫画页
  const MIN_FONT = 9;
  const MAX_FONT = 28;
  const CONCURRENCY = 4;       // 同时翻译几张（后端 OCR 有锁，主要重叠掉 LLM 往返）
  const PRELOAD = '1200px';    // 提前一屏多开始翻译，滚到时通常已就绪
  // 源语言值为 PaddleOCR 的语言代码；目标语言直接作为文案送进翻译 prompt
  const LANGS = {
    auto: '🔍 自动检测',
    japan: '日语', korean: '韩语', en: '英语', es: '西语', ch: '中文',
  };
  const TARGETS = ['中文', '英文', '日文', '韩文'];

  const getSrc = () => GM_getValue('src', 'japan');
  const getTgt = () => GM_getValue('tgt', '中文');

  // 按需翻译的运行状态
  const q = [];                // 待翻译队列
  const st = {
    running: 0, done: 0, failed: 0, tainted: 0, notimg: 0, untranslated: 0,
    lastErr: '', autoLang: '', on: false,
  };
  const RUN_COUNTERS = {
    done: 0, failed: 0, tainted: 0, notimg: 0, untranslated: 0, lastErr: '',
  };
  let io = null;               // 可视区观察器
  let mo = null;               // 监听后续插入的图片

  GM_registerMenuCommand('🈯 打开翻译面板', togglePanel);
  GM_registerMenuCommand('✖ 清除本页译文', reset);

  // ---------- 译文显示/隐藏 ----------

  let hidden = false;

  /** 只切显示状态，覆盖层和译文都留着，再按一次立刻回来，不用重新翻译 */
  function toggleHide(v) {
    hidden = v === undefined ? !hidden : v;
    for (const t of tracked) t.ov.style.display = hidden ? 'none' : '';
    const b = document.getElementById('ocrt-hide');
    if (b) b.textContent = hidden ? '👁 显示译文（`）' : '👁 查看原文（`）';
  }

  // 反引号（键盘左上角 ~ 键）切换，在输入框里打字时不拦截
  document.addEventListener('keydown', (e) => {
    if (e.key !== '`' || e.ctrlKey || e.metaKey || e.altKey) return;
    const t = e.target;
    if (t && (t.isContentEditable ||
              /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName || ''))) return;
    if (!tracked.length) return;
    e.preventDefault();
    toggleHide();
  });

  // ---------- 控制面板 ----------

  let panel = null;

  function togglePanel() {
    if (panel) {
      panel.remove();
      panel = null;
      return;
    }
    panel = document.createElement('div');
    panel.id = 'ocrt-panel';
    panel.style.cssText =
      'position:fixed;bottom:20px;right:20px;z-index:2147483646;' +
      'background:#242424;color:#eee;padding:12px 14px;border-radius:10px;' +
      'font:13px/1.5 "Microsoft YaHei",sans-serif;box-shadow:0 6px 20px rgba(0,0,0,.45);' +
      'display:flex;flex-direction:column;gap:9px;min-width:212px;';

    const row = document.createElement('div');
    row.style.cssText = 'display:flex;align-items:center;gap:7px;';
    const src = sel(LANGS, getSrc(), (v) => GM_setValue('src', v));
    const arrow = document.createElement('span');
    arrow.textContent = '→';
    arrow.style.cssText = 'opacity:.65;flex:none;';
    const tgt = sel(TARGETS, getTgt(), (v) => GM_setValue('tgt', v));
    row.append(src, arrow, tgt);

    const btns = document.createElement('div');
    btns.style.cssText = 'display:flex;gap:6px;';
    btns.append(
      btn('▶ 翻译', '#2d7d46', () => start()),
      btn('⚡ 全部', '#3a6ea5', () => start(true)),
      btn('✖', '#7a3030', reset)
    );

    const hide = btn('', '#5a5a5a', () => toggleHide());
    hide.id = 'ocrt-hide';

    const tip = document.createElement('div');
    tip.id = 'ocrt-status';
    tip.textContent = '滚到哪翻到哪';
    tip.style.cssText = 'opacity:.6;font-size:12px;';

    panel.append(row, btns, hide, tip);
    document.body.appendChild(panel);
    toggleHide(hidden);   // 同步按钮文案到当前状态
  }

  /** 造一个下拉框。opts 可以是 {值:显示名} 或 [值...] */
  function sel(opts, cur, onChange) {
    const s = document.createElement('select');
    s.style.cssText =
      'flex:1;min-width:0;background:#333;color:#eee;border:1px solid #555;' +
      'border-radius:5px;padding:4px 6px;font:13px "Microsoft YaHei",sans-serif;' +
      'cursor:pointer;outline:none;';
    const entries = Array.isArray(opts)
      ? opts.map((v) => [v, v])
      : Object.entries(opts);
    for (const [val, label] of entries) {
      const o = document.createElement('option');
      o.value = val;
      o.textContent = label;
      if (val === cur) o.selected = true;
      s.appendChild(o);
    }
    s.addEventListener('change', () => onChange(s.value));
    return s;
  }

  function btn(text, color, fn) {
    const b = document.createElement('button');
    b.textContent = text;
    b.style.cssText =
      `flex:1;background:${color};color:#fff;border:0;border-radius:5px;` +
      'padding:6px 8px;font:13px "Microsoft YaHei",sans-serif;cursor:pointer;';
    b.addEventListener('click', fn);
    return b;
  }

  /** 有面板时状态显示在面板里，否则退回 toast */
  function status(msg) {
    const el = document.getElementById('ocrt-status');
    if (el) el.textContent = msg;
    else toast(msg);
  }

  // ---------- 主流程：按需翻译 ----------

  function start(all) {
    // 元素上残留的 data-ocrt 会让 scan 全部跳过，表现为"点了没反应"。
    // ▶翻译：只清失败的，重试它们并接着翻新出现的图；
    // ⚡全部：连已完成的一起清掉，整页重来（换了语言时用这个）
    if (all) {
      document.querySelectorAll('.ocrt-overlay').forEach((el) => el.remove());
      tracked.length = 0;
      st.autoLang = '';
    }
    const sel = all ? '[data-ocrt]' : '[data-ocrt="fail"]';
    for (const el of document.querySelectorAll(sel)) {
      delete el.dataset.ocrt;
      delete el.dataset.ocrtWatch;
    }
    Object.assign(st, RUN_COUNTERS);
    if (!st.on) {
      st.on = true;
      io = new IntersectionObserver(onSee, { rootMargin: PRELOAD });
      // 漫画站常边滚边插入图片，这里持续把新出现的纳入观察。
      // 广告位会频繁改 DOM，加防抖避免每次变动都全量扫描
      let timer = null;
      mo = new MutationObserver(() => {
        clearTimeout(timer);
        timer = setTimeout(() => scan(all), 300);
      });
      mo.observe(document.body, { childList: true, subtree: true });
    }
    const n = scan(all);
    status(all ? `开始翻译 ${n} 张…` : `已开启，滚到哪翻到哪（${n} 张待处理）`);
  }

  /** 收集页面上还没处理过的图。all=true 直接全部入队，否则交给可视区观察 */
  function scan(all) {
    let n = 0;
    for (const el of collect()) {
      if (el.dataset.ocrt) continue;        // 已入队/处理过
      if (el.dataset.ocrtWatch && !all) continue;
      if (inChrome(el)) continue;           // 页头页尾里的 logo/banner
      if (!big(el) && loaded(el)) continue; // 已加载但不达标 → 不是漫画
      n++;
      if (all) enqueue(el);
      else {
        el.dataset.ocrtWatch = '1';
        io.observe(el);
      }
    }
    return n;
  }

  /**
   * 收集候选元素。漫画站的图片形态五花八门：<img>、<canvas>、CSS 背景图，
   * 还可能藏在 shadow DOM 或同源 iframe 里，这里一并挖出来。
   */
  function collect() {
    const out = [];
    const walk = (root, depth) => {
      if (!root || depth > 2) return;
      try {
        out.push(...root.querySelectorAll('img, canvas'));
        for (const el of root.querySelectorAll('*')) {
          if (el.shadowRoot) walk(el.shadowRoot, depth + 1);
          else if (el.tagName === 'IFRAME') {
            try {
              walk(el.contentDocument, depth + 1);  // 跨域会抛异常，跳过
            } catch (e) {}
          }
        }
      } catch (e) {}
    };
    walk(document, 0);
    // 背景图站点少见，且逐元素取样式较慢，只在常规图片没几张时才扫
    if (out.length < 3) {
      for (const el of document.querySelectorAll('div,section,figure,a,span')) {
        if (el.offsetWidth >= MIN_SIZE && el.offsetHeight >= MIN_SIZE && bgUrl(el)) {
          out.push(el);
        }
      }
    }
    return out;
  }

  /**
   * 是否已经能处理。非 <img> 元素没有加载状态，视为始终就绪；
   * <img> 只要能拿到真实地址就行——懒加载的图不必等站点把它换上。
   */
  const loaded = (el) => el.tagName !== 'IMG' || !!imgSrc(el);

  /** 页头页尾导航栏里的图基本是 logo/广告，不翻 */
  const inChrome = (el) => !!el.closest('header,footer,nav,aside');

  function big(el) {
    // 懒加载的图 naturalWidth 还是占位图的 1x1，这时按页面上的实际显示尺寸判断
    // （站点通常已为它预留了版位，webtoons 上就是 800x1280）
    if (el.tagName === 'IMG' && el.naturalWidth > 2) {
      return shaped(el.naturalWidth, el.naturalHeight);
    }
    const r = el.getBoundingClientRect();
    return shaped(r.width, r.height);
  }

  /**
   * 漫画页的形状判断。只排除"宽扁"的横幅/logo（如 1024x336 的站点 logo），
   * 不设高度上限——条漫本来就可能是 760x15000 这种极端竖长比例。
   */
  function shaped(w, h) {
    return w >= MIN_SIZE && h >= MIN_SIZE && w / h < MAX_RATIO;
  }

  function bgUrl(el) {
    const m = getComputedStyle(el).backgroundImage.match(/url\(["']?(.+?)["']?\)/);
    return m ? m[1] : '';
  }

  function onSee(entries) {
    for (const e of entries) {
      if (!e.isIntersecting) continue;
      io.unobserve(e.target);
      enqueue(e.target);
    }
  }

  /** 入队；图片若还没加载完（懒加载），等 load 后再入队 */
  function enqueue(el) {
    if (el.dataset.ocrt) return;
    if (!loaded(el)) {
      el.addEventListener('load', () => enqueue(el), { once: true });
      return;
    }
    if (!big(el) || inChrome(el)) return;
    el.dataset.ocrt = 'queued';
    q.push(el);
    pump();
  }

  /** 并发调度：最多 CONCURRENCY 个同时在飞 */
  function pump() {
    while (st.running < CONCURRENCY && q.length) {
      const img = q.shift();
      st.running++;
      handle(img).finally(() => {
        st.running--;
        report();
        pump();
      });
    }
  }

  async function handle(el) {
    el.dataset.ocrt = 'doing';
    el.style.outline = '3px solid #f90';
    try {
      const dataUrl = await grab(el);
      const res = await postJSON(SERVER + '/translate', {
        image: dataUrl,
        // 自动检测只需做一次：后端会把判定结果带回来，后续图片直接复用
        lang: getSrc() === 'auto' ? st.autoLang || 'auto' : getSrc(),
        target: getTgt(),
      });
      if (getSrc() === 'auto' && res.lang && !st.autoLang) {
        st.autoLang = res.lang;
        status(`检测到源语言：${LANGS[res.lang] || res.lang}`);
      }
      el.dataset.ocrt = 'done';
      if (res.blocks && res.blocks.length) overlay(el, res);
      if (res.blocks && res.blocks.length && res.translated === false) st.untranslated++;
      st.done++;
    } catch (e) {
      el.dataset.ocrt = 'fail';
      // 画布被保护、取回的不是图片：都不算"出错"，只是这张跳过，不该报红。
      // 分开计数，否则排查时分不清是哪种情况
      if (e.message === 'TAINTED') st.tainted++;
      else if (e.message === 'NOTIMAGE') st.notimg++;
      else {
        st.failed++;
        st.lastErr = e.message || String(e);
      }
      console.error('[ocrt]', el, e);
    } finally {
      el.style.outline = '';
    }
  }

  /** 按元素类型取出图片 dataURL */
  async function grab(el) {
    if (el.tagName === 'CANVAS') {
      // 画布若绘制过跨域且无 CORS 头的图，浏览器会禁止读取像素（安全限制）
      try {
        return el.toDataURL('image/png');
      } catch (e) {
        throw new Error('TAINTED');
      }
    }
    const url = el.tagName === 'IMG' ? imgSrc(el) : bgUrl(el);
    if (!url) throw new Error('找不到图片地址');
    try {
      return await fetchAsDataURL(url);
    } catch (e) {
      // 抓取失败还有一条后路：图片既然已经显示在页面上，浏览器就已经成功加载过它，
      // 直接从元素读像素即可，绕开防盗链/签名过期/登录态这些抓取才会遇到的问题。
      // 仅对同源或带 CORS 头的图有效，跨域无 CORS 的会污染画布读不出。
      const px = el.tagName === 'IMG' ? fromElement(el) : '';
      if (px) return px;
      throw e;
    }
  }

  /** 把已加载的 <img> 画进 canvas 取像素；跨域无 CORS 时会抛异常，返回空 */
  function fromElement(img) {
    if (!img.naturalWidth) return '';
    try {
      const c = document.createElement('canvas');
      c.width = img.naturalWidth;
      c.height = img.naturalHeight;
      c.getContext('2d').drawImage(img, 0, 0);
      return c.toDataURL('image/png');
    } catch (e) {
      return '';
    }
  }

  /** src 看着像占位图（1x1 透明、data: 内联、blank/loading 之类）就不可信 */
  const isPlaceholder = (u) =>
    !u || u.startsWith('data:') ||
    /transparen|blank|placeholder|spacer|loading|1x1|px\.(gif|png)/i.test(u);

  /**
   * 从 data-* 里找真实图片地址。
   * 不枚举属性名——各站自创的名字五花八门（data-url / data-original / data-echo…），
   * 枚举法遇到没见过的就失效。这里改成扫描所有 data-*，认"值像不像图片地址"。
   */
  function lazyUrl(img) {
    const vals = Object.values(img.dataset).filter((v) => typeof v === 'string');
    return (
      vals.find((v) => /^(https?:)?\/\/|^\//.test(v) && /\.(jpe?g|png|webp|gif|avif|bmp)/i.test(v)) ||
      vals.find((v) => /^(https?:)?\/\/\S{8,}/.test(v)) ||   // 无扩展名但确是网址
      ''
    );
  }

  /**
   * 取图片真实地址。懒加载站点把真地址放在 data-* 上，而 src 常已被填成
   * 占位图（webtoons.com 就是 1x1 透明 png + data-url）——此时不能信 src。
   * 拿到真地址后可以直接抓，不必等站点自己把图换上。
   */
  function imgSrc(img) {
    const lazy = lazyUrl(img);
    const cur = img.currentSrc || img.src || '';
    if (lazy && (img.naturalWidth <= 2 || isPlaceholder(cur))) return lazy;
    return cur || lazy;
  }

  function report() {
    const left = q.length + st.running;
    if (left) return status(`翻译中… 已完成 ${st.done} 张，剩 ${left} 张`);
    let msg = `完成 ${st.done} 张`;
    if (st.autoLang) msg += `（源语言：${LANGS[st.autoLang] || st.autoLang}）`;
    if (st.untranslated) msg += `，${st.untranslated} 张翻译失败显示原文`;
    if (st.failed) msg += `，失败 ${st.failed} 张：${st.lastErr}`;
    if (st.tainted) msg += `，${st.tainted} 张画布受站点保护读不出`;
    if (st.notimg) msg += `，${st.notimg} 张取回的不是图片（多为防盗链）`;
    status(msg);
  }

  function reset() {
    document.querySelectorAll('.ocrt-overlay').forEach((el) => el.remove());
    tracked.length = 0;
    if (syncTimer) {
      clearInterval(syncTimer);
      syncTimer = null;
    }
    if (io) io.disconnect();
    if (mo) mo.disconnect();
    io = mo = null;
    q.length = 0;
    Object.assign(st, RUN_COUNTERS, { running: 0, autoLang: '', on: false });
    toggleHide(false);
    for (const el of document.querySelectorAll('[data-ocrt],[data-ocrt-watch]')) {
      delete el.dataset.ocrt;
      delete el.dataset.ocrtWatch;
    }
    status('已清除，可重新翻译');
  }

  // ---------- 渲染 ----------

  function overlay(img, data) {
    const ov = document.createElement('div');
    ov.className = 'ocrt-overlay';
    ov.style.cssText =
      'position:absolute;z-index:2147483000;pointer-events:none;';
    // 处于"看原文"状态时新翻出来的图也保持隐藏，免得突然冒出来
    if (hidden) ov.style.display = 'none';
    for (let i = 0; i < data.blocks.length; i++) {
      const b = data.blocks[i];
      const el = document.createElement('div');
      el.className = 'ocrt-block';
      el.dataset.i = i;
      el.textContent = b.translation || b.text;
      // 翻译失败时覆盖层显示的其实是原文，用淡黄底标出来，别让人误以为翻译成功
      const failed = data.translated === false;
      el.title = failed ? '翻译失败，显示的是原文：' + b.text : b.text;
      el.style.cssText =
        'position:absolute;display:flex;align-items:center;justify-content:center;' +
        'text-align:center;overflow:hidden;box-sizing:border-box;' +
        `background:${failed ? 'rgba(255,247,214,.95)' : 'rgba(255,255,255,.93)'};` +
        'color:#111;border-radius:4px;' +
        'line-height:1.25;padding:1px 2px;pointer-events:auto;cursor:pointer;' +
        'font-family:"Microsoft YaHei",sans-serif;word-break:break-all;';
      // 点击暂时隐藏，便于看原图
      el.addEventListener('click', () => {
        el.style.visibility = 'hidden';
        setTimeout(() => (el.style.visibility = ''), 2000);
      });
      ov.appendChild(el);
    }
    document.body.appendChild(ov);
    // 覆盖层是 body 下的绝对定位元素，只要页面布局变动（漫画站边滚边加载图片，
    // 上方每插入一张，下面的图就整体下移）它就会错位——而图片只是"移动"没有
    // "改变尺寸"，resize 类事件都不会触发。所以这里持续跟随锚点图片。
    tracked.push({ ov, img, data, key: '', src: srcOf(img) });
    sync();
    startSync();
  }

  const tracked = [];
  let syncTimer = null;

  /** 锚点当前指向的图片地址，用来发现"元素还在但图换了" */
  const srcOf = (el) => (el.tagName === 'IMG' ? imgSrc(el) : '');

  function startSync() {
    if (!syncTimer) syncTimer = setInterval(sync, 200);
    window.addEventListener('resize', sync);
  }

  /** 逐个比对锚点图片的位置，变了才重新定位；图片没了或被换掉就清理 */
  function sync() {
    for (let i = tracked.length - 1; i >= 0; i--) {
      const t = tracked[i];
      // 图片从 DOM 移除，或被换成了另一张（有的阅读器复用同一个 <img> 只改 src）
      if (!t.img.isConnected || srcOf(t.img) !== t.src) {
        t.ov.remove();
        tracked.splice(i, 1);
        if (t.img.isConnected) {
          // 换图了：清掉标记让它重新排队，新页才会被翻译
          delete t.img.dataset.ocrt;
          delete t.img.dataset.ocrtWatch;
          if (st.on) enqueue(t.img);
        }
        continue;
      }
      const r = t.img.getBoundingClientRect();
      // 左右翻页的阅读器把非当前页缩成 0x0（MangaDex 就是如此）。
      // 这时必须把译文一并藏起来——否则它会停在最后的位置，盖在新翻到的那页上。
      // 用 visibility 而非 display，免得和"查看原文"那个开关打架。
      if (!r.width || !r.height) {
        t.ov.style.visibility = 'hidden';
        continue;
      }
      t.ov.style.visibility = '';
      const key = `${Math.round(r.left + scrollX)},${Math.round(r.top + scrollY)},${Math.round(r.width)},${Math.round(r.height)}`;
      if (key !== t.key) {
        t.key = key;
        position(t.ov, t.img, t.data);
      }
    }
    if (!tracked.length && syncTimer) {
      clearInterval(syncTimer);
      syncTimer = null;
    }
  }

  function position(ov, img, data) {
    const r = img.getBoundingClientRect();
    if (!r.width) return;
    ov.style.left = r.left + window.scrollX + 'px';
    ov.style.top = r.top + window.scrollY + 'px';
    ov.style.width = r.width + 'px';
    ov.style.height = r.height + 'px';
    const sx = r.width / data.width;
    const sy = r.height / data.height;
    // 先摆好位置和方向，字号留到后面统一决定
    for (const el of ov.children) {
      const b = data.blocks[el.dataset.i];
      const w = b.w * sx;
      const h = b.h * sy;
      el.style.left = b.x * sx + 'px';
      el.style.top = b.y * sy + 'px';
      el.style.width = w + 'px';
      el.style.height = h + 'px';
      // 竖长气泡（日漫竖排）→ 译文竖排更贴合
      el.style.writingMode = h > w * 1.6 ? 'vertical-rl' : 'horizontal-tb';
    }
    unifyFont(ov);
  }

  /**
   * 同一张图上的译文字号要尽量一致，否则满页大小不一很难看。
   * 逐块量出"能塞下的最大字号"，取中位数作为全图基准；
   * 只有塞不下的块才单独调小，这样既统一又不会溢出。
   */
  function unifyFont(ov) {
    const els = [...ov.children];
    const fits = els.map(maxFit);
    const sorted = [...fits].sort((a, b) => a - b);
    const base = sorted[Math.floor(sorted.length / 2)] || MIN_FONT;
    els.forEach((el, i) => {
      el.style.fontSize = Math.min(base, fits[i]) + 'px';
    });
  }

  /** 二分找这个块能容纳的最大字号（用实际渲染结果判断，不靠估算） */
  function maxFit(el) {
    let lo = MIN_FONT;
    let hi = MAX_FONT;
    let best = MIN_FONT;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      el.style.fontSize = mid + 'px';
      if (el.scrollHeight <= el.clientHeight + 1 &&
          el.scrollWidth <= el.clientWidth + 1) {
        best = mid;
        lo = mid + 1;
      } else {
        hi = mid - 1;
      }
    }
    return best;
  }

  // ---------- 工具 ----------

  function fetchAsDataURL(url) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method: 'GET',
        url,
        responseType: 'blob',
        // 图床普遍有防盗链：GM_xmlhttpRequest 是后台跨域请求，默认不带 Referer，
        // 会被判成盗链直接 403（webtoons 的 pstatic.net 就是这样）。
        // 补上浏览器加载同一张图时本就会带的来源头。
        headers: { Referer: location.href, Origin: location.origin },
        onload: (r) => {
          if (r.status && r.status >= 400) {
            return reject(new Error('图片请求 HTTP ' + r.status));
          }
          // 防盗链常返回 HTML 拦截页，拦下来免得后端报 400。
          // 但只拦"明显不是图片"的：不少 CDN 返回图片时 MIME 为空或是
          // application/octet-stream，一律要求 image/* 会误伤正常图片。
          const blob = r.response;
          const type = (blob && blob.type) || '';
          const bad =
            type.startsWith('text/') ||
            type.includes('html') ||
            type.includes('json') ||
            !blob ||
            blob.size < 100;
          if (bad) return reject(new Error('NOTIMAGE'));
          const fr = new FileReader();
          fr.onload = () => resolve(fr.result);
          fr.onerror = reject;
          fr.readAsDataURL(r.response);
        },
        onerror: reject,
        ontimeout: reject,
      });
    });
  }

  function postJSON(url, body) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method: 'POST',
        url,
        headers: { 'Content-Type': 'application/json' },
        data: JSON.stringify(body),
        timeout: 300000,
        onload: (r) =>
          r.status === 200
            ? resolve(JSON.parse(r.responseText))
            : reject(new Error('HTTP ' + r.status + ': ' + r.responseText)),
        onerror: () => reject(new Error('无法连接本地服务，请确认已启动 server')),
        ontimeout: () => reject(new Error('请求超时')),
      });
    });
  }

  /** ms=0 表示常驻（进度提示），否则到时自动隐藏 */
  function toast(msg, ms = 4000) {
    let t = document.getElementById('ocrt-toast');
    if (!t) {
      t = document.createElement('div');
      t.id = 'ocrt-toast';
      t.style.cssText =
        'position:fixed;bottom:24px;left:24px;z-index:2147483647;' +
        'background:#222;color:#fff;padding:10px 16px;border-radius:8px;' +
        'font:14px sans-serif;box-shadow:0 4px 12px rgba(0,0,0,.3);';
      document.body.appendChild(t);
    }
    t.textContent = msg;
    t.style.display = 'block';
    clearTimeout(t._timer);
    if (ms) t._timer = setTimeout(() => (t.style.display = 'none'), ms);
  }
})();
