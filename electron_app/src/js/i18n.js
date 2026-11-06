const I18N = {
  current: localStorage.getItem('lang') || 'zh',
  _data: {
    // P2-6b 语言包拆分：数据体在 i18n-data-{zh,en,ja}.js（index.html 先于本文件加载）
    zh: (window.I18N_DATA && window.I18N_DATA.zh) || {},
    en: (window.I18N_DATA && window.I18N_DATA.en) || {},
    ja: (window.I18N_DATA && window.I18N_DATA.ja) || {},
  },
  t(key, vars) {
    let s = this._data[this.current]?.[key] || this._data.zh[key] || key;
    if (vars) for (const k of Object.keys(vars)) s = s.split('{' + k + '}').join(String(vars[k]));
    return s;
  },
  setLang(lang) { this.current = lang; localStorage.setItem('lang', lang); this.applyDOM(); },

  /** Build reverse lookup: zh text → key (lazy, cached) */
  _getReverseMap() {
    if (this._reverseMap) return this._reverseMap;
    this._reverseMap = {};
    const zh = this._data.zh;
    for (const key of Object.keys(zh)) {
      const val = zh[key];
      if (val && val.length >= 2 && !this._reverseMap[val]) {
        this._reverseMap[val] = key;
      }
    }
    return this._reverseMap;
  },

  applyDOM() {
    // ── Phase 1: explicit data-i18n attributes (fast path) ──
    document.querySelectorAll('[data-i18n]').forEach(el => {
      el.textContent = this.t(el.getAttribute('data-i18n'));
    });
    document.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
      el.placeholder = this.t(el.getAttribute('data-i18n-placeholder'));
    });
    document.querySelectorAll('[data-i18n-title]').forEach(el => {
      el.title = this.t(el.getAttribute('data-i18n-title'));
    });

    // ── Phase 2: auto-detect untagged text nodes (run once, tags them for future calls) ──
    if (this.current === 'zh') return; // nothing to translate for Chinese (default)
    const reverseMap = this._getReverseMap();

    // Use TreeWalker to iterate text nodes efficiently
    const walker = document.createTreeWalker(
      document.body,
      NodeFilter.SHOW_TEXT,
      { acceptNode: (node) => {
        // Skip script/style content
        const parent = node.parentElement;
        if (!parent) return NodeFilter.FILTER_REJECT;
        const tag = parent.tagName;
        if (tag === 'SCRIPT' || tag === 'STYLE') return NodeFilter.FILTER_REJECT;
        // Skip if already inside a tagged element
        if (parent.closest('[data-i18n]')) return NodeFilter.FILTER_REJECT;
        const text = node.textContent.trim();
        if (!text || text.length < 2) return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      }}
    );

    const nodesToProcess = [];
    while (walker.nextNode()) {
      nodesToProcess.push(walker.currentNode);
    }

    for (const node of nodesToProcess) {
      const text = node.textContent.trim();
      const key = reverseMap[text];
      if (!key) continue;

      const parent = node.parentElement;
      if (!parent) continue;

      // Check if parent has ONLY this text (no other element children)
      const childElements = Array.from(parent.children);
      if (childElements.length === 0) {
        // Simple case: parent only contains text → tag the parent
        parent.setAttribute('data-i18n', key);
        parent.textContent = this.t(key);
      } else {
        // Mixed content: wrap this text node in a span
        const span = document.createElement('span');
        span.setAttribute('data-i18n', key);
        span.textContent = this.t(key);
        node.replaceWith(span);
      }
    }
  },
};

window.t = (key, vars) => I18N.t(key, vars);
