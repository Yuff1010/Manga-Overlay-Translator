// ==UserScript==
// @name         漫画图片翻译 (OCRTranslator)
// @namespace    ocr-translator
// @version      0.10.1
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
  const CUSTOM = '__custom__';   // 模型下拉里"自定义"那一项的值

  // ---------- 界面语言 ----------

  const UI_LANGS = { zh: '中文', en: 'English', ja: '日本語', ko: '한국어', es: 'Español' };

  // 目标语言：值要发给 LLM 写进 prompt，所以固定不翻译；只有显示名跟随界面语言
  const TARGETS = { 中文: 'lZh', 英文: 'lEn', 日文: 'lJa', 韩文: 'lKo' };
  // 源语言：值是 PaddleOCR 的语言代码
  const SRC_LANGS = {
    auto: 'lAuto', japan: 'lJa', korean: 'lKo', en: 'lEn', es: 'lEs', ch: 'lZh',
  };

  const I18N = {
    zh: {
      lAuto: '自动检测', lJa: '日语', lKo: '韩语', lEn: '英语', lEs: '西语', lZh: '中文',
      menuPanel: '打开翻译面板', menuClear: '清除本页译文',
      modeOverlay: '盖在图上', modeList: '图下方列出',
      scopeDialogue: '只翻对白旁白', scopeAll: '翻译全部文字',
      btnTranslate: '翻译', btnAll: '全部', btnClear: '清除',
      showOrig: '查看原文（`）', showTrans: '显示译文（`）',
      hint: '滚到哪翻到哪',
      loadingModels: '正在读取模型列表…', modelsFail: '读不到模型列表（后端未启动？）',
      customModel: '自定义模型…', customHint: '模型名，如 gpt-4o-mini',
      startAll: '开始翻译 {0} 张…', startLazy: '已开启，滚到哪翻到哪（{0} 张待处理）',
      working: '翻译中… 已完成 {0} 张，剩 {1} 张',
      done: '完成 {0} 张', srcIs: '（源语言：{0}）',
      tokens: '　tokens 输入 {0} / 输出 {1}',
      untranslated: '，{0} 张翻译失败显示原文', failed: '，失败 {0} 张：{1}',
      tainted: '，{0} 张画布受站点保护读不出',
      notimg: '，{0} 张取回的不是图片（多为防盗链）',
      cleared: '已清除，可重新翻译', detected: '检测到源语言：{0}',
      tipFailed: '翻译失败，显示的是原文：',
      errServer: '无法连接本地服务，请确认已启动 server',
      errTimeout: '请求超时', errNoUrl: '找不到图片地址', errHttp: '图片请求 HTTP {0}',
    },
    en: {
      lAuto: 'Auto-detect', lJa: 'Japanese', lKo: 'Korean', lEn: 'English',
      lEs: 'Spanish', lZh: 'Chinese',
      menuPanel: 'Open translator panel', menuClear: 'Clear translations',
      modeOverlay: 'Overlay on image', modeList: 'List below image',
      scopeDialogue: 'Dialogue only', scopeAll: 'All text',
      btnTranslate: 'Translate', btnAll: 'All', btnClear: 'Clear',
      showOrig: 'Show original (`)', showTrans: 'Show translation (`)',
      hint: 'Translates as you scroll',
      loadingModels: 'Loading models…', modelsFail: 'Cannot load models (server running?)',
      customModel: 'Custom model…', customHint: 'Model name, e.g. gpt-4o-mini',
      startAll: 'Translating {0} images…',
      startLazy: 'On — translates as you scroll ({0} pending)',
      working: 'Translating… {0} done, {1} left',
      done: '{0} done', srcIs: ' (source: {0})',
      tokens: '　tokens in {0} / out {1}',
      untranslated: ', {0} failed and show original', failed: ', {0} errored: {1}',
      tainted: ', {0} on protected canvas',
      notimg: ', {0} did not return an image (hotlink protection?)',
      cleared: 'Cleared — you can translate again', detected: 'Detected source: {0}',
      tipFailed: 'Translation failed, showing original: ',
      errServer: 'Cannot reach local server — is it running?',
      errTimeout: 'Request timed out', errNoUrl: 'No image URL found',
      errHttp: 'Image request HTTP {0}',
    },
    ja: {
      lAuto: '自動検出', lJa: '日本語', lKo: '韓国語', lEn: '英語',
      lEs: 'スペイン語', lZh: '中国語',
      menuPanel: '翻訳パネルを開く', menuClear: '訳文を消去',
      modeOverlay: '画像に重ねる', modeList: '画像の下に並べる',
      scopeDialogue: 'セリフとナレーションのみ', scopeAll: 'すべての文字',
      btnTranslate: '翻訳', btnAll: '全部', btnClear: '消去',
      showOrig: '原文を見る（`）', showTrans: '訳文を見る（`）',
      hint: 'スクロールした所から翻訳',
      loadingModels: 'モデル一覧を取得中…',
      modelsFail: 'モデル一覧を取得できません（サーバ未起動？）',
      customModel: 'カスタムモデル…', customHint: 'モデル名（例: gpt-4o-mini）',
      startAll: '{0} 枚の翻訳を開始…',
      startLazy: '有効：スクロールした所から翻訳（残り {0} 枚）',
      working: '翻訳中… {0} 枚完了、残り {1} 枚',
      done: '{0} 枚完了', srcIs: '（原文: {0}）',
      tokens: '　tokens 入力 {0} / 出力 {1}',
      untranslated: '、{0} 枚は翻訳失敗で原文表示', failed: '、{0} 枚失敗: {1}',
      tainted: '、{0} 枚はcanvas保護で読めず',
      notimg: '、{0} 枚は画像を取得できず（直リンク制限？）',
      cleared: '消去しました。再翻訳できます', detected: '原文の言語: {0}',
      tipFailed: '翻訳失敗、原文を表示: ',
      errServer: 'ローカルサーバに接続できません',
      errTimeout: 'タイムアウト', errNoUrl: '画像URLが見つかりません',
      errHttp: '画像リクエスト HTTP {0}',
    },
    ko: {
      lAuto: '자동 감지', lJa: '일본어', lKo: '한국어', lEn: '영어',
      lEs: '스페인어', lZh: '중국어',
      menuPanel: '번역 패널 열기', menuClear: '번역문 지우기',
      modeOverlay: '이미지 위에 표시', modeList: '이미지 아래에 나열',
      scopeDialogue: '대사와 해설만', scopeAll: '모든 문자',
      btnTranslate: '번역', btnAll: '전체', btnClear: '지우기',
      showOrig: '원문 보기(`)', showTrans: '번역문 보기(`)',
      hint: '스크롤하는 대로 번역',
      loadingModels: '모델 목록 불러오는 중…',
      modelsFail: '모델 목록을 불러올 수 없음(서버 실행 중?)',
      customModel: '직접 입력…', customHint: '모델명, 예: gpt-4o-mini',
      startAll: '{0}장 번역 시작…',
      startLazy: '켜짐 — 스크롤하는 대로 번역(대기 {0}장)',
      working: '번역 중… {0}장 완료, {1}장 남음',
      done: '{0}장 완료', srcIs: ' (원문: {0})',
      tokens: '　tokens 입력 {0} / 출력 {1}',
      untranslated: ', {0}장 번역 실패로 원문 표시', failed: ', {0}장 오류: {1}',
      tainted: ', {0}장 canvas 보호로 읽기 불가',
      notimg: ', {0}장 이미지가 아님(핫링크 차단?)',
      cleared: '지웠습니다. 다시 번역할 수 있습니다', detected: '원문 언어: {0}',
      tipFailed: '번역 실패, 원문 표시: ',
      errServer: '로컬 서버에 연결할 수 없습니다',
      errTimeout: '요청 시간 초과', errNoUrl: '이미지 주소를 찾을 수 없음',
      errHttp: '이미지 요청 HTTP {0}',
    },
    es: {
      lAuto: 'Detección automática', lJa: 'Japonés', lKo: 'Coreano',
      lEn: 'Inglés', lEs: 'Español', lZh: 'Chino',
      menuPanel: 'Abrir panel de traducción', menuClear: 'Borrar traducciones',
      modeOverlay: 'Sobre la imagen', modeList: 'Lista bajo la imagen',
      scopeDialogue: 'Solo diálogo', scopeAll: 'Todo el texto',
      btnTranslate: 'Traducir', btnAll: 'Todo', btnClear: 'Borrar',
      showOrig: 'Ver original (`)', showTrans: 'Ver traducción (`)',
      hint: 'Traduce según desplazas',
      loadingModels: 'Cargando modelos…',
      modelsFail: 'No se cargan los modelos (¿servidor activo?)',
      customModel: 'Modelo personalizado…', customHint: 'Nombre, p. ej. gpt-4o-mini',
      startAll: 'Traduciendo {0} imágenes…',
      startLazy: 'Activo — traduce según desplazas ({0} pendientes)',
      working: 'Traduciendo… {0} listas, faltan {1}',
      done: '{0} listas', srcIs: ' (origen: {0})',
      tokens: '　tokens entrada {0} / salida {1}',
      untranslated: ', {0} fallaron y muestran el original',
      failed: ', {0} con error: {1}',
      tainted: ', {0} en canvas protegido',
      notimg: ', {0} no devolvieron imagen (¿antienlace?)',
      cleared: 'Borrado — puedes traducir de nuevo',
      detected: 'Idioma detectado: {0}',
      tipFailed: 'Fallo la traducción, se muestra el original: ',
      errServer: 'No se conecta al servidor local',
      errTimeout: 'Tiempo agotado', errNoUrl: 'No se encontró la URL de la imagen',
      errHttp: 'Petición de imagen HTTP {0}',
    },
  };

  /** 首次使用时跟随浏览器语言，认不出就用中文 */
  function detectUI() {
    const l = (navigator.language || '').toLowerCase();
    if (l.startsWith('zh')) return 'zh';
    if (l.startsWith('ja')) return 'ja';
    if (l.startsWith('ko')) return 'ko';
    if (l.startsWith('es')) return 'es';
    if (l.startsWith('en')) return 'en';
    return 'zh';
  }

  const getUI = () => GM_getValue('ui', detectUI());

  /** 取文案并填入 {0}{1}…；缺词条时退回中文，不至于显示成 key */
  function t(key, ...args) {
    const dict = I18N[getUI()] || I18N.zh;
    let s = dict[key] !== undefined ? dict[key] : I18N.zh[key];
    if (s === undefined) return key;
    args.forEach((v, i) => (s = s.split('{' + i + '}').join(v)));
    return s;
  }

  /** 把 {值: 词条key} 映射成下拉框要的 {值: 当前语言显示名} */
  const labeled = (map) =>
    Object.fromEntries(Object.entries(map).map(([v, k]) => [v, t(k)]));
  // 两种译文呈现方式：overlay 盖在原文位置并逐帧跟随图片；
  // list 作为普通文字排在图片下方，气泡太小看不清时使用。
  const MODES = { overlay: 'modeOverlay', list: 'modeList' };
  // 翻译范围：后端会给每个文本块标 dialogue，前端据此即时筛选，不必重翻
  const SCOPES = { dialogue: 'scopeDialogue', all: 'scopeAll' };

  /** 语言代码 → 当前界面语言下的显示名 */
  const langName = (c) => (SRC_LANGS[c] ? t(SRC_LANGS[c]) : c);

  const getSrc = () => GM_getValue('src', 'japan');
  const getTgt = () => GM_getValue('tgt', '中文');
  const getMode = () => GM_getValue('mode', 'overlay');
  const getScope = () => GM_getValue('scope', 'dialogue');

  // 按需翻译的运行状态
  const q = [];                // 待翻译队列
  const st = {
    running: 0, done: 0, failed: 0, tainted: 0, notimg: 0, untranslated: 0,
    tokIn: 0, tokOut: 0, lastErr: '', autoLang: '', on: false,
    // 每次重新开始就 +1。在飞的请求带着旧编号回来时直接丢弃它的计数，
    // 否则「点了✖或重新翻译」后旧请求继续减 running，会出现"剩 -2 张"
    gen: 0,
  };
  const RUN_COUNTERS = {
    done: 0, failed: 0, tainted: 0, notimg: 0, untranslated: 0, lastErr: '',
    tokIn: 0, tokOut: 0, transErr: '',
  };
  let io = null;               // 可视区观察器
  let mo = null;               // 监听后续插入的图片

  GM_registerMenuCommand(t('menuPanel'), togglePanel);
  GM_registerMenuCommand(t('menuClear'), reset);

  // ---------- 译文显示/隐藏 ----------

  let hidden = false;

  /** 只切显示状态，覆盖层和译文都留着，再按一次立刻回来，不用重新翻译 */
  function toggleHide(v) {
    hidden = v === undefined ? !hidden : v;
    applyMode();
    const b = document.getElementById('ocrt-hide');
    if (b) b.textContent = hidden ? t('showTrans') : t('showOrig');
  }

  /**
   * 按当前模式决定每张图显示覆盖层还是下方列表。
   * 两份都建好了留在那儿，切换只是改 display，不用重新翻译。
   */
  function applyMode() {
    const listMode = getMode() === 'list';
    const onlyDialogue = getScope() === 'dialogue';
    for (const t of tracked) {
      if (!listMode) mount(t);              // 两种覆盖方式之间切换要换挂载点
      const off = hidden || t.offscreen;    // 整体隐藏，或图片当前不可见（翻页）
      if (t.ov) t.ov.style.display = off || listMode ? 'none' : '';
      if (t.list) t.list.style.display = off || !listMode ? 'none' : '';
      // 非对白的块（音效、水印、噪声）按范围显隐。标记在翻译时就拿到了，
      // 所以切范围只是改 display，不用重新请求
      for (const box of [t.ov, t.list]) {
        if (!box) continue;
        for (const el of box.children) {
          el.style.display = onlyDialogue && el.dataset.dlg === '0' ? 'none' : '';
        }
      }
    }
  }

  // 反引号（键盘左上角 ~ 键）切换，在输入框里打字时不拦截
  document.addEventListener('keydown', (e) => {
    if (e.key !== '`' || e.ctrlKey || e.metaKey || e.altKey) return;
    const tgt = e.target;   // 别叫 t，会遮蔽 i18n 的 t()
    if (tgt && (tgt.isContentEditable ||
                /^(INPUT|TEXTAREA|SELECT)$/.test(tgt.tagName || ''))) return;
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
      'font:13px/1.5 system-ui,"Microsoft YaHei","Malgun Gothic","Yu Gothic",sans-serif;box-shadow:0 6px 20px rgba(0,0,0,.45);' +
      // 宽度固定：状态行文字长短一直在变，用 min-width 会让整个面板跟着伸缩
      'display:flex;flex-direction:column;gap:9px;width:236px;box-sizing:border-box;';

    const row = document.createElement('div');
    row.style.cssText = 'display:flex;align-items:center;gap:7px;';
    const src = sel(labeled(SRC_LANGS), getSrc(), (v) => GM_setValue('src', v));
    const arrow = document.createElement('span');
    arrow.textContent = '→';
    arrow.style.cssText = 'opacity:.65;flex:none;';
    const tgt = sel(labeled(TARGETS), getTgt(), (v) => GM_setValue('tgt', v));
    row.append(src, arrow, tgt);

    const btns = document.createElement('div');
    btns.style.cssText = 'display:flex;gap:6px;';
    btns.append(
      btn(t('btnTranslate'), '#2d7d46', () => start()),
      btn(t('btnAll'), '#3a6ea5', () => start(true)),
      btn(t('btnClear'), '#7a3030', reset)
    );

    const mode = sel(labeled(MODES), getMode(), (v) => {
      GM_setValue('mode', v);
      applyMode();      // 已翻好的不用重来，只是换个显示方式
    });

    const scope = sel(labeled(SCOPES), getScope(), (v) => {
      GM_setValue('scope', v);
      applyMode();      // 同上：dialogue 标记已经在手，切范围只是显隐
    });

    // 界面语言：换了要重建面板，因为每个控件的文案都是创建时定下的
    const ui = sel(UI_LANGS, getUI(), (v) => {
      GM_setValue('ui', v);
      togglePanel();
      togglePanel();
    });

    const hide = btn('', '#5a5a5a', () => toggleHide());
    hide.id = 'ocrt-hide';

    // 模型下拉框：内容要等 /config 回来才知道，先占位
    const llmRow = document.createElement('div');
    llmRow.id = 'ocrt-llm';
    llmRow.style.cssText = 'display:flex;flex-direction:column;gap:5px;';

    const tip = document.createElement('div');
    tip.id = 'ocrt-status';
    tip.textContent = t('hint');
    tip.style.cssText = 'opacity:.6;font-size:12px;word-break:break-word;white-space:normal;';

    panel.append(row, llmRow, mode, scope, ui, btns, hide, tip);
    document.body.appendChild(panel);
    toggleHide(hidden);   // 同步按钮文案到当前状态
    loadModels(llmRow);
  }

  /** 向后端要可用的供应商/模型，填充下拉框。API Key 留在服务端，不经过浏览器 */
  function loadModels(box) {
    box.textContent = t('loadingModels');
    getJSON(SERVER + '/config')
      .then((cfg) => {
        box.textContent = '';
        const opts = {};
        for (const p of cfg.profiles || []) {
          for (const m of p.models || []) opts[`${p.id}::${m}`] = `${p.name} / ${m}`;
        }
        opts[CUSTOM] = t('customModel');

        const cur = GM_getValue('llm', '');
        const picked = opts[cur] ? cur : Object.keys(opts)[0];
        const s = sel(opts, picked, (v) => {
          GM_setValue('llm', v);
          custom.style.display = v === CUSTOM ? '' : 'none';
        });

        // 自定义：沿用所选供应商的接口和密钥，只把模型名换掉
        const custom = document.createElement('input');
        custom.placeholder = t('customHint');
        custom.value = GM_getValue('llmCustom', '');
        custom.style.cssText =
          'background:#333;color:#eee;border:1px solid #555;border-radius:5px;' +
          'padding:4px 6px;font:13px system-ui,"Microsoft YaHei","Malgun Gothic","Yu Gothic",sans-serif;outline:none;';
        custom.addEventListener('change', () =>
          GM_setValue('llmCustom', custom.value.trim())
        );
        custom.style.display = picked === CUSTOM ? '' : 'none';

        box.append(s, custom);
      })
      .catch(() => {
        box.textContent = t('modelsFail');
      });
  }

  /** 当前选中的供应商和模型 */
  function getLLM() {
    const v = GM_getValue('llm', '');
    if (!v) return {};
    if (v === CUSTOM) {
      // 自定义模型挂在第一个供应商下；服务端拿到空 profile 会用默认那组
      return { model: GM_getValue('llmCustom', '') };
    }
    const [profile, model] = v.split('::');
    return { profile, model };
  }

  /** 造一个下拉框。opts 可以是 {值:显示名} 或 [值...] */
  function sel(opts, cur, onChange) {
    const s = document.createElement('select');
    s.style.cssText =
      'flex:1;min-width:0;background:#333;color:#eee;border:1px solid #555;' +
      'border-radius:5px;padding:4px 6px;font:13px system-ui,"Microsoft YaHei","Malgun Gothic","Yu Gothic",sans-serif;' +
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
      'padding:6px 8px;font:13px system-ui,"Microsoft YaHei","Malgun Gothic","Yu Gothic",sans-serif;cursor:pointer;';
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
      document.querySelectorAll('.ocrt-overlay,.ocrt-list').forEach((el) => el.remove());
      tracked.length = 0;
      st.autoLang = '';
    }
    const sel = all ? '[data-ocrt]' : '[data-ocrt="fail"]';
    for (const el of document.querySelectorAll(sel)) {
      delete el.dataset.ocrt;
      delete el.dataset.ocrtWatch;
    }
    Object.assign(st, RUN_COUNTERS);
    st.gen++;          // 作废上一轮还在飞的请求，它们回来后不再影响计数
    st.running = 0;
    q.length = 0;
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
    status(all ? t('startAll', n) : t('startLazy', n));
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
    // 真加载完了：按图片自身尺寸判断，图标/头像在这里被滤掉
    if (el.tagName === 'IMG' && el.naturalWidth > 2) {
      return shaped(el.naturalWidth, el.naturalHeight);
    }
    // 还没加载（占位 1x1）：先看显示尺寸——有的站为懒加载图预留了版位
    // （webtoons 就是 800x1280），够大就算数
    const r = el.getBoundingClientRect();
    if (shaped(r.width, r.height)) return true;
    // 版位也塌缩了（dongmanhi 这类，整页图挤在几百 px 内）：
    // 只要拿得到懒加载真实地址，就当内容图——懒加载几乎只用于正文大图，
    // 图标不会这么处理。真抓下来若是小图，后端 OCR 出空也不会显示译文。
    return el.tagName === 'IMG' && !!lazyUrl(el);
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
      const gen = st.gen;          // 记下这次请求属于哪一轮
      st.running++;
      handle(img, gen).finally(() => {
        if (gen !== st.gen) return;   // 上一轮的残兵，不该再动本轮计数
        st.running = Math.max(0, st.running - 1);
        report();
        pump();
      });
    }
  }

  async function handle(el, gen) {
    el.dataset.ocrt = 'doing';
    el.style.outline = '3px solid #f90';
    try {
      const dataUrl = await grab(el);
      const res = await postJSON(SERVER + '/translate', {
        image: dataUrl,
        // 自动检测只需做一次：后端会把判定结果带回来，后续图片直接复用
        lang: getSrc() === 'auto' ? st.autoLang || 'auto' : getSrc(),
        target: getTgt(),
        ...getLLM(),
      });
      if (res.usage) {
        st.tokIn += res.usage.prompt_tokens || 0;
        st.tokOut += res.usage.completion_tokens || 0;
      }
      if (getSrc() === 'auto' && res.lang && !st.autoLang) {
        st.autoLang = res.lang;
        status(t('detected', langName(res.lang)));
      }
      if (gen !== st.gen) return;      // 这轮已经作废，别把结果画上去
      el.dataset.ocrt = 'done';
      if (res.blocks && res.blocks.length) overlay(el, res);
      if (res.blocks && res.blocks.length && res.translated === false) {
        st.untranslated++;
        if (res.error) st.transErr = res.error;   // 后端给的具体原因
      }
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
    if (!url) throw new Error(t('errNoUrl'));
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

  /** 上千的数字用 k 简写，免得状态行被一串数字撑爆 */
  const kilo = (n) => (n >= 1000 ? (n / 1000).toFixed(1) + 'k' : String(n));

  function report() {
    const left = q.length + st.running;
    if (left) return status(t('working', st.done, left));
    let msg = t('done', st.done);
    if (st.autoLang) msg += t('srcIs', langName(st.autoLang));
    if (st.tokIn || st.tokOut) msg += t('tokens', kilo(st.tokIn), kilo(st.tokOut));
    if (st.untranslated) {
      msg += t('untranslated', st.untranslated);
      if (st.transErr) msg += `（${st.transErr}）`;
    }
    if (st.failed) msg += t('failed', st.failed, st.lastErr);
    if (st.tainted) msg += t('tainted', st.tainted);
    if (st.notimg) msg += t('notimg', st.notimg);
    status(msg);
  }

  function reset() {
    document.querySelectorAll('.ocrt-overlay,.ocrt-list').forEach((el) => el.remove());
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
    st.gen++;          // 同上：清除后旧请求回来不该再改计数
    toggleHide(false);
    for (const el of document.querySelectorAll('[data-ocrt],[data-ocrt-watch]')) {
      delete el.dataset.ocrt;
      delete el.dataset.ocrtWatch;
    }
    status(t('cleared'));
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
      // 后端标出的"是否对白"，供切换翻译范围时筛选
      el.dataset.dlg = b.dialogue === false ? '0' : '1';
      el.textContent = b.translation || b.text;
      // 翻译失败时覆盖层显示的其实是原文，用淡黄底标出来，别让人误以为翻译成功
      const failed = data.translated === false;
      el.title = failed ? t('tipFailed') + b.text : b.text;
      el.style.cssText =
        'position:absolute;display:flex;align-items:center;justify-content:center;' +
        'text-align:center;overflow:hidden;box-sizing:border-box;' +
        `background:${failed ? 'rgba(255,247,214,.95)' : 'rgba(255,255,255,.93)'};` +
        'color:#111;border-radius:4px;' +
        'line-height:1.25;padding:1px 2px;pointer-events:auto;cursor:pointer;' +
        'font-family:system-ui,"Microsoft YaHei","Malgun Gothic","Yu Gothic",sans-serif;word-break:break-all;';
      // 点击暂时隐藏，便于看原图
      el.addEventListener('click', () => {
        el.style.visibility = 'hidden';
        setTimeout(() => (el.style.visibility = ''), 2000);
      });
      ov.appendChild(el);
    }
    // 覆盖层是 body 下的绝对定位元素，页面布局一变（漫画站边滚边加载图片，
    // 上方插入一张下面就整体下移）就会错位——而图片只是"移动"没"改变尺寸"，
    // resize 类事件都不会触发，所以由 follow() 逐帧跟随。
    // 注意别把这个变量叫 t——上面 el.title 那行要调用 i18n 的 t()，
    // 同名 const 会让整个函数落进暂时性死区，翻译失败的图直接抛错
    const entry = { ov, list: buildList(img, data), img, data, key: '', src: srcOf(img) };
    tracked.push(entry);
    mount(entry);
    applyMode();
    startSync();
  }

  /**
   * 把覆盖层放到该去的地方。
   *
   * attach 模式塞进图片的父容器：容器一动，覆盖层作为它的子元素跟着动，
   * 由浏览器保证同步，不存在校正延迟。父容器若是 static 定位，绝对定位的
   * 子元素会以更外层为基准导致错位，所以要先把它改成 relative——这是唯一
   * 需要改动站点样式的地方，也是这个模式风险所在。
   */
  /** 覆盖层统一挂在 body 上，用文档坐标定位；位置由逐帧的 follow() 跟随 */
  function mount(t) {
    if (!t.ov.isConnected) document.body.appendChild(t.ov);
    t.key = '';
    position(t);
  }


  /**
   * 列表模式用的译文块，作为普通元素插在图片后面，随页面排版自然流动。
   * 插入别人的 DOM 有风险（站点脚本可能对子元素结构有预期），所以包一层
   * try/catch，失败就返回 null——大不了这张图只有覆盖模式可用。
   */
  function buildList(img, data) {
    try {
      const box = document.createElement('div');
      box.className = 'ocrt-list';
      box.style.cssText =
        'display:none;margin:6px auto;padding:10px 12px;max-width:900px;' +
        'background:#f7f5ef;color:#111;border-left:3px solid #2d7d46;' +
        'border-radius:4px;font:15px/1.7 system-ui,"Microsoft YaHei","Malgun Gothic","Yu Gothic",sans-serif;' +
        'text-align:left;white-space:normal;';
      for (const b of data.blocks) {
        const line = document.createElement('div');
        line.textContent = b.translation || b.text;
        line.dataset.dlg = b.dialogue === false ? '0' : '1';
        line.title = b.text;              // 悬停看原文，和覆盖模式一致
        line.style.cssText = 'margin:2px 0;';
        box.appendChild(line);
      }
      img.insertAdjacentElement('afterend', box);
      return box;
    } catch (e) {
      return null;
    }
  }

  const tracked = [];
  let syncTimer = null;
  let rafOn = false;

  /** 锚点当前指向的图片地址，用来发现"元素还在但图换了" */
  const srcOf = (el) => (el.tagName === 'IMG' ? imgSrc(el) : '');

  function startSync() {
    // 结构性检查（图片没了/被换掉/翻页隐藏）比较重，低频做就够
    if (!syncTimer) syncTimer = setInterval(sweep, 400);
    // 位置跟随必须逐帧：定时器再密也会有肉眼可见的拖影。
    // 实测这一圈很便宜——85 张图全量读取位置每帧仅 0.06ms，
    // 不到一帧预算（16.7ms）的 0.4%。
    if (!rafOn) {
      rafOn = true;
      requestAnimationFrame(follow);
    }
  }

  /** 逐帧跟随：位置变了就重新定位，让译文和图片同步移动 */
  function follow() {
    for (const t of tracked) {
      if (t.offscreen) continue;
      const r = t.img.getBoundingClientRect();
      if (!r.width || !r.height) continue;
      const key = `${Math.round(r.left + scrollX)},${Math.round(r.top + scrollY)},${Math.round(r.width)},${Math.round(r.height)}`;
      if (key !== t.key) {
        t.key = key;
        position(t);
      }
    }
    if (tracked.length) requestAnimationFrame(follow);
    else rafOn = false;
  }

  /** 低频巡检：图片没了、被换成另一张、或因翻页被隐藏 */
  function sweep() {
    for (let i = tracked.length - 1; i >= 0; i--) {
      const t = tracked[i];
      // 图片从 DOM 移除，或被换成了另一张（有的阅读器复用同一个 <img> 只改 src）
      if (!t.img.isConnected || srcOf(t.img) !== t.src) {
        t.ov.remove();
        if (t.list) t.list.remove();
        tracked.splice(i, 1);
        if (t.img.isConnected) {
          // 换图了：清掉标记让它重新排队，新页才会被翻译
          delete t.img.dataset.ocrt;
          delete t.img.dataset.ocrtWatch;
          if (st.on) enqueue(t.img);
        }
        continue;
      }
      // 左右翻页的阅读器把非当前页缩成 0x0（MangaDex 就是如此）。
      // 这时必须把译文一并藏起来——否则覆盖层会停在最后的位置盖住新页，
      // 列表块也会为看不见的那页占着版面。
      const r = t.img.getBoundingClientRect();
      const off = !r.width || !r.height;
      if (off !== t.offscreen) {
        t.offscreen = off;
        applyMode();
      }
    }
    if (!tracked.length && syncTimer) {
      clearInterval(syncTimer);
      syncTimer = null;
    }
  }

  function position(t) {
    const { ov, img, data } = t;
    const r = img.getBoundingClientRect();
    if (!r.width || !r.height) return;
    ov.style.left = r.left + window.scrollX + 'px';
    ov.style.top = r.top + window.scrollY + 'px';
    const w = r.width;
    const h = r.height;
    ov.style.width = w + 'px';
    ov.style.height = h + 'px';
    const sx = w / data.width;
    const sy = h / data.height;
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
            return reject(new Error(t('errHttp', r.status)));
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

  function getJSON(url) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method: 'GET',
        url,
        timeout: 10000,
        onload: (r) =>
          r.status === 200
            ? resolve(JSON.parse(r.responseText))
            : reject(new Error('HTTP ' + r.status)),
        onerror: () => reject(new Error(t('errServer'))),
        ontimeout: () => reject(new Error(t('errTimeout'))),
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
        onerror: () => reject(new Error(t('errServer'))),
        ontimeout: () => reject(new Error(t('errTimeout'))),
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
