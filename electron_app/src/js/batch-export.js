/**
 * batch-export.js — 独立批量导出（不经过云端交付）
 *
 * 来源：批量导出页（工作台/选文件/选文件夹）、文件页勾选、照片编辑页勾选。
 * 流程：逐张应用已保存的 sidecar 编辑 → 格式/尺寸/色彩空间 → 文字或图片水印
 *      → 写入指定文件夹。支持活动中心进度与取消。
 */

const BatchExport = {
  paths: [],
  _bound: false,
  _pageBound: false,
  _exporting: false,
  _task: '',
  _defaultsApplied: false,

  _IMG_FILTER: [{
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
  _LOGO_FILTER: [{
    name: 'Watermark',
    extensions: ['png', 'webp', 'jpg', 'jpeg'],
  }],
  _RAW_EXTS: new Set([
    'arw', 'srf', 'sr2', 'arq', 'cr2', 'cr3', 'crw', 'nef', 'nrw', 'raf',
    'rw2', 'orf', 'ori', 'pef', 'ptx', 'raw', 'rwl', 'rwz', 'dng', 'srw',
    'x3f', 'gpr', 'iiq', 'cap', 'eip', '3fr', 'fff', 'mef', 'mos', 'mrw',
    'mdc', 'erf', 'dcr', 'kdc', 'k25', 'dcs', 'dc2', 'bay', 'cs1', 'sti',
  ]),

  init() {
    this._bind();
    this._bindPage();
  },

  // ── 模态框 ──

  _bind() {
    if (this._bound) return;
    this._bound = true;
    const $ = id => document.getElementById(id);
    if (!$('pep-export-modal')) return;

    $('pex-close').addEventListener('click', () => this.close());
    $('pex-cancel').addEventListener('click', () => this.close());
    $('pex-start').addEventListener('click', () => this.start());
    $('pex-format').addEventListener('change', () => this._syncFormatUI());
    $('pex-quality').addEventListener('input', () => {
      $('pex-quality-val').textContent = $('pex-quality').value;
    });
    $('pex-size').addEventListener('change', () => this._syncFormatUI());
    $('pex-wm').addEventListener('change', () => this._syncWmUI());
    $('pex-wm-opacity').addEventListener('input', () => {
      $('pex-wm-opacity-val').textContent = $('pex-wm-opacity').value + '%';
    });
    $('pex-wm-img-opacity').addEventListener('input', () => {
      $('pex-wm-img-opacity-val').textContent = $('pex-wm-img-opacity').value + '%';
    });
    $('pex-wm-img-scale').addEventListener('input', () => {
      $('pex-wm-img-scale-val').textContent = $('pex-wm-img-scale').value + '%';
    });
    $('pex-dest-browse').addEventListener('click', async () => {
      try {
        const f = await window.api.selectFolder();
        if (f) $('pex-dest').value = f;
      } catch (_) { /* canceled */ }
    });
    $('pex-wm-image-browse').addEventListener('click', async () => {
      try {
        const paths = await window.api.selectFiles(this._LOGO_FILTER);
        if (paths && paths.length) $('pex-wm-image-path').value = paths[0];
      } catch (_) { /* canceled */ }
    });
    document.addEventListener('keydown', (e) => {
      if (e.key !== 'Escape') return;
      if ($('pep-export-modal').classList.contains('hidden')) return;
      if (this._exporting) return;
      this.close();
    });
  },

  _applyModalDefaults() {
    if (this._defaultsApplied) return;
    this._defaultsApplied = true;
    const $ = id => document.getElementById(id);
    let p = {};
    try { p = (typeof App !== 'undefined' && App.loadPrefs()) || {}; } catch (_) { p = {}; }
    const mapPos = (v) => ({
      'bottom-right': 'BOTTOM_RIGHT', 'bottom-left': 'BOTTOM_LEFT',
      'top-right': 'TOP_RIGHT', 'top-left': 'TOP_LEFT', 'center': 'CENTER',
    }[String(v || '').toLowerCase()] || 'BOTTOM_RIGHT');
    if (!$('pex-wm-text').value) $('pex-wm-text').value = p.wmText || '';
    if (!$('pex-wm-image-path').value) $('pex-wm-image-path').value = p.wmImage || '';
    if (p.wmPos) {
      $('pex-wm-pos').value = mapPos(p.wmPos);
      $('pex-wm-img-pos').value = mapPos(p.wmPos);
    }
    const op = Math.max(20, Math.min(100, parseInt(p.wmOpacity, 10) || 60));
    $('pex-wm-opacity').value = String(op);
    $('pex-wm-opacity-val').textContent = op + '%';
    $('pex-wm-img-opacity').value = String(op);
    $('pex-wm-img-opacity-val').textContent = op + '%';
    if (!$('pex-wm').value && p.wmEnabled) {
      $('pex-wm').value = p.wmImage ? 'image' : 'text';
    }
    if (p.bexDest && !$('pex-dest').value) $('pex-dest').value = p.bexDest;
  },

  _syncFormatUI() {
    const $ = id => document.getElementById(id);
    const lossy = ['JPEG', 'WEBP'].includes($('pex-format').value);
    $('pex-quality-row').style.display = lossy ? '' : 'none';
    $('pex-size-custom').classList.toggle('hidden', $('pex-size').value !== 'custom');
  },

  _syncWmUI() {
    const $ = id => document.getElementById(id);
    const mode = $('pex-wm').value;
    $('pex-wm-text-opts').classList.toggle('hidden', mode !== 'text');
    $('pex-wm-image-opts').classList.toggle('hidden', mode !== 'image');
  },

  /** Open the export dialog for an explicit path list (or the page's list). */
  open(paths) {
    this._bind();
    this._bindPage();
    if (Array.isArray(paths)) {
      this.paths = [];
      this._addPaths(paths, { silent: true });
      this.renderPage();
    }
    if (!this.paths.length) {
      App.toast(t('bexNoPhotos'), 'warning');
      return;
    }
    const $ = id => document.getElementById(id);
    this._applyModalDefaults();
    $('pex-count').textContent = t('pepSelCount', { count: this.paths.length });
    $('pex-progress').classList.add('hidden');
    $('pep-export-modal').classList.remove('hidden');
    this._syncFormatUI();
    this._syncWmUI();
  },

  close() {
    if (this._exporting) return;
    document.getElementById('pep-export-modal').classList.add('hidden');
  },

  async start() {
    if (this._exporting) return;
    const $ = id => document.getElementById(id);
    if (!this.paths.length) { App.toast(t('bexNoPhotos'), 'warning'); return; }
    const dest = $('pex-dest').value.trim();
    if (!dest) { App.toast(t('pexNoDest'), 'warning'); return; }

    let maxEdge = 0;
    const sizeVal = $('pex-size').value;
    if (sizeVal === 'custom') {
      maxEdge = Math.max(100, Math.min(10000, parseInt($('pex-size-custom').value, 10) || 0));
    } else {
      maxEdge = parseInt(sizeVal, 10) || 0;
    }

    let watermark = null;
    const wmMode = $('pex-wm').value;
    if (wmMode === 'text') {
      const text = $('pex-wm-text').value.trim();
      if (!text) { App.toast(t('pexWmNoText'), 'warning'); return; }
      watermark = {
        type: 'text',
        text,
        position: $('pex-wm-pos').value,
        opacity: parseInt($('pex-wm-opacity').value, 10) || 60,
      };
    } else if (wmMode === 'image') {
      const logo = $('pex-wm-image-path').value.trim();
      if (!logo) { App.toast(t('pexWmImageNoPath'), 'warning'); return; }
      watermark = {
        type: 'image',
        path: logo,
        position: $('pex-wm-img-pos').value,
        opacity: parseInt($('pex-wm-img-opacity').value, 10) || 60,
        scale: parseInt($('pex-wm-img-scale').value, 10) || 15,
      };
    }

    const params = {
      paths: this.paths.slice(),
      dest_dir: dest,
      format: $('pex-format').value,
      quality: parseInt($('pex-quality').value, 10) || 90,
      max_long_edge: maxEdge,
      color_space: $('pex-color').value,
      watermark,
    };
    try { if (typeof App !== 'undefined') App.savePrefs({ bexDest: dest }); } catch (_) { /* ignore */ }

    this._exporting = true;
    $('pex-start').disabled = true;
    $('pex-progress').classList.remove('hidden');
    this._setProgress(0, params.paths.length);
    const taskId = 'batchexport-' + Date.now().toString(36) + Math.random().toString(36).slice(2, 7);
    params.task_id = taskId;
    if (typeof Activity !== 'undefined') {
      this._task = Activity.start({
        title: t('actBatchExport'),
        detail: t('actPhotosN', { count: params.paths.length }),
        total: params.paths.length,
        cancelable: true,
        onCancel: () => { window.api.sendCommand('cancel_batch_task', { task_id: taskId }).catch(() => {}); },
      });
    }
    try {
      const r = await window.api.sendCommand('batch_export_edited', params);
      if (r && r.error) {
        App.toast(t('pexFail') + r.error, 'error');
        if (this._task) Activity.fail(this._task, r.error);
        return;
      }
      if (r.cancelled) {
        App.toast(t('pexCancelled', { ok: r.exported || 0 }), 'warning');
        if (this._task) Activity.finish(this._task, { detail: t('pexCancelled', { ok: r.exported || 0 }) });
        this._exporting = false;
        this.close();
        return;
      }
      const msg = t('pexDone', { ok: r.exported || 0, fail: r.failed || 0 });
      App.toast(msg, (r.failed || 0) ? 'warning' : 'success');
      if (this._task) Activity.finish(this._task, { detail: msg });
      this._exporting = false;
      this.close();
    } catch (e) {
      App.toast(t('pexFail') + (e.message || ''), 'error');
      if (this._task) Activity.fail(this._task, e.message || '');
    } finally {
      this._exporting = false;
      this._task = '';
      $('pex-start').disabled = false;
    }
  },

  _setProgress(current, total) {
    document.getElementById('pex-bar-fill').style.width =
      total ? `${(current / total) * 100}%` : '0%';
    document.getElementById('pex-progress-text').textContent =
      t('pexExporting', { current, total });
    if (this._task && typeof Activity !== 'undefined') {
      Activity.update(this._task, { current, total });
    }
  },

  handleEvent(eventName, data) {
    if (eventName === 'batch_export_progress' && this._exporting) {
      this._setProgress(data.current || 0, data.total || 0);
    }
  },

  // ── 批量导出页面 ──

  _bindPage() {
    if (this._pageBound) return;
    const $ = id => document.getElementById(id);
    if (!$('page-batch-export')) return;
    this._pageBound = true;
    $('bex-add-workbench').addEventListener('click', () => this.addWorkbench());
    $('bex-pick-files').addEventListener('click', () => this.pickFiles());
    $('bex-pick-folder').addEventListener('click', () => this.pickFolder());
    $('bex-clear').addEventListener('click', () => this.clear());
    $('bex-open').addEventListener('click', () => this.open());
    $('bex-list').addEventListener('click', (e) => {
      const btn = e.target.closest('.bex-remove');
      if (!btn) return;
      const i = parseInt(btn.dataset.i, 10);
      if (i >= 0 && i < this.paths.length) {
        this.paths.splice(i, 1);
        this.renderPage();
      }
    });
  },

  onPageEnter() {
    this._bind();
    this._bindPage();
    this.renderPage();
  },

  _norm(p) { return String(p).replace(/\\/g, '/').toLowerCase(); },

  _addPaths(paths, opts = {}) {
    const seen = new Set(this.paths.map(p => this._norm(p)));
    let added = 0;
    for (const raw of paths || []) {
      const p = String(raw || '');
      if (!p) continue;
      const k = this._norm(p);
      if (seen.has(k)) continue;
      seen.add(k);
      this.paths.push(p);
      added++;
    }
    if (!opts.silent) this.renderPage();
    return added;
  },

  addWorkbench() {
    if (typeof Workbench === 'undefined') return;
    const selected = Workbench.getSelected ? Workbench.getSelected() : [];
    const list = selected.length ? selected : (Workbench.getPhotos ? Workbench.getPhotos() : []);
    const paths = (list || []).map(f => f.path || f).filter(Boolean);
    if (!paths.length) { App.toast(t('bexWorkbenchEmpty'), 'warning'); return; }
    const added = this._addPaths(paths);
    App.toast(added ? t('bexAdded', { count: added }) : t('bexNoNew'), added ? 'success' : 'info');
  },

  async pickFiles() {
    try {
      const files = await window.api.selectFiles(this._IMG_FILTER);
      if (!files || !files.length) return;
      const added = this._addPaths(files);
      if (added) App.toast(t('bexAdded', { count: added }), 'success');
    } catch (_) { /* canceled */ }
  },

  async pickFolder() {
    try {
      const folder = await window.api.selectFolder();
      if (!folder) return;
      const r = await window.api.sendCommand('list_images_in_folder', { folder });
      const paths = (r.images || []).map(p => (typeof p === 'string' ? p : p.path)).filter(Boolean);
      if (!paths.length) { App.toast(t('pepFolderEmpty'), 'warning'); return; }
      const added = this._addPaths(paths);
      if (added) App.toast(t('bexAdded', { count: added }), 'success');
    } catch (_) { App.toast(t('fbLoadFail'), 'error'); }
  },

  clear() {
    if (!this.paths.length) return;
    this.paths = [];
    this.renderPage();
  },

  _esc(s) {
    return String(s).replace(/[&<>"']/g, c => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  },

  renderPage() {
    const $ = id => document.getElementById(id);
    const list = $('bex-list');
    if (!list) return;
    const count = $('bex-count');
    const open = $('bex-open');
    if (count) count.textContent = String(this.paths.length);
    if (open) open.disabled = this.paths.length === 0;
    $('bex-empty')?.classList.toggle('hidden', this.paths.length > 0);
    list.classList.toggle('hidden', this.paths.length === 0);
    list.innerHTML = this.paths.map((p, i) => {
      const name = String(p).split(/[\\/]/).pop() || p;
      const ext = (name.split('.').pop() || '').toLowerCase();
      const icon = this._RAW_EXTS.has(ext) ? 'ph-film-strip' : 'ph-image';
      return `<div class="bex-item" title="${this._esc(p)}">
        <span class="bex-icon"><i class="ph ${icon}"></i></span>
        <span class="bex-main">
          <span class="bex-name">${this._esc(name)}</span>
          <span class="bex-path">${this._esc(p)}</span>
        </span>
        <button type="button" class="bex-remove" data-i="${i}" title="${this._esc(t('bexRemove'))}"><i class="ph ph-x"></i></button>
      </div>`;
    }).join('');
  },
};
