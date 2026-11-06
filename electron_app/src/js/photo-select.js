/**
 * photo-select.js — AI Photo Selection page logic
 *
 * 评分不再是黑盒：每张照片实时进结果网格（缩略图+分数+选中色标），
 * 分数分布直方图实时增长，拖阈值即时重筛（不重评），手动 keep/reject 覆盖，
 * 点「应用选择」才同步「优秀照片」文件夹。重开已评文件夹走 SQLite 缓存秒出。
 */

const PhotoSelect = {
  initialized: false,
  modelLoaded: false,
  processing: false,
  sourceFolder: null,
  _scoreTask: '',

  // 结果累积：{path, filename, score, size, is_raw, thumb_path, manual_keep, selected}
  results: [],
  outputBase: '',   // 文件夹模式自定义输出位置（空 = 照片同目录）
  tagMap: {},   // path → [AI 标签]（PhotoTagger 维护；清缓存时一并清空）
  modelVersion: '',
  round2: false,   // 二次筛选（LR 修图后）：results 挂有 xmp_rating/xmp_label/developed/modified
  _round2Meta: {}, // path -> {xmp_rating,xmp_label,developed,modified}（重评后继续生效）
  _searchSet: null,   // P1-1 字段搜索命中集（Set<path>；null = 未启用）
  _searchToken: 0,    // 搜索响应防过期计数器
  _renderPending: false,
  _lightboxIndex: -1,

  // 虚拟列表状态：只渲染可视窗口内的卡片，支撑数百~数千张
  _virt: {
    cols: 0, cardW: 0, rowH: 0, gap: 8, minCard: 160, infoH: 32,
    totalH: 0, view: [], scrollPending: false,
    heatCache: {},   // path -> heatmap_path (in-memory memo for current session)
    heatLoading: new Set(),
  },

  // ── Daily Limit ──
  // Open-source build: AI 选片不限量。原免费档每日 200 张限额（本地加密计数 +
  // 服务端额度同步）已随订阅体系移除；服务端 /api/usage/score-select 仅作
  // 匿名用量统计，不再作为判额依据。

  // ── Initialization ──

  init() {
    if (this.initialized) return;
    this.initialized = true;
    this.bindEvents();
    if (typeof Workbench !== 'undefined') {
      Workbench.onChange(() => this._refreshUserSelection());
    }
    this.updateThresholdLabel();
    this._initVirtualScroll();
  },

  bindEvents() {
    document.getElementById('btn-browse-select').addEventListener('click', async () => {
      const folder = await window.api.selectFolder();
      if (folder) {
        document.getElementById('select-folder').value = folder;
        this.sourceFolder = folder;
        document.getElementById('btn-add-watermark').disabled = false;
        document.getElementById('btn-dedup').disabled = false;
        document.getElementById('btn-ai-tag').disabled = false;
        this.scanImages();
      }
    });

    document.getElementById('select-threshold').addEventListener('input', () => {
      this.updateThresholdLabel();
      App.savePrefs({ scoreThreshold: parseFloat(document.getElementById('select-threshold').value) });
      this.recomputeSelection();
    });

    document.getElementById('select-output-name').addEventListener('change', () => {
      App.savePrefs({ outputName: document.getElementById('select-output-name').value });
    });

    // 输出模式（文件夹 / 星标）与星标分数段
    document.getElementById('ps-mode').addEventListener('change', () => {
      App.savePrefs({ psMode: document.getElementById('ps-mode').value });
      this._applyModeUI(true);
    });
    for (let s = 1; s <= 5; s++) {
      document.getElementById('ps-star-' + s).addEventListener('change', () => {
        App.savePrefs({ psStarThresholds: this._starThresholds() });
        this.renderGrid();  // 星标徽标随分数段即时刷新
      });
    }
    this._applyModeUI();

    // 文件夹模式：自定义输出位置（默认照片同目录）
    document.getElementById('btn-output-base').addEventListener('click', async () => {
      const dir = await window.api.selectFolder();
      if (!dir) return;
      this.outputBase = dir;
      App.savePrefs({ psOutputBase: dir });
      this._renderOutputBaseHint();
    });
    document.getElementById('ps-output-base-clear').addEventListener('click', () => {
      this.outputBase = '';
      App.savePrefs({ psOutputBase: '' });
      this._renderOutputBaseHint();
    });
    this._renderOutputBaseHint();

    document.getElementById('btn-start-scoring').addEventListener('click', () => this.startScoring({ copy_on_score: false }));
    document.getElementById('btn-add-watermark').addEventListener('click', () => this.applyWatermark());
    document.getElementById('btn-switch-backend').addEventListener('click', () => this.switchBackend());
    document.getElementById('btn-ps-clear-cache').addEventListener('click', () => this.clearScoreCache());
    document.getElementById('btn-apply-selection').addEventListener('click', () => this.applySelection());
    document.getElementById('btn-import-retouched').addEventListener('click', () => this.importRetouched());
    const round2Cb = document.getElementById('ps-round2-only');
    if (round2Cb) round2Cb.addEventListener('change', () => this.renderGrid());
    document.getElementById('ps-sort-desc').addEventListener('change', () => this.renderGrid());
    const unmarkedCb = document.getElementById('ps-unmarked-only');
    if (unmarkedCb) unmarkedCb.addEventListener('change', () => this.renderGrid());
    // P1-1 字段搜索（后端 query 语法;防抖 280ms;Esc 清空）
    const searchEl = document.getElementById('ps-search');
    if (searchEl) {
      let searchTimer = null;
      searchEl.addEventListener('input', () => {
        clearTimeout(searchTimer);
        searchTimer = setTimeout(() => this._applySearch(), 280);
      });
      searchEl.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') { searchEl.value = ''; this._applySearch(); }
      });
    }
    document.getElementById('btn-export-xmp').addEventListener('click', () => this.exportXmp());
    document.getElementById('btn-dedup').addEventListener('click', () => PhotoDedup.open());
    if (typeof PhotoDedup !== 'undefined') PhotoDedup.bindEvents();
    if (typeof PhotoTagger !== 'undefined') PhotoTagger.bindEvents();

    // Slideshow
    Slideshow.init();
    document.getElementById('btn-slideshow').addEventListener('click', () => {
      const view = this._lightboxArr || this.results;
      if (!view.length) { App.toast(t('ssNoPhotos'), 'warning'); return; }
      Slideshow.start(view.filter(r => r.score >= 0));
    });

    // Export
    document.getElementById('btn-export').addEventListener('click', () => this.showExportDialog());

    // Photo Editor (shared panel)
    document.getElementById('ps-lb-edit').addEventListener('click', () => this._openEditForPS());
    document.getElementById('ps-lb-close').addEventListener('click', () => this.closeLightbox());
    document.querySelector('.ps-lb-backdrop').addEventListener('click', () => this.closeLightbox());
    document.getElementById('ps-lb-prev').addEventListener('click', () => this.moveLightbox(-1));
    document.getElementById('ps-lb-next').addEventListener('click', () => this.moveLightbox(1));
    document.getElementById('ps-lb-keep').addEventListener('click', () => this._markAndAdvance(this._lightboxIndex, 1));
    document.getElementById('ps-lb-reject').addEventListener('click', () => this._markAndAdvance(this._lightboxIndex, -1));
    document.getElementById('ps-lb-reset').addEventListener('click', () => this.setManual(this._lightboxIndex, 0));
    document.getElementById('ps-lb-select').addEventListener('click', () => {
      const r = this._lightboxArr && this._lightboxArr[this._lightboxIndex];
      if (!r || typeof Workbench === 'undefined') return;
      Workbench.select(r.path, 'toggle');
      this._refreshWorkbenchLightbox();
    });
    document.getElementById('ps-lb-heat').addEventListener('click', () => this.toggleLightboxHeatmap());
    // 键盘筛片流：P=保留 / X=排除 / U=清除覆盖（LR 键位习惯），标记后自动跳到下一张未标记
    document.addEventListener('keydown', (e) => {
      if (document.getElementById('ps-lightbox').classList.contains('hidden')) return;
      // 编辑器浮层打开时按键归编辑器（含滑块输入与自身快捷键）
      if (!document.getElementById('photo-edit-overlay').classList.contains('hidden')) return;
      if (e.target.closest('input, textarea, select, [contenteditable]')) return;
      if (typeof Keymap !== 'undefined') {
        if (Keymap.is(e, 'psClose')) this.closeLightbox();
        else if (Keymap.is(e, 'psPrev')) this.moveLightbox(-1);
        else if (Keymap.is(e, 'psNext')) this.moveLightbox(1);
        else if (Keymap.is(e, 'psKeepNext')) { e.preventDefault(); this._markAndAdvance(this._lightboxIndex, 1); }
        else if (Keymap.is(e, 'psRejectPrev')) { e.preventDefault(); this._markAndAdvance(this._lightboxIndex, -1); }
        else if (Keymap.is(e, 'psClearMark')) { e.preventDefault(); this.setManual(this._lightboxIndex, 0); }
        return;
      }
      if (e.key === 'Escape') this.closeLightbox();
      else if (e.key === 'ArrowLeft') this.moveLightbox(-1);
      else if (e.key === 'ArrowRight') this.moveLightbox(1);
      else if (e.key === 'p' || e.key === 'P' || e.key === 'k' || e.key === 'K') { e.preventDefault(); this._markAndAdvance(this._lightboxIndex, 1); }
      else if (e.key === 'x' || e.key === 'X') { e.preventDefault(); this._markAndAdvance(this._lightboxIndex, -1); }
      else if (e.key === 'u' || e.key === 'U' || e.key === '0') { e.preventDefault(); this.setManual(this._lightboxIndex, 0); }
    });
    // Right-click grid → hand folder to AI Agent
    const psGrid = document.getElementById('ps-grid');
    if (psGrid) psGrid.addEventListener('contextmenu', (e) => {
      if (!this.sourceFolder) return;
      e.preventDefault();
      if (typeof Agent === 'undefined') return;
      const selectedPaths = (this.results || []).filter(r => r.selected).map(r => r.path);
      Agent.quickLaunch({ folder: this.sourceFolder, selectedPaths });
    });
  },

  updateThresholdLabel() {
    document.getElementById('threshold-label').textContent =
      parseFloat(document.getElementById('select-threshold').value).toFixed(1);
  },

  // ── Model ──

  async refreshModelInfo() {
    // Cache count for current folder
    const cacheEl = document.getElementById('model-cache-stat');
    const cacheCount = document.getElementById('model-cache-count');
    if (cacheEl && this.results) {
      cacheEl.style.display = this.results.length ? '' : 'none';
      cacheCount.textContent = this.results.length;
    }
    // Calibration status
    const calEl = document.getElementById('model-cal-stat');
    const calText = document.getElementById('model-cal-status-text');
    if (calEl) {
      try {
        const st = await window.api.sendCommand('get_calibration_status', {});
        calEl.style.display = '';
        calText.textContent = st.calibration_ready ? t('psCalActive', { pairs: st.calibration_pairs }) : st.calibration_pairs > 0 ? t('psCalCollecting', { pairs: st.calibration_pairs }) : t('psInactive');
        calText.style.color = st.calibration_ready ? 'var(--green)' : '';
      } catch(e) { calEl.style.display = 'none'; }
    }
    // Last scoring summary
    const lsEl = document.getElementById('model-score-stat');
    if (lsEl && this.results && this.results.length) {
      const scored = this.results.filter(r => r.score >= 0);
      if (scored.length) {
        const avg = (scored.reduce((s,r)=>s+r.score,0)/scored.length).toFixed(1);
        document.getElementById('model-last-score-text').textContent = t('psLastScoreAvg', { avg, count: scored.length });
        lsEl.style.display = '';
      }
    }
  },

  async preloadModel() {
    if (this.modelLoaded || this._loadingModel) return;
    this._loadingModel = true;
    let retries = 0;
    while (!App.backendReady && retries < 30) {
      await new Promise(r => setTimeout(r, 1000)); retries++;
    }
    if (!App.backendReady) { this._loadingModel = false; this.addLog(t('psBackendNotReady'), 'error'); return; }
    document.getElementById('model-status-text').textContent = t('psLoadingModel');
    document.getElementById('model-device-text').textContent = '';
    this.addLog(t('psLoadingAI'), 'info');

    // 首次运行（本地无任何模型文件且未设偏好）→ 弹窗让用户选 ONNX(CPU)/PyTorch(GPU)，
    // 只下载所选后端的模型文件。已有模型或已设偏好则直接按偏好加载。
    let backend = null;
    try {
      const pref = await window.api.sendCommand('get_backend_pref');
      // 顺便更新模型状态卡的安装包类型提示
      const pkgText = document.getElementById('model-package-text');
      if (pkgText && pref) {
        pkgText.textContent = pref.package_type === 'gpu' ? t('psPkgGpu') : t('psPkgCpu');
      }
      if (pref && !pref.has_any_model && !pref.backend) {
        const picked = await this._showBackendPicker(pref.cuda_available);
        backend = picked || 'onnx'; // 关闭弹窗 → 默认 CPU 永远可用
        await window.api.sendCommand('set_backend_pref', { backend });
        this.addLog(t('psBackendPicked', { backend: backend === 'pytorch' ? 'PyTorch(GPU)' : 'ONNX(CPU)' }), 'info');
      } else {
        backend = (pref && pref.backend) || null;
      }
    } catch (e) { /* 偏好查询失败则走旧自动逻辑 */ }

    try {
      const result = await window.api.sendCommand('load_model', { model_dir: '', backend });
      this.addLog(t('psModelLoadOk', { name: result.model_name, device: result.device }), 'success');
      this.modelLoaded = true;
      document.getElementById('model-status-text').textContent = result.model_name || 'ResNet-18';
      document.getElementById('model-device-text').textContent = result.device || 'CPU';
      UI.setButton('btn-start-scoring', true);
      UI.setButton('btn-switch-backend', true);
      this.refreshModelInfo();
    } catch (e) {
      document.getElementById('model-status-text').textContent = t('fbLoadFail');
      document.getElementById('model-device-text').textContent = e.message;
      this.addLog(t('psModelLoadFail', { msg: e.message }), 'error');
      App.toast(t('psModelLoadFail', { msg: e.message }), 'error');
    } finally { this._loadingModel = false; }
  },

  // ── Backend picker + switch ──

  _showBackendPicker(cudaAvailable, currentBackend = null) {
    // 返回 Promise<'onnx'|'pytorch'|null>。首次下载与切换共用。
    return new Promise((resolve) => {
      const overlay = document.createElement('div');
      overlay.className = 'modal-overlay';
      overlay.style.zIndex = 10001;
      const onnxActive = currentBackend === 'onnx';
      const pyDisabled = !cudaAvailable;
      const pyActive = currentBackend === 'pytorch';
      overlay.innerHTML = `
        <div class="modal-box" style="min-width:440px">
          <div class="modal-title">${t('psBackendPickerTitle')}</div>
          <div class="modal-message" style="margin-bottom:14px;color:var(--text2);font-size:13px">${t('psBackendPickerDesc')}</div>
          <div style="display:flex;flex-direction:column;gap:10px;margin-bottom:18px">
            <label class="ps-be-opt${onnxActive ? ' active' : ''}" data-be="onnx">
              <input type="radio" name="ps-be" value="onnx" ${onnxActive ? 'checked' : ''} style="margin-right:8px">
              <div><div style="font-weight:600">ONNX Runtime (CPU)<span style="font-weight:400;font-size:11px;color:var(--text3);margin-left:6px">≈45MB</span></div>
              <div style="font-size:11px;color:var(--text3)">${t('psOnnxDesc')}</div></div>
            </label>
            <label class="ps-be-opt${pyActive ? ' active' : ''}${pyDisabled ? ' disabled' : ''}" data-be="pytorch">
              <input type="radio" name="ps-be" value="pytorch" ${pyActive ? 'checked' : ''} ${pyDisabled ? 'disabled' : ''} style="margin-right:8px">
              <div><div style="font-weight:600">PyTorch (${t('psGpuAccel')})<span style="font-weight:400;font-size:11px;color:var(--text3);margin-left:6px">≈130MB</span></div>
              <div style="font-size:11px;color:var(--text3)">${pyDisabled ? (window.api && window.api.platform === 'darwin' ? t('psPyNoMps') : t('psPyNoCuda')) : (window.api && window.api.platform === 'darwin' ? t('psPyMpsDesc') : t('psPyCudaDesc'))}</div></div>
            </label>
          </div>
          <div class="modal-buttons">
            <button class="btn btn-outline" data-act="cancel">${t('cancel')}</button>
            <button class="btn btn-primary" data-act="ok">${t('ok')}</button>
          </div>
        </div>`;
      document.body.appendChild(overlay);
      const close = (val) => { overlay.remove(); resolve(val); };
      overlay.querySelectorAll('.ps-be-opt').forEach(opt => {
        opt.addEventListener('click', (e) => {
          if (opt.classList.contains('disabled')) { e.preventDefault(); return; }
          overlay.querySelectorAll('.ps-be-opt').forEach(o => o.classList.remove('active'));
          opt.classList.add('active');
          const radio = opt.querySelector('input'); if (radio) radio.checked = true;
        });
      });
      overlay.querySelector('[data-act="cancel"]').addEventListener('click', () => close(null));
      overlay.querySelector('[data-act="ok"]').addEventListener('click', () => {
        const checked = overlay.querySelector('input[name="ps-be"]:checked');
        close(checked ? checked.value : 'onnx');
      });
    });
  },

  async switchBackend() {
    let pref;
    try { pref = await window.api.sendCommand('get_backend_pref'); }
    catch (e) { App.toast(t('psBackendStatusFail'), 'error'); return; }
    const picked = await this._showBackendPicker(pref.cuda_available, pref.loaded_backend || pref.backend);
    if (!picked) return;
    if (picked === (pref.loaded_backend || pref.backend) && pref.has_any_model) {
      App.toast(t('psAlreadyBackend'), 'info'); return;
    }
    UI.setButton('btn-start-scoring', false);
    UI.setButton('btn-switch-backend', false);
    document.getElementById('model-status-text').textContent = t('psSwitchModel') + '...';
    document.getElementById('model-device-text').textContent = '';
    this.addLog(t('psSwitchingTo', { backend: picked }), 'info');
    try {
      const r = await window.api.sendCommand('switch_backend', { backend: picked });
      // 切换成功：清空当前视图（旧后端结果），提示需重新评分。旧缓存按 model_version 隔离保留。
      this.results = [];
      this.modelVersion = '';
      this._virt.heatCache = {};
      this.renderGrid();
      document.getElementById('model-status-text').textContent = `${r.model_name || 'ResNet-18'}`;
      document.getElementById('model-device-text').textContent = `${r.device || 'CPU'}`;
      UI.setButton('btn-start-scoring', true);
      UI.setButton('btn-switch-backend', true);
      this.refreshModelInfo();
      App.toast(t('psSwitchedTo', { backend: picked === 'pytorch' ? 'PyTorch(GPU)' : 'ONNX(CPU)' }), 'warning');
      this.addLog(t('psSwitchedLog'), 'info');
    } catch (e) {
      document.getElementById('model-status-text').textContent = t('opFailed');
      document.getElementById('model-device-text').textContent = e.message;
      this.addLog(t('psSwitchFail', { msg: e.message }), 'error');
      App.toast(t('psSwitchFail', { msg: e.message }), 'error');
      UI.setButton('btn-switch-backend', true);
    }
  },

  // ── Image Scanning ──

  async scanImages() {
    if (!this.sourceFolder) return;
    if (this.processing) { App.toast(t('psScoringWait'), 'warning'); return; }
    UI.clearLog('score-log');
    this.results = [];
    this.round2 = false;         // 换文件夹：重置二次筛选状态
    this._round2Meta = {};
    this._searchSet = null;
    const searchEl = document.getElementById('ps-search');
    if (searchEl) searchEl.value = '';
    this.renderGrid();
    this.addLog(t('psScanFolder') + ' ' + this.sourceFolder, 'info');
    try {
      const result = await window.api.sendCommand('scan_images', { folder: this.sourceFolder });
      this.addLog(t('psFoundImages').replace('N', result.count) + ` (${UI.formatSize(result.total_size || 0)})`, 'info');
      document.getElementById('score-progress-stats').textContent = t('psTotalImages').replace('N', result.count);
      // 试读缓存：已评过的文件夹秒出结果
      this.tryLoadCachedResults();
    } catch (e) { this.addLog(t('psScanFail', { msg: e.message }), 'error'); }
  },

  async tryLoadCachedResults() {
    try {
      const r = await window.api.sendCommand('get_scored_results', { folder: this.sourceFolder });
      // 评分进行中丢弃晚到的缓存响应，避免覆盖评分流实时累积的 this.results
      if (this.processing) return;
      if (r && r.results && r.results.length) {
        this.modelVersion = r.model_version || '';
        this.results = r.results.map(x => {
          const r2m = this._round2Meta[x.path];
          return {
            path: x.path, filename: x.filename || x.path.split(/[\\/]/).pop(),
            score: x.score, size: x.size || 0, is_raw: !!x.is_raw,
            thumb_path: x.thumb_path || '', manual_keep: x.manual_keep || 0, selected: false,
            xmp_rating: r2m && r2m.xmp_rating, xmp_label: r2m && r2m.xmp_label,
            developed: r2m && r2m.developed, modified: r2m && r2m.modified,
          };
        });
        this.recomputeSelection();
        this.renderGrid();
        document.getElementById('ps-cache-hint').textContent = t('psCacheHitAdjust', { count: this.results.length });
        document.getElementById('btn-slideshow').disabled =
        document.getElementById('btn-export').disabled =
        document.getElementById('btn-apply-selection').disabled = false;
        this.addLog(t('psCacheHitNoRescore', { count: this.results.length }), 'success');
        this.refreshModelInfo();
      }
    } catch (e) { /* ignore */ }
  },

  // ── Clear Score Cache ──

  async clearScoreCache() {
    if (this.processing) { App.toast(t('psScoringWait'), 'warning'); return; }
    const confirmed = await Modal.showConfirm(t('psClearCacheTitle'), t('psClearCacheMsg'));
    if (!confirmed) return;
    try {
      const r = await window.api.sendCommand('clear_score_cache', {});
      if (!r || !r.ok) { App.toast(t('psClearCacheFail'), 'error'); return; }
      // 清空当前视图：缓存已删，网格里的分数不再可信
      this.results = [];
      this._virt.heatCache = {};
      if (typeof PhotoTagger !== 'undefined') PhotoTagger.resetTags();  // tags 同库存储，一并清
      this.renderGrid();
      document.getElementById('ps-cache-hint').textContent = '';
      document.getElementById('result-summary').classList.add('hidden');
      document.getElementById('ps-distribution').classList.add('hidden');
      document.getElementById('btn-slideshow').disabled =
        document.getElementById('btn-export').disabled =
        document.getElementById('btn-apply-selection').disabled = true;
      this.refreshModelInfo();
      this.addLog(t('psCacheClearedLog'), 'info');
      App.toast(t('psCacheCleared'), 'success');
    } catch (e) { App.toast(t('psClearCacheFail'), 'error'); }
  },

  // ── Scoring ──

  async startScoring(opts = {}) {
    if (!this.modelLoaded) { App.toast(t('psModelNotReady'), 'warning'); return; }
    if (!this.sourceFolder) { App.toast(t('psSelectFolderDot'), 'warning'); return; }
    if (this.processing) { App.toast(t('psProcessing'), 'info'); return; }
    if (typeof PhotoTagger !== 'undefined' && PhotoTagger.running) { App.toast(t('tagRunning'), 'info'); return; }

    this.processing = true;  // Must be set immediately after guard to prevent double-click race
    const threshold = parseFloat(document.getElementById('select-threshold').value);
    // 星标模式不物化输出文件夹，输出名为空不阻塞（copy_on_score=false 时后端也不会创建）
    const outputName = document.getElementById('select-output-name').value.trim();
    if (!outputName && !this._isTagMode()) { this.processing = false; App.toast(t('psOutputNameRequired'), 'warning'); return; }

    // copy_on_score: 交互页传 false（只评分+缓存+缩略图，由「应用选择」物化文件夹）；
    // 自动化/旧路径默认 true（评分时直接拷进优秀照片文件夹，供后续上传）。
    const copyOnScore = opts.copy_on_score !== false;
    this._copyOnScore = copyOnScore;

    this.results = [];
    this._searchSet = null;  // 重评分=新结果集,搜索状态一并清空(与换文件夹同策略)
    const searchEl = document.getElementById('ps-search');
    if (searchEl) searchEl.value = '';
    this.renderGrid();
    // 二次筛选元数据(_round2Meta)保留：round-2 重评后徽标/过滤仍生效
    // （换文件夹由 scanImages 重置 round2 + _round2Meta）
    UI.setButton('btn-start-scoring', false);
    document.getElementById('btn-slideshow').disabled =
        document.getElementById('btn-export').disabled =
        document.getElementById('btn-apply-selection').disabled = true;
    document.getElementById('btn-cancel-scoring').classList.remove('hidden');
    document.getElementById('ps-cache-hint').textContent = '';
    UI.updateProgress('score-progress-fill', 'score-progress-text', 0, t('psStartScoring'));
    UI.clearLog('score-log');
    this.addLog(t('psStartFilterLog', { threshold: threshold.toFixed(1) }) + (copyOnScore ? '' : t('psScoreOnlyMode')), 'info');
    if (typeof Activity !== 'undefined') {
      this._scoreTask = Activity.start({
        title: t('actScoring'),
        detail: this.sourceFolder || '',
        total: 0,
        cancelable: true,
        onCancel: () => this._doCancelScoring(),
      });
    }

    try {
      await window.api.sendCommand('score_photos', {
        folder: this.sourceFolder,
        threshold,
        output_name: outputName,
        copy_on_score: copyOnScore,
      });
    } catch (e) {
      this.addLog(t('psFilterFail', { msg: e.message }), 'error');
      this.processing = false;
      UI.setButton('btn-start-scoring', true);
      document.getElementById('btn-cancel-scoring').classList.add('hidden');
      if (this._scoreTask) { Activity.fail(this._scoreTask, e.message || ''); this._scoreTask = ''; }
    }
  },

  async cancelScoring() {
    if (!this.processing) return;
    if (this._scoreTask && typeof Activity !== 'undefined') {
      Activity.cancel(this._scoreTask);
      return;
    }
    this._doCancelScoring();
  },

  async _doCancelScoring() {
    if (!this.processing) return;
    try {
      await window.api.sendCommand('cancel_scoring', {});
    } catch (e) { /* ignore — backend may already be stopped */ }
    this.processing = false;
    this._scoreTask = '';
    UI.setButton('btn-start-scoring', true);
    document.getElementById('btn-cancel-scoring').classList.add('hidden');
    UI.updateProgress('score-progress-fill', 'score-progress-text', 0, t('psCancelled'));
    this.addLog(t('psScoringCancelled'), 'warn');
    // 取消后 score_done 会因 processing=false 被丢弃（handleEvent 495 行），
    // 这里按已有的部分结果恢复按钮启用态（与 score_done/recomputeSelection 逻辑一致）
    this.recomputeSelection();
  },

  // ── Event Handlers ──

  async handleEvent(eventName, data) {
    switch (eventName) {
      case 'scan_complete':
        this.addLog(t('psScanDone', { count: data.count }), 'info');
        document.getElementById('score-progress-stats').textContent =
          t('psScanStats', { count: data.count, size: UI.formatSize(data.total_size || 0) });
        break;

      case 'score_progress': {
        // Ignore scoring events not initiated by this page (e.g. the AI Agent
        // module calls score_photos in-process and re-emits score_progress).
        if (!this.processing) break;
        const pct = data.total > 0 ? (data.current / data.total * 100) : 0;
        UI.updateProgress('score-progress-fill', 'score-progress-text', pct, t('psScoringProgress', { current: data.current, total: data.total }));
        if (this._scoreTask && typeof Activity !== 'undefined') {
          Activity.update(this._scoreTask, {
            current: data.current || 0,
            total: data.total || 0,
            detail: t('psScoringProgress', { current: data.current, total: data.total }),
          });
        }
        let stats = t('psProcessed').replace('N', data.current).replace('N', data.total);
        if (data.selected > 0) stats += t('psSelectedSuffix', { count: data.selected });
        document.getElementById('score-progress-stats').textContent = stats;

        if (data.image) {
          const r2m = this._round2Meta[data.image.path];
          this.results.push({
            path: data.image.path, filename: data.image.filename,
            score: data.image.score, size: data.image.size, is_raw: data.image.is_raw,
            thumb_path: data.image.thumb_path || '', manual_keep: data.image.manual_keep || 0,
            selected: false,
            xmp_rating: r2m && r2m.xmp_rating, xmp_label: r2m && r2m.xmp_label,
            developed: r2m && r2m.developed, modified: r2m && r2m.modified,
          });
          if (data.image.model_version) this.modelVersion = data.image.model_version;
          this.scheduleRender();
          if (data.last_file && data.last_score >= 0) {
            const t = parseFloat(document.getElementById('select-threshold').value) || 6;
            this.addLog(
              data.last_score >= t ? `✓ ${data.last_file}: ${data.last_score.toFixed(1)}` : `  ${data.last_file}: ${data.last_score.toFixed(1)}`,
              data.last_score >= t ? 'success' : 'info'
            );
          }
        }
        break;
      }

      case 'score_done': {
        if (!this.processing) break;  // not our run (e.g. AI Agent in-process scoring)
        this.processing = false;
        if (this._scoreTask && typeof Activity !== 'undefined') {
          Activity.finish(this._scoreTask, { detail: t('psFilterDoneLog', { total: data.total, selected: data.selected, failed: data.failed || 0 }) });
          this._scoreTask = '';
        }
        document.getElementById('btn-cancel-scoring').classList.add('hidden');
        UI.updateProgress('score-progress-fill', 'score-progress-text', 100, t('psFilterDone'));
        UI.setButton('btn-start-scoring', true);
        this.recomputeSelection();
        this.addLog('═'.repeat(40), 'info');
        this.addLog(t('psFilterDoneLog', { total: data.total, selected: data.selected, failed: data.failed || 0 }), 'success');
        if (data.avg_score !== undefined) this.addLog(t('psAvgScoreLog', { avg: data.avg_score.toFixed(2), cached: data.cached || 0 }), 'info');
        document.getElementById('result-summary').textContent = t('psOfTotal', { sel: data.selected, total: data.total });
        document.getElementById('result-summary').classList.remove('hidden');
        document.getElementById('btn-slideshow').disabled =
        document.getElementById('btn-export').disabled =
        document.getElementById('btn-apply-selection').disabled = this.results.length === 0;
        App.saveStat('scored', data.total);
        App.saveStat('excellent', data.selected);
        App.addHistory(t('psAIDone'), t('psOfTotalExcellent', { sel: data.selected, total: data.total }));
        App.notify(t('psAIDone'), t('psOfTotalExcellentPhotos', { sel: data.selected, total: data.total }));

        // 旧路径（copy_on_score）：优秀照片已拷进文件夹，保留水印 + 自动入上传队列行为。
        // 交互路径（copy_on_score=false）由用户点「应用选择」后再上传，这里跳过。
        if (this._copyOnScore && data.output_folder) {
          const p = JSON.parse(localStorage.getItem('prefs_' + (App.user?.id || 'default')) || '{}');
          if (p.wmEnabled && data.selected > 0) {
            try {
              const r = await window.api.sendCommand('list_images_in_folder', { folder: data.output_folder });
              if (r && r.images && r.images.length) {
                const imagePaths = r.images.map(f => typeof f === 'string' ? f : f.path);
                const wm = await window.api.sendCommand('apply_watermark', {
                  file_paths: imagePaths, text: p.wmText || '© LensPilot',
                  position: p.wmPos || 'bottom-right', opacity: (p.wmOpacity || 30) / 100,
                });
                this.addLog(t('psWatermarkAddedLog', { count: wm.processed }), 'success');
              }
            } catch (e) { this.addLog(t('psWatermarkFail', { msg: e.message }), 'warn'); }
          }
          if (App.token && typeof CloudUpload !== 'undefined') {
            window.api.sendCommand('list_images_in_folder', { folder: data.output_folder }).then(r => {
              if (r && r.images && r.images.length) {
                const scores = {}; r.images.forEach(f => {
                  const p = typeof f === 'string' ? f : f.path;
                  scores[p] = 0;
                });
                CloudUpload.addToQueue(r.images, scores);
                App.toast(t('psAddedToQueue', { count: r.images.length }), 'success');
              }
            }).catch(() => {});
          }
        }
        // Notify automation engine
        if (typeof Automation !== 'undefined') {
          Automation.onStepComplete('score', { output_folder: data.output_folder, selected: data.selected, total: data.total });
        }
        break;
      }

      case 'model_loaded':
        this.modelLoaded = true;
        document.getElementById('model-status-text').textContent = `${data.model_name || 'ResNet-18'}`;
        document.getElementById('model-device-text').textContent = `${data.device || 'CPU'}`;
        UI.setButton('btn-start-scoring', true);
        UI.setButton('btn-switch-backend', true);
        break;

      case 'model_error':
        document.getElementById('model-status-text').textContent = t('fbLoadFail');
        document.getElementById('model-device-text').textContent = data.error;
        this.addLog(t('psModelError', { msg: data.error }), 'error');
        break;

      case 'export_progress':
        const ep = data.total > 0 ? (data.current / data.total * 100) : 0;
        UI.updateProgress('score-progress-fill', 'score-progress-text', ep,
          t('psExporting', { current: data.current, total: data.total, exported: data.exported, failed: data.failed }));
        break;
    }
  },

  // ── Rendering ──

  scheduleRender() {
    if (this._renderPending) return;
    this._renderPending = true;
    requestAnimationFrame(() => {
      this._renderPending = false;
      this.renderGrid();
      this.renderDistribution();
    });
  },

  _selectedFor(score, mk) {
    const t = parseFloat(document.getElementById('select-threshold').value) || 6;
    if (mk === 1) return true;
    if (mk === -1) return false;
    return score >= 0 && score >= t;
  },

  recomputeSelection() {
    let sel = 0;
    this.results.forEach(r => { r.selected = this._selectedFor(r.score, r.manual_keep); if (r.selected) sel++; });
    // 只更新已渲染（可见）卡片的类名，未进入可视窗口的卡片在滚动渲染时按 results 状态生成
    document.querySelectorAll('.ps-card').forEach(el => {
      const r = this.results.find(x => x.path === el.dataset.path);
      if (!r) return;
      el.classList.toggle('selected', r.selected && r.manual_keep !== -1);
      el.classList.toggle('rejected', r.manual_keep === -1);
    });
    const total = this.results.length;
    document.getElementById('result-summary').textContent = t('psOfTotal', { sel, total });
    document.getElementById('result-summary').classList.toggle('hidden', total === 0);
    document.getElementById('btn-slideshow').disabled =
        document.getElementById('btn-export').disabled =
        document.getElementById('btn-export-xmp').disabled =
        document.getElementById('btn-apply-selection').disabled = total === 0;
    this.renderDistribution();
  },

  scoreClass(score) {
    if (score < 0) return 'score-fail';
    if (score >= 7) return 'score-high';
    if (score >= 5) return 'score-mid';
    return 'score-low';
  },

  thumbUrl(path) {
    return 'local-image://x/?p=' + encodeURIComponent(path);
  },

  // ── Virtual list ──

  _initVirtualScroll() {
    const el = document.getElementById('ps-grid');
    el.classList.add('ps-grid-virtual');
    el.addEventListener('scroll', () => {
      if (this._virt.scrollPending) return;
      this._virt.scrollPending = true;
      requestAnimationFrame(() => {
        this._virt.scrollPending = false;
        this._renderVisible();
      });
    }, { passive: true });
    // 列数随容器宽度变化时重排
    if (window.ResizeObserver) {
      this._ro = new ResizeObserver(() => {
        if (this._virt.view.length) { this._layoutGrid(); this._renderVisible(); }
      });
      this._ro.observe(el);
    }
  },

  _layoutGrid() {
    const el = document.getElementById('ps-grid');
    const { gap, minCard, infoH } = this._virt;
    const width = (el.clientWidth || 800) - 4; // padding
    let cols = Math.max(1, Math.floor((width + gap) / (minCard + gap)));
    const cardW = (width - (cols - 1) * gap) / cols;
    const imgH = cardW * 2 / 3;
    const rowH = imgH + infoH; // 卡片高度（border 已含在 infoH 余量内）
    this._virt.cols = cols;
    this._virt.cardW = cardW;
    this._virt.rowH = rowH;
    const rows = Math.ceil(this._virt.view.length / cols);
    this._virt.totalH = rows * (rowH + gap);
  },

  _buildCard(r) {
    const cls = this.scoreClass(r.score);
    const scoreTxt = r.score >= 0 ? r.score.toFixed(1) : '×';
    const img = r.thumb_path
      ? `<img src="${this.thumbUrl(r.thumb_path)}" loading="lazy">`
      : `<div class="ps-card-fail"><i class="ph ph-image-broken"></i></div>`;
    const heat = this._virt.heatCache[r.path]
      ? `<img class="ps-card-heat-img" src="${this.thumbUrl(this._virt.heatCache[r.path])}">`
      : '';
    const heatBtn = `<button class="ps-card-heat-btn${this._virt.heatCache[r.path] ? ' active' : ''}" title="${t('psHeatmapTitle')}"><i class="ph ph-fire"></i></button>`;
    const badge = r.manual_keep === 1 ? '<div class="ps-card-badge-keep"><i class="ph ph-check"></i></div>'
      : r.manual_keep === -1 ? '<div class="ps-card-badge-reject"><i class="ph ph-x"></i></div>' : '';
    // 星标模式：卡片上预览将写入的 LR 星级（排除/不打标的不显示）
    let starBadge = '';
    if (this._isTagMode() && r.score >= 0 && r.manual_keep !== -1) {
      let rt = this._ratingFor(r.score);
      if (rt === 0 && r.manual_keep === 1) rt = 1;
      if (rt > 0) starBadge = `<span class="ps-card-stars">${'★'.repeat(rt)}</span>`;
    }
    // 二次筛选：LR 修过图的卡片显示 LR 徽标 + 读回的 LR 星级（XMP 权威）
    let lrBadge = '';
    if (this.round2 && r.developed) {
      lrBadge = `<span class="ps-card-lr" title="${t('psLrBadge')}">LR</span>`;
      const lrStars = (r.xmp_rating || 0);
      if (lrStars > 0) starBadge = `<span class="ps-card-stars" title="${t('psLrRating', { n: lrStars })}">${'★'.repeat(lrStars)}</span>`;
    }
    const userSel = (typeof Workbench !== 'undefined') && Workbench.isSelected(r.path);
    const cardCls = 'ps-card' + (r.manual_keep === -1 ? ' rejected' : (r.selected ? ' selected' : '')) + (userSel ? ' ps-user-sel' : '');
    const tagChips = (typeof PhotoTagger !== 'undefined') ? PhotoTagger._chipsHtml((this.tagMap || {})[r.path]) : '';
    const div = document.createElement('div');
    div.className = cardCls;
    div.dataset.path = r.path;
    div.innerHTML = `${img}${heat}${heatBtn}${badge}${tagChips}
      <div class="ps-card-info"><span class="ps-card-name" title="${r.filename}">${r.filename}</span>${lrBadge}${starBadge}<span class="score-badge ${cls}">${scoreTxt}</span></div>`;
    // 用户选择（与 AI 入选是两回事）
    const chk = document.createElement('label');
    chk.className = 'ps-sel-check';
    chk.title = t('wbSelect');
    chk.innerHTML = `<input type="checkbox" ${userSel ? 'checked' : ''}>`;
    div.appendChild(chk);
    return div;
  },

  _ensureSpacer() {
    const el = document.getElementById('ps-grid');
    let spacer = el.querySelector('.ps-virt-spacer');
    if (!spacer) {
      el.innerHTML = '';
      spacer = document.createElement('div');
      spacer.className = 'ps-virt-spacer';
      el.appendChild(spacer);
    }
    return spacer;
  },

  _renderVisible() {
    const el = document.getElementById('ps-grid');
    const view = this._virt.view;
    if (!view.length) return;
    const spacer = this._ensureSpacer();
    const { cols, rowH, gap, cardW } = this._virt;
    const st = el.scrollTop;
    const viewH = el.clientHeight || 600;
    const startRow = Math.max(0, Math.floor(st / (rowH + gap)) - 1);
    const endRow = Math.ceil((st + viewH) / (rowH + gap)) + 1;
    const startIdx = startRow * cols;
    const endIdx = Math.min(view.length, endRow * cols);

    const frag = document.createDocumentFragment();
    const cards = [];
    const firstPaint = !!this._virt.firstPaint;
    this._virt.firstPaint = false;
    for (let i = startIdx; i < endIdx; i++) {
      const r = view[i];
      const card = this._buildCard(r);
      const row = Math.floor(i / cols), col = i % cols;
      card.style.left = (col * (cardW + gap)) + 'px';
      card.style.top = (row * (rowH + gap)) + 'px';
      card.style.width = cardW + 'px';
      if (firstPaint) {
        card.classList.add('ps-enter');
        card.style.setProperty('--d', Math.min((i - startIdx) * 15, 180) + 'ms');
      }
      frag.appendChild(card);
      cards.push({ card, path: r.path });
    }
    spacer.innerHTML = '';
    spacer.appendChild(frag);
    // 绑定事件（仅当前可见卡片）
    cards.forEach(({ card, path }) => {
      card.addEventListener('click', (e) => {
        if (e.target.closest('.ps-card-heat-btn, .ps-sel-check')) return;
        if (typeof Workbench !== 'undefined') {
          if (e.ctrlKey || e.metaKey) { Workbench.select(path, 'toggle'); return; }
          if (e.shiftKey) { Workbench.selectRange(path, true); return; }
          Workbench.setActive(path);
        }
        const idx = view.findIndex(x => x.path === path);
        if (idx >= 0) this.openLightbox(view, idx);
      });
      const hb = card.querySelector('.ps-card-heat-btn');
      if (hb) hb.addEventListener('click', (e) => {
        e.stopPropagation();
        this.toggleCardHeatmap(path, card);
      });
      const chk = card.querySelector('.ps-sel-check input');
      if (chk) {
        chk.addEventListener('click', (e) => e.stopPropagation());
        chk.addEventListener('change', (e) => {
          e.stopPropagation();
          if (typeof Workbench !== 'undefined') Workbench.select(path, 'toggle');
        });
      }
    });
  },

  // ── 工作台同步：AI 建议是元数据，用户选择才是批量作用域 ──
  _syncWorkbench() {
    if (typeof Workbench === 'undefined' || !this.results || !this.results.length) return;
    // AI 选片页只在自己拥有工作台时自动同步，避免把其他页面的工作台覆盖掉。
    if (Workbench.photos.length && Workbench.source && Workbench.source !== 'ai-select') return;
    const results = this.results;
    const from = Workbench.fromResults(results);
    const wb = Workbench.photos;
    const same = from.length === wb.length && from.every((p, i) => wb[i]
      && wb[i].path === p.path && (wb[i].thumb || '') === (p.thumb || ''));
    if (!same) {
      Workbench.setPhotos(from, { source: 'ai-select' });
    }
    const suggested = results.filter(r => r.selected).map(r => r.path).sort().join('\n');
    if (this._wbSuggestedCache !== suggested) {
      this._wbSuggestedCache = suggested;
      Workbench.setSuggested(suggested ? suggested.split('\n') : []);
    }
  },

  _refreshUserSelection() {
    if (typeof Workbench === 'undefined') return;
    document.querySelectorAll('.ps-card[data-path]').forEach(card => {
      const path = card.dataset.path;
      const on = Workbench.isSelected(path);
      card.classList.toggle('ps-user-sel', on);
      const cb = card.querySelector('.ps-sel-check input');
      if (cb) cb.checked = on;
    });
    this._refreshWorkbenchLightbox();
  },

  _refreshWorkbenchLightbox() {
    if (typeof Workbench === 'undefined') return;
    const r = this._lightboxArr && this._lightboxArr[this._lightboxIndex];
    const btn = document.getElementById('ps-lb-select');
    if (!btn || !r) return;
    const on = Workbench.isSelected(r.path);
    btn.classList.toggle('btn-primary', on);
    const label = btn.querySelector('span');
    if (label) label.textContent = t(on ? 'wbSelected' : 'wbSelect');
  },

  renderGrid() {
    this._syncWorkbench();
    const el = document.getElementById('ps-grid');
    if (!this.results.length) {
      this._virt.view = [];
      el.classList.add('ps-grid-virtual');
      el.innerHTML = `<div class="empty-state"><span class="empty-icon">🖼️</span><span class="empty-text">${t('psEmpty')}</span><span class="empty-hint">${t('psEmptyHint')}</span></div>`;
      return;
    }
    el.classList.add('ps-grid-virtual');
    const desc = document.getElementById('ps-sort-desc').checked;
    const unmarkedOnly = document.getElementById('ps-unmarked-only')?.checked;
    const tagFilter = document.getElementById('ps-tag-filter')?.value || '';
    let base = unmarkedOnly ? this.results.filter(r => !r.manual_keep) : this.results.slice();
    if (tagFilter) base = base.filter(r => ((this.tagMap || {})[r.path] || []).includes(tagFilter));
    // 二次筛选：只看修图后（LR 开发过 / 文件变更过 / XMP 有星级）
    if (document.getElementById('ps-round2-only')?.checked) {
      base = base.filter(r => r.developed || r.modified || (r.xmp_rating || 0) >= 1);
    }
    // 字段搜索（P1-1）：只保留后端 query 命中的照片
    if (this._searchSet) base = base.filter(r => this._searchSet.has(r.path));
    this._virt.view = base.sort((a, b) => desc ? (b.score - a.score) : (a.score - b.score));
    if (!this._virt.view.length) {
      const emptyText = this._searchSet ? t('psSearchEmpty') : (tagFilter ? t('tagNoMatch') : t('psAllMarkedEmpty'));
      el.innerHTML = `<div class="empty-state"><span class="empty-icon">✅</span><span class="empty-text">${emptyText}</span></div>`;
      return;
    }
    this._layoutGrid();
    const spacer = this._ensureSpacer();
    spacer.style.height = this._virt.totalH + 'px';
    this._virt.firstPaint = true; // 仅首批渲染播入场动画，滚动复用不播
    this._renderVisible();
    // 异步补显 tag chips（不阻塞渲染；get_photo_tags 批量拉取未缓存路径）
    if (typeof PhotoTagger !== 'undefined') PhotoTagger.afterRender(this.results.map(r => r.path));
  },

  // ── 字段搜索（P1-1）: 后端 query 语法，命中集过滤网格 ──

  async _applySearch() {
    const el = document.getElementById('ps-search');
    const q = (el && el.value || '').trim();
    if (!q) {
      if (this._searchSet) { this._searchSet = null; this.renderGrid(); }
      return;
    }
    if (!this.results.length || !this.sourceFolder) return;
    const token = ++this._searchToken;
    const th = this._starThresholds();  // [★5, ★4, ★3, ★2, ★1] 下限分数
    try {
      const r = await window.api.sendCommand('search_photos', {
        folder: this.sourceFolder,
        query: q,
        star_bands: { 5: th[0], 4: th[1], 3: th[2], 2: th[3], 1: th[4] },
        limit: 5000,
      });
      if (token !== this._searchToken) return;  // 过期响应丢弃
      if (r && r.error) { App.toast(t('psSearchBad') + r.error, 'warning'); return; }
      this._searchSet = new Set((r && r.matches || []).map(m => m.path));
      this.renderGrid();
    } catch (_) { /* ignore */ }
  },

  renderDistribution() {
    const wrap = document.getElementById('ps-distribution');
    if (!this.results.length) { wrap.classList.add('hidden'); return; }
    wrap.classList.remove('hidden');
    const scored = this.results.filter(r => r.score >= 0);
    if (!scored.length) { wrap.classList.add('hidden'); return; }
    const buckets = [0, 0, 0, 0, 0, 0]; // 3-4,4-5,5-6,6-7,7-8,8-9
    const labels = ['3-4', '4-5', '5-6', '6-7', '7-8', '8-9'];
    scored.forEach(r => {
      let i = Math.floor(r.score) - 3;
      if (i < 0) i = 0; if (i > 5) i = 5;
      buckets[i]++;
    });
    const max = Math.max(1, ...buckets);
    const th = parseFloat(document.getElementById('select-threshold').value) || 6;
    // 阈值标记位置（3-9 → 0-100%）
    const tPct = Math.max(0, Math.min(100, ((th - 3) / 6) * 100));
    document.getElementById('ps-dist-bars').innerHTML =
      buckets.map((c, i) => `<div class="ps-dist-bar${(i + 3) >= th ? ' over' : ''}" style="height:${(c / max) * 100}%" title="${labels[i]}: ${c}"></div>`).join('') +
      `<div class="ps-dist-threshold" style="left:${tPct}%"></div>`;
    // 柱组有 max-width 且整体居中，阈值线按柱组实际像素范围对齐（而非容器百分比）
    const barsWrap = document.getElementById('ps-dist-bars');
    const thEl = barsWrap.querySelector('.ps-dist-threshold');
    const barEls = barsWrap.querySelectorAll('.ps-dist-bar');
    if (thEl && barEls.length) {
      const contR = barsWrap.getBoundingClientRect();
      const firstR = barEls[0].getBoundingClientRect();
      const lastR = barEls[barEls.length - 1].getBoundingClientRect();
      if (contR.width > 0) {
        thEl.style.left = (firstR.left - contR.left + (tPct / 100) * (lastR.right - firstR.left)) + 'px';
      }
    }
    document.getElementById('ps-dist-labels').innerHTML = labels.map(l => `<span>${l}</span>`).join('');
    let min = 10, maxs = 0;
    for (let i = 0; i < scored.length; i++) {
      const s = scored[i].score;
      if (s < min) min = s;
      if (s > maxs) maxs = s;
    }
    const avg = scored.reduce((s, r) => s + r.score, 0) / scored.length;
    const sel = this.results.filter(r => r.selected).length;
    document.getElementById('ps-dist-stats').innerHTML =
      t('psDistStats', { total: scored.length, sel, min: min.toFixed(1), max: maxs.toFixed(1), avg: avg.toFixed(2) });
  },

  // ── Manual override ──

  // 「只看未标记」开启时重算视图（剔除已标记卡片），否则只重绘可视窗口
  _refreshGrid() {
    if (document.getElementById('ps-unmarked-only')?.checked) this.renderGrid();
    else this._renderVisible();
  },

  setManual(idx, keep) {
    // idx is into _lightboxArr (sorted view); resolve to the actual result object
    const r = this._lightboxArr && this._lightboxArr[idx];
    if (!r) return;
    const realIdx = this.results.indexOf(r);
    if (realIdx < 0) return;
    const prevKeep = this.results[realIdx].manual_keep;
    this.results[realIdx].manual_keep = keep;
    const path = r.path;
    window.api.sendCommand('set_manual_keep', { path, keep }).catch(() => {
      // Revert UI on failure so it doesn't show stale state
      this.results[realIdx].manual_keep = 0;
      this.recomputeSelection();
      this._refreshGrid();
    });
    this.recomputeSelection();
    this._refreshGrid();
    if (!document.getElementById('ps-lightbox').classList.contains('hidden')) this._refreshLightboxButtons();
    // Push undo action
    const labels = { 1: t('psKeep'), '-1': t('psExclude'), 0: t('peReset') };
    const label = t('psMarkLabel', { name: r.filename, action: labels[String(keep)] || t('psModified') });
    const self = this;
    App.pushUndo(label, async () => {
      self.results[realIdx].manual_keep = prevKeep;
      await window.api.sendCommand('set_manual_keep', { path, keep: prevKeep });
      self.recomputeSelection();
      self._refreshGrid();
    });
  },

  // 标记并自动跳到下一张未标记（键盘筛片流核心）；全部标记完给出提示
  _markAndAdvance(idx, keep) {
    const arr = this._lightboxArr;
    if (!arr || !arr.length) return;
    this.setManual(idx, keep);
    const n = arr.length;
    // 从当前位置向后找下一张未标记（manual_keep 0/undefined），绕场一周
    for (let step = 1; step <= n; step++) {
      const j = (idx + step) % n;
      if (!arr[j].manual_keep) {
        this._lightboxIndex = j;
        this._renderLightbox();
        return;
      }
    }
    // 没有未标记了：原地停留并提示
    App.toast(t('psAllMarked'), 'success');
  },

  // ── Photo Editor (shared PhotoEdit module) ──

  _openEditForPS() {
    const r = this._lightboxArr && this._lightboxArr[this._lightboxIndex];
    if (!r || !r.path) return;
    // Build file list from lightbox array for prev/next navigation
    const fileList = (this._lightboxArr || []).map(item => ({
      path: item.path,
      thumb: item.thumb_path || item.thumb,
    }));
    if (typeof Workbench !== 'undefined') {
      Workbench.setPhotos(Workbench.fromResults(this._lightboxArr), { source: 'ai-select', active: r.path });
      if (!Workbench.isSelected(r.path)) Workbench.select(r.path, 'add');
    }
    PhotoEdit.open(r.path, {
      fileList: (typeof Workbench !== 'undefined' && Workbench.photos.length) ? Workbench.getPhotos() : fileList,
      index: this._lightboxIndex,
      onSaved: () => { this._renderLightbox(); },
      onReset: () => { this._renderLightbox(); },
      onClose: () => { document.getElementById('ps-lb-edit').classList.remove('active'); },
    });
    document.getElementById('ps-lb-edit').classList.add('active');
  },

  // ── 输出模式（文件夹 / 星标 LR 工作流）──

  _isTagMode() {
    const el = document.getElementById('ps-mode');
    return el && el.value === 'tag';
  },

  _starThresholds() {
    // 返回 [★5, ★4, ★3, ★2, ★1] 的下限分数；非法输入回退默认值
    const defs = { 5: 8.5, 4: 7.5, 3: 6.5, 2: 5.5, 1: 4.5 };
    const out = [];
    for (let s = 5; s >= 1; s--) {
      const el = document.getElementById('ps-star-' + s);
      let v = el ? parseFloat(el.value) : NaN;
      if (!isFinite(v)) v = defs[s];
      out.push(Math.max(1, Math.min(10, v)));
    }
    return out;
  },

  _ratingFor(score) {
    // 评分(0-10) → LR 星级(0-5)，按用户配置的分数段；低于 ★1 下限 → 0（不打标）
    if (score < 0) return 0;
    const th = this._starThresholds();  // [t5, t4, t3, t2, t1]
    for (let star = 5; star >= 1; star--) {
      if (score >= th[5 - star]) return star;
    }
    return 0;
  },

  _applyModeUI(animate = false) {
    const tag = this._isTagMode();
    const folderPanel = document.getElementById('ps-output-name-group');
    const starPanel = document.getElementById('ps-star-settings');
    folderPanel.classList.toggle('hidden', tag);
    starPanel.classList.toggle('hidden', !tag);
    if (animate) {
      // 新出现的设置块播入场动画（重触发自绘；reduced-motion 由全局 CSS 闸门禁用）
      const incoming = tag ? starPanel : folderPanel;
      incoming.classList.remove('ps-swap-in');
      void incoming.offsetWidth;
      incoming.classList.add('ps-swap-in');
    }
    const label = document.getElementById('ps-apply-label');
    if (label) {
      const key = tag ? 'psApplyStars' : 'psApply';
      label.dataset.i18n = key;
      label.textContent = t(key);
    }
    const icon = document.getElementById('ps-apply-icon');
    if (icon) icon.className = tag ? 'ph ph-star' : 'ph ph-folder-plus';
    // 星标模式下「应用选择」即写 XMP，独立「导出XMP」按钮冗余，隐藏
    const xmpBtn = document.getElementById('btn-export-xmp');
    if (xmpBtn) xmpBtn.classList.toggle('hidden', tag);
    this.renderGrid();  // 星标徽标随模式显隐
  },

  // 自定义输出位置提示行（空 = 照片同目录，隐藏提示）
  _renderOutputBaseHint() {
    const hint = document.getElementById('ps-output-base-hint');
    if (!hint) return;
    document.getElementById('ps-output-base-path').textContent =
      this.outputBase ? t('psOutputBasePrefix', { path: this.outputBase }) : '';
    hint.classList.toggle('hidden', !this.outputBase);
  },

  // ── Apply selection (sync output folder / write star ratings) ──

  async applySelection() {
    if (!this.results.length) return;
    if (this._isTagMode()) return this._applyStarTags();
    const keep = this.results.filter(r => r.selected).map(r => r.path);
    const outputName = document.getElementById('select-output-name').value.trim() || '优秀照片';
    App.toast(t('psSyncing', { count: keep.length, name: outputName }), 'info');
    try {
      const r = await window.api.sendCommand('apply_selection', {
        folder: this.sourceFolder, output_name: outputName, keep_paths: keep,
        output_base: this.outputBase || '',
      });
      App.toast(t('psSynced', { copied: r.copied, removed: r.removed }), 'success');
      this.addLog(t('psApplyLog', { copied: r.copied, removed: r.removed, folder: r.output_folder }), 'success');
    } catch (e) { App.toast(t('psApplyFail', { msg: e.message }), 'error'); }
  },

  // 星标模式：按分数段写 XMP 星标；落选/排除的清除 LensPilot sidecar（同步语义）
  async _applyStarTags() {
    const items = [];
    const clearPaths = [];
    for (const r of this.results) {
      if (r.score < 0) continue;  // 评分失败的不动
      if (r.manual_keep === -1) { clearPaths.push(r.path); continue; }  // 手动排除 → 不打标
      let rating = this._ratingFor(r.score);
      if (rating === 0 && r.manual_keep === 1) rating = 1;  // 手动保留保底 ★1
      if (rating > 0) items.push({ path: r.path, rating });
      else clearPaths.push(r.path);
    }
    if (!items.length && !clearPaths.length) { App.toast(t('psXmpNone'), 'warning'); return; }
    try {
      const r = await window.api.sendCommand('export_xmp', { items, clear_paths: clearPaths });
      const failed = (r.errors || []).length;
      App.toast(t('psStarsDone', { written: r.written, cleared: r.cleared || 0, failed }), failed ? 'warning' : 'success');
      this.addLog(t('psStarsDone', { written: r.written, cleared: r.cleared || 0, failed }), failed ? 'warn' : 'success');
    } catch (e) { App.toast(t('psXmpFail', { msg: e.message }), 'error'); }
  },

  // ── 二次筛选（LR 修图后）：导入修图结果 ──

  /** 扫描 Lightroom 写回的 XMP，识别已修图照片并加载进网格做终选。
   *  未评分的新文件（LR 重导出）以占位项补进，重跑「开始筛选」走缓存秒出。 */
  async importRetouched() {
    if (!this.sourceFolder) { App.toast(t('psNeedFolder'), 'warning'); return; }
    const btn = document.getElementById('btn-import-retouched');
    const orig = btn ? btn.innerHTML : '';
    if (btn) { btn.disabled = true; btn.innerHTML = t('psImporting'); }
    try {
      const r = await window.api.sendCommand('scan_retouched', { folder: this.sourceFolder });
      if (r && r.error) throw new Error(r.error);
      // 持久保存元数据映射（重评/重开缓存后继续生效）
      this._round2Meta = {};
      (r.results || []).forEach(it => {
        this._round2Meta[it.path] = {
          xmp_rating: it.xmp_rating || 0, xmp_label: it.xmp_label || '',
          developed: !!it.developed, modified: !!it.modified,
        };
      });
      // 把修图信息挂到已有 results；未在 results 的新文件（LR 重导出）补占位项
      const _RAW_LIKE = new Set(['heic','heif','arw','cr2','cr3','crw','nef','nrw','raf','rw2','orf','pef','dng','srw','x3f','raw','iiq','3fr','mef','mos','mrw','erf','dcr','kdc']);
      const seen = new Set();
      this.results.forEach(x => {
        const m = this._round2Meta[x.path];
        if (!m) return;
        x.xmp_rating = m.xmp_rating; x.xmp_label = m.xmp_label;
        x.developed = m.developed; x.modified = m.modified;
        seen.add(x.path);
      });
      Object.entries(this._round2Meta).forEach(([p, m]) => {
        if (seen.has(p)) return;
        const ext = (p.split('.').pop() || '').toLowerCase();
        this.results.push({
          path: p, filename: p.split(/[\\/]/).pop(), score: -1,
          is_raw: _RAW_LIKE.has(ext), thumb_path: '', manual_keep: 0, selected: false,
          xmp_rating: m.xmp_rating, xmp_label: m.xmp_label,
          developed: m.developed, modified: m.modified,
        });
      });
      const count = (r.counts && r.counts.retouched) || 0;
      this.round2 = count > 0;
      if (this.round2) {
        // 标记已进入终选（供自动化「终选后上传」判断）
        try { localStorage.setItem('automation_deliver_round2_' + encodeURIComponent(this.sourceFolder), new Date().toISOString()); } catch (_) {}
      }
      if (count > 0) {
        App.toast(t('psRound2Found', { count }), 'success');
        this.addLog(`${t('psRound2Found', { count })} (修改 ${r.counts.modified || 0} / LR 开发 ${r.counts.developed || 0})`, 'success');
      } else {
        App.toast(t('psRound2None'), 'warning');
        this.addLog(t('psRound2None'), 'warn');
      }
      this.renderGrid();
    } catch (e) {
      this.addLog(t('psRound2Fail', { msg: e.message }), 'error');
      App.toast(t('psRound2Fail', { msg: e.message }), 'error');
    } finally {
      if (btn) { btn.disabled = false; btn.innerHTML = orig; }
    }
  },

  // ── XMP 导出（Lightroom / Camera Raw / Bridge 互通）──

  async exportXmp() {
    // 评分 → LR 星级(0-5) 按用户配置的分数段；手动保留 → 绿色标签，排除 → 红色标签
    const items = [];
    for (const r of this.results) {
      const rating = this._ratingFor(r.score);
      const label = r.manual_keep === 1 ? 'Green' : r.manual_keep === -1 ? 'Red' : '';
      if (!rating && !label) continue;
      items.push({ path: r.path, rating, label });
    }
    if (!items.length) { App.toast(t('psXmpNone'), 'warning'); return; }
    try {
      const r = await window.api.sendCommand('export_xmp', { items });
      const failed = (r.errors || []).length;
      App.toast(t('psXmpDone', { written: r.written, failed }), failed ? 'warning' : 'success');
      this.addLog(t('psXmpLog', { written: r.written, failed }), failed ? 'warn' : 'success');
    } catch (e) { App.toast(t('psXmpFail', { msg: e.message }), 'error'); }
  },

  // ── Batch Export ──

  async showExportDialog() {
    const view = this._lightboxArr || this.results;
    if (!view.length) { App.toast(t('psNoExportPhotos'), 'warning'); return; }

    const sel = view.filter(r => r.selected);
    const paths = (sel.length ? sel : view).map(r => r.path);
    const count = paths.length;

    const fmt = await Modal.showInput(t('psExportTitle'), t('psExportFormatMsg', { count }));
    if (!fmt) return;
    const format = (fmt.trim().toLowerCase()) || 'jpeg';
    if (!['jpeg', 'png', 'webp'].includes(format)) {
      App.toast(t('psExportBadFormat'), 'warning'); return;
    }

    const qualityStr = await Modal.showInput(t('psQualityTitle'), t('psQualityMsg'));
    const quality = parseInt(qualityStr) || 85;

    const sizeStr = await Modal.showInput(t('psSizeTitle'), t('psSizeMsg'));
    const maxEdge = parseInt(sizeStr) || 0;

    const tmpl = await Modal.showInput(t('psNamingTitle'), t('psNamingMsg'));
    const nameTemplate = tmpl.trim() || '{original}';

    await this._doExport(paths, format, quality, maxEdge, nameTemplate);
  },

  async _doExport(paths, format, quality, maxEdge, nameTemplate) {
    const btn = document.getElementById('btn-export');
    btn.disabled = true;
    btn.textContent = t('psExportingBtn');
    UI.updateProgress('score-progress-fill', 'score-progress-text', 0, t('psPreparingExport'));
    try {
      const r = await window.api.sendCommand('export_photos', {
        paths, format, quality, max_edge: maxEdge || null,
        name_template: nameTemplate,
        output_folder: (this.sourceFolder || '') + '/_exported',
      });
      UI.updateProgress('score-progress-fill', 'score-progress-text', 100, t('psExportDone'));
      App.toast(t('psExportDoneToast', { count: r.exported, folder: r.output_folder }), 'success');
      this.addLog(t('psExportLog', { count: r.exported, format, quality, folder: r.output_folder }), 'success');
      if (r.failed > 0) {
        this.addLog(t('psExportFailedCount', { count: r.failed }), 'warn');
      }
      if (r.errors && r.errors.length) {
        r.errors.forEach(e => this.addLog(`  ⚠ ${e}`, 'warn'));
      }
      // Open the export folder
      window.api.openFolder(r.output_folder);
    } catch (e) {
      App.toast(t('peExportFail') + e.message, 'error');
      this.addLog(t('peExportFail') + e.message, 'error');
    }
    btn.disabled = (this.results && this.results.length === 0);
    btn.textContent = t('psExport');
  },

  // ── Lightbox ──

  openLightbox(arr, idx) {
    this._lightboxArr = arr;
    this._lightboxIndex = idx;
    this._renderLightbox();
    document.getElementById('ps-lightbox').classList.remove('hidden');
    if (typeof Workbench !== 'undefined' && arr[idx]) Workbench.setActive(arr[idx].path);
  },

  _renderLightbox() {
    const r = this._lightboxArr[this._lightboxIndex];
    if (!r) return;
    // RAW/HEIC 原图 Chromium 解不了（local-image:// 直返原始字节）→ 优先用后端缩略图；
    // JPEG 等可解码格式仍用原图（local-image:// 直读本地文件），缩略图仅作兜底，
    // 否则 260px 缩略图在大屏上会非常小。
    const ext = (r.path || '').split('.').pop().toLowerCase();
    const RAW_EXTS = new Set(['heic','heif','arw','cr2','cr3','nef','nrw','raf','rw2','orf',
      'pef','dng','srw','x3f','raw','iiq','3fr','mef','mos','mrw','erf','dcr','kdc']);
    const src = (RAW_EXTS.has(ext) && r.thumb_path) ? r.thumb_path : (r.path || r.thumb_path);
    document.getElementById('ps-lb-img').src = this.thumbUrl(src);
    document.getElementById('ps-lb-name').textContent = r.filename;
    const sc = document.getElementById('ps-lb-score');
    sc.textContent = r.score >= 0 ? t('psScoreValue', { score: r.score.toFixed(1) }) : t('psScoreFailed');
    sc.className = 'score-badge ' + this.scoreClass(r.score);
    // 切图时重置热力图态：若已缓存则沿用，否则隐藏
    const heatImg = document.getElementById('ps-lb-heat-img');
    const heatBtn = document.getElementById('ps-lb-heat');
    const cached = r.path && this._virt.heatCache[r.path];
    if (cached) {
      heatImg.src = this.thumbUrl(cached);
      heatImg.classList.remove('hidden');
      heatBtn.classList.add('active');
    } else {
      heatImg.classList.add('hidden');
      heatImg.src = '';
      heatBtn.classList.remove('active');
    }
    this._refreshLightboxButtons();
  },

  _refreshLightboxButtons() {
    const r = this._lightboxArr[this._lightboxIndex];
    if (!r) return;
    document.getElementById('ps-lb-keep').classList.toggle('btn-primary', r.manual_keep !== 1);
    document.getElementById('ps-lb-reject').classList.toggle('btn-danger', r.manual_keep !== -1);
  },

  moveLightbox(delta) {
    const n = this._lightboxArr.length;
    if (!n) return;
    this._lightboxIndex = (this._lightboxIndex + delta + n) % n;
    if (typeof Workbench !== 'undefined') {
      const r = this._lightboxArr[this._lightboxIndex];
      if (r) Workbench.setActive(r.path);
    }
    this._renderLightbox();
  },

  closeLightbox() {
    document.getElementById('ps-lightbox').classList.add('hidden');
    document.getElementById('ps-lb-img').src = '';
    document.getElementById('ps-lb-heat-img').src = '';
    document.getElementById('ps-lb-heat-img').classList.add('hidden');
    document.getElementById('ps-lb-edit').classList.remove('active');
    this._refreshWorkbenchLightbox();
  },

  // ── Heatmap (explainability) ──

  async _fetchHeatmap(path) {
    if (!path) return '';
    if (path in this._virt.heatCache) return this._virt.heatCache[path];
    if (this._virt.heatLoading.has(path)) return '';
    this._virt.heatLoading.add(path);
    try {
      const r = await window.api.sendCommand('get_heatmap', { path });
      const hp = (r && r.heatmap_path) || '';
      this._virt.heatCache[path] = hp; // '' 也缓存，避免重复请求不可用的模型
      return hp;
    } catch (e) {
      return '';
    } finally {
      this._virt.heatLoading.delete(path);
    }
  },

  async toggleCardHeatmap(path, card) {
    const hp = await this._fetchHeatmap(path);
    if (!hp) { App.toast(t('psHeatmapUnavailable'), 'warning'); return; }
    let overlay = card.querySelector('.ps-card-heat-img');
    const btn = card.querySelector('.ps-card-heat-btn');
    if (overlay) {
      // 已显示 → 关闭
      overlay.remove();
      if (btn) btn.classList.remove('active');
    } else {
      overlay = document.createElement('img');
      overlay.className = 'ps-card-heat-img';
      overlay.src = this.thumbUrl(hp);
      card.insertBefore(overlay, card.firstChild);
      if (btn) btn.classList.add('active');
    }
  },

  async toggleLightboxHeatmap() {
    const r = this._lightboxArr[this._lightboxIndex];
    if (!r) return;
    const heatImg = document.getElementById('ps-lb-heat-img');
    const heatBtn = document.getElementById('ps-lb-heat');
    if (!heatImg.classList.contains('hidden')) {
      heatImg.classList.add('hidden');
      heatImg.src = '';
      heatBtn.classList.remove('active');
      return;
    }
    heatBtn.classList.add('active');
    heatBtn.disabled = true;
    const hp = await this._fetchHeatmap(r.path);
    heatBtn.disabled = false;
    if (!hp) {
      heatBtn.classList.remove('active');
      App.toast(t('psHeatmapUnavailable'), 'warning');
      return;
    }
    heatImg.src = this.thumbUrl(hp);
    heatImg.classList.remove('hidden');
  },

  // ── Utilities ──

  async applyWatermark() {
    if (!this.sourceFolder) { App.toast(t('psNeedFolder'), 'warning'); return; }
    const p = JSON.parse(localStorage.getItem('prefs_' + (App.user?.id || 'default')) || '{}');
    const wmText = p.wmText || '';
    const wmImage = p.wmImage || '';

    // First use: prompt to configure watermark
    if (!wmText && !wmImage) {
      App.toast(t('psGotoWatermarkSettings'), 'warning');
      App.setPage('settings');
      const wmInput = document.getElementById('setting-wm-text');
      if (wmInput) setTimeout(() => wmInput.focus(), 300);
      return;
    }

    // Immediate feedback: disable button, show status
    const btn = document.getElementById('btn-add-watermark');
    const origHTML = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = t('psProcessingHtml');
    document.getElementById('status-bar').textContent = t('psAddingWatermark');

    try {
      const r = await window.api.sendCommand('list_images_in_folder', { folder: this.sourceFolder });
      if (!r || !r.images || !r.images.length) { App.toast(t('psNoImagesInFolder'), 'warning'); return; }
      const imagePaths = r.images.map(f => typeof f === 'string' ? f : f.path);
      const wmResult = await window.api.sendCommand('apply_watermark', {
        file_paths: imagePaths,
        text: wmText || '© LensPilot',
        image_path: wmImage || '',
        position: p.wmPos || 'bottom-right',
        opacity: (p.wmOpacity || 30) / 100,
        font_size: p.wmFontSize || 36,
      });
      this.addLog(t('psWatermarkDoneLog', { count: wmResult.processed }), 'success');
      App.toast(t('psWatermarkDoneToast', { count: wmResult.processed }), 'success');
      document.getElementById('status-bar').textContent = t('psWatermarkDoneStatus', { count: wmResult.processed });
    } catch (e) {
      this.addLog(t('psWatermarkFail', { msg: e.message }), 'warn');
      App.toast(t('psWatermarkFail', { msg: e.message }), 'error');
      document.getElementById('status-bar').textContent = t('psWatermarkFailedStatus');
    } finally {
      btn.disabled = false;
      btn.innerHTML = origHTML;
    }
  },

  addLog(message, level = 'info') { UI.addLog('score-log', message, level); },

  // ── Model Download Progress ──
  _downloadTotal: 0,
  onModelDownloadProgress(data) {
    if (!this._downloadTotal && data.total) this._downloadTotal = data.total;
    const pct = data.pct || 0;
    const mb = data.downloaded ? (data.downloaded / 1048576).toFixed(0) : 0;
    const totalMb = this._downloadTotal ? (this._downloadTotal / 1048576).toFixed(0) : '?';
    document.getElementById('model-status-text').textContent = t('psDownloading', { file: data.file || t('psModel'), mb, totalMb, pct });
    UI.updateProgress('score-progress-fill', 'score-progress-text', pct, t('psDownloadingPct', { pct }));
    document.getElementById('btn-start-scoring').disabled = true;
  },
  onModelDownloadError(data) {
    if (data && data.file) this.addLog(t('psDownloadRetry', { file: data.file, msg: data.error || '' }), 'warn');
  },
  onModelDownloadDone(data) {
    this._downloadTotal = 0;
    document.getElementById('model-status-text').textContent = t('psDownloadDoneLoading');
    UI.updateProgress('score-progress-fill', 'score-progress-text', 100, t('psDownloadDone'));
  },
};
