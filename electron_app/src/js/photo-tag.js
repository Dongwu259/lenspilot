/**
 * photo-tag.js — AI 自动标签（选片页「AI 打标」按钮入口）
 *
 * 后端 photo_tagger.auto_tag_photos：多模态 LLM 4 张/批生成中文标签，
 * 持久化在 score_cache.photo_tags（mtime/size 失效）。本模块负责：
 *  - 对当前结果中无 tags 的照片发起打标（确认弹窗含数量与预估耗时），
 *    复用评分进度条 + 独立「取消打标」按钮；
 *  - 网格渲染后异步 get_photo_tags 补显卡片 tag chips（前 3 个 + "+N"），
 *    不阻塞 renderGrid；
 *  - 维护标签过滤下拉（全部/各出现过的标签，按频次降序），与
 *    「只看未标记」复选框在 renderGrid 中联合过滤。
 * tags 存于 PhotoSelect.tagMap（path → [tags]），随清缓存一并清空。
 */

const PhotoTagger = {
  running: false,
  total: 0,
  done: 0,
  _fetching: new Set(),   // get_photo_tags 在途路径（防重复请求）
  _runId: 0,              // 迟到响应失效

  // ── 事件绑定 ──

  bindEvents() {
    document.getElementById('btn-ai-tag').addEventListener('click', () => this.run());
    document.getElementById('btn-cancel-tag').addEventListener('click', () => this.cancel());
    const sel = document.getElementById('ps-tag-filter');
    if (sel) sel.addEventListener('change', () => {
      if (typeof PhotoSelect !== 'undefined') PhotoSelect.renderGrid();
    });
  },

  // 当前结果中还没有 tags 的照片
  _untaggedPaths() {
    const rs = (typeof PhotoSelect !== 'undefined' && PhotoSelect.results) || [];
    const tm = (typeof PhotoSelect !== 'undefined' && PhotoSelect.tagMap) || {};
    return rs.filter(r => !(tm[r.path] && tm[r.path].length)).map(r => r.path);
  },

  // ── 发起打标 ──

  async run() {
    if (this.running) { App.toast(t('tagRunning'), 'info'); return; }
    if (typeof PhotoSelect === 'undefined' || !PhotoSelect.results || !PhotoSelect.results.length) {
      App.toast(t('psNeedFolder'), 'warning'); return;
    }
    if (PhotoSelect.processing) { App.toast(t('psScoringWait'), 'warning'); return; }
    const paths = this._untaggedPaths();
    if (!paths.length) { App.toast(t('tagAllTagged'), 'info'); return; }
    // 估时：~6s/批（4 张），供确认弹窗提示（实际取决于模型与网络）
    const estMin = Math.max(1, Math.ceil(paths.length / 4 * 6 / 60));
    const ok = await Modal.showConfirm(
      t('tagConfirmTitle'),
      t('tagConfirmMsg', { count: paths.length, minutes: estMin }),
      t('tagConfirmBtn'), t('cancel'));
    if (!ok) return;

    const runId = ++this._runId;
    this._setRunning(true, paths.length);
    try {
      const r = await window.api.sendCommand('auto_tag_photos', { paths, batch_size: 4 });
      if (runId !== this._runId) return;
      // 预检错误走响应体（此时无 tag_done 事件，需要这里收尾）
      if (r && r.error) {
        this._setRunning(false);
        if (r.error === 'multimodal_disabled') this._showMultimodalDisabled(r.detail);
        else App.toast(t('tagFail', { msg: r.detail || r.error }), 'error');
      }
      // 成功路径由 tag_done 事件收尾（事件先于响应到达，二者幂等）
    } catch (e) {
      if (runId !== this._runId) return;
      this._setRunning(false);
      App.toast(t('tagFail', { msg: e.message }), 'error');
    }
  },

  async cancel() {
    if (!this.running) return;
    try { await window.api.sendCommand('cancel_auto_tag', {}); } catch (e) { /* 后端可能已结束 */ }
  },

  _setRunning(on, total = 0) {
    this.running = on;
    this.total = on ? total : 0;
    this.done = 0;
    document.getElementById('btn-ai-tag').disabled = on ||
      !(typeof PhotoSelect !== 'undefined' && PhotoSelect.sourceFolder);
    document.getElementById('btn-cancel-tag').classList.toggle('hidden', !on);
    if (on) {
      UI.updateProgress('score-progress-fill', 'score-progress-text', 0, t('tagProgress', { current: 0, total }));
    }
  },

  async _showMultimodalDisabled(detail) {
    const ok = await Modal.showConfirm(
      t('tagMmDisabledTitle'), detail || t('tagMmDisabledMsg'), t('tagGoSettings'), t('cancel'));
    if (ok) App.setPage('agent');
  },

  // ── 后端事件 ──

  handleEvent(eventName, data) {
    if (eventName === 'tag_progress') {
      if (!this.running) return;  // 非本页发起的打标（理论上没有第二入口，防御）
      this.done = data.current || 0;
      const pct = this.total > 0 ? (this.done / this.total * 100) : 0;
      UI.updateProgress('score-progress-fill', 'score-progress-text', pct,
        t('tagProgress', { current: this.done, total: this.total }));
      return;
    }
    if (eventName === 'tag_done') {
      this._runId++;            // 使 run() 中 await 的迟到响应失效
      this._setRunning(false);
      const res = (data && data.results) || {};
      this._applyTags(res);
      const tagged = (data && data.tagged) || 0;
      const failed = (data && data.failed) || 0;
      if (data && data.cancelled) {
        const msg = t('tagCancelled', { done: tagged, total: (data.total || 0) });
        UI.updateProgress('score-progress-fill', 'score-progress-text', 0, msg);
        App.toast(msg, 'warning');
      } else {
        UI.updateProgress('score-progress-fill', 'score-progress-text', 100,
          t('tagDoneText', { tagged, failed }));
        App.toast(t('tagDoneToast', { tagged, failed }), failed ? 'warning' : 'success');
        if (typeof PhotoSelect !== 'undefined') {
          PhotoSelect.addLog(t('tagDoneLog', { tagged, failed, skipped: (data.skipped || 0) }), failed ? 'warn' : 'success');
          if (failed && data.last_error) {
            PhotoSelect.addLog(t('tagFailReason', { reason: data.last_error }), 'warn');
          }
        }
      }
    }
  },

  // ── tags 补显（renderGrid 后异步拉取，不阻塞渲染） ──

  afterRender(paths) {
    if (typeof PhotoSelect === 'undefined') return;
    const tm = PhotoSelect.tagMap || {};
    const missing = [];
    for (const p of paths) {
      if (!(p in tm) && !this._fetching.has(p)) missing.push(p);
    }
    if (missing.length) this._fetchTags(missing);
    else this._updateVisibleCards();
  },

  async _fetchTags(paths) {
    paths.forEach(p => this._fetching.add(p));
    try {
      const r = await window.api.sendCommand('get_photo_tags', { paths });
      const tags = (r && r.tags) || {};
      // 未命中也写空数组，避免每次 renderGrid 重复请求
      const tm = PhotoSelect.tagMap;
      paths.forEach(p => { if (!(p in tm)) tm[p] = tags[p] || []; });
      this._applyTags({});
    } catch (e) { /* 下轮 renderGrid 会重试 */ } finally {
      paths.forEach(p => this._fetching.delete(p));
    }
  },

  // 合并新 tags → 更新可见卡片 chips + 过滤下拉
  _applyTags(map) {
    if (typeof PhotoSelect === 'undefined') return;
    const tm = PhotoSelect.tagMap || (PhotoSelect.tagMap = {});
    let changed = false;
    for (const p of Object.keys(map)) {
      const tags = map[p];
      if (Array.isArray(tags) && tags.length) { tm[p] = tags; changed = true; }
    }
    this._updateVisibleCards();
    this._rebuildFilterOptions();
    // 若当前选了标签过滤，tags 变化可能影响可见集合
    const sel = document.getElementById('ps-tag-filter');
    if (sel && sel.value) PhotoSelect.renderGrid();
  },

  // ── 卡片 chips ──

  _chipsHtml(tags) {
    if (!tags || !tags.length) return '';
    const esc = (s) => UI._escapeHtml(String(s));
    const chips = tags.slice(0, 3).map(x => `<span class="ps-tag-chip">${esc(x)}</span>`).join('');
    const more = tags.length > 3 ? `<span class="ps-tag-chip ps-tag-more">+${tags.length - 3}</span>` : '';
    return `<div class="ps-card-tags" title="${esc(tags.join(' · '))}">${chips}${more}</div>`;
  },

  _updateVisibleCards() {
    if (typeof PhotoSelect === 'undefined') return;
    const tm = PhotoSelect.tagMap || {};
    document.querySelectorAll('#ps-grid .ps-card').forEach(card => {
      const tags = tm[card.dataset.path];
      const old = card.querySelector('.ps-card-tags');
      const html = this._chipsHtml(tags);
      if (!html) { if (old) old.remove(); return; }
      if (old) {
        const tmp = document.createElement('div');
        tmp.innerHTML = html;
        card.replaceChild(tmp.firstChild, old);
      } else {
        const tmp = document.createElement('div');
        tmp.innerHTML = html;
        // chips 插在 info 行之前（img 之后），绝对定位不依赖位置
        card.insertBefore(tmp.firstChild, card.querySelector('.ps-card-info'));
      }
    });
  },

  // ── 过滤下拉 ──

  _rebuildFilterOptions() {
    const sel = document.getElementById('ps-tag-filter');
    if (!sel || typeof PhotoSelect === 'undefined') return;
    const tm = PhotoSelect.tagMap || {};
    // 只统计当前结果中出现的标签，按频次降序
    const freq = {};
    (PhotoSelect.results || []).forEach(r => (tm[r.path] || []).forEach(x => { freq[x] = (freq[x] || 0) + 1; }));
    const tags = Object.keys(freq).sort((a, b) => freq[b] - freq[a] || a.localeCompare(b));
    const prev = sel.value;
    sel.innerHTML = `<option value="">${UI._escapeHtml(t('tagFilterAll'))}</option>` +
      tags.map(x => `<option value="${UI._escapeHtml(x)}">${UI._escapeHtml(x)} (${freq[x]})</option>`).join('');
    sel.value = tags.includes(prev) ? prev : '';
    sel.classList.toggle('hidden', tags.length === 0);
    if (prev && sel.value !== prev && typeof PhotoSelect !== 'undefined') PhotoSelect.renderGrid();
  },

  // 清缓存后重置（PhotoSelect.clearScoreCache 调用）
  resetTags() {
    if (typeof PhotoSelect !== 'undefined') PhotoSelect.tagMap = {};
    const sel = document.getElementById('ps-tag-filter');
    if (sel) { sel.innerHTML = ''; sel.value = ''; sel.classList.add('hidden'); }
  },
};
