/**
 * photo-edit-page.js — 照片编辑页(0.8.9)
 *
 * 独立的照片编辑入口页(导航:选片组 → 文件浏览之后)。
 * - 工作流接力:从 AI 选片 / 文件浏览 / 拷卡目录拉入照片
 * - 手动选取照片 / 文件夹
 * - 网格缩略图,点击进入 PhotoEdit 编辑器(带前后翻页)
 * - 勾选 2-3 张进入并排对比,对比中点击某张进入编辑
 * - 批量导出委托给 BatchExport（格式/尺寸/色彩空间/文字或图片水印/目标文件夹）
 */

const PhotoEditPage = {
  photos: [],            // [{path, name, ext, thumb}]
  _view: [],             // 当前筛选后的可见照片
  _compareSel: new Set(),
  _sourceLabel: '',
  _bound: false,
  _filter: 'all',
  _presets: [],          // list_editor_presets（网格内直接套用）
  _presetLoading: false,
  // Compare/Survey overlay: synced zoom/pan + active panel
  _pcZoom: 1, _pcPanX: 0, _pcPanY: 0, _pcActive: 0, _pcDrag: null,
  _pcMode: 'compare', _pcPaths: [], _pcAutoAdvance: false,

  _IMAGE_FILTER: [{
    name: 'Photos',
    extensions: [
      'jpg', 'jpeg', 'png', 'bmp', 'tif', 'tiff', 'webp', 'heic', 'heif',
      'arw', 'srf', 'sr2', 'arq', 'cr2', 'cr3', 'crw', 'nef', 'nrw',
      'raf', 'rw2', 'orf', 'ori', 'pef', 'ptx', 'raw', 'rwl', 'rwz',
      'dng', 'srw', 'x3f', 'gpr', 'iiq', 'cap', 'eip', '3fr', 'fff',
      'mef', 'mos', 'mrw', 'mdc', 'erf', 'dcr', 'kdc', 'k25', 'dcs',
      'dc2', 'bay', 'cs1', 'sti',
    ],
  }],

  onPageEnter() { this._bind(); this._syncFromWorkbench(); this._loadPresets(); },

  _bind() {
    if (this._bound) return;
    this._bound = true;
    if (typeof Workbench !== 'undefined') {
      this._compareSel = Workbench.selection;
      Workbench.onChange(() => this._syncFromWorkbench());
    }
    const $ = id => document.getElementById(id);
    $('btn-pep-relay-select').addEventListener('click', () => this.relayFromSelect());
    $('btn-pep-relay-browser').addEventListener('click', () => this.relayFromBrowser());
    $('btn-pep-relay-copy').addEventListener('click', () => this.relayFromCopy());
    $('btn-pep-pick-files').addEventListener('click', () => this.pickFiles());
    $('btn-pep-pick-folder').addEventListener('click', () => this.pickFolder());
    $('btn-pep-compare').addEventListener('click', () => this.openCompare());
    $('btn-pep-survey').addEventListener('click', () => this.openSurvey());
    $('pep-filter').addEventListener('change', (e) => {
      this._filter = e.target.value || 'all';
      this.render();
    });
    $('btn-pep-hdr').addEventListener('click', () => this._merge('hdr'));
    $('btn-pep-pano').addEventListener('click', () => this._merge('pano'));
    $('btn-pep-export').addEventListener('click', () => this.openExport());
    $('pep-preset-select').addEventListener('change', () => this._updatePresetUI());
    $('btn-pep-preset').addEventListener('click', () => this.applyPresetToSelection());
    const pepAd = $('pep-preset-adaptive');
    if (pepAd) {
      pepAd.checked = localStorage.getItem('pe_preset_adaptive_v1') !== '0';
      pepAd.addEventListener('change', () => {
        localStorage.setItem('pe_preset_adaptive_v1', pepAd.checked ? '1' : '0');
      });
    }
    $('btn-pep-sel-all').addEventListener('click', () => this.selectAll());
    $('btn-pep-sel-clear').addEventListener('click', () => this.selectClear());
    $('btn-pep-wb-clear').addEventListener('click', () => this.clearWorkbench());
    $('btn-pep-paste').addEventListener('click', () => this.pasteEdits());
    $('pc-close').addEventListener('click', () => this.closeCompare());
    document.querySelector('#photo-compare-overlay .pc-backdrop')
      .addEventListener('click', () => this.closeCompare());
    $('pc-reset-zoom').addEventListener('click', () => this._pcResetZoom());
    $('pc-make-select').addEventListener('click', () => this._pcMakeSelect());
    $('pc-auto-advance').addEventListener('click', () => this._pcToggleAutoAdvance());
    const pcPanels = $('pc-panels');
    pcPanels.addEventListener('wheel', (e) => {
      if (document.getElementById('photo-compare-overlay').classList.contains('hidden')) return;
      e.preventDefault();
      const factor = e.deltaY < 0 ? 1.12 : (1 / 1.12);
      this._pcZoom = Math.max(1, Math.min(4, this._pcZoom * factor));
      if (this._pcZoom <= 1.001) { this._pcZoom = 1; this._pcPanX = 0; this._pcPanY = 0; }
      this._pcApply();
    }, { passive: false });
    pcPanels.addEventListener('pointerdown', (e) => {
      if (this._pcZoom <= 1) return;
      if (e.target.closest('.pc-panel')) {
        this._pcDrag = { x: e.clientX, y: e.clientY, px: this._pcPanX, py: this._pcPanY };
        pcPanels.setPointerCapture?.(e.pointerId);
      }
    });
    pcPanels.addEventListener('pointermove', (e) => {
      if (!this._pcDrag) return;
      this._pcPanX = this._pcDrag.px + (e.clientX - this._pcDrag.x);
      this._pcPanY = this._pcDrag.py + (e.clientY - this._pcDrag.y);
      this._pcApply();
    });
    const endPan = () => { this._pcDrag = null; };
    pcPanels.addEventListener('pointerup', endPan);
    pcPanels.addEventListener('pointercancel', endPan);
    // 网格事件委托:点卡片进编辑,勾选框进选择
    $('pep-grid').addEventListener('click', (e) => {
      const card = e.target.closest('.pep-card');
      if (!card) return;
      const f = this._view[+card.dataset.i];
      if (!f) return;
      if (e.target.closest('.pep-check')) return; // checkbox 走 change
      const star = e.target.closest('[data-rate]');
      if (star) {
        if (typeof Workbench !== 'undefined') {
          const n = parseInt(star.dataset.rate, 10) || 0;
          Workbench.setRating(f.path, Workbench.getRating(f.path) === n ? 0 : n);
        }
        return;
      }
      if (e.target.closest('[data-cand]')) {
        if (typeof Workbench !== 'undefined') Workbench.toggleCandidate(f.path);
        return;
      }
      this._openEditor(f.path);
    });
    $('pep-grid').addEventListener('change', (e) => {
      const cb = e.target.closest('input[data-act="cmp"]');
      if (!cb) return;
      const card = cb.closest('.pep-card');
      const f = card && this._view[+card.dataset.i];
      if (f) this._toggleSelect(f.path, cb.checked, card);
    });
    document.addEventListener('keydown', (e) => {
      const overlay = document.getElementById('photo-compare-overlay');
      const compareOpen = overlay && !overlay.classList.contains('hidden');
      const editorOpen = !document.getElementById('photo-edit-overlay').classList.contains('hidden');
      const paletteOpen = document.getElementById('command-palette') &&
        !document.getElementById('command-palette').classList.contains('hidden');
      const tag = (e.target && e.target.tagName) || '';

      if (!compareOpen) {
        if (editorOpen || paletteOpen) return;
        if (typeof App === 'undefined' || App.currentPage !== 'photo-edit') return;
        if (['INPUT', 'TEXTAREA', 'SELECT'].includes(tag)) return;
        if (typeof Keymap === 'undefined') return;
        if (Keymap.is(e, 'pepSelectAll')) {
          e.preventDefault(); this.selectAll();
        } else if (Keymap.is(e, 'pepOpenEditor') && tag !== 'BUTTON') {
          const path = (typeof Workbench !== 'undefined' &&
            (Workbench.getActive() || Workbench.getSelected()[0])) ||
            (this._view[0] && this._view[0].path);
          if (path) { e.preventDefault(); this._openEditor(path); }
        } else if (Keymap.is(e, 'pepCompare')) {
          e.preventDefault(); this.openCompare();
        } else if (Keymap.is(e, 'pepSurvey')) {
          e.preventDefault(); this.openSurvey();
        }
        return;
      }

      if (paletteOpen) return;
      if (['INPUT', 'TEXTAREA', 'SELECT'].includes(tag)) return;
      if (typeof Keymap === 'undefined') {
        if (e.key === 'Escape') { e.preventDefault(); this.closeCompare(); }
        return;
      }
      if (Keymap.is(e, 'pcClose')) { e.preventDefault(); this.closeCompare(); }
      else if (Keymap.is(e, 'pcMakeSelect')) { e.preventDefault(); this._pcMakeSelect(); }
      else if (Keymap.is(e, 'pcAutoAdvance')) { e.preventDefault(); this._pcToggleAutoAdvance(); }
      else if (Keymap.is(e, 'pcPrev')) { e.preventDefault(); this._pcMove(-1); }
      else if (Keymap.is(e, 'pcNext')) { e.preventDefault(); this._pcMove(1); }
      else if (Keymap.is(e, 'pcOpenEditor') && tag !== 'BUTTON') {
        const path = this._pcActivePath();
        if (path) { e.preventDefault(); this.closeCompare(); this._openEditor(path); }
      }
    });
  },

  // ── 数据源:工作流接力 + 手动选取 ──

  relayFromSelect() {
    const ps = (typeof PhotoSelect !== 'undefined') ? PhotoSelect : null;
    const keepers = ps && Array.isArray(ps.results) ? ps.results.filter(r => r.selected) : [];
    let chosen = [];
    if (typeof Workbench !== 'undefined') {
      chosen = Workbench.getSelectedPhotos();
      if (!chosen.length) chosen = Workbench.fromResults(keepers);
    } else {
      chosen = keepers.map(r => ({ path: r.path }));
    }
    if (!chosen.length) { App.toast(t('pepRelaySelectEmpty'), 'warning'); return; }
    if (typeof Workbench !== 'undefined') {
      Workbench.setPhotos(chosen, { source: 'ai-select' });
      if (!Workbench.getSelected().length) Workbench.setSelection(chosen.map(p => p.path));
      this._syncFromWorkbench();
    } else {
      this.loadPaths(chosen.map(r => r.path), t('pepFromSelect'));
    }
  },

  relayFromBrowser() {
    const fb = (typeof FileBrowser !== 'undefined') ? FileBrowser : null;
    const paths = fb && Array.isArray(fb.files)
      ? fb.files.filter(f => f.isLocal).map(f => f.path) : [];
    if (!paths.length) { App.toast(t('pepRelayBrowserEmpty'), 'warning'); return; }
    if (typeof Workbench !== 'undefined') {
      Workbench.addPhotos(paths, { source: 'file-browser' });
      this._syncFromWorkbench();
    } else {
      this.loadPaths(paths, t('pepFromBrowser'));
    }
  },

  async relayFromCopy() {
    const folder = App.config && App.config.copy_target_path;
    if (!folder) { App.toast(t('pepRelayCopyEmpty'), 'warning'); return; }
    await this.loadFolder(folder, t('pepFromCopy'));
  },

  async pickFiles() {
    try {
      const files = await window.api.selectFiles(this._IMAGE_FILTER);
      if (files && files.length) this.loadPaths(files, t('pepFromPick'));
    } catch (_) { /* canceled */ }
  },

  async pickFolder() {
    try {
      const f = await window.api.selectFolder();
      if (f) await this.loadFolder(f, f);
    } catch (_) { /* canceled */ }
  },

  async loadFolder(folder, sourceLabel) {
    try {
      const r = await window.api.sendCommand('list_images_in_folder', { folder });
      const paths = (r.images || []).map(p => (typeof p === 'string' ? p : p.path));
      if (!paths.length) { App.toast(t('pepFolderEmpty'), 'warning'); return; }
      this.loadPaths(paths, sourceLabel || folder);
    } catch (e) { App.toast(t('fbLoadFail'), 'error'); }
  },

  loadPaths(paths, sourceLabel) {
    const wasEmpty = this.photos.length === 0;
    const existing = new Set(this.photos.map(f => this._normPath(f.path)));
    let added = 0, dup = 0;
    paths.forEach(p => {
      const norm = this._normPath(p);
      if (existing.has(norm)) { dup++; return; }
      const name = p.split('\\').pop().split('/').pop();
      this.photos.push({ path: p, name, ext: (name.split('.').pop() || '').toLowerCase(), thumb: '' });
      existing.add(norm);
      added++;
    });
    this._sourceLabel = sourceLabel || this._sourceLabel;
    if (typeof Workbench !== 'undefined') {
      this._wbSyncing = true;
      Workbench.addPhotos(this.photos, { source: 'photo-edit-page' });
      this._wbSyncing = false;
      this._compareSel = Workbench.selection;
      this.photos = Workbench.getPhotos().map(f => ({
        path: f.path,
        name: f.name || (f.path || '').split(/[\\/]/).pop(),
        ext: f.ext || ((f.path || '').split('.').pop() || '').toLowerCase(),
        thumb: f.thumb || '',
      }));
    }
    this.render();
    if (added) this._loadThumbs();
    if (wasEmpty) {
      App.toast(t('pepLoaded', { count: this.photos.length, source: this._sourceLabel }), 'success');
    } else if (added || dup) {
      let msg = t('pepAdded', { added, source: this._sourceLabel });
      if (dup) msg += t('pepAddedDup', { dup });
      App.toast(msg, added ? 'success' : 'info');
    }
  },

  // 路径归一化(分隔符 + 大小写)用于去重比较
  _normPath(p) {
    return String(p).replace(/\//g, '\\').toLowerCase();
  },

  // ── 工作台镜像：本页只是 Workbench 的一个视图，不再自建队列 ──

  _syncFromWorkbench() {
    if (typeof Workbench === 'undefined' || this._wbSyncing) return;
    this._compareSel = Workbench.selection;
    this._pcSyncSelected();
    if (!Workbench.photos.length) return;
    const wb = Workbench.getPhotos().map(f => ({
      path: f.path,
      name: f.name || (f.path || '').split(/[\\/]/).pop(),
      ext: f.ext || ((f.path || '').split('.').pop() || '').toLowerCase(),
      thumb: f.thumb || '',
    }));
    const same = wb.length === this.photos.length && wb.every((f, i) => this.photos[i] && this.photos[i].path === f.path);
    if (!same) {
      this.photos = wb;
      this.render();
      if (wb.some(f => !f.thumb)) this._loadThumbs();
    } else {
      this.render();
    }
  },

  // ── 网格渲染 + 缩略图 ──

  render() {
    const grid = document.getElementById('pep-grid');
    const wb = (typeof Workbench !== 'undefined');
    this._view = this.photos.filter(f => {
      if (this._filter === 'candidates') return wb && Workbench.isCandidate(f.path);
      if (this._filter === 'stars3') return wb && Workbench.getRating(f.path) >= 3;
      return true;
    });
    document.getElementById('pep-empty').classList.toggle('hidden', this.photos.length > 0);
    const filteredOut = this.photos.length > 0 && this._view.length === 0;
    document.getElementById('pep-filter-empty')?.classList.toggle('hidden', !filteredOut);
    document.getElementById('pep-count').textContent = this.photos.length
      ? (filteredOut
        ? t('pepFilterCount', { shown: 0, total: this.photos.length })
        : t('pepCount', { count: this._view.length }))
      : '';
    grid.innerHTML = this._view.map((f, i) => {
      const sel = this._compareSel.has(f.path);
      const rating = wb ? Workbench.getRating(f.path) : 0;
      const cand = wb && Workbench.isCandidate(f.path);
      const thumbSrc = f.thumb ? `local-image://x/?p=${encodeURIComponent(f.thumb)}` : '';
      const stars = [1, 2, 3, 4, 5].map(n =>
        `<button class="pep-star${n <= rating ? ' on' : ''}" data-rate="${n}" title="${t('pepRateN', { n })}">★</button>`
      ).join('');
      return `<div class="pep-card${sel ? ' pep-sel' : ''}${cand ? ' pep-cand-card' : ''}" data-i="${i}">
        <button class="pep-cand${cand ? ' on' : ''}" data-cand="1" title="${t('pepCandidateTip')}"><i class="ph ph-flag"></i></button>
        <div class="pep-thumb">${thumbSrc
          ? `<img src="${thumbSrc}" loading="lazy" onerror="this.style.display='none';this.parentElement.classList.add('pep-noimg')">`
          : ''}<i class="ph ph-image"></i></div>
        <div class="pep-name" title="${this._esc(f.path)}">${this._esc(f.name)}</div>
        <div class="pep-rate" title="${t('pepRateTip')}">${stars}</div>
        <label class="pep-check" title="${t('pepCompareHint')}">
          <input type="checkbox" data-act="cmp" ${sel ? 'checked' : ''}></label>
      </div>`;
    }).join('');
    this._updateSelUI();
  },

  async _loadThumbs() {
    const need = this.photos.filter(f => !f.thumb);
    const batchSize = 30;
    for (let i = 0; i < need.length; i += batchSize) {
      const batch = need.slice(i, i + batchSize);
      try {
        const r = await window.api.sendCommand('get_thumbnails', { paths: batch.map(f => f.path) });
        if (r && r.thumbs) {
          for (const [path, thumb] of Object.entries(r.thumbs)) {
            const f = this.photos.find(x => x.path === path);
            if (f) f.thumb = thumb;
          }
          this.render();
        }
      } catch (_) { App.toast(t('fbThumbLoadFail'), 'warning'); break; }
    }
  },

  _esc(s) { const d = document.createElement('div'); d.textContent = String(s ?? ''); return d.innerHTML; },

  // ── 勾选选择(不限数量;对比取 2-3,导出取全部选中) ──

  _toggleSelect(path, checked) {
    if (typeof Workbench !== 'undefined') {
      if (checked) Workbench.select(path, 'add');
      else Workbench.deselect(path);
    } else {
      if (checked) this._compareSel.add(path); else this._compareSel.delete(path);
    }
    this._compareSel = (typeof Workbench !== 'undefined') ? Workbench.selection : this._compareSel;
    const card = document.querySelector(`.pep-card[data-i="${this._view.findIndex(f => f.path === path)}"]`);
    if (card) card.classList.toggle('pep-sel', checked);
    this._updateSelUI();
  },

  selectAll() {
    if (!this.photos.length) return;
    if (typeof Workbench !== 'undefined') Workbench.selectAll();
    else this.photos.forEach(f => this._compareSel.add(f.path));
    this.render();
  },

  selectClear() {
    if (typeof Workbench !== 'undefined') Workbench.clearSelection();
    else this._compareSel.clear();
    this.render();
  },

  clearWorkbench() {
    if (typeof Workbench !== 'undefined') Workbench.clear();
    this.photos = [];
    this._sourceLabel = '';
    this._compareSel = (typeof Workbench !== 'undefined') ? Workbench.selection : new Set();
    this.render();
    App.toast(t('pepWbCleared'), 'info');
  },

  // ── 网格内直接套用预设（与编辑器共用 list_editor_presets / batch_apply_preset） ──

  async _loadPresets() {
    if (this._presetLoading) return;
    this._presetLoading = true;
    try {
      const r = await window.api.sendCommand('list_editor_presets', {});
      this._presets = (r && r.presets) || [];
    } catch (_) { this._presets = []; }
    this._presetLoading = false;
    this._renderPresetOptions();
  },

  _presetName(p) {
    return p.nameKey ? (t(p.nameKey) || p.name) : p.name;
  },

  _renderPresetOptions() {
    const sel = document.getElementById('pep-preset-select');
    if (!sel) return;
    const current = sel.value;
    sel.innerHTML = `<option value="">${this._esc(t('pepPresetPick'))}</option>`;
    const cats = [
      ['landscape', 'pePresetCatLandscape'], ['portrait', 'pePresetCatPortrait'],
      ['film', 'pePresetCatFilm'], ['bw', 'pePresetCatBW'],
      ['mood', 'pePresetCatMood'], ['lut', 'pePresetCatLut'],
    ];
    cats.forEach(([cat, key]) => {
      const items = this._presets.filter(p => p.builtin && (p.category || 'lut') === cat);
      if (!items.length) return;
      const og = document.createElement('optgroup');
      og.label = t(key) || cat;
      items.forEach(p => {
        const o = document.createElement('option');
        o.value = p.id;
        o.textContent = this._presetName(p);
        og.appendChild(o);
      });
      sel.appendChild(og);
    });
    const mine = this._presets.filter(p => !p.builtin);
    if (mine.length) {
      const og = document.createElement('optgroup');
      og.label = t('pePresetMine');
      mine.forEach(p => {
        const o = document.createElement('option');
        o.value = p.id;
        o.textContent = (p.reference_stats ? t('pePresetRefBadge') + ' · ' : '') +
          this._presetName(p);
        og.appendChild(o);
      });
      sel.appendChild(og);
    }
    if (current && this._presets.some(p => p.id === current)) sel.value = current;
    this._updatePresetUI();
  },

  _selectedPreset() {
    const sel = document.getElementById('pep-preset-select');
    if (!sel || !sel.value) return null;
    return this._presets.find(p => p.id === sel.value) || null;
  },

  _updatePresetUI() {
    const btn = document.getElementById('btn-pep-preset');
    if (!btn) return;
    const p = this._selectedPreset();
    const n = (typeof Workbench !== 'undefined') ? Workbench.getSelected().length : 0;
    btn.disabled = !(p && n > 0);
    const ad = document.getElementById('pep-preset-adaptive');
    if (ad) {
      const isRef = !!(p && p.reference_stats);
      ad.disabled = isRef;
      ad.checked = isRef ? true
        : (localStorage.getItem('pe_preset_adaptive_v1') !== '0');
    }
  },

  async applyPresetToSelection() {
    const p = this._selectedPreset();
    if (!p) { App.toast(t('pePresetXmpNoPreset'), 'info'); return; }
    const paths = (typeof Workbench !== 'undefined') ? Workbench.getSelected() : [];
    if (!paths.length) { App.toast(t('pepPresetNoSel'), 'warning'); return; }
    const isRef = !!p.reference_stats;
    const adEl = document.getElementById('pep-preset-adaptive');
    const adaptive = isRef ? true : !!(adEl && adEl.checked);
    const name = this._presetName(p);
    const confirmed = await Modal.showConfirm(
      t('pePresetBatchTitle'),
      isRef
        ? t('pePresetBatchMsgRef', { name, count: paths.length })
        : t('pePresetBatchMsg', {
          name, count: paths.length,
          adaptive: t(adaptive ? 'pePresetAdaptiveOn' : 'pePresetAdaptiveOff'),
        })
    );
    if (!confirmed) return;

    const previous = {};
    const taskId = 'gridpreset-' + Date.now().toString(36) +
      Math.random().toString(36).slice(2, 7);
    const act = (typeof Activity !== 'undefined')
      ? Activity.start({
        title: t('actPresetBatch'),
        detail: t('actPhotosN', { count: paths.length }),
        total: paths.length, cancelable: true,
        onCancel: () => {
          window.api.sendCommand('cancel_batch_task', { task_id: taskId }).catch(() => {});
        },
      })
      : '';
    try {
      await Promise.all(paths.map(async path => {
        try {
          const r = await window.api.sendCommand('get_edits', { path });
          previous[path] = (r && r.edits) ? r.edits : {};
        } catch (_) { previous[path] = {}; }
      }));
      const r = await window.api.sendCommand('batch_apply_preset', {
        paths,
        preset_edits: isRef ? {} : (p.edits || {}),
        reference_stats: isRef ? p.reference_stats : null,
        source_stats: isRef ? null : (p.source_stats || null),
        adaptive: isRef ? false : adaptive,
        adaptive_strength: 1.0,
        task_id: taskId,
      });
      if (r && r.error) throw new Error(r.error);
      const fail = r.failed || 0;
      if (r.cancelled) {
        App.toast(t('pePresetBatchCancelled', { ok: r.processed || 0 }), 'warning');
        if (act && typeof Activity !== 'undefined' && Activity.markCancelled) {
          Activity.markCancelled(act);
        }
      } else {
        const msg = t('pePresetBatchDone', { ok: r.processed || 0, fail });
        App.toast(msg, fail ? 'warning' : 'success');
        if (act && typeof Activity !== 'undefined') Activity.finish(act, { detail: msg });
      }
      if (typeof App !== 'undefined' && App.pushUndo) {
        App.pushUndo(t('pePresetBatchUndo', { count: paths.length }), async () => {
          for (const [path, edits] of Object.entries(previous)) {
            try {
              await window.api.sendCommand('save_edits', { path, edits });
            } catch (_) { /* best effort */ }
          }
        });
      }
      // 后端已刷新缩略图缓存；加时间戳破浏览器缓存（同 pasteEdits）
      const ts = Date.now();
      paths.forEach(path => {
        const i = this.photos.findIndex(f => f.path === path);
        const img = i >= 0 && document.querySelector(`.pep-card[data-i="${i}"] img`);
        if (img && this.photos[i].thumb) {
          img.src = `local-image://x/?p=${encodeURIComponent(this.photos[i].thumb)}&t=${ts}`;
        }
      });
    } catch (e) {
      App.toast(t('pePresetBatchFail') + (e.message || ''), 'error');
      if (act && typeof Activity !== 'undefined') Activity.fail(act, e.message || '');
    }
  },

  _updateSelUI() {
    const n = this._compareSel.size;
    document.getElementById('btn-pep-compare').disabled = n < 2 || n > 3;
    const surveyBtn = document.getElementById('btn-pep-survey');
    if (surveyBtn) surveyBtn.disabled = n < 2;
    const hdrBtn = document.getElementById('btn-pep-hdr');
    if (hdrBtn) hdrBtn.disabled = n < 2 || n > 7;
    const panoBtn = document.getElementById('btn-pep-pano');
    if (panoBtn) panoBtn.disabled = n < 2;
    document.getElementById('btn-pep-export').disabled = n < 1;
    document.getElementById('btn-pep-paste').disabled =
      n < 1 || !(typeof PhotoEdit !== 'undefined' && PhotoEdit._clipboard);
    document.getElementById('pep-compare-label').textContent = n ? t('pepCompareN', { count: n }) : t('pepCompare');
    const badge = document.getElementById('pep-sel-count');
    badge.classList.toggle('hidden', n === 0);
    if (n) badge.textContent = t('pepSelCount', { count: n });
    this._updatePresetUI();
  },

  // ── 粘贴编辑(剪贴板来自 PhotoEdit._copyEdits,不含裁剪) ──

  async pasteEdits() {
    const clip = (typeof PhotoEdit !== 'undefined' && PhotoEdit._clipboard) || null;
    if (!clip) { App.toast(t('pepPasteEmpty'), 'warning'); return; }
    const paths = [...this._compareSel];
    if (!paths.length) { App.toast(t('pepPasteNoSel'), 'warning'); return; }
    try {
      const r = await window.api.sendCommand('batch_apply_edits', { paths, edits: clip });
      if (r && r.error) { App.toast(t('peBatchFail') + r.error, 'error'); return; }
      App.toast(t('peBatchDone', { ok: r.processed || 0, fail: r.failed || 0 }),
        r.failed ? 'warning' : 'success');
      // 后端已刷新缩略图缓存,加时间戳破浏览器缓存
      const ts = Date.now();
      paths.forEach(p => {
        const i = this.photos.findIndex(f => f.path === p);
        const img = i >= 0 && document.querySelector(`.pep-card[data-i="${i}"] img`);
        if (img && this.photos[i].thumb) {
          img.src = `local-image://x/?p=${encodeURIComponent(this.photos[i].thumb)}&t=${ts}`;
        }
      });
    } catch (e) { App.toast(t('peBatchFail') + e.message, 'error'); }
  },

  // ── 并排对比 overlay(2-3 张) ──

  // ── HDR 合并 / 全景拼接（P6-2，对勾选照片）──
  async _merge(kind) {
    const paths = [...this._compareSel];
    if (paths.length < 2) { App.toast(t('pepCompareNeed2'), 'warning'); return; }
    const isHdr = kind === 'hdr';
    const confirmed = await Modal.showConfirm(
      t(isHdr ? 'pepHdr' : 'pepPano'),
      t(isHdr ? 'pepHdrConfirm' : 'pepPanoConfirm', { n: paths.length }));
    if (!confirmed) return;
    const act = (typeof Activity !== 'undefined')
      ? Activity.start({ title: t(isHdr ? 'pepHdr' : 'pepPano'), detail: t('actPhotosN', { count: paths.length }) })
      : '';
    try {
      const r = await window.api.sendCommand(
        isHdr ? 'merge_hdr' : 'merge_panorama',
        isHdr ? { paths, align: true } : { paths });
      if (!r || r.error) {
        App.toast(t(isHdr ? 'pepHdr' : 'pepPano') + ': ' + ((r && r.error) || 'fail'), 'error');
        if (act && typeof Activity !== 'undefined') Activity.fail(act, (r && r.error) || 'fail');
        return;
      }
      App.toast(t('pepMergeDone', { path: r.path }), 'success');
      if (act && typeof Activity !== 'undefined') Activity.finish(act, { detail: r.path });
    } catch (e) {
      App.toast(e.message, 'error');
      if (act && typeof Activity !== 'undefined') Activity.fail(act, e.message || '');
    }
  },

  openCompare() {
    const paths = [...this._compareSel];
    if (paths.length < 2) { App.toast(t('pepCompareNeed2'), 'warning'); return; }
    if (paths.length > 3) { App.toast(t('pepCompareLimit'), 'warning'); return; }
    this._openGallery(paths, 'compare');
  },

  openSurvey() {
    let paths = [...this._compareSel];
    if (paths.length < 2) paths = this.photos.map(p => p.path);
    if (paths.length < 2) { App.toast(t('pepCompareNeed2'), 'warning'); return; }
    this._openGallery(paths.slice(0, 60), 'survey');
  },

  _openGallery(paths, mode) {
    const panels = document.getElementById('pc-panels');
    const overlay = document.getElementById('photo-compare-overlay');
    panels.innerHTML = '';
    panels.dataset.count = paths.length;
    overlay.classList.toggle('pc-survey', mode === 'survey');
    overlay.classList.toggle('pc-survey-few', mode === 'survey' && paths.length <= 4);
    overlay.classList.toggle('pc-survey-mid', mode === 'survey' && paths.length > 4 && paths.length <= 9);
    this._pcMode = mode;
    this._pcPaths = paths.slice();
    this._pcAutoAdvance = false;
    this._pcSyncAutoAdvance();
    this._pcZoom = 1; this._pcPanX = 0; this._pcPanY = 0; this._pcActive = 0; this._pcDrag = null;
    const modeLabel = document.getElementById('pc-mode-label');
    if (modeLabel) {
      modeLabel.textContent = mode === 'survey'
        ? t('pcSurveyN', { count: paths.length })
        : t('pcCompareN', { count: paths.length });
    }
    paths.forEach((p, i) => {
      const f = this.photos.find(x => x.path === p);
      const div = document.createElement('div');
      div.className = 'pc-panel' + (i === 0 ? ' pc-active' : '');
      div.dataset.i = String(i);
      div.dataset.path = p;
      const wb = (typeof Workbench !== 'undefined');
      const rating = wb ? Workbench.getRating(p) : 0;
      const cand = wb && Workbench.isCandidate(p);
      const stars = [1, 2, 3, 4, 5].map(n =>
        `<button class="pc-star${n <= rating ? ' on' : ''}" data-rate="${n}" title="${t('pepRateN', { n })}">★</button>`
      ).join('');
      div.innerHTML = `<div class="pc-panel-head">
          <span class="pc-sel-badge" title="${t('pcSelectedMark')}">✓</span>
          <span class="pc-name" title="${this._esc(p)}">${this._esc(f ? f.name : p)}</span>
          <button class="pc-cand${cand ? ' on' : ''}" data-cand="1" title="${t('pepCandidateTip')}"><i class="ph ph-flag"></i></button>
        </div>
        <img alt="">
        <div class="pc-stars" title="${t('pepRateTip')}">${stars}</div>`;
      // Single click selects the panel; double-click enters the editor.
      div.addEventListener('click', (e) => {
        if (e.target.closest('[data-rate]') || e.target.closest('[data-cand]')) return;
        this._pcActive = i;
        this._pcSyncActive();
      });
      div.addEventListener('dblclick', (e) => {
        if (e.target.closest('[data-rate]') || e.target.closest('[data-cand]')) return;
        this.closeCompare(); this._openEditor(p);
      });
      div.querySelectorAll('[data-rate]').forEach(btn => {
        btn.addEventListener('click', (e) => {
          e.stopPropagation();
          if (typeof Workbench === 'undefined') return;
          const n = parseInt(btn.dataset.rate, 10) || 0;
          Workbench.setRating(p, Workbench.getRating(p) === n ? 0 : n);
          const now = Workbench.getRating(p);
          div.querySelectorAll('[data-rate]').forEach(b => {
            b.classList.toggle('on', (parseInt(b.dataset.rate, 10) || 0) <= now);
          });
        });
        btn.addEventListener('dblclick', (e) => e.stopPropagation());
      });
      const candBtn = div.querySelector('[data-cand]');
      candBtn?.addEventListener('click', (e) => {
        e.stopPropagation();
        if (typeof Workbench === 'undefined') return;
        const on = Workbench.toggleCandidate(p);
        candBtn.classList.toggle('on', on);
        div.classList.toggle('pc-candidate', on);
      });
      candBtn?.addEventListener('dblclick', (e) => e.stopPropagation());
      div.classList.toggle('pc-candidate', cand);
      panels.appendChild(div);
      this._fillCompareImg(div.querySelector('img'), p);
    });
    this._pcSyncSelected();
    this._pcApply();
    overlay.classList.remove('hidden');
  },

  _pcSyncSelected() {
    if (typeof Workbench === 'undefined') return;
    let selectedInView = 0;
    document.querySelectorAll('#pc-panels .pc-panel').forEach(el => {
      const on = Workbench.isSelected(el.dataset.path);
      if (on) selectedInView += 1;
      el.classList.toggle('pc-selected', on);
    });
    const count = document.getElementById('pc-sel-count');
    if (count) {
      count.classList.toggle('hidden', selectedInView === 0);
      if (selectedInView) count.textContent = t('pepSelCount', { count: selectedInView });
    }
  },

  _pcMakeSelect() {
    const path = this._pcActivePath();
    if (!path) return;
    if (typeof Workbench === 'undefined') return;
    // Review loop: Make Select accumulates keepers; it never clears the
    // existing selection, so Auto Advance can build a batch across a survey.
    Workbench.select(path, 'add');
    this._pcSyncSelected();
    // Selection badges + count are the feedback; a toast per keeper would
    // stack up fast during an Auto Advance review loop.
    if (this._pcAutoAdvance) this._pcAdvance();
  },

  _pcToggleAutoAdvance() {
    this._pcAutoAdvance = !this._pcAutoAdvance;
    this._pcSyncAutoAdvance();
  },

  _pcSyncAutoAdvance() {
    const btn = document.getElementById('pc-auto-advance');
    if (btn) btn.classList.toggle('pe-active', this._pcAutoAdvance);
  },

  _pcAdvance() { this._pcMove(1); },

  _pcMove(delta) {
    if (!this._pcPaths.length) return;
    const n = this._pcPaths.length;
    this._pcActive = ((this._pcActive + delta) % n + n) % n;
    this._pcSyncActive();
    const el = document.querySelector(`#pc-panels .pc-panel[data-i="${this._pcActive}"]`);
    if (el) el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  },

  _pcActivePath() {
    return this._pcPaths[this._pcActive] || this._pcPaths[0] || '';
  },

  _pcSyncActive() {
    document.querySelectorAll('#pc-panels .pc-panel').forEach((el, i) => {
      el.classList.toggle('pc-active', i === this._pcActive);
    });
  },

  _pcApply() {
    document.querySelectorAll('#pc-panels .pc-panel img').forEach(img => {
      img.style.transform = `translate(${this._pcPanX}px, ${this._pcPanY}px) scale(${this._pcZoom})`;
    });
  },

  _pcResetZoom() {
    this._pcZoom = 1; this._pcPanX = 0; this._pcPanY = 0;
    this._pcApply();
  },

  closeCompare() {
    const overlay = document.getElementById('photo-compare-overlay');
    overlay.classList.add('hidden');
    overlay.classList.remove('pc-survey', 'pc-survey-few', 'pc-survey-mid');
    this._pcMode = 'compare';
    this._pcPaths = [];
    this._pcAutoAdvance = false;
    this._pcSyncAutoAdvance();
    this._pcZoom = 1; this._pcPanX = 0; this._pcPanY = 0; this._pcDrag = null;
    document.querySelectorAll('#pc-panels img').forEach(img => { img.src = ''; img.style.transform = ''; });
  },

  async _fillCompareImg(img, path) {
    const ext = (path.split('.').pop() || '').toLowerCase();
    if (PhotoEdit._RAW_LIKE_EXTS.has(ext)) {
      try {
        const r = await window.api.sendCommand('apply_edits', { path, edits: {}, preview: true });
        if (r && r.preview) { img.src = r.preview; return; }
      } catch (_) { /* fall through */ }
    }
    img.src = `local-image://x/?p=${encodeURIComponent(path)}`;
  },

  // ── 进入编辑器 ──

  _openEditor(path) {
    let fileList = this.photos;
    if (typeof Workbench !== 'undefined') {
      if (!Workbench.has(path)) Workbench.addPhotos([path], { source: 'photo-edit-page' });
      Workbench.setActive(path);
      if (!Workbench.getSelected().length) Workbench.select(path, 'add');
      fileList = Workbench.getPhotos();
    }
    const index = fileList.findIndex(f => f.path === path);
    PhotoEdit.open(path, {
      fileList, index: Math.max(0, index),
      // Presets may have been added/imported while the editor was open.
      onClose: () => { this._loadPresets(); this._syncFromWorkbench(); },
    });
  },

  // ── 批量导出（通用逻辑在 batch-export.js） ──

  openExport() {
    const n = this._compareSel.size;
    if (!n) { App.toast(t('peBatchNoSelection'), 'warning'); return; }
    if (typeof BatchExport !== 'undefined') BatchExport.open([...this._compareSel]);
  },
};
