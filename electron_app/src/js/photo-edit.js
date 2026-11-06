/**
 * photo-edit.js — Universal self-contained photo editor.
 *
 * One overlay with large preview on the left + edit sliders on the right.
 * Manages its own preview image and prev/next navigation.
 *
 * Usage:
 *   // Simple: edit a single file
 *   PhotoEdit.open('/path/to/photo.jpg')
 *
 *   // With file list for prev/next navigation:
 *   PhotoEdit.open('/path/to/photo.jpg', { fileList: [...], index: 3 })
 *
 *   // Callbacks (all optional):
 *   PhotoEdit.open(path, {
 *     fileList: [...], index: 0,
 *     onSaved: () => {}, onReset: () => {}, onClose: () => {},
 *   })
 *
 *   PhotoEdit.close();
 */

const PhotoEdit = {
  _path: '',
  _edits: {},
  _debounce: null,
  _callbacks: {},
  _bound: false,
  _presets: [],
  // ── 修改前后对比(分割条) ──
  _baMode: false,
  _baPos: 0.5,
  _beforeSrc: '',
  // ── 调色 (LR) 复合控件状态 ──
  _hsl: {},              // {red: [dh, ds, dl], ...} 仅存非零色域
  _hslDomain: 'red',
  _grading: {},          // {shadows: [hue, sat, lum], ...} 仅存生效区
  _gradingZone: 'shadows',
  // ── 色调曲线(点曲线)状态 ──
  _curves: {},           // {rgb|r|g|b: [[x,y],...]} 仅存非恒等通道
  _curveCh: 'rgb',
  _curveDrag: null,      // 拖动中的控制点索引
  _curveJustAdded: -1,   // 刚新建的点(双击防误删)
  // ── 局部调整蒙版(P1-3) ──
  _masks: [],            // [{id,type,invert,amount,params,adjustments}]
  _maskSel: -1,
  _maskMode: null,       // null|draw-linear|draw-radial|paint|pick-color
  _maskSeq: 0,
  _maTmp: null,          // 绘制中的临时几何
  _maskCompSel: -1,      // 选中的蒙版组件索引
  _maskModeTarget: 'new',// 创建目标: new=新蒙版 comp=选中蒙版的新组件
  // ── 污点修复/仿制/红眼(P4-2) ──
  _spots: [],            // [{id,mode,x,y,r,sx?,sy?}] mode: heal|clone|redeye
  _spotSel: -1,
  _spotKind: 'heal',
  _spotOn: false,
  _spotRadius: 18,       // 显示像素半径（写入时换算为相对短边比例）
  _spotDrag: null,       // {idx, src}
  // ── 历史(P4-4，会话级；持久审计走 edit_journal) ──
  _hist: {},             // path -> [{ts, label, edits, json}]
  // ── 几何 / 裁剪 / 缩放 ──
  _imgW: 0, _imgH: 0,    // 原图(或 RAW 内嵌预览)尺寸,裁剪坐标换算用
  _cropMode: false,
  _cropRect: null,       // {x,y,w,h} 显示坐标(相对裁剪层)
  _cropRatio: 'free',
  _zoom: false,
  _zoomScale: 1,          // 相对“适应窗口”的倍数（1 = fit）
  _zoomBaseW: 0, _zoomBaseH: 0, // 进入缩放时的适应尺寸（冻结）
  _zoomPreviewTimer: null,
  _ZOOM_DETAIL_MAX: 6000, // 高倍/1:1 细节预览的最长边（按需解码 + LRU）
  _tileTimer: null, _tileToken: 0, _tileRegion: '', _tilePending: '', _tileEditsJson: '',
  _panX: 0, _panY: 0,
  _holdOrig: false,      // 按住 \ 看原图
  // ── 交互契约：自动保存 / 撤销 / 预览节流 ──
  _undoStacks: {},       // path -> [edits snapshot]
  _redoStacks: {},       // path -> [edits snapshot]
  _lastCommitted: {},    // 上一次提交的 edits
  _lastCommittedJson: '',
  _lastSavedJson: '',
  _commitTimer: null,
  _autoSaveTimer: null,
  _previewBusy: false,
  _previewPending: false,
  _previewToken: 0,
  _lastPreviewMaxSide: 0,
  _viewportResizeTimer: null,
  _restoring: false,
  _closing: false,
  _workspaceMode: false,
  _saveErrorNoted: false,
  _lastTool: 'edit',
  _maskBrushR: 0.06,     // 蒙版笔刷半径（相对短边）
  _soloSection: '',      // '' | lr | mask | hist | vc
  _panelOrder: ['lr', 'mask', 'hist', 'vc'],
  _dragSection: '',
  _suppressSectionClickUntil: 0,
  // ── 图表(示波器)：折叠 + 悬浮/吸附到左侧工作区 ──
  _scopeOpen: { hist: true, wave: false, parade: false, vector: false },
  _scopePlace: { hist: 'inline', wave: 'inline', parade: 'inline', vector: 'inline' },
  _scopePos: {},          // kind -> {x, y}（相对 .pe-preview）
  _scopeDrag: null,
  _scopesReady: false,
  _scopeTmp: null,
  _scopeMenuKind: '',
  _scopeDefs: [
    { kind: 'hist', titleKey: 'peScopeHistogram', icon: 'ph-chart-bar' },
    { kind: 'wave', titleKey: 'peScopeWaveform', icon: 'ph-waveform' },
    { kind: 'parade', titleKey: 'peScopeParade', icon: 'ph-columns' },
    { kind: 'vector', titleKey: 'peScopeVectorscope', icon: 'ph-crosshair' },
  ],

  // ── File list for prev/next navigation ──
  _fileList: [],        // [{path, thumb?, ext?}] or simple string paths
  _fileIndex: -1,
  // ── 预设(部分合并 + Amount) ──
  _presetBase: null,     // 应用预设前的编辑快照(Amount 实时重算基准)
  _presetApplied: null,  // 当前生效的预设 id
  _presetResolvedEdits: null, // 实际套用的参数（固定预设=缩放后 edits；参考预设=逐张 fit 结果）
  _presetFitToken: 0,
  _presetAmountTimer: null,
  _presetAmountToken: 0,
  _presetCat: 'all',     // 卡片墙分类筛选
  _presetThumbs: {},     // preset id -> data URL（当前照片的卡片缩略图）
  _presetThumbToken: 0,
  _presetThumbTimer: null,
  _presetHoverId: null,
  _presetHoverTimer: null,
  _presetHoverToken: 0,
  _presetThumbBusy: false,
  _presetThumbPending: false,
  _presetSaving: false,
  _editsLoaded: false,

  // Extensions that Chromium can't decode — need backend JPEG preview
  _RAW_LIKE_EXTS: new Set([
    'heic','heif',
    'arw','srf','sr2','arq','cr2','cr3','crw','nef','nrw',
    'raf','rw2','orf','ori','pef','ptx','raw','rwl','rwz',
    'dng','srw','x3f','gpr','iiq','cap','eip','3fr','fff',
    'mef','mos','mrw','mdc','erf','dcr','kdc','k25','dcs',
    'dc2','bay','cs1','sti',
  ]),

  /** Open the universal editor for a photo.
   *  @param {string} path — absolute file path
   *  @param {object} [opts]
   *    fileList: array of {path,thumb?,ext?} or string paths for prev/next
   *    index: current index in fileList
   *    onSaved / onReset / onClose / onExport: lifecycle callbacks
   */
  // 全部编辑参数默认值（含 PhotoS 增强键）
  _defaultEdits() {
    return {
      exposure: 0, contrast: 1, saturation: 1, warmth: 0,
      shadows: 0, highlights: 0, sharpness: 0,
      wb_temp: 0, wb_reference: '', lut_file: '', log_curve: '',
      denoise: 0, auto_levels: false, auto_exposure: 0,
      grayscale: false, sepia: false, srgb: false,
      auto_straighten: false,
      // ── 调色 (LR) ──
      vibrance: 0, wb_tint: 0, clarity: 0, texture: 0, dehaze: 0,
      levels: null, curves: null, hsl: null, color_grading: null,
      calibration: null,
      masks: null, spots: null, redeye: null, optics: null,
      vignette: null, grain: null, rotate_deg: 0,
    };
  },

  open(path, opts = {}) {
    this._path = path;
    this._callbacks = opts;
    this._closing = false;
    this._bind();
    this._zoomFit();

    // Set up file list for navigation (Workbench is the shared context when no explicit list)
    const rawList = (opts.fileList && opts.fileList.length)
      ? opts.fileList
      : ((typeof Workbench !== 'undefined') ? Workbench.getPhotos() : []);
    this._fileList = rawList.map(f => (typeof f === 'string' ? { path: f } : f));
    if (!this._fileList.length) this._fileList = [{ path }];
    this._fileIndex = (opts.index != null) ? opts.index : this._fileList.findIndex(f => f.path === path);
    if (this._fileIndex < 0) this._fileIndex = 0;

    // Workbench context: never let an editor session exist outside it
    if (typeof Workbench !== 'undefined') {
      if (!Workbench.has(path)) Workbench.addPhotos(this._fileList, { active: path });
      if (!Workbench.getSelected().length) Workbench.select(path, 'add');
      Workbench.setActive(path);
    }

    // A fresh session starts with a clean undo/redo stack for this photo
    this._undoStacks[path] = [];
    this._redoStacks[path] = [];
    this._lastCommitted = {};
    this._lastCommittedJson = '';
    this._lastSavedJson = '';
    this._saveErrorNoted = false;
    this._lastPreviewMaxSide = 0;
    clearTimeout(this._viewportResizeTimer);

    // Reset sliders to defaults
    this._setSliders(this._defaultEdits());
    this._editsLoaded = false;
    this._presetBase = null;
    this._presetApplied = null;
    this._presetResolvedEdits = null;
    this._presetThumbs = {};
    this._presetThumbToken++;
    this._presetHoverId = null;
    this._presetHoverToken++;
    this._presetFitToken++;
    this._presetAmountToken++;
    clearTimeout(this._presetThumbTimer);
    clearTimeout(this._presetHoverTimer);
    clearTimeout(this._presetAmountTimer);
    this._renderPresetWall();

    // Show overlay immediately so user sees something
    const overlay = document.getElementById('photo-edit-overlay');
    overlay.classList.remove('hidden');
    this._workspaceMode = !!opts.workspace;
    overlay.classList.toggle('pe-workspace', this._workspaceMode);
    this._setSaveStatus('peAutoSave');
    this._soloSection = '';
    this._restorePanelOrder();
    this._setTool('edit', { silent: true });
    this._restorePanelState();
    this._renderFilmstrip();
    this._updateWorkspaceBar();
    this._restoreScopeState();

    // Load saved edits + initial preview + image info + presets in parallel
    this._loadEdits(path);
    this._loadInitialPreview(path);
    this._loadColorSpace(path);
    this._loadPresets();
    this._updateNavUI();
    this._refreshBatchScope();
  },

  /** Open the editor as a full task-page workspace (nav: 选片 → 编辑工作区). */
  openWorkspace(path) {
    const wb = (typeof Workbench !== 'undefined') ? Workbench.getPhotos() : [];
    const target = path || this._path || (typeof Workbench !== 'undefined' ? Workbench.getActive() : '') ||
      (wb[0] && wb[0].path);
    if (!target) return false;
    const overlay = document.getElementById('photo-edit-overlay');
    if (this._path === target && overlay && !overlay.classList.contains('hidden')) {
      this._setWorkspaceMode(true);
      return true;
    }
    const index = wb.findIndex(f => f.path === target);
    this.open(target, { fileList: wb.length ? wb : [target], index: Math.max(0, index), workspace: true });
    return true;
  },

  _setWorkspaceMode(on) {
    this._workspaceMode = !!on;
    const overlay = document.getElementById('photo-edit-overlay');
    if (overlay) overlay.classList.toggle('pe-workspace', this._workspaceMode);
    if (this._workspaceMode) this._updateWorkspaceBar();
  },

  exitWorkspace() {
    this.close();
    if (typeof App !== 'undefined' && App.currentPage === 'editor-workspace') {
      App.setPage('photo-edit');
    }
  },

  _requestClose() {
    if (this._workspaceMode && typeof App !== 'undefined' && App.currentPage === 'editor-workspace') {
      this.exitWorkspace();
    } else {
      this.close();
    }
  },

  _updateWorkspaceBar() {
    const bar = document.getElementById('pe-workspace-bar');
    if (!bar) return;
    const nameEl = document.getElementById('pe-ws-name');
    const countEl = document.getElementById('pe-ws-count');
    const selEl = document.getElementById('pe-ws-selected');
    const cur = this._fileList[this._fileIndex];
    if (nameEl) nameEl.textContent = cur ? (cur.name || (cur.path || '').split(/[\\/]/).pop()) : '';
    if (countEl) {
      countEl.textContent = this._fileList.length > 1
        ? t('peWsCount', { current: this._fileIndex + 1, total: this._fileList.length })
        : t('peWsSingle');
    }
    if (selEl) {
      const n = (typeof Workbench !== 'undefined') ? Workbench.getSelected().length : 0;
      selEl.classList.toggle('hidden', n === 0);
      if (n) selEl.textContent = t('peWsSelected', { count: n });
    }
  },

  async close() {
    if (this._closing) return;
    this._closing = true;
    clearTimeout(this._debounce);
    clearTimeout(this._commitTimer);
    clearTimeout(this._autoSaveTimer);
    // Auto-save contract: closing must never lose an adjustment.
    this._commitChange();
    await this._flushSave();
    const overlay = document.getElementById('photo-edit-overlay');
    overlay.classList.add('hidden');
    overlay.classList.remove('pe-workspace');
    this._workspaceMode = false;
    this._path = '';
    const onClose = this._callbacks.onClose;
    this._callbacks = {};
    this._edits = {};
    this._fileList = [];
    this._fileIndex = -1;
    if (this._cropMode) this._exitCrop(true);
    if (this._zoom) this._zoomFit();
    if (this._holdOrig) this._holdOriginal(false);
    this._imgW = 0; this._imgH = 0;
    this._curves = {}; this._curveCh = 'rgb';
    this._curveDrag = null; this._curveJustAdded = -1;
    this._masks = []; this._maskSel = -1; this._maskExitMode();
    this._spots = []; this._spotSel = -1; this._spotToggle(false);
    this._presetBase = null; this._presetApplied = null;
    this._presetResolvedEdits = null;
    this._presetThumbs = {};
    this._presetThumbToken++;
    this._presetHoverId = null;
    this._presetHoverToken++;
    this._presetFitToken++;
    this._presetAmountToken++;
    clearTimeout(this._presetThumbTimer);
    clearTimeout(this._presetHoverTimer);
    clearTimeout(this._presetAmountTimer);
    if (this._baMode) this._toggleBA();
    this._beforeSrc = '';
    this._baPos = 0.5;
    this._lastCommitted = {};
    this._lastCommittedJson = '';
    this._lastSavedJson = '';
    this._lastPreviewMaxSide = 0;
    clearTimeout(this._viewportResizeTimer);
    this._renderFilmstrip();
    document.getElementById('pe-preview-img').src = '';
    document.getElementById('pe-before-img').src = '';
    this._closing = false;
    if (onClose) onClose();
  },

  _bind() {
    if (this._bound) return;
    this._bound = true;
    const $ = id => document.getElementById(id);

    $('pe-close').addEventListener('click', () => this._requestClose());
    // Close on backdrop click
    document.querySelector('.pe-backdrop').addEventListener('click', () => this._requestClose());
    $('pe-ws-exit')?.addEventListener('click', () => this.exitWorkspace());
    $('pe-ws-cmd')?.addEventListener('click', () => {
      if (typeof CommandPalette !== 'undefined') CommandPalette.open();
    });
    $('pe-auto').addEventListener('click', () => this._autoEnhance());
    $('pe-ai-tone').addEventListener('click', () => this._autoTone());
    $('pe-reset').addEventListener('click', () => this._reset());
    $('pe-save').addEventListener('click', () => this._save());
    $('pe-export').addEventListener('click', () => this._export());
    $('pe-undo').addEventListener('click', () => this._undo());
    $('pe-redo').addEventListener('click', () => this._redo());

    // Persistent tool rail (mode is a first-class state, not a scroll position)
    $('pe-toolrail').addEventListener('click', (e) => {
      const b = e.target.closest('.pe-tool');
      if (b) this._setTool(b.dataset.tool);
    });

    // Filmstrip is the editor's shared context, not a modal page queue
    $('pe-filmstrip').addEventListener('click', (e) => {
      const cell = e.target.closest('.pe-film-cell');
      if (!cell) return;
      const mode = e.shiftKey ? 'range' : ((e.ctrlKey || e.metaKey) ? 'toggle' : 'replace');
      this._navigateTo(parseInt(cell.dataset.i, 10), { select: mode });
    });

    $('pe-mask-brush-r').addEventListener('input', () => {
      this._maskBrushR = Math.max(0.01, Math.min(0.2, (parseInt($('pe-mask-brush-r').value, 10) || 6) / 100));
      $('pe-mask-brush-r-val').textContent = $('pe-mask-brush-r').value;
    });

    // Panel layout memory: section open/closed + scroll position
    document.querySelector('.pe-body')?.addEventListener('click', (e) => {
      if (Date.now() < this._suppressSectionClickUntil) return;
      if (e.target.closest('[data-drag]')) { e.stopPropagation(); return; }
      const solo = e.target.closest('[data-solo]');
      if (solo) {
        e.stopPropagation();
        this._toggleSolo(solo.dataset.solo);
        return;
      }
      const move = e.target.closest('[data-move]');
      if (move) {
        e.stopPropagation();
        const sec = move.closest('[data-section]');
        if (sec) this._moveSection(sec.dataset.section, move.dataset.move);
        return;
      }
      if (e.target.closest('.pe-section-toggle')) this._savePanelState();
    });

    // Drag-to-reorder sections (handle only; buttons stay clickable)
    const bodyEl = document.querySelector('.pe-body');
    bodyEl?.addEventListener('dragstart', (e) => {
      const handle = e.target.closest('[data-drag]');
      if (!handle) return;
      this._dragSection = handle.dataset.drag;
      const header = handle.closest('.pe-section-toggle');
      if (header) header.classList.add('dragging');
      if (e.dataTransfer) {
        e.dataTransfer.effectAllowed = 'move';
        e.dataTransfer.setData('text/plain', this._dragSection);
      }
    });
    bodyEl?.addEventListener('dragover', (e) => {
      if (!this._dragSection) return;
      const header = e.target.closest('.pe-section-toggle[data-section]');
      if (!header) return;
      e.preventDefault();
      if (e.dataTransfer) e.dataTransfer.dropEffect = 'move';
      const key = header.dataset.section;
      this._clearDropMarks();
      if (key === this._dragSection) return;
      const r = header.getBoundingClientRect();
      header.classList.add(e.clientY < r.top + r.height / 2 ? 'drop-before' : 'drop-after');
    });
    bodyEl?.addEventListener('drop', (e) => {
      if (!this._dragSection) return;
      const header = e.target.closest('.pe-section-toggle[data-section]');
      if (!header) { this._endSectionDrag(); return; }
      e.preventDefault();
      const r = header.getBoundingClientRect();
      this._dropSection(this._dragSection, header.dataset.section,
        e.clientY < r.top + r.height / 2);
      this._endSectionDrag();
    });
    bodyEl?.addEventListener('dragend', () => this._endSectionDrag());
    let scrollTimer = null;
    document.querySelector('.pe-body')?.addEventListener('scroll', () => {
      clearTimeout(scrollTimer);
      scrollTimer = setTimeout(() => this._savePanelState(), 250);
    });

    this._initScopes();

    if (typeof Workbench !== 'undefined') {
      Workbench.onChange(() => {
        this._refreshBatchScope();
        this._refreshFilmstripSelection();
        this._updatePresetBatchUI();
        if (!document.getElementById('photo-edit-overlay').classList.contains('hidden')) {
          this._renderFilmstrip();
        }
        this._updateWorkspaceBar();
      });
    }

    // Last-chance flush if the app is closed while the editor is open.
    window.addEventListener('beforeunload', () => {
      if (!this._path) return;
      this._commitChange();
      try {
        window.api.sendCommand('save_edits', { path: this._path, edits: this._readEdits() });
      } catch (_) { /* best effort */ }
    });

    // Prev/next navigation
    $('pe-prev-btn').addEventListener('click', () => this._navigate(-1));
    $('pe-next-btn').addEventListener('click', () => this._navigate(1));

    const sliders = ['exposure','contrast','saturation','warmth','shadows','highlights','sharpness'];
    sliders.forEach(name => {
      $(`pe-${name}`).addEventListener('input', () => {
        $(`pe-${name}-val`).textContent = this._sliderVal(name);
        this._schedulePreview();
      });
    });

    // ── PhotoS 增强控件 ──
    ['wb-temp','denoise','auto-exposure'].forEach(name => {
      $(`pe-${name}`).addEventListener('input', () => {
        $(`pe-${name}-val`).textContent = this._sliderVal(name);
        this._schedulePreview();
      });
    });
    $('pe-log-curve').addEventListener('change', () => this._schedulePreview());
    ['pe-auto-levels','pe-auto-straighten','pe-grayscale','pe-sepia','pe-srgb'].forEach(id => {
      $(id).addEventListener('change', () => this._schedulePreview());
    });
    $('pe-wb-ref-btn').addEventListener('click', () => this._pickFile('wb_reference'));
    $('pe-wb-ref-clear').addEventListener('click', () => this._clearEdit('wb_reference'));
    $('pe-lut-btn').addEventListener('click', () => this._pickFile('lut_file'));
    $('pe-lut-clear').addEventListener('click', () => this._clearEdit('lut_file'));
    $('pe-refmatch-btn').addEventListener('click', () => this._refMatch());
    // P1-3 参考强度滑杆：只改下次匹配的强度，不触发预览
    $('pe-refmatch-strength').addEventListener('input', () => {
      $('pe-refmatch-strength-val').textContent = $('pe-refmatch-strength').value + '%';
    });
    this._renderRecentRefs();
    $('pe-batch').addEventListener('click', () => this._batchApply());

    // ── 调色 (LR) 折叠分组 ──
    $('pe-lr-toggle').addEventListener('click', () => {
      $('pe-lr-toggle').classList.toggle('open');
      $('pe-lr-wrap').classList.toggle('open');
    });

    // ── 调色 (LR) 单值滑杆 ──
    ['vibrance', 'wb-tint', 'clarity', 'texture', 'dehaze', 'vignette', 'grain',
     'levels-black', 'levels-white', 'levels-gamma',
     'cal-shadows-tint', 'cal-r-hue', 'cal-r-sat', 'cal-g-hue', 'cal-g-sat',
     'cal-b-hue', 'cal-b-sat'].forEach(name => {
      $(`pe-${name}`).addEventListener('input', () => {
        $(`pe-${name}-val`).textContent = this._sliderVal(name);
        this._schedulePreview();
      });
    });

    // ── HSL 分色: 色点切换 + 滑杆写当前色域 ──
    $('pe-hsl-dots').querySelectorAll('.pe-dot').forEach(btn => {
      btn.addEventListener('click', () => {
        this._hslDomain = btn.dataset.domain;
        $('pe-hsl-dots').querySelectorAll('.pe-dot').forEach(b => b.classList.toggle('active', b === btn));
        this._syncHslSliders();
      });
    });
    ['pe-hsl-hue', 'pe-hsl-sat', 'pe-hsl-lum'].forEach(id => {
      $(id).addEventListener('input', () => this._onHslInput(id));
    });

    // ── 颜色分级: 区域切换 + 滑杆写当前区域 ──
    $('pe-cg-zones').querySelectorAll('.pe-zone').forEach(btn => {
      btn.addEventListener('click', () => {
        this._gradingZone = btn.dataset.zone;
        $('pe-cg-zones').querySelectorAll('.pe-zone').forEach(b => b.classList.toggle('active', b === btn));
        this._syncCgSliders();
      });
    });
    ['pe-cg-hue', 'pe-cg-sat', 'pe-cg-lum'].forEach(id => {
      $(id).addEventListener('input', () => this._onCgInput(id));
    });

    // ── 色调曲线: 通道切换 / 重置 / 画布交互 ──
    $('pe-curve-tabs').querySelectorAll('[data-ch]').forEach(btn => {
      btn.addEventListener('click', () => {
        this._curveCh = btn.dataset.ch;
        $('pe-curve-tabs').querySelectorAll('[data-ch]').forEach(b => b.classList.toggle('active', b === btn));
        this._renderCurve();
      });
    });
    $('pe-curve-reset').addEventListener('click', () => this._curveReset());
    $('pe-curve-canvas').addEventListener('pointerdown', (e) => this._onCurvePointerDown(e));
    $('pe-curve-canvas').addEventListener('dblclick', (e) => this._onCurveDblClick(e));

    // ── 局部调整蒙版 (P1-3) ──
    $('pe-mask-toggle').addEventListener('click', () => {
      const opening = !$('pe-mask-wrap').classList.contains('open');
      $('pe-mask-toggle').classList.toggle('open');
      $('pe-mask-wrap').classList.toggle('open');
      this._maskOverlayResize();
      // P5: 首次展开蒙版面板时预热分割模型（缺失则后台下载 4.5MB，一次性）
      if (opening && !this._segmentWarmed) {
        this._segmentWarmed = true;
        window.api.sendCommand('segment_warmup', {}).catch(() => { /* ignore */ });
      }
    });
    $('pe-mask-add').querySelectorAll('[data-mt]').forEach(btn => {
      btn.addEventListener('click', () => this._maskAdd(btn.dataset.mt));
    });
    $('pe-ma-amount').addEventListener('input', () => {
      const m = this._masks[this._maskSel];
      if (!m) return;
      m.amount = (parseInt($('pe-ma-amount').value) || 10) / 10;
      $('pe-ma-amount-val').textContent = m.amount.toFixed(1);
      this._schedulePreview();
    });
    $('pe-ma-invert').addEventListener('change', () => {
      const m = this._masks[this._maskSel];
      if (!m) return;
      m.invert = $('pe-ma-invert').checked;
      this._schedulePreview();
    });
    $('pe-mask-del').addEventListener('click', () => this._maskDelete(this._maskSel));
    $('pe-mask-layer').addEventListener('pointerdown', (e) => this._maskPointerDown(e));
    // 局部调整滑杆(事件委托)
    $('pe-ma-sliders').addEventListener('input', (e) => {
      const row = e.target.closest('.pe-ma-row');
      const m = this._masks[this._maskSel];
      if (!row || !m) return;
      const def = this._maskAdjDef(row.dataset.key);
      if (!def) return;
      const v = (parseInt(e.target.value) || 0) / def.scale;
      if (Math.abs(v - def.def) < 1e-6) delete m.adjustments[def.key];
      else m.adjustments[def.key] = v;
      row.querySelector('.edit-val').textContent = v.toFixed(1);
      this._schedulePreview();
    });
    $('pe-lr-import').addEventListener('click', () => this._importLr());
    this._maskAdjBuild();

    // ── 蒙版组件组合 (P4-1) ──
    $('pe-mc-add').querySelectorAll('[data-mt]').forEach(btn => {
      btn.addEventListener('click', () => this._mcAdd(btn.dataset.mt));
    });
    $('pe-mc-modes').querySelectorAll('[data-m]').forEach(btn => {
      btn.addEventListener('click', () => this._mcSetMode(btn.dataset.m));
    });

    // ── 污点工具 (P4-2) ──
    $('pe-spot-btn').addEventListener('click', () => this._spotToggle());
    $('pe-spot-exit').addEventListener('click', () => this._spotToggle(false));
    $('pe-spot-clear').addEventListener('click', () => {
      this._spots = [];
      this._spotSel = -1;
      this._spotDraw();
      this._schedulePreview();
    });
    $('pe-spot-bar').querySelectorAll('[data-sm]').forEach(btn => {
      btn.addEventListener('click', () => {
        this._spotKind = btn.dataset.sm;
        $('pe-spot-bar').querySelectorAll('[data-sm]').forEach(b => b.classList.toggle('active', b === btn));
      });
    });
    $('pe-spot-radius').addEventListener('input', () => {
      this._spotRadius = parseInt($('pe-spot-radius').value) || 18;
      const s = this._spots[this._spotSel];
      if (s) { s.r = this._spotRadiusNorm(); this._spotDraw(); this._schedulePreview(); }
    });
    $('pe-spot-layer').addEventListener('pointerdown', (e) => this._spotPointerDown(e));
    $('pe-spot-layer').addEventListener('dblclick', (e) => this._spotDblClick(e));

    // ── 历史 (P4-4) ──
    $('pe-hist-toggle').addEventListener('click', () => {
      $('pe-hist-toggle').classList.toggle('open');
      $('pe-hist-wrap').classList.toggle('open');
      if ($('pe-hist-wrap').classList.contains('open')) { this._histRender(); this._journalRefresh(); }
    });
    // ── 命名版本 (P6-1) ──
    $('pe-vc-toggle').addEventListener('click', () => {
      $('pe-vc-toggle').classList.toggle('open');
      $('pe-vc-wrap').classList.toggle('open');
      if ($('pe-vc-wrap').classList.contains('open')) this._vcRefresh();
    });
    $('pe-vc-save').addEventListener('click', () => this._vcSaveCurrent());
    // ── DNG 内嵌镜头档案 (P6-4) ──
    $('pe-optics-dng').addEventListener('click', () => this._importDngProfile());

    // ── 镜头校正 (P4-3) ──
    ['optics-distortion', 'optics-ca', 'optics-vig'].forEach(name => {
      $(`pe-${name}`).addEventListener('input', () => {
        $(`pe-${name}-val`).textContent = $(`pe-${name}`).value;
        this._schedulePreview();
      });
    });

    // ── 数值点击直输(LR 式): 点击 .edit-val → 临时输入框,回车/失焦提交,Esc 取消 ──
    document.querySelector('#photo-edit-overlay .pe-body').addEventListener('click', (e) => {
      const span = e.target.closest('.edit-val');
      if (!span || !span.id || !span.id.endsWith('-val')) return;
      this._openValInput(span);
    });

    // ── 几何工具: 裁剪 / 旋转 / 翻转 / 复制编辑 ──
    $('pe-crop-btn').addEventListener('click', () => {
      if (this._cropMode) this._exitCrop(true); else this._enterCrop();
    });
    $('pe-rotate-l').addEventListener('click', () => this._rotate(-1));
    $('pe-rotate-r').addEventListener('click', () => this._rotate(1));
    $('pe-flip-h').addEventListener('click', () => this._flip('flip_h'));
    $('pe-flip-v').addEventListener('click', () => this._flip('flip_v'));
    $('pe-copy-edits').addEventListener('click', () => this._copyEdits());
    // 水平校正拖动中只预览; 松手(change)时若有裁剪框则清除(画布尺寸已变)
    $('pe-straighten').addEventListener('change', () => this._onGeometryChange());

    // ── 裁剪层交互: 拖新建 / 框内移动 / 四角缩放 ──
    $('pe-crop-layer').addEventListener('pointerdown', (e) => this._onCropPointerDown(e));
    $('pe-crop-ok').addEventListener('click', () => this._applyCrop());
    $('pe-crop-cancel').addEventListener('click', () => this._exitCrop(true));
    document.querySelectorAll('.pe-crop-ratio').forEach(btn => {
      btn.addEventListener('click', () => {
        this._cropRatio = btn.dataset.ratio;
        document.querySelectorAll('.pe-crop-ratio').forEach(b => b.classList.toggle('active', b === btn));
        // 切换比例后按宽适配高
        const rc = this._cropRect, rv = this._ratioValue();
        if (rc && rv) {
          rc.h = rc.w / rv;
          this._clampCropRect();
          this._renderCropBox();
        }
      });
    });

    // ── 缩放 (滑块 + 加减 + 适应/1:1 + 滚轮) ──
    $('pe-zoom-in').addEventListener('click', () => this._zoomStep(0.25));
    $('pe-zoom-out').addEventListener('click', () => this._zoomStep(-0.25));
    $('pe-zoom-range').addEventListener('input', (e) => {
      this._setZoom((parseInt(e.target.value, 10) || 100) / 100);
    });
    $('pe-zoom-fit').addEventListener('click', () => this._zoomFit());
    $('pe-zoom-actual').addEventListener('click', () => this._zoomActual());
    $('pe-img-wrap').addEventListener('wheel', (e) => {
      if (document.getElementById('photo-edit-overlay').classList.contains('hidden')) return;
      if (this._cropMode || this._spotOn || this._maskMode || this._baMode) return;
      e.preventDefault();
      const factor = e.deltaY < 0 ? 1.15 : (1 / 1.15);
      const base = this._zoom ? this._zoomScale : 1;
      this._setZoom(base * factor, e);
    }, { passive: false });
    $('pe-img-wrap').addEventListener('dblclick', (e) => {
      if (this._cropMode || this._baMode) return;
      this._toggleZoom();
    });

    // 预览图每次加载完成: 直方图 + 缩放/裁剪布局刷新
    $('pe-preview-img').addEventListener('load', () => this._onPreviewLoaded());
    // 窗口尺寸变化 → 图表/曲线按新面板宽度重绘(防拉伸模糊)
    window.addEventListener('resize', () => {
      if (!document.getElementById('photo-edit-overlay').classList.contains('hidden')) {
        this._renderScopes();
        this._renderCurve();
        this._layoutDockedScopes();
        this._scopeDefs.forEach(def => {
          if (this._scopePlace[def.kind] === 'float') this._clampFloatingScope(def.kind);
        });
        // 窗口从小变大/变大：重新按新视口请求预览，并重建缩放基准
        clearTimeout(this._viewportResizeTimer);
        this._viewportResizeTimer = setTimeout(() => this._handleViewportResize(), 220);
      }
    });
    // 预设区默认折叠：点标题展开卡片墙（标题上的按钮不触发折叠）
    $('pe-preset-toggle').addEventListener('click', (e) => {
      if (e.target.closest('button')) return;
      this._togglePresetSection();
    });

    // ── 预设卡片墙（悬停预览 + 点击部分合并 + 0-200% 强度） ──
    const presetGrid = $('pe-preset-grid');
    presetGrid.addEventListener('click', (e) => {
      const card = e.target.closest('.pe-preset-card');
      if (!card || !card.dataset.id) return;
      this._applyPreset(card.dataset.id);
      this._updatePresetDelBtn();
    });
    presetGrid.addEventListener('pointerover', (e) => {
      const card = e.target.closest('.pe-preset-card');
      if (card && card.dataset.id) this._presetHoverEnter(card.dataset.id);
    });
    presetGrid.addEventListener('pointerout', (e) => {
      const card = e.target.closest('.pe-preset-card');
      if (!card || card.contains(e.relatedTarget)) return;
      this._presetHoverLeave(card.dataset.id);
    });
    presetGrid.addEventListener('focusin', (e) => {
      const card = e.target.closest('.pe-preset-card');
      if (card && card.dataset.id) this._presetHoverEnter(card.dataset.id);
    });
    presetGrid.addEventListener('focusout', (e) => {
      const card = e.target.closest('.pe-preset-card');
      if (card && card.dataset.id) this._presetHoverLeave(card.dataset.id);
    });
    presetGrid.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        // Let the focused card's native activation apply it; keep the global
        // keymap from also treating Enter/Space as an editor command.
        e.stopPropagation();
        return;
      }
      if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight' &&
          e.key !== 'ArrowUp' && e.key !== 'ArrowDown') return;
      const cards = [...presetGrid.querySelectorAll('.pe-preset-card')];
      const cur = cards.indexOf(document.activeElement);
      if (cur < 0) return;
      const cols = 2;
      const step = (e.key === 'ArrowLeft' || e.key === 'ArrowRight') ? 1 : cols;
      const next = cur + ((e.key === 'ArrowLeft' || e.key === 'ArrowUp') ? -step : step);
      if (cards[next]) { e.preventDefault(); e.stopPropagation(); cards[next].focus(); }
    });
    $('pe-preset-cats').addEventListener('click', (e) => {
      const chip = e.target.closest('[data-cat]');
      if (!chip) return;
      this._presetCat = chip.dataset.cat || 'all';
      this._renderPresetWall();
      this._loadPresetThumbs();
    });
    $('pe-preset-amount').addEventListener('input', () => {
      const raw = parseInt($('pe-preset-amount').value, 10);
      const v = Number.isFinite(raw) ? raw : 100;  // 0 is a valid amount
      $('pe-preset-amount-val').textContent = v + '%';
      this._reapplyPresetAmount(v);
      this._updatePresetBatchUI();
      clearTimeout(this._presetThumbTimer);
      this._presetThumbTimer = setTimeout(() => this._loadPresetThumbs(), 500);
    });
    $('pe-preset-save').addEventListener('click', () => this._savePreset());
    $('pe-preset-from-ref').addEventListener('click', () => this._saveReferencePreset());
    $('pe-preset-xmp-import').addEventListener('click', () => this._importXmpPresets());
    $('pe-preset-xmp-export').addEventListener('click', () => this._exportXmpPreset());
    $('pe-preset-del').addEventListener('click', () => this._deletePreset());
    const adaptiveEl = $('pe-preset-adaptive');
    if (adaptiveEl) {
      adaptiveEl.checked = this._presetAdaptivePref();
      adaptiveEl.addEventListener('change', () => {
        localStorage.setItem('pe_preset_adaptive_v1', adaptiveEl.checked ? '1' : '0');
      });
    }
    $('pe-preset-apply-sel').addEventListener('click', () => this._applyPresetToSelection());

    // ── 修改前后对比 ──
    $('pe-compare').addEventListener('click', () => this._toggleBA());
    $('pe-img-wrap').addEventListener('pointerdown', (e) => {
      if (this._cropMode) return;  // 裁剪层在上方自行处理
      if (this._zoom) {            // 缩放模式: 拖拽平移
        e.preventDefault();
        const sx = e.clientX - this._panX, sy = e.clientY - this._panY;
        const move = (ev) => {
          this._panX = ev.clientX - sx;
          this._panY = ev.clientY - sy;
          this._applyZoomLayout();
        };
        const up = () => {
          document.removeEventListener('pointermove', move);
          document.removeEventListener('pointerup', up);
        };
        document.addEventListener('pointermove', move);
        document.addEventListener('pointerup', up);
        return;
      }
      if (!this._baMode) return;
      e.preventDefault();
      const wrap = e.currentTarget;
      const move = (ev) => {
        const r = wrap.getBoundingClientRect();
        if (!r.width) return;
        this._setBAPos(Math.min(0.98, Math.max(0.02, (ev.clientX - r.left) / r.width)));
      };
      const up = () => {
        document.removeEventListener('pointermove', move);
        document.removeEventListener('pointerup', up);
      };
      document.addEventListener('pointermove', move);
      document.addEventListener('pointerup', up);
      move(e);
    });

    // Keyboard: Esc=exit mode/close, Enter=apply crop, \=original, arrows=prev/next,
    // E/C/M/H/Y/V=tools, Ctrl+Z/Ctrl+Shift+Z=undo/redo, [/]=brush or spot size.
    document.addEventListener('keydown', (e) => {
      if (document.getElementById('photo-edit-overlay').classList.contains('hidden')) return;
      if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.tagName === 'SELECT') return;
      const km = (id) => (typeof Keymap !== 'undefined') && Keymap.is(e, id);
      if (km('undo')) { e.preventDefault(); this._undo(); return; }
      if (km('redo')) { e.preventDefault(); this._redo(); return; }
      if (km('close')) {
        if (this._cropMode) { this._exitCrop(true); return; }
        if (this._maskMode) { this._maskExitMode(); return; }
        if (this._spotOn) { this._spotToggle(false); return; }
        this._requestClose();
      }
      else if (e.key === 'Enter' && this._cropMode) { e.preventDefault(); this._applyCrop(); }
      else if (km('original') && !e.repeat) { this._holdOriginal(true); }
      else if (km('prev')) { e.preventDefault(); this._navigate(-1); }
      else if (km('next')) { e.preventDefault(); this._navigate(1); }
      else if (e.key === '[' || e.key === ']') {
        e.preventDefault();
        const d = e.key === ']' ? 1 : -1;
        if (this._maskMode === 'paint') {
          this._maskBrushR = Math.max(0.01, Math.min(0.2, this._maskBrushR + d * 0.01));
          const el = document.getElementById('pe-mask-brush-r');
          if (el) el.value = String(Math.round(this._maskBrushR * 100));
          const val = document.getElementById('pe-mask-brush-r-val');
          if (val) val.textContent = String(Math.round(this._maskBrushR * 100));
        } else {
          this._spotRadius = Math.max(6, Math.min(60, this._spotRadius + d * 2));
          const el = document.getElementById('pe-spot-radius');
          if (el) el.value = String(this._spotRadius);
        }
      } else if (km('toolEdit')) { e.preventDefault(); this._setTool('edit'); }
      else if (km('toolCrop')) { e.preventDefault(); this._setTool(this._cropMode ? 'edit' : 'crop'); }
      else if (km('toolMask')) { e.preventDefault(); this._setTool('mask'); }
      else if (km('toolSpot')) { e.preventDefault(); this._setTool('spot'); }
      else if (km('toolHistory')) { e.preventDefault(); this._setTool('history'); }
      else if (km('toolVersion')) { e.preventDefault(); this._setTool('version'); }
    });
    document.addEventListener('keyup', (e) => {
      const original = (typeof Keymap !== 'undefined') ? Keymap.is(e, 'original') : e.key === '\\';
      if (original) this._holdOriginal(false);
    });
  },

  // ── Navigation ──

  _navigate(dir) {
    this._navigateTo(this._fileIndex + dir);
  },

  async _navigateTo(newIdx, opts = {}) {
    if (!this._fileList.length) return;
    if (newIdx < 0 || newIdx >= this._fileList.length || newIdx === this._fileIndex) return;
    const f = this._fileList[newIdx];
    const newPath = f.path || f;
    if (!newPath) return;
    if (opts.select && typeof Workbench !== 'undefined') {
      if (opts.select === 'toggle') Workbench.select(newPath, 'toggle');
      else if (opts.select === 'range') Workbench.selectRange(newPath, true);
      else Workbench.select(newPath, 'replace');
    }

    // Save current edits before navigating away (auto-save contract)
    this._commitChange();
    await this._saveSilent();

    // 退出单照片专属模式(裁剪/缩放/按住看原图)
    if (this._cropMode) this._exitCrop(true);
    if (this._zoom) this._zoomFit();
    if (this._holdOrig) this._holdOriginal(false);
    if (this._spotOn) this._spotToggle(false);
    if (this._maskMode) this._maskExitMode();

    // Switch to new file
    this._fileIndex = newIdx;
    this._path = newPath;
    this._edits = {};
    this._hideAiCard();
    this._imgW = 0; this._imgH = 0;
    this._setSliders(this._defaultEdits());
    this._editsLoaded = false;
    this._presetBase = null;
    this._presetApplied = null;
    this._presetResolvedEdits = null;
    this._presetThumbs = {};
    this._presetThumbToken++;
    this._presetFitToken++;
    this._presetAmountToken++;
    clearTimeout(this._presetThumbTimer);
    clearTimeout(this._presetAmountTimer);
    this._renderPresetWall();
    this._lastCommitted = {};
    this._lastCommittedJson = '';
    this._lastSavedJson = '';
    this._saveErrorNoted = false;
    this._undoStacks[newPath] = [];
    this._redoStacks[newPath] = [];
    if (typeof Workbench !== 'undefined') Workbench.setActive(newPath);
    this._loadEdits(newPath);
    this._loadInitialPreview(newPath);
    this._loadColorSpace(newPath);
    this._renderFilmstrip();
    this._updateNavUI();
    this._updateWorkspaceBar();
  },

  _updateNavUI() {
    const hasList = this._fileList.length > 1;
    document.getElementById('pe-prev-btn').disabled = !hasList || this._fileIndex <= 0;
    document.getElementById('pe-next-btn').disabled = !hasList || this._fileIndex >= this._fileList.length - 1;
    document.getElementById('pe-nav-info').textContent = hasList
      ? `${this._fileIndex + 1} / ${this._fileList.length}` : '';
    document.querySelectorAll('.pe-film-cell').forEach(cell => {
      cell.classList.toggle('active', parseInt(cell.dataset.i, 10) === this._fileIndex);
    });
  },

  _filmThumb(f) {
    if (f.thumb) return `local-image://x/?p=${encodeURIComponent(f.thumb)}`;
    const ext = (f.ext || ((f.path || '').split('.').pop() || '')).toLowerCase();
    if (!this._RAW_LIKE_EXTS.has(ext)) return `local-image://x/?p=${encodeURIComponent(f.path)}`;
    return '';
  },

  _renderFilmstrip() {
    const el = document.getElementById('pe-filmstrip');
    if (!el) return;
    if (this._fileList.length < 2) {
      el.classList.add('hidden');
      el.innerHTML = '';
      return;
    }
    el.classList.remove('hidden');
    el.innerHTML = this._fileList.map((f, i) => {
      const name = f.name || (f.path || '').split(/[\\/]/).pop() || '';
      const src = this._filmThumb(f);
      const wb = (typeof Workbench !== 'undefined');
      const selected = wb && Workbench.isSelected(f.path);
      const rated = wb ? Workbench.getRating(f.path) : 0;
      const candidate = wb && Workbench.isCandidate(f.path);
      return `<button class="pe-film-cell${i === this._fileIndex ? ' active' : ''}${selected ? ' selected' : ''}" data-i="${i}" title="${this._escHtml(f.path || name)}">
        <span class="pe-film-img">${src ? `<img src="${src}" loading="lazy" alt="">` : '<i class="ph ph-image"></i>'}${selected ? '<span class="pe-film-sel">✓</span>' : ''}${candidate ? '<span class="pe-film-cand">⚑</span>' : ''}${rated ? `<span class="pe-film-rate">★${rated}</span>` : ''}</span>
        <span class="pe-film-name">${this._escHtml(name)}</span>
      </button>`;
    }).join('');
    const active = el.querySelector('.pe-film-cell.active');
    if (active) active.scrollIntoView({ block: 'nearest', inline: 'center' });
    this._layoutDockedScopes();
  },

  _refreshFilmstripSelection() {
    if (typeof Workbench === 'undefined') return;
    document.querySelectorAll('.pe-film-cell').forEach(cell => {
      const f = this._fileList[parseInt(cell.dataset.i, 10)];
      if (!f) return;
      const on = Workbench.isSelected(f.path);
      cell.classList.toggle('selected', on);
      const img = cell.querySelector('.pe-film-img');
      let badge = cell.querySelector('.pe-film-sel');
      if (on && !badge && img) {
        badge = document.createElement('span');
        badge.className = 'pe-film-sel';
        badge.textContent = '✓';
        img.appendChild(badge);
      } else if (!on && badge) {
        badge.remove();
      }
    });
  },

  // ── Tool rail: mode is a first-class state ──

  _setTool(tool, opts = {}) {
    const next = tool || 'edit';
    if (!opts.silent && this._soloSection) this._soloSection = '';
    if (next === 'crop') {
      if (this._maskMode) this._maskExitMode();
      if (this._spotOn) this._spotToggle(false);
      if (!this._cropMode) this._enterCrop();
    } else if (next === 'mask') {
      if (this._cropMode) this._exitCrop(true);
      if (this._spotOn) this._spotToggle(false);
      this._openSection('pe-mask-toggle', 'pe-mask-wrap', !opts.silent);
    } else if (next === 'spot') {
      if (this._cropMode) this._exitCrop(true);
      if (this._maskMode) this._maskExitMode();
      this._spotToggle(!this._spotOn);
      this._syncToolRail(this._spotOn ? 'spot' : 'edit');
      return;
    } else if (next === 'history') {
      if (this._cropMode) this._exitCrop(true);
      if (this._maskMode) this._maskExitMode();
      if (this._spotOn) this._spotToggle(false);
      this._openSection('pe-hist-toggle', 'pe-hist-wrap', !opts.silent);
      this._journalRefresh();
    } else if (next === 'version') {
      if (this._cropMode) this._exitCrop(true);
      if (this._maskMode) this._maskExitMode();
      if (this._spotOn) this._spotToggle(false);
      this._openSection('pe-vc-toggle', 'pe-vc-wrap', !opts.silent);
      this._vcRefresh();
    } else {
      if (this._cropMode) this._exitCrop(true);
      if (this._maskMode) this._maskExitMode();
      if (this._spotOn) this._spotToggle(false);
      this._openSection('pe-lr-toggle', 'pe-lr-wrap', !opts.silent);
    }
    this._lastTool = next;
    this._syncToolRail(next);
    if (!opts.silent) this._savePanelState();
  },

  _syncToolRail(tool) {
    document.querySelectorAll('.pe-tool').forEach(b => {
      b.classList.toggle('active', b.dataset.tool === tool);
    });
    this._applyToolPanel(tool);
  },

  // Tool-specific panel: the rail selects a task, not just a scroll anchor.
  // crop/mask/spot/history/version each show only their own controls.
  _applyToolPanel(tool) {
    const body = document.querySelector('.pe-body');
    if (!body) return;
    if (!this._toolScope || this._toolScope.length !== body.children.length) {
      this._toolScope = [...body.children].map(el => {
        const id = el.id || '';
        let scope = 'edit';
        if (id === 'pe-tool-title' || id === 'pe-scopes') scope = 'all';
        else if (id === 'pe-spot-panel') scope = 'spot';
        else if (id === 'pe-lr-toggle' || id === 'pe-lr-wrap') scope = 'lr';
        else if (id === 'pe-mask-toggle' || id === 'pe-mask-wrap') scope = 'mask';
        else if (id === 'pe-hist-toggle' || id === 'pe-hist-wrap') scope = 'history';
        else if (id === 'pe-vc-toggle' || id === 'pe-vc-wrap') scope = 'version';
        else if (el.querySelector && el.querySelector('#pe-crop-btn, #pe-straighten')) scope = 'crop';
        return { el, scope };
      });
    }
    const mode = tool || 'edit';
    const changed = this._currentToolPanel !== mode;
    this._currentToolPanel = mode;
    this._toolScope.forEach(({ el, scope }) => {
      let visible;
      if (mode === 'edit') {
        visible = this._soloSection
          ? (scope === 'all' || scope === this._soloSection)
          : scope !== 'spot';
      } else {
        visible = (scope === 'all' || scope === mode);
      }
      el.style.display = visible ? '' : 'none';
    });
    const title = document.getElementById('pe-tool-title');
    if (title) {
      const key = { edit: 'peToolEdit', crop: 'peToolCrop', mask: 'peToolMask', spot: 'peToolSpot',
        history: 'peToolHistory', version: 'peToolVersion' }[mode] || 'peToolEdit';
      title.innerHTML = `<i class="ph ${mode === 'edit' ? 'ph-sliders' : mode === 'crop' ? 'ph-crop' : mode === 'mask' ? 'ph-mask-sad' : mode === 'spot' ? 'ph-eraser' : mode === 'history' ? 'ph-clock-counter-clockwise' : 'ph-bookmark-simple'}"></i>
        <span>${t('peToolTitle', { tool: t(key) })}</span>`;
    }
    if (changed) body.scrollTop = 0;
    this._syncSectionTools();
  },

  _sectionIds(key) {
    const map = {
      lr: ['pe-lr-toggle', 'pe-lr-wrap'],
      mask: ['pe-mask-toggle', 'pe-mask-wrap'],
      hist: ['pe-hist-toggle', 'pe-hist-wrap'],
      vc: ['pe-vc-toggle', 'pe-vc-wrap'],
    };
    return map[key] || null;
  },

  _toggleSolo(key) {
    this._soloSection = (this._soloSection === key) ? '' : key;
    if (this._soloSection) {
      const ids = this._sectionIds(key);
      if (ids) {
        document.getElementById(ids[0])?.classList.add('open');
        document.getElementById(ids[1])?.classList.add('open');
      }
    }
    this._applyToolPanel(this._currentToolPanel || 'edit');
    if (this._soloSection) {
      const ids = this._sectionIds(key);
      const wrap = ids && document.getElementById(ids[1]);
      if (wrap) setTimeout(() => { try { wrap.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); } catch (_) {} }, 20);
    }
  },

  _syncSectionTools() {
    document.querySelectorAll('[data-solo]').forEach(btn => {
      btn.classList.toggle('active', btn.dataset.solo === this._soloSection);
    });
    this._panelOrder.forEach((key, i) => {
      const ids = this._sectionIds(key);
      const toggle = ids && document.getElementById(ids[0]);
      if (!toggle) return;
      const up = toggle.querySelector('[data-move="up"]');
      const down = toggle.querySelector('[data-move="down"]');
      if (up) up.disabled = i === 0;
      if (down) down.disabled = i === this._panelOrder.length - 1;
    });
  },

  _applyPanelOrder(order) {
    const body = document.querySelector('.pe-body');
    const anchor = document.getElementById('pe-section-end');
    if (!body || !anchor) return;
    order.forEach(key => {
      const ids = this._sectionIds(key);
      if (!ids) return;
      const toggle = document.getElementById(ids[0]);
      const wrap = document.getElementById(ids[1]);
      if (!toggle || !wrap) return;
      body.insertBefore(toggle, anchor);
      body.insertBefore(wrap, anchor);
    });
  },

  _restorePanelOrder() {
    try {
      const raw = localStorage.getItem('pe_panel_order_v1');
      const saved = raw ? JSON.parse(raw) : null;
      if (Array.isArray(saved) && saved.length === 4 &&
          ['lr', 'mask', 'hist', 'vc'].every(k => saved.includes(k))) {
        this._panelOrder = saved;
      }
    } catch (_) { /* fall back to default order */ }
    this._applyPanelOrder(this._panelOrder);
    this._syncSectionTools();
  },

  _setPanelOrder(order) {
    if (!Array.isArray(order) || order.length !== 4 ||
        !['lr', 'mask', 'hist', 'vc'].every(k => order.includes(k))) return;
    this._panelOrder = order.slice();
    try { localStorage.setItem('pe_panel_order_v1', JSON.stringify(this._panelOrder)); } catch (_) { /* ignore */ }
    this._applyPanelOrder(this._panelOrder);
    this._syncSectionTools();
    this._savePanelState();
  },

  _moveSection(key, dir) {
    const from = this._panelOrder.indexOf(key);
    if (from < 0) return;
    const to = Math.max(0, Math.min(this._panelOrder.length - 1, from + (dir === 'up' ? -1 : 1)));
    if (to === from) return;
    const order = this._panelOrder.slice();
    order.splice(from, 1);
    order.splice(to, 0, key);
    this._setPanelOrder(order);
  },

  _dropSection(dragKey, targetKey, before) {
    if (!dragKey || !targetKey || dragKey === targetKey) return;
    const order = this._panelOrder.filter(k => k !== dragKey);
    let idx = order.indexOf(targetKey);
    if (idx < 0) return;
    if (!before) idx += 1;
    order.splice(idx, 0, dragKey);
    this._setPanelOrder(order);
  },

  _clearDropMarks() {
    document.querySelectorAll('.pe-section-toggle.drop-before, .pe-section-toggle.drop-after')
      .forEach(el => el.classList.remove('drop-before', 'drop-after'));
  },

  _endSectionDrag() {
    this._dragSection = '';
    this._suppressSectionClickUntil = Date.now() + 180;
    this._clearDropMarks();
    document.querySelectorAll('.pe-section-toggle.dragging')
      .forEach(el => el.classList.remove('dragging'));
  },

  _savePanelState() {
    try {
      const body = document.querySelector('.pe-body');
      const state = {
        lr: document.getElementById('pe-lr-wrap')?.classList.contains('open'),
        mask: document.getElementById('pe-mask-wrap')?.classList.contains('open'),
        hist: document.getElementById('pe-hist-wrap')?.classList.contains('open'),
        vc: document.getElementById('pe-vc-wrap')?.classList.contains('open'),
        preset: document.getElementById('pe-preset-wrap')?.classList.contains('open'),
        scroll: body ? body.scrollTop : 0,
      };
      localStorage.setItem('pe_panel_state', JSON.stringify(state));
    } catch (_) { /* ignore */ }
  },

  _restorePanelState() {
    try {
      const raw = localStorage.getItem('pe_panel_state');
      if (!raw) return;
      const s = JSON.parse(raw);
      const set = (toggleId, wrapId, open) => {
        if (open == null) return;
        document.getElementById(toggleId)?.classList.toggle('open', !!open);
        document.getElementById(wrapId)?.classList.toggle('open', !!open);
      };
      set('pe-lr-toggle', 'pe-lr-wrap', s.lr);
      set('pe-mask-toggle', 'pe-mask-wrap', s.mask);
      set('pe-hist-toggle', 'pe-hist-wrap', s.hist);
      set('pe-vc-toggle', 'pe-vc-wrap', s.vc);
      set('pe-preset-toggle', 'pe-preset-wrap', s.preset);
      if (typeof s.scroll === 'number') {
        setTimeout(() => {
          const body = document.querySelector('.pe-body');
          if (body) body.scrollTop = s.scroll;
        }, 30);
      }
    } catch (_) { /* ignore */ }
  },

  // 预设区默认折叠，展开状态随面板状态一起持久化
  _togglePresetSection() {
    const toggle = document.getElementById('pe-preset-toggle');
    const wrap = document.getElementById('pe-preset-wrap');
    if (!toggle || !wrap) return;
    const open = !toggle.classList.contains('open');
    toggle.classList.toggle('open', open);
    wrap.classList.toggle('open', open);
    this._savePanelState();
    if (open) this._loadPresetThumbs();
  },

  // ── 图表(示波器)：折叠 + 右键悬浮/吸附左侧/回位 ──

  _initScopes() {
    if (this._scopesReady) return;
    this._scopesReady = true;
    const host = document.querySelector('.pe-preview');
    if (!host) return;

    // 悬浮面板(每个图表一个，画布在 DOM 中原地迁移)
    this._scopeDefs.forEach(def => {
      const panel = document.createElement('div');
      panel.className = 'pe-scope-float hidden';
      panel.id = `pe-scope-float-${def.kind}`;
      panel.dataset.scope = def.kind;
      panel.innerHTML =
        `<div class="pe-scope-float-head" data-scope-float-head="${def.kind}">` +
          `<i class="ph ph-dots-six-vertical pe-scope-drag"></i>` +
          `<i class="ph ${def.icon} pe-scope-icon"></i>` +
          `<span class="pe-scope-float-name" data-i18n="${def.titleKey}"></span>` +
          `<span class="pe-scope-float-state hidden"></span>` +
          `<span class="pe-scope-float-spacer"></span>` +
          `<button type="button" class="pe-scope-float-btn" data-scope-return="${def.kind}" data-i18n-title="peScopeReset" title="${this._escHtml(t('peScopeReset'))}"><i class="ph ph-arrow-u-down-left"></i></button>` +
        `</div>` +
        `<div class="pe-scope-float-body"></div>`;
      panel.querySelector('.pe-scope-float-name').textContent = t(def.titleKey);
      host.appendChild(panel);
    });

    // 右侧面板：点击标题折叠/展开；右键图表任意位置打开悬浮/吸附菜单。
    // 图表标题不参与 LR/蒙版等分区排序（没有 data-drag）。
    const bodyEl = document.querySelector('.pe-body');
    bodyEl?.addEventListener('click', (e) => {
      const head = e.target.closest('.pe-scope-head');
      if (!head) return;
      e.stopPropagation();
      this._toggleScope(head.dataset.scopeHead);
    });
    bodyEl?.addEventListener('contextmenu', (e) => {
      const scopeEl = e.target.closest('.pe-scope');
      if (!scopeEl) return;
      e.preventDefault();
      e.stopPropagation();
      this._showScopeMenu(scopeEl.dataset.scope, e);
    });

    // 左侧工作区：悬浮面板的右键菜单 / 回位按钮 / 拖动
    host.addEventListener('click', (e) => {
      const ret = e.target.closest('[data-scope-return]');
      if (!ret) return;
      e.stopPropagation();
      this._setScopePlace(ret.dataset.scopeReturn, 'inline');
    });
    host.addEventListener('contextmenu', (e) => {
      const panel = e.target.closest('.pe-scope-float');
      if (!panel) return;
      e.preventDefault();
      e.stopPropagation();
      this._showScopeMenu(panel.dataset.scope, e);
    });
    host.addEventListener('pointerdown', (e) => this._scopeDragStart(e));

    // 右键菜单(全局单例)
    const menu = document.createElement('div');
    menu.id = 'pe-scope-menu';
    menu.className = 'pe-scope-menu hidden';
    menu.innerHTML =
      `<button type="button" data-scope-act="float"><i class="ph ph-arrows-out"></i> <span></span></button>` +
      `<button type="button" data-scope-act="dock"><i class="ph ph-arrow-line-left"></i> <span></span></button>` +
      `<div class="pe-scope-menu-sep"></div>` +
      `<button type="button" data-scope-act="reset"><i class="ph ph-arrow-u-down-left"></i> <span></span></button>`;
    document.body.appendChild(menu);
    menu.addEventListener('click', (e) => {
      const btn = e.target.closest('[data-scope-act]');
      if (!btn || !this._scopeMenuKind) return;
      const kind = this._scopeMenuKind;
      const act = btn.dataset.scopeAct;
      this._hideScopeMenu();
      if (act === 'float') this._setScopePlace(kind, 'float');
      else if (act === 'dock') this._setScopePlace(kind, 'dock');
      else if (act === 'reset') this._setScopePlace(kind, 'inline');
    });
    document.addEventListener('pointerdown', (e) => {
      if (!e.target.closest('#pe-scope-menu')) this._hideScopeMenu();
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') this._hideScopeMenu();
    });
  },

  _toggleScope(kind) {
    if (!kind) return;
    const open = !this._scopeOpen[kind];
    this._scopeOpen[kind] = open;
    document.getElementById(`pe-scope-${kind}`)?.classList.toggle('open', open);
    this._saveScopeState();
    if (open) requestAnimationFrame(() => this._renderScope(kind));
  },

  _setScopePlace(kind, place, opts = {}) {
    if (!this._scopeDefs.some(d => d.kind === kind)) return;
    if (!['inline', 'float', 'dock'].includes(place)) return;
    const inline = document.getElementById(`pe-scope-${kind}`);
    const inlineBody = document.getElementById(`pe-scope-${kind}-body`);
    const floatEl = document.getElementById(`pe-scope-float-${kind}`);
    const floatBody = floatEl && floatEl.querySelector('.pe-scope-float-body');
    const cv = document.getElementById(this._scopeCanvasId(kind));
    if (!inline || !inlineBody || !floatEl || !floatBody || !cv) return;

    this._scopePlace[kind] = place;
    if (place === 'inline') {
      if (cv.parentElement !== inlineBody) inlineBody.appendChild(cv);
      floatEl.classList.add('hidden');
      floatEl.classList.remove('docked');
      inline.classList.remove('is-detached');
      // 用户点“回位”时展开图表，避免回到折叠行后看起来像消失了
      if (!opts.silent) {
        this._scopeOpen[kind] = true;
        inline.classList.add('open');
      }
    } else {
      if (cv.parentElement !== floatBody) floatBody.appendChild(cv);
      floatEl.classList.remove('hidden');
      floatEl.classList.toggle('docked', place === 'dock');
      inline.classList.add('is-detached');
      if (place === 'float') {
        floatEl.style.width = '';
        const pos = this._scopePos[kind];
        if (pos && Number.isFinite(pos.x) && Number.isFinite(pos.y)) {
          floatEl.style.left = pos.x + 'px';
          floatEl.style.top = pos.y + 'px';
        } else {
          const idx = this._scopeDefs.findIndex(d => d.kind === kind);
          const overlay = document.getElementById('photo-edit-overlay');
          const baseTop = overlay && overlay.classList.contains('pe-workspace') ? 16 : 88;
          floatEl.style.left = (16 + idx * 22) + 'px';
          floatEl.style.top = (baseTop + idx * 22) + 'px';
        }
      }
    }
    this._updateScopeBadges();
    if (!opts.silent) this._saveScopeState();
    requestAnimationFrame(() => {
      if (place === 'dock') this._layoutDockedScopes();
      else if (place === 'float') this._clampFloatingScope(kind);
      this._renderScope(kind);
    });
  },

  _updateScopeBadges() {
    this._scopeDefs.forEach(def => {
      const place = this._scopePlace[def.kind] || 'inline';
      const label = place === 'dock' ? t('peScopeDocked')
        : (place === 'float' ? t('peScopeFloating') : '');
      document.querySelectorAll(`[data-scope-state="${def.kind}"], #pe-scope-float-${def.kind} .pe-scope-float-state`)
        .forEach(el => {
          el.textContent = label;
          el.classList.toggle('hidden', !label);
        });
    });
  },

  _clampFloatingScope(kind) {
    const host = document.querySelector('.pe-preview');
    const panel = document.getElementById(`pe-scope-float-${kind}`);
    if (!host || !panel || panel.classList.contains('hidden')) return;
    const maxX = Math.max(0, host.clientWidth - panel.offsetWidth);
    const maxY = Math.max(0, host.clientHeight - panel.offsetHeight);
    const x = Math.max(0, Math.min(maxX, parseFloat(panel.style.left) || 0));
    const y = Math.max(0, Math.min(maxY, parseFloat(panel.style.top) || 0));
    panel.style.left = x + 'px';
    panel.style.top = y + 'px';
    this._scopePos[kind] = { x, y };
  },

  // 吸附在左侧边界：避开工作台胶片条，多张图表纵向堆叠
  _layoutDockedScopes() {
    const host = document.querySelector('.pe-preview');
    if (!host) return;
    const overlay = document.getElementById('photo-edit-overlay');
    const strip = document.getElementById('pe-filmstrip');
    const inWorkspace = overlay && overlay.classList.contains('pe-workspace');
    const stripW = (inWorkspace && strip && !strip.classList.contains('hidden')) ? strip.offsetWidth : 0;
    const left = Math.max(8, stripW + 8);
    // 普通 overlay 模式下避开左上角关闭按钮；工作区模式从内容区顶部开始
    let top = inWorkspace ? 8 : 88;
    this._scopeDefs.forEach(def => {
      if (this._scopePlace[def.kind] !== 'dock') return;
      const panel = document.getElementById(`pe-scope-float-${def.kind}`);
      if (!panel) return;
      const width = Math.max(200, Math.min(340, host.clientWidth - left - 16));
      panel.style.width = width + 'px';
      panel.style.left = left + 'px';
      panel.style.top = top + 'px';
      top += panel.offsetHeight + 8;
    });
  },

  _scopeDragStart(e) {
    if (e.button !== 0) return;
    const head = e.target.closest('.pe-scope-float-head');
    if (!head || e.target.closest('button')) return;
    const kind = head.dataset.scopeFloatHead;
    const panel = document.getElementById(`pe-scope-float-${kind}`);
    const host = document.querySelector('.pe-preview');
    if (!panel || !host || panel.classList.contains('hidden')) return;
    e.preventDefault();

    // 吸附状态直接拖拽 → 转为自由悬浮，位置从当前吸附点继续
    if (this._scopePlace[kind] === 'dock') {
      this._scopePlace[kind] = 'float';
      panel.classList.remove('docked');
      panel.style.width = '';
      this._updateScopeBadges();
    }
    const sx = e.clientX, sy = e.clientY;
    const startX = parseFloat(panel.style.left) || 0;
    const startY = parseFloat(panel.style.top) || 0;
    this._scopeDrag = { kind, panel, host, sx, sy, startX, startY };
    const move = (ev) => this._scopeDragMove(ev);
    const up = () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
      window.removeEventListener('pointercancel', up);
      this._scopeDrag = null;
      this._saveScopeState();
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
    window.addEventListener('pointercancel', up);
  },

  _scopeDragMove(ev) {
    const d = this._scopeDrag;
    if (!d) return;
    const maxX = Math.max(0, d.host.clientWidth - d.panel.offsetWidth);
    const maxY = Math.max(0, d.host.clientHeight - d.panel.offsetHeight);
    const x = Math.max(0, Math.min(maxX, d.startX + ev.clientX - d.sx));
    const y = Math.max(0, Math.min(maxY, d.startY + ev.clientY - d.sy));
    d.panel.style.left = x + 'px';
    d.panel.style.top = y + 'px';
    this._scopePos[d.kind] = { x, y };
  },

  _showScopeMenu(kind, e) {
    const menu = document.getElementById('pe-scope-menu');
    if (!menu) return;
    this._scopeMenuKind = kind;
    const place = this._scopePlace[kind] || 'inline';
    const setLabel = (act, text) => {
      const span = menu.querySelector(`[data-scope-act="${act}"] span`);
      if (span) span.textContent = text;
    };
    setLabel('float', t('peScopeFloat'));
    setLabel('dock', t('peScopeDock'));
    setLabel('reset', t('peScopeReset'));
    menu.querySelector('[data-scope-act="float"]').classList.toggle('hidden', place === 'float');
    menu.querySelector('[data-scope-act="dock"]').classList.toggle('hidden', place === 'dock');
    const sep = menu.querySelector('.pe-scope-menu-sep');
    const reset = menu.querySelector('[data-scope-act="reset"]');
    sep.classList.toggle('hidden', place === 'inline');
    reset.classList.toggle('hidden', place === 'inline');
    menu.classList.remove('hidden');
    const mw = menu.offsetWidth, mh = menu.offsetHeight;
    menu.style.left = Math.max(6, Math.min(e.clientX, window.innerWidth - mw - 8)) + 'px';
    menu.style.top = Math.max(6, Math.min(e.clientY, window.innerHeight - mh - 8)) + 'px';
  },

  _hideScopeMenu() {
    document.getElementById('pe-scope-menu')?.classList.add('hidden');
    this._scopeMenuKind = '';
  },

  _saveScopeState() {
    try {
      localStorage.setItem('pe_scope_state_v1', JSON.stringify({
        open: this._scopeOpen,
        place: this._scopePlace,
        pos: this._scopePos,
      }));
    } catch (_) { /* ignore */ }
  },

  _restoreScopeState() {
    this._initScopes();
    let s = {};
    try { s = JSON.parse(localStorage.getItem('pe_scope_state_v1') || '{}') || {}; } catch (_) { s = {}; }
    if (s.open && typeof s.open === 'object') {
      this._scopeDefs.forEach(def => {
        if (typeof s.open[def.kind] === 'boolean') this._scopeOpen[def.kind] = s.open[def.kind];
      });
    }
    if (s.pos && typeof s.pos === 'object') {
      this._scopeDefs.forEach(def => {
        const p = s.pos[def.kind];
        if (p && Number.isFinite(p.x) && Number.isFinite(p.y)) {
          this._scopePos[def.kind] = { x: p.x, y: p.y };
        }
      });
    }
    this._scopeDefs.forEach(def => {
      document.getElementById(`pe-scope-${def.kind}`)?.classList.toggle('open', !!this._scopeOpen[def.kind]);
      const place = s.place && ['inline', 'float', 'dock'].includes(s.place[def.kind])
        ? s.place[def.kind] : 'inline';
      this._setScopePlace(def.kind, place, { silent: true });
    });
    this._updateScopeBadges();
  },

  _openSection(toggleId, wrapId, scroll = true) {
    const toggle = document.getElementById(toggleId);
    const wrap = document.getElementById(wrapId);
    if (!toggle || !wrap) return;
    toggle.classList.add('open');
    wrap.classList.add('open');
    if (scroll) {
      setTimeout(() => {
        try { wrap.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); } catch (_) {}
      }, 20);
    }
  },

  // ── Initial preview (handles RAW/HEIC by requesting backend JPEG) ──

  async _loadInitialPreview(path) {
    const img = document.getElementById('pe-preview-img');
    if (!img) return;

    const ext = (path.split('.').pop() || '').toLowerCase();
    if (this._RAW_LIKE_EXTS.has(ext)) {
      // RAW/HEIC: request a JPEG preview from the backend
      try {
        const r = await window.api.sendCommand('apply_edits', {
          path, edits: {}, preview: true,
        });
        if (r && r.preview) {
          img.src = r.preview;
          this._beforeSrc = r.preview;
          this._syncBeforeImg();
          return;
        }
      } catch (_) { /* fall through to local-image */ }
    }
    // JPEG/PNG/etc.: use local-image:// protocol directly
    this._beforeSrc = `local-image://x/?p=${encodeURIComponent(path)}`;
    img.src = this._beforeSrc;
    this._syncBeforeImg();
  },

  // ── 修改前后对比(分割条) ──

  _syncBeforeImg() {
    if (!this._baMode) return;
    const bi = document.getElementById('pe-before-img');
    if (bi) bi.src = this._beforeSrc || '';
  },

  _toggleBA() {
    if (!this._baMode) {  // 进入对比前退出互斥模式
      if (this._cropMode) this._exitCrop(true);
      if (this._zoom) this._zoomFit();
    }
    this._baMode = !this._baMode;
    const show = this._baMode;
    ['pe-before-wrap', 'pe-ba-handle', 'pe-ba-after-label'].forEach(id => {
      const el = document.getElementById(id);
      if (el) el.classList.toggle('hidden', !show);
    });
    const btn = document.getElementById('pe-compare');
    if (btn) btn.classList.toggle('pe-active', show);
    if (show) {
      this._syncBeforeImg();
      this._setBAPos(this._baPos || 0.5);
    }
  },

  _setBAPos(pos) {
    this._baPos = pos;
    const bw = document.getElementById('pe-before-wrap');
    const h = document.getElementById('pe-ba-handle');
    if (bw) bw.style.clipPath = `inset(0 ${(1 - pos) * 100}% 0 0)`;
    if (h) h.style.left = `${pos * 100}%`;
  },

  async _loadColorSpace(path) {
    const el = document.getElementById('pe-color-space');
    if (!el) return;
    el.textContent = '';
    if (!path) return;
    try {
      const r = await window.api.sendCommand('get_image_info', { path });
      if (r && !r.error && r.width) {
        this._imgW = r.width;
        this._imgH = r.height;
        if (r.colorSpace) el.textContent = r.colorSpace + ' · ' + r.width + '×' + r.height;
      }
    } catch (_) { /* ignore */ }
  },

  // ── Sliders ──

  _sliderVal(name) {
    // name 兼容滑杆 id 后缀('wb-temp')与 edits 键('wb_temp') — 下划线统一转连字符
    const id = `pe-${name.replace(/_/g, '-')}`;
    const v = parseInt(document.getElementById(id).value);
    if (name === 'wb-temp' || name === 'wb_temp') return v === 0 ? (t('peLogOff') || '关') : v + 'K';
    if (name === 'wb-tint' || name === 'wb_tint') return String(v);
    if (name === 'denoise') return String(v);
    if (name === 'auto-exposure' || name === 'auto_exposure') return v === 0 ? (t('peLogOff') || '关') : (v / 100).toFixed(2);
    if (name === 'levels-black' || name === 'levels-white') return String(v);
    if (name === 'levels-gamma') return (v / 100).toFixed(2);
    if (name.startsWith('cal-')) return String(v);
    if (name === 'straighten') return (v / 10).toFixed(1) + '°';
    if (name === 'exposure') return (v / 10).toFixed(1);
    return (v / 10).toFixed(1);
  },

  _readEdits() {
    const e = {};
    ['exposure','contrast','saturation','warmth','shadows','highlights','sharpness'].forEach(k => {
      e[k] = parseFloat(this._sliderVal(k));
    });
    // PhotoS 增强
    e.wb_temp = parseInt(document.getElementById('pe-wb-temp').value) || 0;
    e.denoise = parseInt(document.getElementById('pe-denoise').value) || 0;
    e.auto_exposure = (parseInt(document.getElementById('pe-auto-exposure').value) || 0) / 100;
    e.log_curve = document.getElementById('pe-log-curve').value || '';
    e.auto_levels = document.getElementById('pe-auto-levels').checked;
    e.grayscale = document.getElementById('pe-grayscale').checked;
    e.sepia = document.getElementById('pe-sepia').checked;
    e.srgb = document.getElementById('pe-srgb').checked;
    e.auto_straighten = document.getElementById('pe-auto-straighten').checked;
    // 文件选择类: 仅存于 this._edits（按钮设置）
    e.wb_reference = this._edits.wb_reference || '';
    e.lut_file = this._edits.lut_file || '';
    // ── 调色 (LR) ──
    const g = id => parseInt(document.getElementById(id).value) || 0;
    e.vibrance = g('pe-vibrance') / 10;
    e.wb_tint = g('pe-wb-tint');
    e.clarity = g('pe-clarity') / 10;
    e.texture = g('pe-texture') / 10;
    e.dehaze = g('pe-dehaze') / 10;
    const lb = g('pe-levels-black'), lw = g('pe-levels-white') || 255, lg = (g('pe-levels-gamma') || 100) / 100;
    e.levels = (lb > 0 || lw < 255 || Math.abs(lg - 1) > 0.001) ? [lb, lw, lg] : null;
    // ── 相机校准（P6-3）──
    const cal = {};
    ['shadows-tint', 'r-hue', 'r-sat', 'g-hue', 'g-sat', 'b-hue', 'b-sat'].forEach(kk => {
      const v = g(`pe-cal-${kk}`);
      if (v) cal[kk.replace(/-/g, '_')] = v;
    });
    e.calibration = Object.keys(cal).length ? cal : null;
    e.hsl = Object.keys(this._hsl).length ? this._hsl : null;
    e.color_grading = Object.keys(this._grading).length ? this._grading : null;
    e.curves = Object.keys(this._curves).length ? JSON.parse(JSON.stringify(this._curves)) : null;
    e.masks = this._masks.length ? JSON.parse(JSON.stringify(this._masks)) : null;
    // ── 污点/红眼（spots 与 redeye 拆两键,mode==='redeye' 归 redeye） ──
    const spotList = this._spots.filter(s => s.mode !== 'redeye')
      .map(({ id, mode, x, y, r, sx, sy }) => ({ id, mode, x, y, r, sx, sy }));
    e.spots = spotList.length ? spotList : null;
    const reList = this._spots.filter(s => s.mode === 'redeye')
      .map(({ id, x, y, r }) => ({ id, x, y, r }));
    e.redeye = reList.length ? reList : null;
    // ── 镜头校正 ──
    const od = parseInt(document.getElementById('pe-optics-distortion').value) || 0;
    const oc = parseInt(document.getElementById('pe-optics-ca').value) || 0;
    const ov = parseInt(document.getElementById('pe-optics-vig').value) || 0;
    e.optics = (od || oc || ov) ? { distortion: od, ca: oc, vig: ov } : null;
    const vig = g('pe-vignette') / 10;
    e.vignette = vig ? [vig, 0.5, 0.5] : null;
    const gr = g('pe-grain') / 10;
    e.grain = gr ? [gr, 1.0] : null;
    // ── 几何(存于 this._edits,非滑杆) + 水平校正(滑杆) ──
    e.rotation = this._edits.rotation || 0;
    e.flip_h = !!this._edits.flip_h;
    e.flip_v = !!this._edits.flip_v;
    e.crop = this._edits.crop || null;
    e.rotate_deg = g('pe-straighten') / 10;
    // ── AI 自动修图参数快照（存于 this._edits,非滑杆;None = 关） ──
    e.auto_tone = (this._edits.auto_tone && typeof this._edits.auto_tone === 'object')
      ? this._edits.auto_tone : null;
    return e;
  },

  _setSliders(edits) {
    Object.entries(edits).forEach(([k, v]) => {
      // 文件选择类: 只恢复文件名标签+清除按钮(无对应滑杆)
      if (k === 'wb_reference' || k === 'lut_file') {
        const base = k === 'wb_reference' ? 'pe-wb-ref' : 'pe-lut';
        const nameEl = document.getElementById(`${base}-name`);
        const clearEl = document.getElementById(`${base}-clear`);
        const fname = (v || '').split(/[\\/]/).pop();
        if (nameEl) nameEl.textContent = fname || '';
        if (clearEl) clearEl.classList.toggle('hidden', !fname);
        return;
      }
      // ── 调色 (LR) 复合键 ──
      if (k === 'levels') {
        const arr = Array.isArray(v) ? v : [0, 255, 1];
        [['pe-levels-black', Math.round(arr[0] || 0)],
         ['pe-levels-white', arr[1] != null ? Math.round(arr[1]) : 255],
         ['pe-levels-gamma', Math.round((arr[2] != null ? arr[2] : 1) * 100)]].forEach(([id, val]) => {
          const s = document.getElementById(id);
          if (s) { s.value = val; const l = document.getElementById(id + '-val'); if (l) l.textContent = this._sliderVal(id.slice(3)); }
        });
        return;
      }
      if (k === 'hsl') {
        this._hsl = (v && typeof v === 'object') ? JSON.parse(JSON.stringify(v)) : {};
        this._syncHslSliders();
        return;
      }
      if (k === 'color_grading') {
        this._grading = (v && typeof v === 'object') ? JSON.parse(JSON.stringify(v)) : {};
        this._syncCgSliders();
        return;
      }
      if (k === 'curves') {
        this._curves = (v && typeof v === 'object') ? JSON.parse(JSON.stringify(v)) : {};
        this._renderCurve();
        return;
      }
      if (k === 'masks') {
        this._masks = Array.isArray(v) ? JSON.parse(JSON.stringify(v)) : [];
        this._maskSeq = this._masks.reduce((s, m) => Math.max(s, m.id || 0), 0);
        this._maskSel = -1;
        this._maskCompSel = -1;
        this._maskExitMode();
        this._maskSyncEditor();
        return;
      }
      if (k === 'spots' || k === 'redeye') {
        // spots/redeye 两键合并进 _spots（旧 sidecar 两键并存），替换同类保留异类
        const incoming = (Array.isArray(v) ? v : []).map((s, i) => (
          k === 'redeye' ? { mode: 'redeye', ...s } : { mode: 'heal', ...s }));
        const others = this._spots.filter(s =>
          k === 'spots' ? s.mode === 'redeye' : s.mode !== 'redeye');
        this._spots = [...others, ...incoming].map((s, i) => ({ ...s, id: s.id || i + 1 }));
        this._spotSel = -1;
        this._spotDraw();
        return;
      }
      if (k === 'calibration') {
        const c = (v && typeof v === 'object') ? v : {};
        ['shadows-tint', 'r-hue', 'r-sat', 'g-hue', 'g-sat', 'b-hue', 'b-sat'].forEach(kk => {
          const el = document.getElementById(`pe-cal-${kk}`);
          if (!el) return;
          el.value = Math.max(-100, Math.min(100, Math.round(c[kk.replace(/-/g, '_')] || 0)));
          const l = document.getElementById(`pe-cal-${kk}-val`);
          if (l) l.textContent = el.value;
        });
        return;
      }
      if (k === 'optics') {
        const o = (v && typeof v === 'object') ? v : {};
        [['distortion', 'pe-optics-distortion'], ['ca', 'pe-optics-ca'],
         ['vig', 'pe-optics-vig']].forEach(([kk, id]) => {
          const el = document.getElementById(id);
          if (!el) return;
          el.value = Math.max(-100, Math.min(100, Math.round(o[kk] || 0)));
          const l = document.getElementById(id + '-val');
          if (l) l.textContent = el.value;
        });
        return;
      }
      if (k === 'vignette' || k === 'grain') {
        const amt = Array.isArray(v) ? (v[0] || 0) : 0;
        const s = document.getElementById(`pe-${k}`);
        if (s) { s.value = Math.round(amt * 10); const l = document.getElementById(`pe-${k}-val`); if (l) l.textContent = amt.toFixed(1); }
        return;
      }
      if (k === 'rotate_deg') {
        const s = document.getElementById('pe-straighten');
        if (s) { s.value = Math.round((v || 0) * 10); const l = document.getElementById('pe-straighten-val'); if (l) l.textContent = (v || 0).toFixed(1) + '°'; }
        return;
      }
      // 常规键: edits 键下划线 → 元素 id 连字符('wb_temp' → 'pe-wb-temp')
      const el = document.getElementById(`pe-${k.replace(/_/g, '-')}`);
      const valEl = document.getElementById(`pe-${k.replace(/_/g, '-')}-val`);
      if (!el) return;
      if (['auto_levels','auto_straighten','grayscale','sepia','srgb'].includes(k)) { el.checked = !!v; return; }
      if (k === 'log_curve') { el.value = v || ''; return; }
      if (k === 'wb_tint') { el.value = Math.max(-100, Math.min(100, Math.round(v || 0))); }
      else if (k === 'wb_temp' || k === 'denoise') { el.value = Math.max(parseInt(el.min) || 0, Math.min(parseInt(el.max) || 20, v)); }
      else if (k === 'auto_exposure') { el.value = Math.max(0, Math.min(100, Math.round((v || 0) * 100))); }
      else { el.value = Math.round(v * 10); }
      if (valEl) valEl.textContent = this._sliderVal(k);
    });
  },

  // ── HSL 分色控件 ──

  _onHslInput(id) {
    const d = this._hslDomain;
    const cur = (this._hsl[d] || [0, 0, 0]).slice();
    const i = id === 'pe-hsl-hue' ? 0 : id === 'pe-hsl-sat' ? 1 : 2;
    const raw = parseInt(document.getElementById(id).value) || 0;
    cur[i] = i === 0 ? raw : raw / 10;
    if (cur.every(x => Math.abs(x) < 1e-6)) delete this._hsl[d]; else this._hsl[d] = cur;
    this._syncHslLabels();
    this._schedulePreview();
  },

  _syncHslSliders() {
    const cur = this._hsl[this._hslDomain] || [0, 0, 0];
    document.getElementById('pe-hsl-hue').value = Math.round(cur[0]);
    document.getElementById('pe-hsl-sat').value = Math.round(cur[1] * 10);
    document.getElementById('pe-hsl-lum').value = Math.round(cur[2] * 10);
    this._syncHslLabels();
  },

  _syncHslLabels() {
    document.getElementById('pe-hsl-hue-val').textContent = document.getElementById('pe-hsl-hue').value + '°';
    document.getElementById('pe-hsl-sat-val').textContent = (document.getElementById('pe-hsl-sat').value / 10).toFixed(1);
    document.getElementById('pe-hsl-lum-val').textContent = (document.getElementById('pe-hsl-lum').value / 10).toFixed(1);
  },

  // ── 颜色分级控件 ──

  _onCgInput(id) {
    const z = this._gradingZone;
    const cur = (this._grading[z] || [0, 0, 0]).slice();
    const i = id === 'pe-cg-hue' ? 0 : id === 'pe-cg-sat' ? 1 : 2;
    const raw = parseInt(document.getElementById(id).value) || 0;
    cur[i] = i === 0 ? raw : raw / 10;
    // 区域仅在饱和或明度非零时生效(色相单独无作用)
    if (Math.abs(cur[1]) < 1e-6 && Math.abs(cur[2]) < 1e-6) delete this._grading[z]; else this._grading[z] = cur;
    this._syncCgLabels();
    this._schedulePreview();
  },

  _syncCgSliders() {
    const cur = this._grading[this._gradingZone] || [0, 0, 0];
    document.getElementById('pe-cg-hue').value = Math.round(cur[0]);
    document.getElementById('pe-cg-sat').value = Math.round(cur[1] * 10);
    document.getElementById('pe-cg-lum').value = Math.round(cur[2] * 10);
    this._syncCgLabels();
  },

  _syncCgLabels() {
    document.getElementById('pe-cg-hue-val').textContent = document.getElementById('pe-cg-hue').value + '°';
    document.getElementById('pe-cg-sat-val').textContent = (document.getElementById('pe-cg-sat').value / 10).toFixed(1);
    document.getElementById('pe-cg-lum-val').textContent = (document.getElementById('pe-cg-lum').value / 10).toFixed(1);
  },

  // ── 色调曲线(点曲线) ──
  // 前端 PCHIP 与后端 photo_s.grade._monotone_cubic 同构(单调性构造保证，
  // 控制点间绝不过冲)：画布上画的曲线就是导出时的真实映射。

  _curveLut(pts) {
    const xs = pts.map(p => p[0]), ys = pts.map(p => p[1]);
    const n = xs.length;
    const m = new Array(n).fill(0);
    const dx = [], dy = [];
    for (let i = 0; i < n - 1; i++) { dx.push(xs[i + 1] - xs[i]); dy.push(ys[i + 1] - ys[i]); }
    if (n === 2) {
      const s = dy[0] / dx[0];
      for (let i = 0; i < n; i++) m[i] = s;
    } else {
      for (let i = 1; i < n - 1; i++) m[i] = 0.5 * (dy[i - 1] / dx[i - 1] + dy[i] / dx[i]);
      for (let i = 1; i < n - 1; i++) {  // 强制单调(Fritsch–Carlson)
        if (dy[i - 1] * dy[i] <= 0) { m[i] = 0; continue; }
        const a = dy[i - 1] / dx[i - 1], b = dy[i] / dx[i];
        const w1 = 2 * dx[i] + dx[i - 1], w2 = dx[i] + 2 * dx[i - 1];
        const den = w1 / a + w2 / b;
        m[i] = den ? (w1 + w2) / den : 0;
      }
      m[0] = dy[0] / dx[0];
      m[n - 1] = dy[n - 2] / dx[n - 2];  // 末点切线=末段斜率(对应后端 dy[-1]/dx[-1])
    }
    const out = new Array(256);
    let seg = 0;
    for (let x = 0; x <= 255; x++) {
      while (seg < n - 2 && x > xs[seg + 1]) seg++;
      let v;
      if (x <= xs[0]) v = ys[0] + m[0] * (x - xs[0]);
      else if (x >= xs[n - 1]) v = ys[n - 1] + m[n - 1] * (x - xs[n - 1]);
      else {
        const h = dx[seg], t = (x - xs[seg]) / h, t2 = t * t;
        v = (1 + 2 * t) * (1 - t) * (1 - t) * ys[seg] + t * (1 - t) * (1 - t) * h * m[seg]
          + t2 * (3 - 2 * t) * ys[seg + 1] + t2 * (t - 1) * h * m[seg + 1];
      }
      out[x] = Math.max(0, Math.min(255, Math.round(v)));
    }
    return out;
  },

  _curvePts() {  // 当前通道控制点(未存 → 标准对角线)
    return this._curves[this._curveCh] || [[0, 0], [255, 255]];
  },

  _curveIdentity(pts) {
    return pts.length === 2 && pts[0][0] === 0 && pts[0][1] === 0
      && pts[1][0] === 255 && pts[1][1] === 255;
  },

  // 恒等通道出表(空表 = curves: null,与 HSL/分级同一套"仅存生效项"约定)
  _syncCurves() {
    const pts = this._curves[this._curveCh];
    if (pts && this._curveIdentity(pts)) delete this._curves[this._curveCh];
    this._renderCurve();
    this._schedulePreview();
  },

  _curveReset() {
    delete this._curves[this._curveCh];
    this._renderCurve();
    this._schedulePreview();
  },

  _renderCurve() {
    const cv = document.getElementById('pe-curve-canvas');
    if (!cv) return;
    const cssW = cv.clientWidth, cssH = cv.clientHeight;
    if (!cssW || !cssH) return;  // 面板隐藏时无布局,跳过
    const dpr = window.devicePixelRatio || 1;
    const pw = Math.round(cssW * dpr), ph = Math.round(cssH * dpr);
    if (cv.width !== pw || cv.height !== ph) { cv.width = pw; cv.height = ph; }
    const ctx = cv.getContext('2d');
    const W = cv.width, H = cv.height;
    const X = v => v / 255 * W, Y = v => H - v / 255 * H;
    ctx.clearRect(0, 0, W, H);

    // 背后垫当前预览的亮度直方图(与直方图面板同源,LR 式)
    const hist = this._samplePreviewHist();
    if (hist) {
      const max = Math.max(...hist.l) || 1;
      ctx.fillStyle = 'rgba(128,128,128,.22)';
      const bw = W / 256;
      for (let x = 0; x < 256; x++) {
        if (!hist.l[x]) continue;
        const h = (Math.log(1 + hist.l[x]) / Math.log(1 + max)) * H;
        ctx.fillRect(X(x), H - h, bw + 0.6 * dpr, h);
      }
    }
    // 网格 + 对角参考线
    ctx.strokeStyle = 'rgba(128,128,128,.25)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (let i = 1; i < 4; i++) {
      ctx.moveTo(X(i * 64), 0); ctx.lineTo(X(i * 64), H);
      ctx.moveTo(0, Y(i * 64)); ctx.lineTo(W, Y(i * 64));
    }
    ctx.stroke();
    ctx.strokeStyle = 'rgba(128,128,128,.35)';
    ctx.beginPath(); ctx.moveTo(0, H); ctx.lineTo(W, 0); ctx.stroke();

    // 曲线
    const pts = this._curvePts();
    const lut = this._curveLut(pts);
    const color = { rgb: '#eaeaea', r: '#e5484d', g: '#30a46c', b: '#3e63dd' }[this._curveCh] || '#eaeaea';
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.6 * dpr;
    ctx.beginPath();
    for (let x = 0; x <= 255; x++) {
      const px = X(x), py = Y(lut[x]);
      if (x === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
    }
    ctx.stroke();

    // 控制点
    ctx.fillStyle = color;
    ctx.strokeStyle = 'rgba(255,255,255,.85)';
    pts.forEach(p => {
      ctx.beginPath();
      ctx.arc(X(p[0]), Y(p[1]), 4 * dpr, 0, Math.PI * 2);
      ctx.fill();
      ctx.lineWidth = 1.2 * dpr;
      ctx.stroke();
    });
  },

  // 命中测试: px/py 为 0-255 空间,返回控制点索引或 -1
  _curveHit(px, py) {
    const r = document.getElementById('pe-curve-canvas').getBoundingClientRect();
    const tolX = 10 / r.width * 255, tolY = 14 / r.height * 255;
    const pts = this._curvePts();
    for (let i = 0; i < pts.length; i++) {
      if (Math.abs(pts[i][0] - px) <= tolX && Math.abs(pts[i][1] - py) <= tolY) return i;
    }
    return -1;
  },

  _onCurvePointerDown(e) {
    e.preventDefault();
    const cv = document.getElementById('pe-curve-canvas');
    const r = cv.getBoundingClientRect();
    const toPt = (ev) => [
      Math.max(0, Math.min(255, Math.round((ev.clientX - r.left) / r.width * 255))),
      Math.max(0, Math.min(255, Math.round(255 - (ev.clientY - r.top) / r.height * 255))),
    ];
    const [px, py] = toPt(e);
    let idx = this._curveHit(px, py);
    this._curveJustAdded = -1;
    if (idx < 0) {  // 空白点击 → 在曲线上新增一点(上限 12)
      const cur = this._curvePts();
      if (cur.length >= 12) return;
      const lut = this._curveLut(cur);
      const pts = cur.slice();
      idx = pts.findIndex(p => p[0] > px);
      if (idx < 0) idx = pts.length;
      pts.splice(idx, 0, [px, lut[Math.max(0, Math.min(255, px))]]);
      this._curves[this._curveCh] = pts;
      this._curveJustAdded = idx;
    }
    this._curveDrag = idx;
    try { cv.setPointerCapture(e.pointerId); } catch (_) { /* ignore */ }
    const move = (ev) => {
      if (this._curveDrag == null) return;
      const [mx, my] = toPt(ev);
      const pts = this._curves[this._curveCh];
      if (!pts) return;
      const i = this._curveDrag;
      const GAP = 8;  // 控制点最小间距,避免 x 重复(后端会对重 x 去重)
      let x = mx;
      if (i === 0) x = 0;                            // 端点锁 x,只调 y
      else if (i === pts.length - 1) x = 255;
      else x = Math.max(pts[i - 1][0] + GAP, Math.min(pts[i + 1][0] - GAP, x));
      pts[i] = [x, my];
      this._renderCurve();
      this._schedulePreview();
    };
    const up = () => {
      this._curveDrag = null;
      cv.removeEventListener('pointermove', move);
      cv.removeEventListener('pointerup', up);
      cv.removeEventListener('pointercancel', up);
      this._syncCurves();  // 把端点拖回原位 → 该通道自动出表
    };
    cv.addEventListener('pointermove', move);
    cv.addEventListener('pointerup', up);
    cv.addEventListener('pointercancel', up);
  },

  _onCurveDblClick(e) {
    const cv = document.getElementById('pe-curve-canvas');
    const r = cv.getBoundingClientRect();
    const px = Math.round((e.clientX - r.left) / r.width * 255);
    const py = Math.round(255 - (e.clientY - r.top) / r.height * 255);
    const i = this._curveHit(px, py);
    if (i === this._curveJustAdded) { this._curveJustAdded = -1; return; }  // 双击加点手势,勿删
    const pts = this._curvePts();
    if (i <= 0 || i >= pts.length - 1) return;  // 端点不可删
    pts.splice(i, 1);
    this._syncCurves();
  },

  // ── 局部调整蒙版（P1-3，参照 LightCraft 无 AI 启发式路线）──
  // 几何坐标一律 0-1 相对坐标（分辨率无关）；应用语义见后端 local_masks.py。

  _maskAdjDefs: [
    { key: 'exposure',   min: -30, max: 30, def: 0,  scale: 10, i18n: 'peExposure',   label: '曝光' },
    { key: 'contrast',   min: 0,   max: 20, def: 10, scale: 10, i18n: 'peContrast',   label: '对比度' },
    { key: 'saturation', min: 0,   max: 20, def: 10, scale: 10, i18n: 'peSaturation', label: '饱和度' },
    { key: 'vibrance',   min: -10, max: 10, def: 0,  scale: 10, i18n: 'peVibrance',   label: '自然饱和度' },
    { key: 'warmth',     min: -10, max: 10, def: 0,  scale: 10, i18n: 'peWarmth',     label: '色温' },
    { key: 'shadows',    min: -10, max: 10, def: 0,  scale: 10, i18n: 'peShadows',    label: '阴影' },
    { key: 'highlights', min: -10, max: 10, def: 0,  scale: 10, i18n: 'peHighlights', label: '高光' },
    { key: 'clarity',    min: -10, max: 10, def: 0,  scale: 10, i18n: 'peClarity',    label: '清晰度' },
    { key: 'texture',    min: -10, max: 10, def: 0,  scale: 10, i18n: 'peTexture',    label: '纹理' },
    { key: 'dehaze',     min: -10, max: 10, def: 0,  scale: 10, i18n: 'peDehaze',     label: '去雾' },
    { key: 'sharpness',  min: 0,   max: 20, def: 0,  scale: 10, i18n: 'peSharpness',  label: '锐度' },
  ],

  _maskAdjDef(key) {
    return this._maskAdjDefs.find(d => d.key === key);
  },

  _maskAdjBuild() {
    const wrap = document.getElementById('pe-ma-sliders');
    if (!wrap || wrap.dataset.built) return;
    wrap.dataset.built = '1';
    wrap.innerHTML = this._maskAdjDefs.map(d => `
      <div class="edit-slider-row pe-ma-row" data-key="${d.key}">
        <span><i class="ph ph-dot"></i> <span>${t(d.i18n) || d.label}</span></span>
        <input type="range" min="${d.min}" max="${d.max}" value="${d.def}" step="1">
        <span class="edit-val">${(d.def / d.scale).toFixed(1)}</span>
      </div>`).join('');
  },

  _maskTypeLabel(type) {
    const key = 'peMask' + type.charAt(0).toUpperCase() + type.slice(1);
    return t(key) || type;
  },

  _maskRenderList() {
    const el = document.getElementById('pe-mask-list');
    if (!el) return;
    if (!this._masks.length) {
      el.innerHTML = `<div class="pe-curve-hint">${t('peMaskEmpty')}</div>`;
      return;
    }
    el.innerHTML = '';
    this._masks.forEach((m, i) => {
      const chip = document.createElement('div');
      chip.className = 'pe-mask-chip' + (i === this._maskSel ? ' active' : '');
      chip.innerHTML = `<span>${this._maskTypeLabel(m.type)} ${i + 1}</span>` +
        `<button class="pe-mask-chip-x" title="✕">✕</button>`;
      chip.addEventListener('click', (e) => {
        if (e.target.closest('.pe-mask-chip-x')) { this._maskDelete(i); return; }
        this._maskSel = i;
        this._maskExitMode();
        this._maskSyncEditor();
      });
      el.appendChild(chip);
    });
  },

  _maskSyncEditor() {
    const editor = document.getElementById('pe-mask-editor');
    const layer = document.getElementById('pe-mask-layer');
    const m = this._masks[this._maskSel] || null;
    if (editor) editor.classList.toggle('hidden', !m);
    const geo = !!(m && (m.type === 'linear' || m.type === 'radial' || m.type === 'brush'));
    if (layer) {
      layer.classList.toggle('active', !!this._maskMode || geo);
      layer.classList.toggle('brush', this._maskMode === 'paint' || (geo && m.type === 'brush'));
    }
    this._maskRenderList();
    this._maskOverlayResize();
    if (!m) return;
    document.getElementById('pe-ma-amount').value = Math.round((m.amount != null ? m.amount : 1) * 10);
    document.getElementById('pe-ma-amount-val').textContent = (m.amount != null ? m.amount : 1).toFixed(1);
    document.getElementById('pe-ma-invert').checked = !!m.invert;
    this._maskAdjDefs.forEach(d => {
      const row = document.querySelector(`.pe-ma-row[data-key="${d.key}"]`);
      if (!row) return;
      const dv = d.def / d.scale;  // 语义默认值
      const v = m.adjustments[d.key] != null ? m.adjustments[d.key] : dv;
      row.querySelector('input').value = Math.round(v * d.scale);
      row.querySelector('.edit-val').textContent = v.toFixed(1);
    });
    this._mcSync();
  },

  // ── 蒙版组件组合（P4-1：add=并集 / sub=减去 / inter=相交，LR 语义）──

  // 旧格式(顶层 type/params)统一为单组件
  _mcNorm(m) {
    if (!Array.isArray(m.components) || !m.components.length) {
      m.components = [{ type: m.type, params: m.params || {}, mode: 'add' }];
    }
    return m;
  },

  // 顶层 type/params 始终镜像首组件（可读性 + 后端旧格式兼容）
  _mcMirror(m) {
    this._mcNorm(m);
    m.type = m.components[0].type;
    m.params = m.components[0].params;
    return m;
  },

  _mcAdd(type) {
    const m = this._masks[this._maskSel];
    if (!m) { App.toast(t('peMaskEmpty'), 'warning'); return; }
    this._mcNorm(m);
    this._maskModeTarget = 'comp';
    this._maskCompSel = m.components.length;
    if (['luminance', 'sky', 'subject', 'background'].includes(type)) {
      this._mcCreate(type, {});
      return;
    }
    if (type === 'color') { this._maskSetMode('pick-color'); return; }
    this._maskSetMode(type === 'linear' ? 'draw-linear'
      : type === 'radial' ? 'draw-radial' : 'paint');
  },

  _mcCreate(type, params) {
    const m = this._masks[this._maskSel];
    if (!m) return;
    this._mcNorm(m);
    m.components.push({ type, params: params || {}, mode: 'add' });
    this._maskCompSel = m.components.length - 1;
    if (type !== 'brush') this._maskMode = null;
    this._mcMirror(m);
    this._mcSync();
    this._schedulePreview();
  },

  _mcSetMode(mode) {
    const m = this._masks[this._maskSel];
    if (!m) return;
    this._mcNorm(m);
    const c = m.components[this._maskCompSel];
    if (!c || this._maskCompSel === 0) return;  // 首组件是基底,无组合模式
    c.mode = mode;
    $('pe-mc-modes').querySelectorAll('[data-m]').forEach(b =>
      b.classList.toggle('active', b.dataset.m === mode));
    this._schedulePreview();
  },

  _mcDelete(i) {
    const m = this._masks[this._maskSel];
    if (!m) return;
    this._mcNorm(m);
    if (m.components.length <= 1) { this._maskDelete(this._maskSel); return; }
    m.components.splice(i, 1);
    this._maskCompSel = Math.min(this._maskCompSel, m.components.length - 1);
    this._mcMirror(m);
    this._mcSync();
    this._schedulePreview();
  },

  _mcRenderList(m) {
    const el = document.getElementById('pe-mc-list');
    if (!el) return;
    this._mcNorm(m);
    el.innerHTML = '';
    m.components.forEach((c, i) => {
      const chip = document.createElement('div');
      chip.className = 'pe-mask-chip' + (i === this._maskCompSel ? ' active' : '');
      const glyph = i === 0 ? '·' : (c.mode === 'sub' ? '－' : c.mode === 'inter' ? '∩' : '＋');
      chip.innerHTML = `<span>${this._maskTypeLabel(c.type)} ${glyph}</span>` +
        `<button class="pe-mask-chip-x" title="✕">✕</button>`;
      chip.addEventListener('click', (e) => {
        if (e.target.closest('.pe-mask-chip-x')) { this._mcDelete(i); return; }
        this._maskCompSel = i;
        this._maskExitMode();
        this._maskSyncEditor();
      });
      el.appendChild(chip);
    });
  },

  _mcSync() {
    const m = this._masks[this._maskSel];
    if (!m) return;
    this._mcNorm(m);
    this._mcRenderList(m);
    const modes = document.getElementById('pe-mc-modes');
    if (modes) {
      const c = m.components[this._maskCompSel];
      const cur = (this._maskCompSel > 0 && c) ? (c.mode || 'add') : 'add';
      modes.classList.toggle('hidden', this._maskCompSel === 0);
      modes.querySelectorAll('[data-m]').forEach(b =>
        b.classList.toggle('active', b.dataset.m === cur));
    }
    this._maskOverlayResize();
  },

  _maskAdd(type) {
    if (this._cropMode) this._exitCrop(true);
    this._maskModeTarget = 'new';
    if (['luminance', 'sky', 'subject', 'background'].includes(type)) {
      this._maskCreate(type, {});
      return;
    }
    if (type === 'color') { this._maskSetMode('pick-color'); return; }
    this._maskSetMode(type === 'linear' ? 'draw-linear'
      : type === 'radial' ? 'draw-radial' : 'paint');
  },

  _maskSetMode(mode) {
    if (mode && this._zoom) this._zoomFit();
    this._maskMode = mode;
    const layer = document.getElementById('pe-mask-layer');
    const hint = document.getElementById('pe-mask-hint');
    const HINTS = { 'draw-linear': 'peMaskHintLinear', 'draw-radial': 'peMaskHintRadial',
      paint: 'peMaskHintBrush', 'pick-color': 'peMaskHintColor' };
    if (layer) layer.classList.toggle('active', !!mode);
    if (mode) {
      if (hint) { hint.textContent = t(HINTS[mode]) || mode; hint.classList.remove('hidden'); }
      this._syncToolRail('mask');
    } else if (hint) {
      hint.textContent = '';
      hint.classList.add('hidden');
      this._syncToolRail('edit');
    }
    this._maskSyncEditor();  // 刷 layer active 态(含选中蒙版几何态)
    this._maskOverlayResize();
  },

  _maskExitMode() {
    this._maTmp = null;
    if (this._maskMode) {
      this._maskMode = null;
      const hint = document.getElementById('pe-mask-hint');
      if (hint) { hint.textContent = ''; hint.classList.add('hidden'); }
      this._syncToolRail('edit');
    }
    this._maskSyncEditor();
    this._maskOverlayDraw();
  },

  _maskCreate(type, params) {
    this._maskSeq += 1;
    const m = { id: this._maskSeq, type, invert: false, amount: 1.0,
      params: params || {}, adjustments: {},
      components: [{ type, params: params || {}, mode: 'add' }] };
    this._masks.push(m);
    this._maskSel = this._masks.length - 1;
    this._maskCompSel = 0;
    if (type !== 'brush') this._maskMode = null;
    this._maskSyncEditor();
    this._schedulePreview();
  },

  _maskDelete(idx) {
    if (idx == null || idx < 0 || idx >= this._masks.length) return;
    this._masks.splice(idx, 1);
    this._maskSel = Math.min(this._maskSel, this._masks.length - 1);
    this._maskExitMode();
    this._maskSyncEditor();
    this._schedulePreview();
  },

  _maskActiveGeom() {
    // 选中蒙版的选中组件（components 模式），回退顶层 type/params（旧数据）
    const m = this._masks[this._maskSel];
    if (!m) return null;
    this._mcNorm(m);
    return m.components[this._maskCompSel] || m.components[0] || null;
  },

  _maskPins(geom) {
    if (!geom) return [];
    if (geom.type === 'linear') {
      const p = geom.params || {};
      return [{ x: p.x1, y: p.y1 }, { x: p.x2, y: p.y2 }];
    }
    if (geom.type === 'radial') {
      const p = geom.params || {};
      const cx = p.cx || 0.5, cy = p.cy || 0.5;
      const ang = p.angle || 0;
      return [{ x: cx, y: cy },
        { x: cx + Math.cos(ang) * (p.rx || 0.3),
          y: cy + Math.sin(ang) * (p.ry || 0.3) }];
    }
    return [];
  },

  _maskHitPin(rel, geom, rect) {
    const pins = this._maskPins(geom);
    for (let i = 0; i < pins.length; i++) {
      const dx = (pins[i].x - rel[0]) * rect.width;
      const dy = (pins[i].y - rel[1]) * rect.height;
      if (Math.hypot(dx, dy) < 14) return i;
    }
    return null;
  },

  _maskPointerDown(e) {
    e.preventDefault();
    e.stopPropagation();
    const layer = document.getElementById('pe-mask-layer');
    const r = layer.getBoundingClientRect();
    if (!r.width || !r.height) return;
    const toRel = (ev) => [
      Math.max(0, Math.min(1, (ev.clientX - r.left) / r.width)),
      Math.max(0, Math.min(1, (ev.clientY - r.top) / r.height)),
    ];
    const dragUntil = (move, up) => {
      document.addEventListener('pointermove', move);
      document.addEventListener('pointerup', up);
      document.addEventListener('pointercancel', up);
    };

    // 创建: 线性/径向 (拖画)
    if (this._maskMode === 'draw-linear' || this._maskMode === 'draw-radial') {
      const [x1, y1] = toRel(e);
      this._maTmp = this._maskMode === 'draw-linear'
        ? { x1, y1, x2: x1, y2: y1 }
        : { cx: x1, cy: y1, rx: 0.02, ry: 0.02, angle: 0 };
      this._maskOverlayDraw();
      const move = (ev) => {
        const [x2, y2] = toRel(ev);
        const t = this._maTmp;
        if (!t) return;
        if (t.x1 != null) { t.x2 = x2; t.y2 = y2; }
        else {
          t.rx = Math.max(0.02, Math.abs(x2 - t.cx));
          t.ry = Math.max(0.02, Math.abs(y2 - t.cy));
          t.angle = Math.atan2(y2 - t.cy, x2 - t.cx);
        }
        this._maskOverlayDraw();
      };
      const up = () => {
        const type = (this._maskMode === 'draw-linear') ? 'linear' : 'radial';
        const tmp = this._maTmp;
        this._maTmp = null;
        if (!tmp) return;
        if (this._maskModeTarget === 'comp') this._mcCreate(type, tmp);
        else this._maskCreate(type, tmp);
      };
      dragUntil(move, up);
      return;
    }

    // 涂抹: 笔刷（写入选中蒙版的选中 brush 组件）
    if (this._maskMode === 'paint') {
      const m = this._masks[this._maskSel];
      const geom = this._maskActiveGeom();
      if (!m || !geom || geom.type !== 'brush') return;
      const stampR = this._maskBrushR || 0.06;  // 笔刷半径(相对短边)，可用 [ ] 调整
      const add = (ev) => {
        const [x, y] = toRel(ev);
        const st = geom.params.stamps || (geom.params.stamps = []);
        const last = st[st.length - 1];
        if (last) {
          const d = Math.hypot((x - last.x) * r.width, (y - last.y) * r.height);
          if (d < stampR * Math.min(r.width, r.height) * 0.35) return;  // 间距节流
        }
        if (st.length < 800) st.push({ x, y, r: stampR });
        this._maskOverlayDraw();
        this._schedulePreview();
      };
      add(e);
      dragUntil(add, () => {});
      return;
    }

    // 取色: 颜色范围
    if (this._maskMode === 'pick-color') {
      const [x, y] = toRel(e);
      const rgb = this._maskSampleColor(x, y);
      this._maskExitMode();
      if (!rgb) { App.toast(t('peMaskPickFail'), 'warning'); return; }
      if (this._maskModeTarget === 'comp') this._mcCreate('color', { rgb, tolerance: 0.28 });
      else this._maskCreate('color', { rgb, tolerance: 0.28 });
      return;
    }

    // 编辑: 选中组件的手柄拖动
    const m = this._masks[this._maskSel];
    const geom = this._maskActiveGeom();
    if (!m || !geom || (geom.type !== 'linear' && geom.type !== 'radial')) return;
    const pin = this._maskHitPin(toRel(e), geom, r);
    if (pin == null) return;
    const move = (ev) => {
      const [x, y] = toRel(ev);
      const p = geom.params || (geom.params = {});
      if (geom.type === 'linear') {
        p[pin === 0 ? 'x1' : 'x2'] = x;
        p[pin === 0 ? 'y1' : 'y2'] = y;
      } else if (pin === 0) {
        p.cx = x;
        p.cy = y;
      } else {
        p.angle = Math.atan2(y - (p.cy || 0.5), x - (p.cx || 0.5));
        p.rx = Math.max(0.02, Math.abs(x - (p.cx || 0.5)));
        p.ry = Math.max(0.02, Math.abs(y - (p.cy || 0.5)));
      }
      this._mcMirror(m);
      this._maskOverlayDraw();
      this._schedulePreview();
    };
    dragUntil(move, () => {});
  },

  _maskSampleColor(x, y) {
    try {
      const img = document.getElementById('pe-preview-img');
      if (!img || !img.naturalWidth) return null;
      const tmp = this._histTmp || (this._histTmp = document.createElement('canvas'));
      tmp.width = 1;
      tmp.height = 1;
      const c = tmp.getContext('2d', { willReadFrequently: true });
      const sx = Math.max(0, Math.min(img.naturalWidth - 1, Math.round(x * img.naturalWidth)));
      const sy = Math.max(0, Math.min(img.naturalHeight - 1, Math.round(y * img.naturalHeight)));
      c.drawImage(img, sx, sy, 1, 1, 0, 0, 1, 1);
      const d = c.getImageData(0, 0, 1, 1).data;
      return [d[0], d[1], d[2]];
    } catch (_) {
      return null;  // local-image:// 等协议可能污染画布 → 取色失败
    }
  },

  _maskOverlayResize() {
    const layer = document.getElementById('pe-mask-layer');
    const cv = document.getElementById('pe-mask-canvas');
    if (!layer || !cv) return;
    const w = layer.clientWidth, h = layer.clientHeight;
    if (!w || !h) return;
    const dpr = window.devicePixelRatio || 1;
    const pw = Math.round(w * dpr), ph = Math.round(h * dpr);
    if (cv.width !== pw || cv.height !== ph) { cv.width = pw; cv.height = ph; }
    this._maskOverlayDraw();
  },

  _maskPin(ctx, x, y) {
    ctx.beginPath();
    ctx.arc(x, y, 6, 0, Math.PI * 2);
    ctx.fillStyle = '#fff';
    ctx.fill();
    ctx.strokeStyle = 'rgba(229,72,77,.95)';
    ctx.lineWidth = 2.5;
    ctx.stroke();
  },

  _maskOverlayDraw() {
    const cv = document.getElementById('pe-mask-canvas');
    if (!cv || !cv.width) return;
    const ctx = cv.getContext('2d');
    const W = cv.width, H = cv.height;
    const X = v => v * W, Y = v => v * H;
    ctx.clearRect(0, 0, W, H);

    const drawLinear = (p) => {
      const x1 = X(p.x1), y1 = Y(p.y1), x2 = X(p.x2), y2 = Y(p.y2);
      const g = ctx.createLinearGradient(x1, y1, x2, y2);
      g.addColorStop(0, 'rgba(255,80,80,.30)');
      g.addColorStop(1, 'rgba(255,80,80,0)');
      ctx.fillStyle = g;
      ctx.fillRect(0, 0, W, H);
      ctx.strokeStyle = 'rgba(255,255,255,.9)';
      ctx.lineWidth = 1.5;
      ctx.setLineDash([6, 5]);
      ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke();
      ctx.setLineDash([]);
      this._maskPin(ctx, x1, y1);
      this._maskPin(ctx, x2, y2);
    };
    const drawRadial = (p) => {
      const cx = X(p.cx), cy = Y(p.cy);
      const rx = (p.rx || 0.3) * W, ry = (p.ry || 0.3) * H;
      const rot = p.angle || 0;
      ctx.save();
      ctx.translate(cx, cy); ctx.rotate(rot); ctx.translate(-cx, -cy);
      const g = ctx.createRadialGradient(cx, cy, 0, cx, cy, Math.max(rx, ry, 1));
      g.addColorStop(0, 'rgba(255,80,80,.28)');
      g.addColorStop(0.55, 'rgba(255,80,80,.14)');
      g.addColorStop(1, 'rgba(255,80,80,0)');
      ctx.fillStyle = g;
      ctx.beginPath(); ctx.ellipse(cx, cy, rx, ry, 0, 0, Math.PI * 2); ctx.fill();
      ctx.strokeStyle = 'rgba(255,255,255,.85)';
      ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.ellipse(cx, cy, rx, ry, 0, 0, Math.PI * 2); ctx.stroke();
      ctx.restore();
      this._maskPin(ctx, cx, cy);
      this._maskPin(ctx, cx + Math.cos(rot) * rx, cy + Math.sin(rot) * ry);
    };
    const drawBrush = (geom) => {
      const scale = Math.min(W, H);
      ctx.strokeStyle = 'rgba(255,80,80,.55)';
      ctx.lineWidth = 1.2;
      ((geom.params || {}).stamps || []).forEach(s => {
        ctx.beginPath();
        ctx.arc(X(s.x), Y(s.y), (s.r || 0.06) * scale, 0, Math.PI * 2);
        ctx.stroke();
      });
    };

    if (this._maTmp) {
      if (this._maTmp.x1 != null) drawLinear(this._maTmp);
      else drawRadial(this._maTmp);
      return;
    }
    const m = this._masks[this._maskSel];
    if (!m) return;
    const geom = this._maskActiveGeom();
    if (!geom) return;
    if (geom.type === 'linear') drawLinear(geom.params || {});
    else if (geom.type === 'radial') drawRadial(geom.params || {});
    else if (geom.type === 'brush') drawBrush(geom);
  },

  // ── LR 修改参数导入（P2-1：XMP crs: 读取）──

  async _importLr() {
    if (!this._path) return;
    const btn = document.getElementById('pe-lr-import');
    if (btn) btn.disabled = true;
    try {
      const r = await window.api.sendCommand('import_lr_develop', { path: this._path });
      if (!r || r.error) { App.toast(t('peLrImportFail') + ((r && r.error) || 'unknown'), 'error'); return; }
      if (!r.edits || !Object.keys(r.edits).length) { App.toast(t('peLrImportNone'), 'warning'); return; }
      this._edits = { ...this._edits, ...r.edits };
      this._setSliders(r.edits);
      this._updatePreview();
      const extra = (r.unmatched && r.unmatched.length)
        ? ` (${t('peLrImportSkip', { n: r.unmatched.length })})` : '';
      App.toast(t('peLrImportOk', { n: Object.keys(r.edits).length }) + extra, 'success');
    } catch (e) {
      App.toast(t('peLrImportFail') + e.message, 'error');
    } finally {
      if (btn) btn.disabled = false;
    }
  },

  // ── 数值点击直输(LR 式) ──

  _openValInput(span) {
    if (span.dataset.editing) return;
    const name = span.id.slice(3, -4);  // 'pe-xxx-val' → 'xxx'
    const slider = document.getElementById(`pe-${name}`);
    if (!slider) return;
    span.dataset.editing = '1';
    const m = span.textContent.trim().match(/-?\d+\.?\d*/);
    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'edit-val-input';
    input.value = m ? m[0] : '0';  // '关'/'Off' 等非数值 → 0
    span.classList.add('hidden');
    span.parentNode.insertBefore(input, span.nextSibling);
    input.focus();
    input.select();
    let done = false;
    const finish = (commit) => {
      if (done) return;
      done = true;
      if (commit) {
        const d = parseFloat(input.value);
        if (!isNaN(d)) {
          const raw = this._displayToRaw(name, d);
          slider.value = Math.max(parseFloat(slider.min), Math.min(parseFloat(slider.max), raw));
          // 复用既有 input 接线: 标签刷新 + 防抖预览 + HSL/分级状态写入
          slider.dispatchEvent(new Event('input'));
        }
      }
      input.remove();
      span.classList.remove('hidden');
      delete span.dataset.editing;
    };
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); finish(true); }
      else if (e.key === 'Escape') { e.preventDefault(); finish(false); }
    });
    input.addEventListener('blur', () => finish(true));
  },

  // 显示值 → 滑杆原始值(各滑杆的量纲换算,与 _sliderVal 互逆)
  _displayToRaw(name, d) {
    if (['wb-temp', 'wb-tint', 'denoise', 'levels-black', 'levels-white',
         'hsl-hue', 'cg-hue'].includes(name)) return Math.round(d);
    if (name === 'auto-exposure' || name === 'levels-gamma') return Math.round(d * 100);
    return Math.round(d * 10);  // straighten 及其余 ×10 滑杆
  },

  // ── 几何: 旋转 / 翻转(裁剪框因坐标空间变化而清除) ──

  _rotate(dir) {  // dir: 1 = 顺时针 90°, -1 = 逆时针
    const cur = this._edits.rotation || 0;
    this._edits.rotation = (((cur + dir * 90) % 360) + 360) % 360;
    this._onGeometryChange();
  },

  _flip(axis) {  // 'flip_h' | 'flip_v'
    this._edits[axis] = !this._edits[axis];
    this._onGeometryChange();
  },

  _onGeometryChange() {
    if (this._edits.crop) {
      this._edits.crop = null;
      App.toast(t('peCropReset'), 'info');
    }
    this._schedulePreview();
  },

  // 有效(应用旋转/水平校正后的)原图尺寸 — 裁剪坐标空间
  _effDims() {
    let w = this._imgW, h = this._imgH;
    if (!w || !h) return null;
    const rot = this._edits.rotation || 0;
    if (rot === 90 || rot === 270) { const tmp = w; w = h; h = tmp; }
    const rd = ((parseInt(document.getElementById('pe-straighten').value) || 0) / 10) * Math.PI / 180;
    if (Math.abs(rd) > 1e-6) {  // expand=True 的画布膨胀公式
      const c = Math.abs(Math.cos(rd)), s = Math.abs(Math.sin(rd));
      const nw = w * c + h * s;
      h = w * s + h * c;
      w = nw;
    }
    return { w, h };
  },

  // 编辑后（裁剪切过）的完整图像尺寸：1:1 与 tile 判定以此为准，避免裁剪后
  // 仍按原图宽度放大、把 tile 变成上采样。
  _editedDims() {
    const eff = this._effDims();
    if (!eff) return null;
    let w = eff.w, h = eff.h;
    const crop = this._edits && this._edits.crop;
    if (crop && crop.length === 4) {
      const cw = Number(crop[2]), ch = Number(crop[3]);
      if (cw > 0 && ch > 0) { w = cw; h = ch; }
    }
    return { w, h };
  },

  // ── 裁剪模式 ──

  _enterCrop() {
    if (this._cropMode) return;
    if (this._baMode) this._toggleBA();
    if (this._zoom) this._zoomFit();
    if (this._maskMode) this._maskExitMode();
    if (this._spotOn) this._spotToggle(false);
    this._cropMode = true;
    this._cropRect = null;
    document.getElementById('pe-crop-layer').classList.remove('hidden');
    document.getElementById('pe-crop-bar').classList.remove('hidden');
    document.getElementById('pe-crop-btn').classList.add('pe-active');
    this._syncToolRail('crop');
    this._initCropRect();
  },

  _exitCrop(discard) {
    if (!this._cropMode) return;
    this._cropMode = false;
    this._cropRect = null;
    document.getElementById('pe-crop-layer').classList.add('hidden');
    document.getElementById('pe-crop-bar').classList.add('hidden');
    document.getElementById('pe-crop-btn').classList.remove('pe-active');
    this._syncToolRail('edit');
  },

  _ratioValue() {
    if (this._cropRatio === 'free') return null;
    if (this._cropRatio === 'orig') {
      const eff = this._effDims();
      return eff ? eff.w / eff.h : null;
    }
    const [a, b] = this._cropRatio.split(':').map(Number);
    return (a && b) ? a / b : null;
  },

  _initCropRect() {
    const layer = document.getElementById('pe-crop-layer');
    const R = layer.getBoundingClientRect();
    if (!R.width || !R.height) return;  // 图未加载完,等 load 钩子再来
    // 已有裁剪 → 反映射到显示坐标
    const cur = this._edits.crop;
    const eff = this._effDims();
    if (cur && cur[2] > 0 && cur[3] > 0 && eff) {
      const s = R.width / eff.w;
      this._cropRect = { x: cur[0] * s, y: cur[1] * s, w: cur[2] * s, h: cur[3] * s };
    } else {
      // 默认: 居中 80%(锁定比例时按宽适配)
      const rv = this._ratioValue();
      let w = R.width * 0.8, h = rv ? w / rv : R.height * 0.8;
      if (h > R.height * 0.8) { h = R.height * 0.8; if (rv) w = h * rv; }
      this._cropRect = { x: (R.width - w) / 2, y: (R.height - h) / 2, w, h };
    }
    this._renderCropBox();
  },

  _clampCropRect() {
    const layer = document.getElementById('pe-crop-layer');
    const R = layer.getBoundingClientRect();
    const rc = this._cropRect;
    if (!rc || !R.width) return;
    rc.w = Math.max(20, Math.min(rc.w, R.width));
    rc.h = Math.max(20, Math.min(rc.h, R.height));
    rc.x = Math.max(0, Math.min(rc.x, R.width - rc.w));
    rc.y = Math.max(0, Math.min(rc.y, R.height - rc.h));
  },

  _renderCropBox() {
    const box = document.getElementById('pe-crop-box');
    const shade = document.getElementById('pe-crop-shade');
    const rc = this._cropRect;
    if (!box || !shade) return;
    if (!rc) { box.style.display = 'none'; shade.style.clipPath = ''; return; }
    box.style.display = 'block';
    box.style.left = rc.x + 'px';
    box.style.top = rc.y + 'px';
    box.style.width = rc.w + 'px';
    box.style.height = rc.h + 'px';
    // 暗角遮罩挖洞: 外框顺时针 + 内框逆时针
    const x2 = rc.x + rc.w, y2 = rc.y + rc.h;
    shade.style.clipPath = `polygon(0 0, 100% 0, 100% 100%, 0 100%, 0 0, ${rc.x}px ${rc.y}px, ${rc.x}px ${y2}px, ${x2}px ${y2}px, ${x2}px ${rc.y}px, ${rc.x}px ${rc.y}px)`;
  },

  _onCropPointerDown(e) {
    if (!this._cropMode) return;
    e.preventDefault();
    e.stopPropagation();
    const layer = e.currentTarget;
    const R = layer.getBoundingClientRect();
    const px = e.clientX - R.left, py = e.clientY - R.top;
    const handle = e.target.dataset && e.target.dataset.h;
    const rc = this._cropRect;
    const inBox = rc && px >= rc.x && px <= rc.x + rc.w && py >= rc.y && py <= rc.y + rc.h;
    const mode = handle ? 'resize' : inBox ? 'move' : 'new';
    const start = { px, py, rect: rc ? { ...rc } : null };
    const move = (ev) => {
      const mx = ev.clientX - R.left, my = ev.clientY - R.top;
      const dx = mx - start.px, dy = my - start.py;
      const rv = this._ratioValue();
      if (mode === 'new') {
        let w = Math.abs(dx), h = Math.abs(dy);
        if (rv) h = w / rv;
        this._cropRect = {
          x: Math.min(start.px, start.px + (rv ? Math.sign(dx || 1) * w : dx)),
          y: Math.min(start.py, start.py + (rv ? Math.sign(dy || 1) * h : dy)),
          w, h,
        };
        if (rv) {  // 自由拖时角向符号已在上面处理; 这里只需规范化
          const r2 = this._cropRect;
          r2.x = dx < 0 ? start.px - w : start.px;
          r2.y = dy < 0 ? start.py - h : start.py;
        }
      } else if (mode === 'move') {
        this._cropRect = { ...start.rect, x: start.rect.x + dx, y: start.rect.y + dy };
      } else {  // resize(四角)
        const r0 = start.rect;
        let { x, y, w, h } = r0;
        if (handle === 'se') { w = r0.w + dx; h = rv ? w / rv : r0.h + dy; }
        else if (handle === 'nw') { w = r0.w - dx; h = rv ? w / rv : r0.h - dy; x = r0.x + r0.w - w; y = r0.y + r0.h - h; }
        else if (handle === 'ne') { w = r0.w + dx; h = rv ? w / rv : r0.h - dy; y = r0.y + r0.h - h; }
        else if (handle === 'sw') { w = r0.w - dx; h = rv ? w / rv : r0.h + dy; x = r0.x + r0.w - w; }
        this._cropRect = { x, y, w, h };
      }
      this._clampCropRect();
      this._renderCropBox();
    };
    const up = () => {
      document.removeEventListener('pointermove', move);
      document.removeEventListener('pointerup', up);
    };
    document.addEventListener('pointermove', move);
    document.addEventListener('pointerup', up);
  },

  _applyCrop() {
    const eff = this._effDims();
    const layer = document.getElementById('pe-crop-layer');
    const R = layer.getBoundingClientRect();
    const rc = this._cropRect;
    if (!eff || !rc || !R.width) { this._exitCrop(true); return; }
    const s = eff.w / R.width;
    const crop = [Math.round(rc.x * s), Math.round(rc.y * s), Math.round(rc.w * s), Math.round(rc.h * s)];
    // 近似全图 = 无裁剪
    this._edits.crop = (crop[2] >= eff.w - 4 && crop[3] >= eff.h - 4) ? null : crop;
    this._exitCrop(true);
    this._schedulePreview();
  },

  // ── 缩放(100% = 适应窗口；100–800%，滑块/加减/滚轮/拖拽) ──

  _toggleZoom() {
    if (this._zoom) this._zoomFit();
    else this._setZoom(2);
  },

  _zoomStep(delta) {
    const base = this._zoom ? this._zoomScale : 1;
    this._setZoom(base + delta);
  },

  /** Enter or update zoom. scale is relative to fit size (1 = fit, 0.5 = 50%). */
  _setZoom(scale, anchorEvent) {
    if (!this._path) return;
    const target = Math.max(0.5, Math.min(16, Number(scale) || 1));
    // 100% 回到“适应窗口”的基准语义；低于 100% 进入缩小视图
    if (Math.abs(target - 1) <= 0.001) { this._zoomFit(); return; }
    if (this._cropMode) this._exitCrop(true);
    if (this._maskMode) this._maskExitMode();
    if (this._spotOn) this._spotToggle(false);
    if (this._baMode) this._toggleBA();

    const img = document.getElementById('pe-preview-img');
    const wrap = document.getElementById('pe-img-wrap');
    if (!img || !wrap) return;

    let entered = false;
    if (!this._zoom) {
      const ir = img.getBoundingClientRect();
      if (!ir.width || !ir.height) return;
      const wr = wrap.getBoundingClientRect();
      this._zoomBaseW = ir.width;
      this._zoomBaseH = ir.height;
      wrap.style.width = wr.width + 'px';   // 冻结当前布局盒
      wrap.style.height = wr.height + 'px';
      wrap.classList.add('zoomed');
      this._zoom = true;
      this._zoomScale = 1;
      this._panX = 0; this._panY = 0;
      entered = true;
    }

    const oldScale = this._zoomScale;
    let px = this._panX, py = this._panY;
    if (anchorEvent) {
      const wr = wrap.getBoundingClientRect();
      const dx = anchorEvent.clientX - (wr.left + wr.width / 2);
      const dy = anchorEvent.clientY - (wr.top + wr.height / 2);
      const oldW = this._zoomBaseW * oldScale;
      const oldH = this._zoomBaseH * oldScale;
      const nx = 0.5 + (dx - this._panX) / oldW;  // 光标下的图像归一化位置
      const ny = 0.5 + (dy - this._panY) / oldH;
      const newW = this._zoomBaseW * target;
      const newH = this._zoomBaseH * target;
      px = dx - (nx - 0.5) * newW;                 // 保持该点仍在光标下
      py = dy - (ny - 0.5) * newH;
    }
    this._zoomScale = target;
    this._panX = px;
    this._panY = py;
    this._applyZoomLayout();
    // Interactive preview stays 800px; zoom needs real pixels. Debounce so a
    // wheel/slider burst triggers one high-resolution render, not ten.
    this._scheduleZoomPreview(entered ? 80 : 240);
  },

  _scheduleZoomPreview(delay = 220) {
    clearTimeout(this._zoomPreviewTimer);
    this._zoomPreviewTimer = setTimeout(() => {
      if (this._zoom && this._path) this._updatePreview();
    }, delay);
  },

  // ── >6000px originals: stream the visible viewport as a native tile ──

  _shouldLoadTile() {
    if (!this._zoom || !this._imgW || !this._imgH) return false;
    const ed = this._editedDims();
    if (!ed || Math.max(ed.w, ed.h) <= this._ZOOM_DETAIL_MAX) return false;
    const dispW = this._zoomBaseW * this._zoomScale;
    const dispH = this._zoomBaseH * this._zoomScale;
    const wrap = document.getElementById('pe-img-wrap');
    if (!wrap || !wrap.clientWidth || !wrap.clientHeight) return false;
    return (dispW > this._ZOOM_DETAIL_MAX + 1 && dispH >= wrap.clientHeight - 1) ||
           (dispH > this._ZOOM_DETAIL_MAX + 1 && dispW >= wrap.clientWidth - 1);
  },

  _scheduleTile(delay = 150) {
    clearTimeout(this._tileTimer);
    if (!this._shouldLoadTile()) { this._hideTile(); return; }
    // Hide immediately so the old tile cannot stay visually misaligned while
    // the user is panning/zooming; the base preview fills in until the new tile.
    this._hideTile();
    this._tileTimer = setTimeout(() => this._loadTile(), delay);
  },

  async _loadTile() {
    if (!this._shouldLoadTile() || !this._path) { this._hideTile(); return; }
    const wrap = document.getElementById('pe-img-wrap');
    if (!wrap || !wrap.clientWidth || !wrap.clientHeight) return;
    const dispW = this._zoomBaseW * this._zoomScale;
    const dispH = this._zoomBaseH * this._zoomScale;
    const visW = Math.min(1, wrap.clientWidth / dispW);
    const visH = Math.min(1, wrap.clientHeight / dispH);
    const cx = 0.5 - this._panX / dispW;
    const cy = 0.5 - this._panY / dispH;
    const x = Math.max(0, Math.min(1 - visW, cx - visW / 2));
    const y = Math.max(0, Math.min(1 - visH, cy - visH / 2));
    const region = [x, y, visW, visH].map(v => Math.round(v * 100000) / 100000);
    const key = region.join(',');
    const existing = document.getElementById('pe-tile-img');
    const tileVisible = existing && !existing.classList.contains('hidden');
    if ((key === this._tileRegion && tileVisible) || key === this._tilePending) return;
    const token = ++this._tileToken;
    const path = this._path;
    const edits = this._readEdits();
    this._tileEditsJson = JSON.stringify(edits);
    this._tilePending = key;
    this._hideTile();
    const bar = document.getElementById('pe-zoom-bar');
    if (bar) bar.classList.add('tile-loading');
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    try {
      const r = await window.api.sendCommand('apply_edits_tile', {
        path, edits, region,
        out_width: Math.round(wrap.clientWidth * dpr),
        out_height: Math.round(wrap.clientHeight * dpr),
      });
      if (token !== this._tileToken || path !== this._path || !this._zoom) return;
      this._tilePending = '';
      if (!r || r.error || !r.preview) { this._tileRegion = ''; return; }
      const tile = document.getElementById('pe-tile-img');
      if (!tile) return;
      tile.src = r.preview;
      tile.classList.remove('hidden');
      this._tileRegion = key;
    } catch (_) {
      if (token === this._tileToken) {
        this._tilePending = '';
        this._tileRegion = '';
      }
    } finally {
      if (token === this._tileToken && bar) bar.classList.remove('tile-loading');
    }
  },

  _hideTile() {
    const tile = document.getElementById('pe-tile-img');
    if (tile) {
      tile.classList.add('hidden');
      tile.removeAttribute('src');
    }
  },

  _clearTile() {
    this._tileRegion = '';
    this._tilePending = '';
    clearTimeout(this._tileTimer);
    this._tileTimer = null;
    this._tileToken += 1;
    this._hideTile();
    document.getElementById('pe-zoom-bar')?.classList.remove('tile-loading');
  },

  _zoomFit() {
    clearTimeout(this._zoomPreviewTimer);
    this._zoomPreviewTimer = null;
    this._clearTile();
    const wrap = document.getElementById('pe-img-wrap');
    const img = document.getElementById('pe-preview-img');
    this._zoom = false;
    this._zoomScale = 1;
    this._zoomBaseW = 0; this._zoomBaseH = 0;
    this._panX = 0; this._panY = 0;
    if (wrap) {
      wrap.classList.remove('zoomed');
      wrap.style.width = '';
      wrap.style.height = '';
    }
    if (img) {
      img.style.width = '';
      img.style.height = '';
      img.style.transform = '';
    }
    document.getElementById('pe-zoom-minimap')?.classList.add('hidden');
    this._syncZoomUI();
  },

  /** 1:1 based on original dimensions, capped by the detail render limit. */
  _zoomActual() {
    const img = document.getElementById('pe-preview-img');
    if (!img || !img.naturalWidth) return;
    const baseW = this._zoom ? this._zoomBaseW : img.getBoundingClientRect().width;
    if (!baseW) return;
    const ed = this._editedDims();
    const original = (ed && ed.w) || this._imgW || img.naturalWidth;
    const targetPx = Math.min(original, 12000);
    this._setZoom(targetPx / baseW);
  },

  _applyZoomLayout() {
    if (!this._zoom) return;
    const img = document.getElementById('pe-preview-img');
    const wrap = document.getElementById('pe-img-wrap');
    if (!img || !img.naturalWidth) return;
    const w = this._zoomBaseW * this._zoomScale;
    const h = this._zoomBaseH * this._zoomScale;
    img.style.width = w + 'px';
    img.style.height = h + 'px';
    // 平移范围钳制: 图小于视口时居中
    const maxX = Math.max(0, (w - wrap.clientWidth) / 2);
    const maxY = Math.max(0, (h - wrap.clientHeight) / 2);
    this._panX = Math.max(-maxX, Math.min(maxX, this._panX));
    this._panY = Math.max(-maxY, Math.min(maxY, this._panY));
    img.style.transform = `translate(calc(-50% + ${this._panX}px), calc(-50% + ${this._panY}px))`;
    this._syncZoomUI();
    this._updateZoomMinimap();
    this._scheduleTile();
  },

  // 放大时在图片左下角显示当前视野在整张照片中的位置与大小
  _updateZoomMinimap() {
    const mm = document.getElementById('pe-zoom-minimap');
    if (!mm) return;
    if (!this._zoom || !this._zoomBaseW || !this._zoomBaseH) {
      mm.classList.add('hidden');
      return;
    }
    const img = document.getElementById('pe-preview-img');
    const wrap = document.getElementById('pe-img-wrap');
    if (!img || !wrap || !wrap.clientWidth || !wrap.clientHeight) {
      mm.classList.add('hidden');
      return;
    }
    const dispW = this._zoomBaseW * this._zoomScale;
    const dispH = this._zoomBaseH * this._zoomScale;
    const visW = Math.min(1, wrap.clientWidth / dispW);
    const visH = Math.min(1, wrap.clientHeight / dispH);
    // 缩小时整张照片都在视野内，不显示导航图
    if (visW >= 0.999 && visH >= 0.999) {
      mm.classList.add('hidden');
      return;
    }
    const iw = img.naturalWidth || 1, ih = img.naturalHeight || 1;
    const k = Math.min(150 / iw, 110 / ih);
    mm.style.width = Math.max(56, Math.round(iw * k)) + 'px';
    mm.style.height = Math.max(42, Math.round(ih * k)) + 'px';
    const cx = 0.5 - this._panX / dispW;
    const cy = 0.5 - this._panY / dispH;
    const rx = Math.max(0, Math.min(1 - visW, cx - visW / 2));
    const ry = Math.max(0, Math.min(1 - visH, cy - visH / 2));
    const rect = document.getElementById('pe-zoom-minimap-rect');
    if (rect) {
      rect.style.left = (rx * 100).toFixed(2) + '%';
      rect.style.top = (ry * 100).toFixed(2) + '%';
      rect.style.width = (visW * 100).toFixed(2) + '%';
      rect.style.height = (visH * 100).toFixed(2) + '%';
    }
    mm.classList.remove('hidden');
  },

  _syncZoomUI() {
    const pct = Math.round((this._zoom ? this._zoomScale : 1) * 100);
    const range = document.getElementById('pe-zoom-range');
    const label = document.getElementById('pe-zoom-label');
    const out = document.getElementById('pe-zoom-out');
    const inBtn = document.getElementById('pe-zoom-in');
    const fit = document.getElementById('pe-zoom-fit');
    if (range && document.activeElement !== range) range.value = String(Math.min(1600, Math.max(50, pct)));
    if (label) label.textContent = pct + '%';
    if (out) out.disabled = this._zoom && this._zoomScale <= 0.501;
    if (inBtn) inBtn.disabled = this._zoomScale >= 15.999;
    if (fit) fit.classList.toggle('active', !this._zoom);
  },

  // ── 预览加载完成钩子: 图表 / 缩放 / 裁剪布局 ──

  _onPreviewLoaded() {
    this._renderScopes();
    this._renderCurve();
    this._maskOverlayResize();
    this._spotResize();
    this._syncMinimapImage();
    this._maybeUpgradePreview();
    if (this._zoom) this._applyZoomLayout();
    if (this._cropMode && !this._cropRect) this._initCropRect();
  },

  // 首次打开时预览请求可能早于图片尺寸就绪（回落到 800px）；图片加载后
  // 若视口允许更大分辨率，补一次高清预览，避免大窗口里图片仍按小图显示。
  _maybeUpgradePreview() {
    if (this._zoom || this._presetHoverId || !this._lastPreviewMaxSide) return;
    const target = this._previewMaxSide();
    if (target > this._lastPreviewMaxSide * 1.08) this._updatePreview();
  },

  // 预览换图时同步一次导航图缩略图（避免平移时反复比较超长 data URL）
  _syncMinimapImage() {
    const img = document.getElementById('pe-preview-img');
    const mmImg = document.getElementById('pe-zoom-minimap-img');
    if (!img || !mmImg || !img.src) return;
    if (mmImg.getAttribute('src') !== img.src) mmImg.src = img.src;
  },

  // 采样当前预览的 RGB/亮度直方图(64×64 足够统计;直方图与曲线背后垫图共用)
  _samplePreviewHist() {
    const img = document.getElementById('pe-preview-img');
    if (!img || !img.naturalWidth) return null;
    const S = 64;  // 采样尺寸(足够直方图统计)
    const tmp = this._histTmp || (this._histTmp = document.createElement('canvas'));
    tmp.width = S; tmp.height = S;
    const tctx = tmp.getContext('2d', { willReadFrequently: true });
    let data;
    try {
      tctx.drawImage(img, 0, 0, S, S);
      data = tctx.getImageData(0, 0, S, S).data;
    } catch (_) { return null; }  // 跨域等异常静默
    const r = new Float32Array(256), g = new Float32Array(256),
          b = new Float32Array(256), l = new Float32Array(256);
    for (let i = 0; i < data.length; i += 4) {
      r[data[i]]++; g[data[i + 1]]++; b[data[i + 2]]++;
      l[Math.round(0.299 * data[i] + 0.587 * data[i + 1] + 0.114 * data[i + 2])]++;
    }
    return { r, g, b, l };
  },

  // ── 图表(示波器)：直方图 / 波形图 / RGB 分量图 / 矢量示波器 ──

  _scopeCanvasId(kind) {
    return {
      hist: 'pe-histogram', wave: 'pe-waveform',
      parade: 'pe-parade', vector: 'pe-vectorscope',
    }[kind];
  },

  // 高分屏适配: 画布位图 = 显示尺寸 × DPR
  _scopeTarget(cv) {
    if (!cv) return null;
    const cssW = cv.clientWidth, cssH = cv.clientHeight;
    if (!cssW || !cssH) return null;  // 折叠/隐藏时无布局,跳过
    const dpr = window.devicePixelRatio || 1;
    const pw = Math.round(cssW * dpr), ph = Math.round(cssH * dpr);
    if (cv.width !== pw || cv.height !== ph) { cv.width = pw; cv.height = ph; }
    return { ctx: cv.getContext('2d'), W: cv.width, H: cv.height, dpr };
  },

  // 一次降采样给所有图表共用(256px 长边足够波形/矢量统计)
  _scopeSample() {
    const img = document.getElementById('pe-preview-img');
    if (!img || !img.naturalWidth) return null;
    const nw = img.naturalWidth, nh = img.naturalHeight;
    const scale = Math.min(1, 256 / Math.max(nw, nh));
    const w = Math.max(8, Math.round(nw * scale));
    const h = Math.max(8, Math.round(nh * scale));
    const tmp = this._scopeTmp || (this._scopeTmp = document.createElement('canvas'));
    tmp.width = w; tmp.height = h;
    const tctx = tmp.getContext('2d', { willReadFrequently: true });
    let data;
    try {
      tctx.drawImage(img, 0, 0, w, h);
      data = tctx.getImageData(0, 0, w, h).data;
    } catch (_) { return null; }  // 跨域等异常静默
    return { w, h, data };
  },

  _scopeMax(arr) {
    let m = 0;
    for (let i = 0; i < arr.length; i++) if (arr[i] > m) m = arr[i];
    return m;
  },

  _renderScopes() {
    const sample = this._scopeSample();
    if (!sample) return;
    this._scopeDefs.forEach(def => this._renderScopeKind(def.kind, sample));
  },

  _renderScope(kind) {
    const sample = this._scopeSample();
    if (sample) this._renderScopeKind(kind, sample);
  },

  _renderScopeKind(kind, sample) {
    if (kind === 'hist') return this._renderHistogramCanvas(sample);
    if (kind === 'wave') return this._renderWaveform(sample);
    if (kind === 'parade') return this._renderParade(sample);
    if (kind === 'vector') return this._renderVectorscope(sample);
  },

  // 直方图: RGB + 亮度，低透明填充 + 亮色轮廓线（深底上更清晰）
  _renderHistogramCanvas(sample) {
    const tgt = this._scopeTarget(document.getElementById('pe-histogram'));
    if (!tgt || !sample) return;
    const { ctx, W, H, dpr } = tgt;
    const r = new Float32Array(256), g = new Float32Array(256),
          b = new Float32Array(256), l = new Float32Array(256);
    const data = sample.data;
    for (let i = 0; i < data.length; i += 4) {
      r[data[i]]++; g[data[i + 1]]++; b[data[i + 2]]++;
      l[Math.round(0.299 * data[i] + 0.587 * data[i + 1] + 0.114 * data[i + 2])]++;
    }
    ctx.clearRect(0, 0, W, H);
    const bw = W / 256;
    const draw = (bins, fill, stroke) => {
      const max = this._scopeMax(bins) || 1;
      const hs = new Float32Array(256);
      for (let x = 0; x < 256; x++) {
        if (bins[x]) hs[x] = Math.pow(bins[x] / max, 0.45) * H;
      }
      ctx.fillStyle = fill;
      for (let x = 0; x < 256; x++) {
        if (bins[x]) ctx.fillRect(x * bw, H - hs[x], bw + 0.6, hs[x]);
      }
      ctx.strokeStyle = stroke;
      ctx.lineWidth = Math.max(1, dpr * 0.8);
      ctx.beginPath();
      let started = false;
      for (let x = 0; x < 256; x++) {
        if (!bins[x]) continue;
        const y = H - hs[x];
        if (!started) { ctx.moveTo(x * bw + bw / 2, y); started = true; }
        else ctx.lineTo(x * bw + bw / 2, y);
      }
      ctx.stroke();
    };
    ctx.globalCompositeOperation = 'lighter';
    draw(r, 'rgba(229,72,77,.13)', 'rgba(255,108,108,.85)');
    draw(g, 'rgba(48,164,108,.13)', 'rgba(92,220,150,.85)');
    draw(b, 'rgba(62,99,221,.13)', 'rgba(112,150,255,.85)');
    draw(l, 'rgba(255,255,255,.05)', 'rgba(255,255,255,.5)');
    ctx.globalCompositeOperation = 'source-over';
  },

  // 波形图: 横轴=画面 x, 纵轴=亮度; 密度对数映射
  _renderWaveform(sample) {
    const tgt = this._scopeTarget(document.getElementById('pe-waveform'));
    if (!tgt || !sample) return;
    const { ctx, W, H, dpr } = tgt;
    const BW = 256, BH = 128;
    const grid = new Float32Array(BW * BH);
    const { w, h, data } = sample;
    for (let y = 0; y < h; y++) {
      for (let x = 0; x < w; x++) {
        const i = (y * w + x) * 4;
        const lum = (0.299 * data[i] + 0.587 * data[i + 1] + 0.114 * data[i + 2]) | 0;
        const xb = ((x * BW / w) | 0);
        const yb = Math.min(BH - 1, (lum * BH / 256) | 0);
        grid[yb * BW + xb]++;
      }
    }
    ctx.clearRect(0, 0, W, H);
    ctx.strokeStyle = 'rgba(255,255,255,.06)';
    ctx.lineWidth = Math.max(1, dpr * 0.6);
    for (let i = 1; i < 4; i++) {
      const y = H * i / 4;
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
    }
    const max = this._scopeMax(grid);
    if (!max) return;
    const cw = W / BW, ch = H / BH;
    const inv = 1 / Math.log1p(max);
    ctx.fillStyle = 'rgba(226,238,255,.92)';
    for (let yb = 0; yb < BH; yb++) {
      const py = H - (yb + 1) * ch;
      for (let xb = 0; xb < BW; xb++) {
        const v = grid[yb * BW + xb];
        if (!v) continue;
        const a = Math.pow(Math.log1p(v) * inv, 0.62);
        if (a < 0.04) continue;
        ctx.globalAlpha = Math.min(1, a * 1.15);
        ctx.fillRect(xb * cw, py, cw + 0.6, ch + 0.6);
      }
    }
    ctx.globalAlpha = 1;
  },

  // RGB 分量图: R/G/B 三条波形并排
  _renderParade(sample) {
    const tgt = this._scopeTarget(document.getElementById('pe-parade'));
    if (!tgt || !sample) return;
    const { ctx, W, H, dpr } = tgt;
    const BW = 160, BH = 128;
    const grids = [new Float32Array(BW * BH), new Float32Array(BW * BH), new Float32Array(BW * BH)];
    const { w, h, data } = sample;
    for (let y = 0; y < h; y++) {
      for (let x = 0; x < w; x++) {
        const i = (y * w + x) * 4;
        const xb = ((x * BW / w) | 0);
        for (let c = 0; c < 3; c++) {
          const yb = Math.min(BH - 1, (data[i + c] * BH / 256) | 0);
          grids[c][yb * BW + xb]++;
        }
      }
    }
    ctx.clearRect(0, 0, W, H);
    const third = W / 3;
    const colors = ['255,84,84', '74,214,124', '96,142,255'];
    ctx.strokeStyle = 'rgba(255,255,255,.06)';
    ctx.lineWidth = Math.max(1, dpr * 0.6);
    for (let i = 0; i < 3; i++) {
      ctx.beginPath(); ctx.moveTo(third * i, 0); ctx.lineTo(third * i, H); ctx.stroke();
    }
    const cw = third / BW, ch = H / BH;
    for (let c = 0; c < 3; c++) {
      const max = this._scopeMax(grids[c]);
      const x0 = third * c;
      const color = colors[c];
      ctx.save();
      ctx.beginPath(); ctx.rect(x0, 0, third, H); ctx.clip();
      if (max) {
        const inv = 1 / Math.log1p(max);
        ctx.fillStyle = `rgba(${color},.9)`;
        for (let yb = 0; yb < BH; yb++) {
          const py = H - (yb + 1) * ch;
          for (let xb = 0; xb < BW; xb++) {
            const v = grids[c][yb * BW + xb];
            if (!v) continue;
            const a = Math.pow(Math.log1p(v) * inv, 0.62);
            if (a < 0.04) continue;
            ctx.globalAlpha = Math.min(1, a * 1.05);
            ctx.fillRect(x0 + xb * cw, py, cw + 0.6, ch + 0.6);
          }
        }
      }
      ctx.globalAlpha = 1;
      ctx.fillStyle = `rgba(${color},.55)`;
      ctx.font = `${Math.round(9 * dpr)}px sans-serif`;
      ctx.fillText(['R', 'G', 'B'][c], x0 + 5 * dpr, 11 * dpr);
      ctx.restore();
    }
  },

  // 矢量示波器: Cb/Cr 平面, 叠加皮肤线参考
  _renderVectorscope(sample) {
    const tgt = this._scopeTarget(document.getElementById('pe-vectorscope'));
    if (!tgt || !sample) return;
    const { ctx, W, H, dpr } = tgt;
    const cx = W / 2, cy = H / 2;
    const R = Math.min(W, H) * 0.46;
    const B = 160;
    const grid = new Float32Array(B * B);
    const rs = new Float32Array(B * B), gs = new Float32Array(B * B), bs = new Float32Array(B * B);
    const { w, h, data } = sample;
    for (let y = 0; y < h; y++) {
      for (let x = 0; x < w; x++) {
        const i = (y * w + x) * 4;
        const r = data[i], g = data[i + 1], b = data[i + 2];
        const cb = -0.168736 * r - 0.331264 * g + 0.5 * b;
        const cr = 0.5 * r - 0.418688 * g - 0.081312 * b;
        const xb = Math.max(0, Math.min(B - 1, Math.round((cr / 127.5 * 0.5 + 0.5) * (B - 1))));
        const yb = Math.max(0, Math.min(B - 1, Math.round((cb / 127.5 * 0.5 + 0.5) * (B - 1))));
        const k = yb * B + xb;
        grid[k]++; rs[k] += r; gs[k] += g; bs[k] += b;
      }
    }
    ctx.clearRect(0, 0, W, H);
    // 刻度环 + 十字 + 皮肤线(约 123°)
    ctx.strokeStyle = 'rgba(255,255,255,.10)';
    ctx.lineWidth = Math.max(1, dpr * 0.7);
    for (const f of [0.25, 0.5, 0.75, 1]) {
      ctx.beginPath(); ctx.arc(cx, cy, R * f, 0, Math.PI * 2); ctx.stroke();
    }
    ctx.beginPath();
    ctx.moveTo(cx - R, cy); ctx.lineTo(cx + R, cy);
    ctx.moveTo(cx, cy - R); ctx.lineTo(cx, cy + R);
    ctx.stroke();
    const ang = Math.PI * 123 / 180;
    ctx.strokeStyle = 'rgba(255,180,120,.30)';
    ctx.beginPath();
    ctx.moveTo(cx, cy);
    ctx.lineTo(cx + Math.cos(ang) * R, cy + Math.sin(ang) * R);
    ctx.stroke();
    // 密度点云: 用平均颜色着色, 对数透明度
    const max = this._scopeMax(grid);
    if (max) {
      const inv = 1 / Math.log1p(max);
      const cell = Math.max(1.4, 2 * R / (B - 1));
      ctx.globalCompositeOperation = 'lighter';
      for (let yb = 0; yb < B; yb++) {
        for (let xb = 0; xb < B; xb++) {
          const k = yb * B + xb;
          const v = grid[k];
          if (!v) continue;
          const a = Math.pow(Math.log1p(v) * inv, 0.7);
          if (a < 0.04) continue;
          // 矢量示波器惯例：+Cr 朝左，皮肤线落在左上方
          const px = cx - ((xb / (B - 1)) - 0.5) * 2 * R;
          const py = cy + ((yb / (B - 1)) - 0.5) * 2 * R;
          const rr = Math.min(255, rs[k] / v * 1.5 + 32) | 0;
          const gg = Math.min(255, gs[k] / v * 1.5 + 32) | 0;
          const bb = Math.min(255, bs[k] / v * 1.5 + 32) | 0;
          ctx.fillStyle = `rgb(${rr},${gg},${bb})`;
          ctx.globalAlpha = Math.min(.9, .10 + a * .75);
          ctx.fillRect(px - cell / 2, py - cell / 2, cell, cell);
        }
      }
      ctx.globalAlpha = 1;
      ctx.globalCompositeOperation = 'source-over';
    }
  },

  _renderHistogram() {
    this._renderHistogramCanvas(this._scopeSample());
  },

  // ── 按住 \ 看原图 ──

  _holdOriginal(on) {
    const img = document.getElementById('pe-preview-img');
    if (on) {
      if (this._holdOrig || this._baMode || !this._beforeSrc) return;
      this._holdOrig = true;
      img.dataset.afterSrc = img.src;
      img.src = this._beforeSrc;
    } else {
      if (!this._holdOrig) return;
      this._holdOrig = false;
      if (img.dataset.afterSrc) { img.src = img.dataset.afterSrc; delete img.dataset.afterSrc; }
    }
  },

  // ── 复制编辑参数(粘贴入口在照片编辑页多选工具栏) ──

  _copyEdits() {
    const e = this._readEdits();
    // 单张照片专属键不随剪贴板传播
    delete e.crop;
    delete e.spots;
    delete e.redeye;
    PhotoEdit._clipboard = e;
    App.toast(t('peEditsCopied'), 'success');
  },

  async _loadEdits(path) {
    try {
      const r = await window.api.sendCommand('get_edits', { path });
      if (path !== this._path) return;
      if (r && r.edits) {
        this._edits = r.edits;
        this._setSliders(r.edits);
        // Apply saved edits to the preview
        this._updatePreview();
      }
      this._markLoaded(r && r.edits ? r.edits : {});
    } catch (_) { /* ignore */ }
    finally {
      if (path === this._path) {
        this._editsLoaded = true;
        this._loadPresetThumbs();
      }
    }
  },

  _markLoaded(edits) {
    const current = this._readEdits();
    this._lastCommitted = JSON.parse(JSON.stringify(current));
    this._lastCommittedJson = JSON.stringify(current);
    this._lastSavedJson = this._lastCommittedJson;
    this._undoStacks[this._path] = [];
    this._redoStacks[this._path] = [];
    this._setSaveStatus('peSavedAuto');
  },

  // ── Preview ──

  _schedulePreview() {
    if (this._restoring) return;
    this._scheduleCommit();
    // Live scrubbing: send now; `_updatePreview` coalesces while a request is in flight.
    this._updatePreview();
  },

  _scheduleCommit() {
    if (this._restoring) return;
    clearTimeout(this._commitTimer);
    this._commitTimer = setTimeout(() => this._commitChange(), 450);
  },

  _commitChange() {
    if (this._restoring || !this._path) return;
    const edits = this._readEdits();
    const json = JSON.stringify(edits);
    if (!this._lastCommittedJson) {
      this._lastCommitted = JSON.parse(JSON.stringify(edits));
      this._lastCommittedJson = json;
      return;
    }
    if (json === this._lastCommittedJson) return;
    const prev = JSON.parse(JSON.stringify(this._lastCommitted || this._defaultEdits()));
    this._pushUndoSnapshot(prev);
    this._lastCommitted = JSON.parse(JSON.stringify(edits));
    this._lastCommittedJson = json;
    this._histPush(edits, prev);
    this._scheduleAutoSave();
    // Card thumbnails follow the committed base, but only after the user has
    // stopped moving sliders — hover previews stay exact meanwhile.
    clearTimeout(this._presetThumbTimer);
    this._presetThumbTimer = setTimeout(() => this._loadPresetThumbs(), 1200);
  },

  _pushUndoSnapshot(snapshot) {
    if (!this._path) return;
    const stack = this._undoStacks[this._path] || (this._undoStacks[this._path] = []);
    stack.push(JSON.parse(JSON.stringify(snapshot || this._readEdits())));
    if (stack.length > 60) stack.shift();
    this._redoStacks[this._path] = [];
  },

  _applySnapshot(snapshot) {
    if (!this._path || !snapshot) return;
    this._restoring = true;
    this._edits = JSON.parse(JSON.stringify(snapshot));
    // Undo/redo leaves the preset's exact state; the card wall must not keep
    // claiming a preset is active when the edits no longer match it.
    this._presetBase = null;
    this._presetApplied = null;
    this._presetResolvedEdits = null;
    this._presetFitToken++;
    this._setSliders(this._edits);
    this._lastCommitted = JSON.parse(JSON.stringify(this._edits));
    this._lastCommittedJson = JSON.stringify(this._edits);
    this._restoring = false;
    this._renderPresetWall();
    this._loadPresetThumbs();
    this._updatePreview();
    this._scheduleAutoSave();
    this._histRender();
    this._setSaveStatus('peSaving');
  },

  _undo() {
    // A drag that has not hit the 450 ms commit window is still undoable.
    this._commitChange();
    const stack = this._undoStacks[this._path] || [];
    if (!stack.length) { App.toast(t('peUndoEmpty'), 'info'); return; }
    const cur = JSON.parse(JSON.stringify(this._readEdits()));
    const prev = stack.pop();
    const redo = this._redoStacks[this._path] || (this._redoStacks[this._path] = []);
    redo.push(cur);
    this._applySnapshot(prev);
  },

  _redo() {
    const stack = this._redoStacks[this._path] || [];
    if (!stack.length) { App.toast(t('peRedoEmpty'), 'info'); return; }
    const cur = JSON.parse(JSON.stringify(this._readEdits()));
    const next = stack.pop();
    const undo = this._undoStacks[this._path] || (this._undoStacks[this._path] = []);
    undo.push(cur);
    this._applySnapshot(next);
  },

  _previewMaxSide() {
    let maxSide = 800;
    if (this._zoom) {
      const ed = this._editedDims();
      const origLong = Math.max(this._imgW || 0, this._imgH || 0);
      const edLong = ed ? Math.max(ed.w, ed.h) : origLong;
      const dispLong = Math.max(this._zoomBaseW * this._zoomScale,
        this._zoomBaseH * this._zoomScale);
      // max_side caps the decoded source, while output is capped after crop.
      // For cropped images ask for a larger decode so the crop still yields
      // native pixels (e.g., 4000px crop from an 8000px original → decode 8000).
      const outTarget = Math.min(dispLong * 1.05, edLong || Infinity, this._ZOOM_DETAIL_MAX);
      const ratio = edLong ? (origLong / edLong) : 1;
      maxSide = Math.min(12000, Math.max(1200, Math.round(outTarget * ratio)));
      return maxSide;
    }
    // 适应窗口时按“实际显示尺寸 × 屏幕像素比”请求预览。固定 800px 会在
    // 小窗口打开、之后最大化时把图片钉在旧的 800px 原图尺寸，看起来像
    // 仍停留在小窗口。小窗口仍保持 800 的下限，避免无谓放大解码。
    const img = document.getElementById('pe-preview-img');
    const preview = document.querySelector('.pe-preview');
    if (img && img.naturalWidth && img.naturalHeight && preview && preview.clientWidth) {
      const pcs = getComputedStyle(preview);
      const nav = document.querySelector('.pe-navbar');
      const cs = document.getElementById('pe-color-space');
      const strip = document.getElementById('pe-filmstrip');
      const inWorkspace = document.getElementById('photo-edit-overlay')?.classList.contains('pe-workspace');
      const stripH = (!inWorkspace && strip && !strip.classList.contains('hidden')) ? strip.offsetHeight : 0;
      const availW = Math.max(200, preview.clientWidth -
        (parseFloat(pcs.paddingLeft) || 0) - (parseFloat(pcs.paddingRight) || 0));
      const availH = Math.max(200, preview.clientHeight -
        (parseFloat(pcs.paddingTop) || 0) - (parseFloat(pcs.paddingBottom) || 0) -
        (nav ? nav.offsetHeight : 0) - (cs ? cs.offsetHeight : 0) - stripH);
      const ar = img.naturalWidth / img.naturalHeight;
      const fitW = Math.min(availW, availH * ar);
      const dpr = Math.min(2, window.devicePixelRatio || 1);
      maxSide = Math.round(Math.max(fitW, fitW / ar) * dpr);
    }
    // 1280 是后端交互预览缓存上限；再高会走细节路径（全尺寸解码），
    // 滑杆连动会明显变慢，留给缩放/1:1 使用。
    return Math.min(1280, Math.max(800, maxSide));
  },

  // 窗口尺寸变化（含最大化）：适应视图重新请求合适分辨率的预览；
  // 缩放视图重建冻结容器，保持当前缩放倍数和画面中心。
  async _handleViewportResize() {
    if (!this._path) return;
    const overlay = document.getElementById('photo-edit-overlay');
    if (!overlay || overlay.classList.contains('hidden')) return;
    if (this._zoom) {
      const scale = this._zoomScale;
      const w = this._zoomBaseW * scale, h = this._zoomBaseH * scale;
      if (!w || !h) return;
      const cx = 0.5 - this._panX / w;
      const cy = 0.5 - this._panY / h;
      const path = this._path;
      this._zoomFit();
      await new Promise(r => requestAnimationFrame(r));
      if (this._path !== path) return;
      this._setZoom(scale);
      if (!this._zoom) return;
      const nw = this._zoomBaseW * this._zoomScale;
      const nh = this._zoomBaseH * this._zoomScale;
      this._panX = (0.5 - cx) * nw;
      this._panY = (0.5 - cy) * nh;
      this._applyZoomLayout();
      return;
    }
    const target = this._previewMaxSide();
    if (this._presetHoverId) return;
    if (Math.abs(target - this._lastPreviewMaxSide) >
        Math.max(80, this._lastPreviewMaxSide * 0.08)) {
      this._updatePreview();
    }
  },

  async _updatePreview() {
    if (!this._path) return;
    // An authoritative preview supersedes any in-flight hover render.
    this._presetHoverToken++;
    if (this._previewBusy) { this._previewPending = true; return; }
    this._previewBusy = true;
    const token = ++this._previewToken;
    const reqPath = this._path;
    const maxSide = this._previewMaxSide();
    this._lastPreviewMaxSide = maxSide;
    const bar = document.getElementById('pe-zoom-bar');
    if (bar) bar.classList.toggle('loading', maxSide > 800);
    try {
      const r = await window.api.sendCommand('apply_edits', {
        path: reqPath, edits: this._readEdits(), preview: true, preview_max_side: maxSide,
      });
      if (reqPath === this._path && token === this._previewToken && !this._previewPending && r && r.preview) {
        const img = document.getElementById('pe-preview-img');
        if (img) img.src = r.preview;
        if (this._callbacks.onPreview) this._callbacks.onPreview(r.preview);
        if (this._zoom) {
          if (JSON.stringify(this._readEdits()) !== this._tileEditsJson) {
            this._tileRegion = '';
            this._hideTile();
          }
          this._scheduleTile(180);
        }
      }
    } catch (_) { /* ignore */ }
    finally {
      if (bar) bar.classList.remove('loading');
      this._previewBusy = false;
      if (this._previewPending) {
        this._previewPending = false;
        setTimeout(() => this._updatePreview(), 16);
      }
    }
  },

  _scheduleAutoSave() {
    clearTimeout(this._autoSaveTimer);
    this._autoSaveTimer = setTimeout(() => this._flushSave(), 700);
  },

  async _flushSave() {
    if (!this._path) return true;
    const edits = this._readEdits();
    const json = JSON.stringify(edits);
    if (json === this._lastSavedJson) {
      if (this._lastCommittedJson) this._setSaveStatus('peSavedAuto');
      return true;
    }
    this._setSaveStatus('peSaving');
    try {
      const r = await window.api.sendCommand('save_edits', { path: this._path, edits });
      if (r && r.error) throw new Error(r.error);
      this._lastSavedJson = json;
      this._saveErrorNoted = false;
      this._setSaveStatus('peSavedAuto');
      return true;
    } catch (e) {
      this._setSaveStatus('peSaveFailed');
      App.toast(t('peSaveFail') + (e.message || ''), 'error');
      if (!this._saveErrorNoted && typeof Activity !== 'undefined') {
        this._saveErrorNoted = true;
        Activity.note({ title: t('actSaveFailed'), detail: e.message || '', status: 'error' });
      }
      return false;
    }
  },

  _setSaveStatus(key) {
    const el = document.getElementById('pe-save-status');
    if (!el) return;
    el.textContent = t(key);
    el.classList.toggle('saving', key === 'peSaving');
    el.classList.toggle('error', key === 'peSaveFailed');
  },

  // ── Actions ──

  async _save() {
    if (!this._path) return;
    this._commitChange();
    this._histPush();
    const ok = await this._flushSave();
    if (ok) {
      App.toast(t('peSaved'), 'success');
      if (this._callbacks.onSaved) this._callbacks.onSaved();
    }
  },

  async _saveSilent() {
    if (!this._path) return;
    this._commitChange();
    this._histPush();
    await this._flushSave();
  },

  async _reset() {
    if (!this._path) return;
    const ok = await Modal.showConfirm(t('peResetTitle'), t('peResetConfirm'));
    if (!ok) return;
    const prev = JSON.parse(JSON.stringify(this._readEdits()));
    this._pushUndoSnapshot(prev);
    try {
      await window.api.sendCommand('reset_edits', { path: this._path });
      this._edits = {};
      this._hideAiCard();
      this._setSliders(this._defaultEdits());
      this._lastCommitted = JSON.parse(JSON.stringify(this._readEdits()));
      this._lastCommittedJson = JSON.stringify(this._lastCommitted);
      this._lastSavedJson = this._lastCommittedJson;
      this._histPush(this._lastCommitted, prev);
      this._loadInitialPreview(this._path);
      if (this._callbacks.onReset) this._callbacks.onReset();
      this._setSaveStatus('peSavedAuto');
      App.toast(t('peResetOk'), 'info');
    } catch (e) { App.toast(t('peResetFail') + e.message, 'error'); }
  },

  async _autoEnhance() {
    if (!this._path) return;
    try {
      const r = await window.api.sendCommand('auto_enhance', { path: this._path });
      if (r && r.preview) {
        const img = document.getElementById('pe-preview-img');
        if (img) img.src = r.preview;
        if (this._callbacks.onPreview) this._callbacks.onPreview(r.preview);
        if (r.edits) this._setSliders(r.edits);
        App.toast(t('peAutoApplied'), 'info');
      }
    } catch (e) { App.toast(t('peAutoFail') + e.message, 'error'); }
  },

  // ── AI 自动修图：lr-v1 蒸馏大脑优先（用户 LR 风格 + 决策叙事），降级 auto→suggest ──

  async _autoTone() {
    if (!this._path) return;
    const btn = document.getElementById('pe-ai-tone');
    if (btn) btn.disabled = true;
    try {
      const r = await window.api.sendCommand('auto_tone_editor', { path: this._path, engine: 'distill' });
      if (!r || r.error) {
        App.toast(t('peAiToneFail') + ((r && r.error) || 'unknown'), 'error');
        return;
      }
      const at = r.auto_tone || {};
      const img = document.getElementById('pe-preview-img');
      if (r.preview && img) img.src = r.preview;
      if (r.preview && this._callbacks.onPreview) this._callbacks.onPreview(r.preview);
      this._edits.auto_tone = at;            // 快照持久化(save_edits / 导出共用)
      this._saveSilent();
      this._aiVariantIdx = 0;
      this._renderAiCard();
      const msg = at.engine === 'distill' ? t('peAiToneDistill')
        : (at.engine === 'auto' ? t('peAiToneApplied') : t('peAiToneRule'));
      App.toast(msg, 'success');
    } catch (e) { App.toast(t('peAiToneFail') + e.message, 'error'); }
    finally { if (btn) btn.disabled = false; }
  },

  // ── lr-v1 决策叙事卡（场景判读 / 理由 / 档位；记忆参与时加角标 + 建议变体）──

  _escHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  },

  _renderAiCard() {
    const card = document.getElementById('pe-ai-card');
    if (!card) return;
    const at = this._edits.auto_tone || {};
    const narrative = at.narrative || null;
    const options = at.options || null;
    const mem = at.memory || null;
    const variants = Array.isArray(at.variants) && at.variants.length > 1 ? at.variants : null;
    const bands = (narrative && narrative.bands) || {};
    const changes = this._aiChangesLines(options, bands);
    // 无叙事（auto/suggest 引擎）也出精简卡：只要有"AI 调整"清单/记忆角标/变体
    // ——CPU 包默认 suggest 引擎, 否则用户点「AI 修图」只看到 toast
    if ((!narrative || !narrative.summary) && !mem && !variants && !changes.length) {
      this._hideAiCard();
      return;
    }
    const reasons = narrative ? (narrative.reasoning || [])
      .map(r => `<li>${this._escHtml(r)}</li>`).join('') : '';
    const notes = narrative && (narrative.notes || []).length
      ? `<div class="pe-ai-card-notes">⚠ ${narrative.notes.map(n => this._escHtml(n)).join('；')}</div>` : '';
    // P1-1 记忆角标：used_memory=true → "已结合你的修图习惯 (置信度 x%)"
    const memBadge = mem && mem.used_memory
      ? `<span class="pe-ai-mem-badge" data-i18n-title="peAiMemBadgeTip" title="建议已按你的历史修图习惯个性化"><i class="ph ph-user-focus"></i> ${t('peAiMemBadge', { conf: Math.round((Number(mem.confidence) || 0) * 100) })}</span>`
      : '';
    // P1-1 建议变体：当前方案 chip + "换个方案"循环按钮
    const idx = this._aiVariantIdx || 0;
    const cur = variants ? variants[idx] : null;
    const curLabelKey = cur ? 'peAiVar' + String(cur.label || '')
      .replace(/^./, c => c.toUpperCase()) : '';
    const varChip = cur ? `<span class="pe-ai-var-chip">${this._escHtml(t(curLabelKey))}</span>` : '';
    const varBtn = variants
      ? `<button class="pe-ai-variant-btn"><i class="ph ph-shuffle"></i> ${t('peAiVariantBtn', { i: idx + 1, n: variants.length })}</button>`
      : '';
    const varRow = variants ? `<div class="pe-ai-card-variants">${varChip}${varBtn}</div>` : '';
    card.innerHTML = `
      <div class="pe-ai-card-head">
        <i class="ph ph-brain"></i>
        <span class="pe-ai-card-scene" title="${this._escHtml((narrative && narrative.scene) || '')}">${this._escHtml((narrative && narrative.scene) || t('peAiCardTitle'))}</span>
        ${memBadge}
        <button class="pe-ai-card-close" data-i18n-title="peAiCardClose" title="关闭"><i class="ph ph-x"></i></button>
      </div>
      ${narrative && narrative.summary ? `<div class="pe-ai-card-summary">${this._escHtml(narrative.summary)}</div>` : ''}
      ${reasons ? `<ul class="pe-ai-card-reasons">${reasons}</ul>` : ''}
      ${changes.length ? `<div class="pe-ai-card-sec">${t('peAiChanges')}</div><ul class="pe-ai-card-changes">${changes.join('')}</ul>` : ''}
      ${varRow}
      ${notes}`;
    card.classList.remove('hidden');
    const close = card.querySelector('.pe-ai-card-close');
    if (close) close.addEventListener('click', () => this._hideAiCard());
    const vbtn = card.querySelector('.pe-ai-variant-btn');
    if (vbtn) vbtn.addEventListener('click', () => this._switchAiVariant());
  },

  // P1-1 换个方案：循环切换建议变体（记忆融合 → 原始 AI → 纯个人风格）
  _switchAiVariant() {
    const at = this._edits.auto_tone || {};
    const variants = Array.isArray(at.variants) ? at.variants : [];
    if (variants.length < 2) return;
    this._aiVariantIdx = ((this._aiVariantIdx || 0) + 1) % variants.length;
    this._edits.auto_tone = { ...at, options: variants[this._aiVariantIdx].options };
    this._saveSilent();
    this._updatePreview();
    this._renderAiCard();
  },

  // AI 改了什么（借鉴 lumen-raw changes()）：非中性字段 → 人可读增量 + 档位 chip
  _aiChangesLines(options, bands) {
    const LABEL = { exposure: 'peExposure', contrast: 'peContrast', saturation: 'peSaturation',
      vibrance: 'peVibrance', wb_temp: 'peWarmth', wb_tint: 'peWbTint',
      clarity: 'peClarity', texture: 'peTexture', dehaze: 'peDehaze' };
    const sgn = v => (v >= 0 ? '+' : '') + v.toFixed(2);
    const lines = [];
    for (const [k, v] of Object.entries(options || {})) {
      const num = Number(v);
      if (!Number.isFinite(num)) continue;
      let disp = null;
      if (k === 'wb_temp') {
        const d = num - 6500;                 // 引擎中性 6500K，偏移量即 AI 动作
        if (Math.abs(d) < 1) continue;
        disp = sgn(d) + 'K';
      } else if (k === 'exposure') {
        if (Math.abs(num) < 0.005) continue;
        disp = sgn(num) + ' EV';
      } else if (k === 'contrast' || k === 'saturation') {
        if (Math.abs(num - 1) < 0.005) continue;
        disp = num.toFixed(2);
      } else {
        if (Math.abs(num) < 0.005) continue;
        disp = sgn(num);
      }
      const band = bands && bands[k] ? `<span class="pe-ai-chip">${this._escHtml(bands[k])}</span>` : '';
      lines.push(`<li><span class="pe-ai-chg-label">${t(LABEL[k] || k)}</span> <b>${disp}</b> ${band}</li>`);
    }
    return lines;
  },

  _hideAiCard() {
    this._aiVariantIdx = 0;
    const card = document.getElementById('pe-ai-card');
    if (card) { card.classList.add('hidden'); card.innerHTML = ''; }
  },

  // ── PhotoS 文件选择: 灰卡参考图 / LUT ──

  async _pickFile(key) {
    try {
      const filters = key === 'lut_file'
        ? [{ name: 'LUT (.cube)', extensions: ['cube'] }]
        : [{ name: '参考图', extensions: ['jpg','jpeg','png','bmp','arw','cr2','cr3','nef','dng','tif','tiff'] }];
      const files = await window.api.selectFiles(filters);
      if (files && files[0]) {
        this._edits[key] = files[0];
        this._setSliders({ [key]: files[0] });
        this._schedulePreview();
      }
    } catch (_) { /* canceled */ }
  },

  // ── 参考匹配: 获奖图/参考图统计 → 反推本图初始参数（标注基建 + 参数化仿色）──
  // P1-3 v2：强度滑杆直传 + 匹配距离 + 统计卡对比图 + 最近参考图(localStorage)

  _recentRefs() {
    try { return JSON.parse(localStorage.getItem('pe_recent_refs') || '[]'); }
    catch (_) { return []; }
  },

  _pushRecentRef(path) {
    if (!path) return;
    const list = this._recentRefs().filter(p => p !== path);
    list.unshift(path);
    localStorage.setItem('pe_recent_refs', JSON.stringify(list.slice(0, 6)));
    this._renderRecentRefs();
  },

  _renderRecentRefs() {
    const row = document.getElementById('pe-refmatch-recent');
    if (!row) return;
    const list = this._recentRefs();
    if (!list.length) { row.classList.add('hidden'); row.innerHTML = ''; return; }
    row.classList.remove('hidden');
    row.innerHTML = `<span class="pe-refmatch-recent-label"><i class="ph ph-clock-counter-clockwise"></i> ${t('peRefMatchRecent')}</span>` +
      list.map(p => `<button class="pe-refmatch-chip" data-path="${this._escHtml(p)}" title="${this._escHtml(p)}">${this._escHtml(String(p).split(/[\\/]/).pop())}</button>`).join('') +
      `<button class="pe-refmatch-chip-clear" data-i18n-title="peRefMatchRecentClear" title="清除">${t('peRefMatchRecentClear')}</button>`;
    row.querySelectorAll('.pe-refmatch-chip').forEach(btn => {
      btn.addEventListener('click', () => this._refMatch(btn.dataset.path));
    });
    const clear = row.querySelector('.pe-refmatch-chip-clear');
    if (clear) clear.addEventListener('click', () => {
      localStorage.removeItem('pe_recent_refs');
      this._renderRecentRefs();
    });
  },

  async _refMatch(refPath) {
    try {
      if (!refPath) {
        const filters = [{ name: '参考图', extensions: ['jpg','jpeg','png','bmp','arw','cr2','cr3','nef','dng','tif','tiff'] }];
        const files = await window.api.selectFiles(filters);
        if (!files || !files[0]) return;
        refPath = files[0];
      }
    } catch (_) { return; /* canceled */ }
    try {
      if (!this._path) return;
      const sEl = document.getElementById('pe-refmatch-strength');
      const strength = sEl ? (Number(sEl.value) || 100) / 100 : 1;
      App.toast(t('peRefMatchRunning'), 'info');
      const r = await window.api.sendCommand('reference_suggest_params',
        { path: this._path, reference_path: refPath, strength });
      if (!r || r.error) { App.toast(t('peRefMatchFail') + ((r && r.error) || ''), 'error'); return; }
      this._pushRecentRef(refPath);
      const edits = r.edits || {};
      // 部分合并: 只覆盖参考匹配给出的键, 其余当前调整保留; 几何永不被动
      if (Object.keys(edits).length) {
        const cur = this._readEdits();
        const geo = this._geometryEdits();
        this._edits = { ...cur, ...edits, ...geo };
        this._setSliders(this._edits);
        this._updatePreview();
      }
      this._renderRefStats(r);
      App.toast(Object.keys(edits).length ? t('peRefMatchDone') : t('peRefMatchEmpty'),
        Object.keys(edits).length ? 'success' : 'info');
    } catch (e) { App.toast(t('peRefMatchFail') + (e.message || ''), 'error'); }
  },

  // 参考图 vs 本图统计卡对比：曝光/对比度横条 + 8 色相饱和谱双色柱状（canvas）
  _renderRefStats(r) {
    const panel = document.getElementById('pe-refmatch-detail');
    if (!panel || !r) return;
    const ref = r.reference_stats || {}, mine = r.my_stats || {};
    const dv = document.getElementById('pe-refmatch-dist-val');
    if (dv) {
      const d = r.distance || {};
      dv.textContent = (d.before != null && d.after != null)
        ? `${d.before} → ${d.after}` : (d.before != null ? `${d.before}` : '—');
    }
    panel.classList.remove('hidden');
    const cv = document.getElementById('pe-refmatch-canvas');
    if (!cv || !cv.getContext) return;
    const W = 320, H = 168, dpr = window.devicePixelRatio || 1;
    cv.width = W * dpr; cv.height = H * dpr;
    cv.style.width = W + 'px'; cv.style.height = H + 'px';
    const ctx = cv.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    const css = getComputedStyle(document.documentElement);
    const ACCENT = css.getPropertyValue('--accent').trim() || '#3e63dd';
    const GRID = 'rgba(128,128,128,.25)';
    const TXT = css.getPropertyValue('--text2').trim() || '#666';
    const BANDS = ['red','orange','yellow','green','aqua','blue','purple','magenta'];
    const BCOL = { red:'#e5484d', orange:'#f76b15', yellow:'#f5d90a', green:'#30a46c',
                   aqua:'#12a594', blue:'#3e63dd', purple:'#8e4ec6', magenta:'#d6409f' };
    const BKEY = { red:'peHslRed', orange:'peHslOrange', yellow:'peHslYellow', green:'peHslGreen',
                   aqua:'peHslAqua', blue:'peHslBlue', purple:'peHslPurple', magenta:'peHslMagenta' };
    // 上：曝光 key / 对比度 contrast 双横条（0..1 归一）
    ctx.font = '10px sans-serif';
    let y = 14;
    for (const [label, key] of [[t('peExposure'), 'key'], [t('peContrast'), 'contrast']]) {
      const labW = 46;
      ctx.fillStyle = TXT; ctx.textAlign = 'left';
      ctx.fillText(label, 0, y + 4);
      const x0 = labW, maxW = W - labW - 4;
      const rv = Math.max(0, Math.min(1, Number(ref[key]) || 0));
      const mv = Math.max(0, Math.min(1, Number(mine[key]) || 0));
      ctx.fillStyle = ACCENT; ctx.fillRect(x0, y - 6, maxW * rv, 6);
      ctx.fillStyle = GRID;  ctx.fillRect(x0, y + 1, maxW * mv, 5);
      ctx.strokeStyle = 'rgba(128,128,128,.4)';
      ctx.strokeRect(x0 + .5, y - 6.5, maxW - 1, 12);
      y += 20;
    }
    // 下：8 色相饱和谱双色柱（按两系列最大值归一）
    const rp = ref.sat_profile || {}, mp = mine.sat_profile || {};
    const maxSat = Math.max(0.05, ...BANDS.map(b => Math.max(Number(rp[b]) || 0, Number(mp[b]) || 0)));
    const chartTop = 58, chartH = H - chartTop - 16;
    ctx.strokeStyle = GRID;
    ctx.beginPath(); ctx.moveTo(0, chartTop + chartH + .5); ctx.lineTo(W, chartTop + chartH + .5); ctx.stroke();
    const gw = W / BANDS.length;
    BANDS.forEach((b, i) => {
      const cx = i * gw + gw / 2;
      const rh = chartH * Math.min(1, (Number(rp[b]) || 0) / maxSat);
      const mh = chartH * Math.min(1, (Number(mp[b]) || 0) / maxSat);
      ctx.fillStyle = BCOL[b];
      ctx.fillRect(cx - 9, chartTop + chartH - rh, 7, rh);
      ctx.fillStyle = BCOL[b] + '55';
      ctx.fillRect(cx + 2, chartTop + chartH - mh, 7, mh);
      ctx.fillStyle = TXT; ctx.textAlign = 'center';
      ctx.fillText(t(BKEY[b]), cx, chartTop + chartH + 12);
    });
  },

  _clearEdit(key) {
    this._edits[key] = '';
    this._setSliders({ [key]: '' });
    this._schedulePreview();
  },

  // ── 批量同步编辑: 当前编辑应用到本列表全部照片 ──

  async _batchApply() {
    if (!this._path) return;
    // 唯一批量作用域：工作台里的用户选择。绝不回退到 _fileList。
    const paths = (typeof Workbench !== 'undefined') ? Workbench.getSelected() : [];
    if (!paths.length) {
      App.toast(t('peBatchNoSelection'), 'warn');
      return;
    }
    const confirmed = await Modal.showConfirm(
      t('peBatchTitle'),
      t('peBatchMsgSel', { count: paths.length })
    );
    if (!confirmed) return;
    const edits = this._readEdits();
    const previous = {};
    const taskId = 'batchapply-' + Date.now().toString(36) + Math.random().toString(36).slice(2, 7);
    const act = (typeof Activity !== 'undefined')
      ? Activity.start({
        title: t('actBatchApply'), detail: t('actPhotosN', { count: paths.length }),
        total: paths.length, cancelable: true,
        onCancel: () => { window.api.sendCommand('cancel_batch_task', { task_id: taskId }).catch(() => {}); },
      })
      : '';
    try {
      await Promise.all(paths.map(async p => {
        try {
          const r = await window.api.sendCommand('get_edits', { path: p });
          previous[p] = (r && r.edits) ? r.edits : {};
        } catch (_) { previous[p] = {}; }
      }));
      const r = await window.api.sendCommand('batch_apply_edits', { paths, edits, task_id: taskId });
      if (r && r.error) throw new Error(r.error);
      const fail = r.failed || 0;
      if (r.cancelled) {
        App.toast(t('peBatchCancelled'), 'warning');
      } else {
        App.toast(t('peBatchDone', { ok: r.processed || 0, fail }), fail ? 'warning' : 'success');
        if (act && typeof Activity !== 'undefined') Activity.finish(act, { detail: t('peBatchDone', { ok: r.processed || 0, fail }) });
      }
      if (typeof App !== 'undefined' && App.pushUndo) {
        App.pushUndo(t('peBatchUndo', { count: paths.length }), async () => {
          for (const [p, e] of Object.entries(previous)) {
            try { await window.api.sendCommand('save_edits', { path: p, edits: e }); } catch (_) {}
          }
        });
      }
      this._lastSavedJson = JSON.stringify(this._readEdits());
      this._setSaveStatus('peSavedAuto');
      if (this._callbacks.onSaved) this._callbacks.onSaved();
    } catch (e) {
      App.toast(t('peBatchFail') + (e.message || ''), 'error');
      if (act && typeof Activity !== 'undefined') Activity.fail(act, e.message || '');
    }
  },

  _refreshBatchScope() {
    const btn = document.getElementById('pe-batch');
    if (!btn) return;
    const n = (typeof Workbench !== 'undefined') ? Workbench.getSelected().length : 0;
    btn.title = t('peBatchTitleN', { count: n });
    const span = btn.querySelector('span');
    if (span) span.textContent = n ? t('peBatchN', { count: n }) : t('peBatch');
  },

  // ── 预设: 内置 LUT 预设 + 用户命名预设 ──

  async _loadPresets() {
    try {
      const r = await window.api.sendCommand('list_editor_presets', {});
      this._presets = (r && r.presets) || [];
    } catch (_) { this._presets = []; }
    this._renderPresetWall();
    this._loadPresetThumbs();
  },

  _presetCategory(p) {
    return p.builtin ? (p.category || 'lut') : 'mine';
  },

  _presetName(p) {
    return p.nameKey ? (t(p.nameKey) || p.name) : p.name;
  },

  // Switching looks must not compound: clear the keys the previously applied
  // preset owns (e.g. grayscale from a B&W preset) before layering the next.
  _clearPresetKeys(state, preset, amt) {
    const GEO = new Set(['crop', 'rotation', 'flip_h', 'flip_v', 'rotate_deg',
      'spots', 'redeye']);
    const D = this._defaultEdits();
    const out = { ...state };
    const scaled = this._scalePresetEdits(preset.edits || {}, amt);
    Object.keys(scaled).forEach(k => {
      if (GEO.has(k)) return;
      if (Object.prototype.hasOwnProperty.call(D, k)) {
        const d = D[k];
        out[k] = (d && typeof d === 'object') ? JSON.parse(JSON.stringify(d)) : d;
      } else {
        delete out[k];
      }
    });
    return out;
  },

  _presetMergeBase() {
    const cur = this._readEdits();
    if (!this._presetApplied || !this._presetResolvedEdits) return cur;
    return this._clearPresetKeys(cur, { edits: this._presetResolvedEdits }, 1.0);
  },

  _presetAmount() {
    const el = document.getElementById('pe-preset-amount');
    const raw = parseInt(el && el.value, 10);
    return Math.max(0, Math.min(2, (Number.isFinite(raw) ? raw : 100) / 100));
  },

  _presetAdaptivePref() {
    return localStorage.getItem('pe_preset_adaptive_v1') !== '0';
  },

  _activePreset() {
    return this._presetApplied
      ? (this._presets.find(x => x.id === this._presetApplied) || null)
      : null;
  },

  _presetAdaptive() {
    const p = this._activePreset();
    // Reference presets match each photo independently; the toggle is moot.
    if (p && p.reference_stats) return true;
    const el = document.getElementById('pe-preset-adaptive');
    return el ? !!el.checked : this._presetAdaptivePref();
  },

  _updatePresetBatchUI() {
    const btn = document.getElementById('pe-preset-apply-sel');
    const label = document.getElementById('pe-preset-apply-sel-label');
    if (!btn) return;
    const p = this._activePreset();
    const isRef = !!(p && p.reference_stats);
    const ad = document.getElementById('pe-preset-adaptive');
    if (ad) {
      ad.disabled = isRef;
      ad.checked = isRef ? true : this._presetAdaptivePref();
    }
    const selected = (typeof Workbench !== 'undefined') ? Workbench.getSelected() : [];
    // The current photo already shows the active preset; the batch targets the
    // other selected photos so the visible result stays the reference.
    const others = selected.filter(p => p !== this._path);
    btn.disabled = !(p && others.length > 0 && this._presetAmount() > 0.001);
    if (label) label.textContent = t('pePresetApplySel', { count: others.length });
  },

  _presetCats() {
    return [
      ['all', 'pePresetCatAll'],
      ['landscape', 'pePresetCatLandscape'],
      ['portrait', 'pePresetCatPortrait'],
      ['film', 'pePresetCatFilm'],
      ['bw', 'pePresetCatBW'],
      ['mood', 'pePresetCatMood'],
      ['lut', 'pePresetCatLut'],
      ['mine', 'pePresetMine'],
    ];
  },

  // 预设卡片墙：缩略图是「当前照片 + 当前强度下的该预设」的小图，
  // 由后端一次解码批量渲染；悬停只改预览，不改 _edits，也不进历史。
  _renderPresetWall() {
    const grid = document.getElementById('pe-preset-grid');
    const catsEl = document.getElementById('pe-preset-cats');
    if (!grid) return;
    const present = new Set(this._presets.map(p => this._presetCategory(p)));
    if (this._presetCat !== 'all' && !present.has(this._presetCat)) this._presetCat = 'all';
    if (catsEl) {
      catsEl.innerHTML = this._presetCats()
        .filter(([cat]) => cat === 'all' || present.has(cat))
        .map(([cat, key]) => {
          const on = this._presetCat === cat;
          return `<button type="button" class="pe-preset-chip${on ? ' active' : ''}" ` +
            `data-cat="${cat}" role="tab" aria-selected="${on}">${this._escHtml(t(key) || cat)}</button>`;
        }).join('');
    }
    const items = this._presets.filter(p =>
      this._presetCat === 'all' || this._presetCategory(p) === this._presetCat);
    grid.innerHTML = items.map(p => {
      const active = this._presetApplied === p.id;
      const src = this._presetThumbs[p.id] || '';
      const isRef = !!p.reference_stats;
      const title = this._presetName(p) +
        (p.reference_name ? ` · ${p.reference_name}` : '');
      return `<button type="button" class="pe-preset-card${active ? ' active' : ''}${src ? ' has-thumb' : ''}${isRef ? ' reference' : ''}" ` +
        `data-id="${this._escHtml(p.id)}" role="option" aria-selected="${active}" ` +
        `title="${this._escHtml(title)}">` +
        `<span class="pe-preset-thumb">` +
        `<img src="${src}" alt="" draggable="false" loading="lazy">` +
        `<span class="pe-preset-skel"><i class="ph ph-sparkle"></i></span>` +
        (isRef ? `<span class="pe-preset-ref-badge">${this._escHtml(t('pePresetRefBadge'))}</span>` : '') +
        `<i class="ph ph-check pe-preset-check"></i></span>` +
        `<span class="pe-preset-name">${this._escHtml(this._presetName(p))}</span></button>`;
    }).join('');
    const empty = document.getElementById('pe-preset-empty');
    if (empty) empty.classList.toggle('hidden', items.length > 0);
    this._updatePresetCurrent();
    this._updatePresetDelBtn();
    this._updatePresetBatchUI();
  },

  _patchPresetThumbs() {
    document.querySelectorAll('#pe-preset-grid .pe-preset-card').forEach(card => {
      const src = this._presetThumbs[card.dataset.id];
      if (!src) return;
      const img = card.querySelector('img');
      if (img && img.getAttribute('src') !== src) img.src = src;
      card.classList.add('has-thumb');
    });
  },

  _updatePresetCurrent() {
    const el = document.getElementById('pe-preset-current');
    if (!el) return;
    const p = this._presets.find(x => x.id === this._presetApplied);
    el.textContent = p ? this._presetName(p) : t('pePresetNone');
    el.classList.toggle('applied', !!p);
  },

  _updatePresetDelBtn() {
    const del = document.getElementById('pe-preset-del');
    if (!del) return;
    del.classList.toggle('hidden',
      !(this._presetApplied && this._presetApplied.startsWith('u:')));
  },

  async _loadPresetThumbs() {
    if (!this._path || !this._presets.length || !this._editsLoaded) return;
    // Coalesce: renders are serialized, and any request that arrives while one
    // is running re-runs once with the latest base instead of stacking decodes.
    if (this._presetThumbBusy) { this._presetThumbPending = true; return; }
    this._presetThumbBusy = true;
    try {
      do {
        this._presetThumbPending = false;
        await this._loadPresetThumbsOnce();
      } while (this._presetThumbPending);
    } finally {
      this._presetThumbBusy = false;
    }
  },

  async _loadPresetThumbsOnce() {
    if (!this._path || !this._presets.length || !this._editsLoaded) return;
    const overlay = document.getElementById('photo-edit-overlay');
    if (!overlay || overlay.classList.contains('hidden')) return;
    // 预设区默认折叠：没展开就不做整墙解码，展开时再补
    const wrap = document.getElementById('pe-preset-wrap');
    if (!wrap || !wrap.classList.contains('open')) return;
    const amt = this._presetAmount();
    // Only render what the current filter shows: a 3-card category should not
    // pay for all 19 presets on every committed slider change.
    const visible = this._presets.filter(p =>
      this._presetCat === 'all' || this._presetCategory(p) === this._presetCat);
    const presets = visible.map(p => ({
      id: p.id,
      edits: p.reference_stats ? {} : this._scalePresetEdits(p.edits || {}, amt),
      reference_stats: p.reference_stats || null,
      strength: p.reference_stats ? amt : null,
    }));
    const grid = document.getElementById('pe-preset-grid');
    if (grid) grid.classList.add('loading');
    const token = ++this._presetThumbToken;
    const path = this._path;
    try {
      const r = await window.api.sendCommand('apply_preset_thumbnails', {
        path, base_edits: this._presetMergeBase(), presets, max_side: 180,
      });
      if (token !== this._presetThumbToken || path !== this._path) return;
      if (r && r.thumbs) {
        // Merge instead of replacing: the request only covers the visible
        // category, and other categories' cached cards must survive.
        r.thumbs.forEach(t => {
          if (t.preview) this._presetThumbs[t.id] = t.preview;
          else delete this._presetThumbs[t.id];
        });
        this._patchPresetThumbs();
      }
    } catch (_) { /* skeleton stays; cards remain usable */ }
    finally {
      if (grid) grid.classList.remove('loading');
    }
  },

  _presetHoverEnter(id) {
    if (!id) return;
    clearTimeout(this._presetHoverTimer);
    this._presetHoverId = id;
    this._presetHoverTimer = setTimeout(() => this._previewPreset(id), 110);
  },

  _presetHoverLeave(id) {
    if (this._presetHoverId !== id) return;
    this._presetHoverId = null;
    clearTimeout(this._presetHoverTimer);
    this._presetHoverToken++;
    if (this._path) this._updatePreview();
  },

  async _previewPreset(id) {
    if (!this._path || this._presetHoverId !== id) return;
    const p = this._presets.find(x => x.id === id);
    if (!p) return;
    const merged = {
      ...this._presetMergeBase(),
      ...this._scalePresetEdits(p.edits || {}, this._presetAmount()),
      ...this._geometryEdits(),
    };
    const token = ++this._presetHoverToken;
    const path = this._path;
    // 悬停是“挑选”，不是 1:1 检查：放大状态下也封顶，避免跨卡片移动卡顿
    const maxSide = Math.min(2000, this._previewMaxSide());
    this._hideTile();
    try {
      const r = await window.api.sendCommand('apply_edits', {
        path, edits: merged, preview: true, preview_max_side: maxSide,
      });
      if (token !== this._presetHoverToken || this._presetHoverId !== id ||
          path !== this._path) return;
      const img = document.getElementById('pe-preview-img');
      if (r && r.preview && img) img.src = r.preview;
    } catch (_) { /* keep the last preview */ }
  },

  // ── 预设应用（LR/LightCraft 式：部分合并 + 0-200% Amount 按偏移缩放）──

  _scalePresetEdits(edits, amt) {
    /** 预设参数按 Amount 缩放：数值键按「默认值偏移」缩放；曲线向对角线收缩
     *  （单调性保持）；HSL/分级/蒙版调整缩偏移；布尔/文件类 ≥50% 生效。 */
    if (amt >= 0.999 && amt <= 1.001) return JSON.parse(JSON.stringify(edits || {}));
    const D = this._defaultEdits();
    const r3 = x => Math.round(x * 1000) / 1000;
    const out = {};
    Object.entries(edits || {}).forEach(([k, v]) => {
      if (v == null) return;
      if (typeof v === 'boolean') {
        if (amt >= 0.5 && v) out[k] = true;
        return;
      }
      if (k === 'lut_file' || k === 'log_curve' || k === 'wb_reference') {
        if (amt >= 0.5 && v) out[k] = v;
        return;
      }
      const def = D[k];
      if (typeof v === 'number' && typeof def === 'number') {
        const s = r3(def + (v - def) * amt);
        if (Math.abs(s - def) > 1e-3) out[k] = s;  // 缩回默认值 → 不写键(部分合并语义)
        return;
      }
      if (k === 'vignette' || k === 'grain') {
        const a0 = (Array.isArray(v) ? (v[0] || 0) : 0) * amt;
        if (Math.abs(a0) > 1e-3) {
          out[k] = (k === 'vignette')
            ? [r3(a0), v[1] != null ? v[1] : 0.5, v[2] != null ? v[2] : 0.5]
            : [r3(a0), v[1] != null ? v[1] : 1.0];
        }
        return;
      }
      if (k === 'levels' && Array.isArray(v)) {
        const b = (v[0] || 0) * amt;
        const w = 255 + ((v[1] != null ? v[1] : 255) - 255) * amt;
        const gm = 1 + ((v[2] != null ? v[2] : 1) - 1) * amt;
        if (Math.abs(b) > 0.5 || Math.abs(w - 255) > 0.5 || Math.abs(gm - 1) > 0.002) {
          out[k] = [Math.round(b), Math.round(w), r3(gm)];
        }
        return;
      }
      if (k === 'curves' && typeof v === 'object') {
        const c = {};
        Object.entries(v).forEach(([ch, pts]) => {
          const scaled = (pts || []).map(p => [p[0], Math.round(p[0] + (p[1] - p[0]) * amt)]);
          if (scaled.some(p => Math.abs(p[1] - p[0]) > 0.5)) c[ch] = scaled;  // 全对角线 → 通道省略
        });
        if (Object.keys(c).length) out[k] = c;
        return;
      }
      if (k === 'hsl' && typeof v === 'object') {
        const h = {};
        Object.entries(v).forEach(([dom, tr]) => {
          const t2 = (tr || []).map(x => r3(x * amt));
          if (t2.some(x => Math.abs(x) > 1e-3)) h[dom] = t2;
        });
        if (Object.keys(h).length) out[k] = h;
        return;
      }
      if (k === 'calibration' && typeof v === 'object') {
        const c = {};
        Object.entries(v).forEach(([ck, cv]) => {
          const s = r3(cv * amt);
          if (Math.abs(s) > 0.5) c[ck] = s;
        });
        if (Object.keys(c).length) out[k] = c;
        return;
      }
      if (k === 'color_grading' && typeof v === 'object') {
        const g = {};
        Object.entries(v).forEach(([zone, tr]) => {
          const t2 = [tr[0] || 0, r3((tr[1] || 0) * amt), r3((tr[2] || 0) * amt)];
          if (Math.abs(t2[1]) > 1e-3 || Math.abs(t2[2]) > 1e-3) g[zone] = t2;
        });
        if (Object.keys(g).length) out[k] = g;
        return;
      }
      if (k === 'masks' && Array.isArray(v)) {
        const ms = v.map(m => {
          const adjustments = {};
          Object.entries(m.adjustments || {}).forEach(([ak, av]) => {
            const d = this._maskAdjDef(ak);
            const dv = d ? d.def / d.scale : 0;  // 语义默认(滑杆刻度÷量纲)
            const s = r3(dv + (av - dv) * amt);
            if (Math.abs(s - dv) > 1e-3) adjustments[ak] = s;
          });
          return { ...m, adjustments };
        }).filter(m => Object.keys(m.adjustments).length);
        if (ms.length) out[k] = ms;
        return;
      }
      out[k] = v;  // 其余透传（预设不应含几何键，防御性保留）
    });
    return out;
  },

  async _applyPreset(id, amountPct) {
    if (!id) {
      this._presetBase = null;
      this._presetApplied = null;
      this._presetResolvedEdits = null;
      this._presetFitToken++;
      this._presetAmountToken++;
      clearTimeout(this._presetAmountTimer);
      this._renderPresetWall();
      return;
    }
    const p = this._presets.find(x => x.id === id);
    if (!p) return;
    const amt = Math.max(0, Math.min(2,
      amountPct != null ? amountPct / 100 : this._presetAmount()));
    const base = this._presetMergeBase();
    const geo = this._geometryEdits();
    this._commitChange();  // 预设应用本身就是一步可撤销操作
    clearTimeout(this._presetAmountTimer);
    this._presetAmountToken++;
    const fitToken = ++this._presetFitToken;
    let resolved;
    if (p.reference_stats) {
      const path = this._path;
      resolved = await this._fitReferencePreset(p, amt);
      if (fitToken !== this._presetFitToken || path !== this._path) return;
      if (resolved == null) {
        App.toast(t('pePresetRefMatchFail'), 'error');
        return;
      }
    } else {
      resolved = this._scalePresetEdits(p.edits || {}, amt);
    }
    this._presetBase = base;
    this._presetApplied = p.id;
    this._presetResolvedEdits = resolved;
    const merged = { ...base, ...resolved, ...geo };
    this._edits = merged;
    this._setSliders(merged);
    this._updatePreview();
    this._scheduleCommit();
    this._renderPresetWall();
    this._loadPresetThumbs();
    App.toast(t(p.reference_stats ? 'pePresetRefApplied' : 'pePresetApplied',
      { name: this._presetName(p) }), 'success');
  },

  async _fitReferencePreset(preset, amount) {
    if (!this._path || !preset || !preset.reference_stats) return null;
    try {
      const r = await window.api.sendCommand('reference_suggest_params', {
        path: this._path,
        reference_stats: preset.reference_stats,
        strength: Math.max(0, Math.min(1.5, amount)),
      });
      if (!r || r.error || !r.edits) return null;
      return r.edits;
    } catch (_) {
      return null;
    }
  },

  // Apply the active preset to the other Workbench-selected photos. The current
  // photo is the visual reference and is excluded; each target keeps its own
  // crop/rotation/spot repairs/optics/calibration (backend merge).
  async _applyPresetToSelection() {
    if (!this._path || !this._presetApplied) return;
    const p = this._presets.find(x => x.id === this._presetApplied);
    if (!p) return;
    const selected = (typeof Workbench !== 'undefined') ? Workbench.getSelected() : [];
    const paths = selected.filter(x => x !== this._path);
    if (!paths.length) {
      App.toast(t('pePresetBatchNoOthers'), 'info');
      return;
    }
    const amt = this._presetAmount();
    if (amt <= 0.001) {
      App.toast(t('pePresetBatchEmpty'), 'info');
      return;
    }
    const adaptive = this._presetAdaptive();
    const isRef = !!p.reference_stats;
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
    const taskId = 'presetbatch-' + Date.now().toString(36) +
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
        preset_edits: isRef ? {} : this._scalePresetEdits(p.edits || {}, amt),
        reference_stats: isRef ? p.reference_stats : null,
        source_stats: isRef ? null : (p.source_stats || null),
        adaptive: isRef ? false : adaptive,
        adaptive_strength: amt,
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
    } catch (e) {
      App.toast(t('pePresetBatchFail') + (e.message || ''), 'error');
      if (act && typeof Activity !== 'undefined') Activity.fail(act, e.message || '');
    }
  },

  _reapplyPresetAmount(pct) {
    if (!this._presetApplied || !this._presetBase) return;
    const p = this._activePreset();
    if (!p) return;
    const amt = Math.max(0, Math.min(2, (pct != null ? pct : 100) / 100));
    if (p.reference_stats) {
      // Reference presets need a per-photo re-fit; debounce the slider stream.
      clearTimeout(this._presetAmountTimer);
      const token = ++this._presetAmountToken;
      this._presetAmountTimer = setTimeout(async () => {
        if (token !== this._presetAmountToken) return;
        const resolved = await this._fitReferencePreset(p, amt);
        if (token !== this._presetAmountToken ||
            this._presetApplied !== p.id || resolved == null) return;
        this._applyResolvedPreset(resolved);
      }, 180);
      return;
    }
    this._applyResolvedPreset(this._scalePresetEdits(p.edits || {}, amt));
  },

  _applyResolvedPreset(resolved) {
    if (!this._presetBase) return;
    this._presetResolvedEdits = resolved;
    const merged = {
      ...this._presetBase,
      ...resolved,
      ...this._geometryEdits(),
    };
    this._edits = merged;
    this._setSliders(merged);
    this._schedulePreview();
  },

  _geometryEdits() {
    return {
      rotation: this._edits.rotation || 0,
      flip_h: !!this._edits.flip_h,
      flip_v: !!this._edits.flip_v,
      crop: this._edits.crop || null,
      rotate_deg: (parseInt(document.getElementById('pe-straighten').value) || 0) / 10,
    };
  },

  async _savePreset() {
    if (this._presetSaving) return;
    const name = await Modal.showInput(t('pePresetNameTitle'), t('pePresetNamePrompt'));
    if (!name || !name.trim()) return;
    this._presetSaving = true;
    try {
      const pe = this._readEdits();
      // 单张照片专属键不进预设（几何键应用时本就被保留覆盖）
      ['crop', 'spots', 'redeye', 'rotation', 'flip_h', 'flip_v', 'rotate_deg']
        .forEach(k => delete pe[k]);
      const r = await window.api.sendCommand('save_editor_preset', {
        name: name.trim(), edits: pe, path: this._path,
      });
      if (r && r.error) { App.toast(t('pePresetSaveFail') + r.error, 'error'); return; }
      if (r && r.preset) {
        // 新预设即当前状态：标记为生效，Amount 100% 时就是刚保存的效果
        this._presetBase = this._readEdits();
        this._presetApplied = r.preset.id;
        this._presetResolvedEdits = r.preset.edits || {};
        this._presetCat = 'mine';
      }
      await this._loadPresets();
      App.toast(t('pePresetSaved'), 'success');
    } catch (e) { App.toast(t('pePresetSaveFail') + e.message, 'error'); }
    finally { this._presetSaving = false; }
  },

  // Learn a look from a finished image: only aggregate stats are stored, never
  // the reference pixels; applying it re-fits each photo independently.
  async _saveReferencePreset() {
    let files;
    try {
      files = await window.api.selectFiles([
        { name: t('peRefMatch'), extensions: [
          'jpg', 'jpeg', 'png', 'bmp', 'arw', 'cr2', 'cr3', 'nef',
          'dng', 'tif', 'tiff',
        ] },
      ]);
    } catch (_) { return; }
    if (!files || !files[0]) return;
    await this._saveReferencePresetFrom(files[0]);
  },

  async _saveReferencePresetFrom(refPath) {
    if (!refPath) return;
    const base = String(refPath).split(/[\\/]/).pop();
    const name = await Modal.showInput(
      t('pePresetRefNameTitle'),
      t('pePresetRefNamePrompt', { name: base })
    );
    if (!name || !name.trim()) return;
    try {
      const r = await window.api.sendCommand('save_editor_preset', {
        name: name.trim(), edits: {}, reference_path: refPath,
      });
      if (r && r.error) {
        App.toast(t('pePresetRefFail') + r.error, 'error');
        return;
      }
      this._presetCat = 'mine';
      await this._loadPresets();
      App.toast(t('pePresetRefSaved'), 'success');
    } catch (e) { App.toast(t('pePresetRefFail') + e.message, 'error'); }
  },

  async _importXmpPresets() {
    let files;
    try {
      files = await window.api.selectFiles([{ name: 'XMP', extensions: ['xmp'] }]);
    } catch (_) { return; }
    if (!files || !files.length) return;
    App.toast(t('pePresetXmpImporting'), 'info');
    try {
      const r = await window.api.sendCommand('import_xmp_presets', { paths: files });
      if (r && r.error) throw new Error(r.error);
      const ok = (r.imported || []).length;
      const skip = (r.skipped || []).length;
      this._presetCat = 'mine';
      await this._loadPresets();
      App.toast(t('pePresetXmpImported', { ok, skip }),
        ok ? (skip ? 'warning' : 'success') : 'warning');
    } catch (e) {
      App.toast(t('pePresetXmpImportFail') + (e.message || ''), 'error');
    }
  },

  async _exportXmpPreset() {
    const p = this._activePreset();
    if (!p) { App.toast(t('pePresetXmpNoPreset'), 'info'); return; }
    if (p.reference_stats) {
      App.toast(t('pePresetXmpRefNo'), 'warning');
      return;
    }
    const safe = this._presetName(p).replace(/[\\/:*?"<>|]/g, '_');
    const dir = String(this._path || '').replace(/[\\/][^\\/]*$/, '');
    let dest = null;
    try {
      dest = await window.api.selectSave({
        title: t('pePresetXmpExportTitle'),
        defaultPath: dir ? `${dir}/${safe}.xmp` : `${safe}.xmp`,
        filters: [{ name: 'XMP', extensions: ['xmp'] }],
      });
    } catch (_) { return; }
    if (!dest) return;
    try {
      const r = await window.api.sendCommand('export_xmp_preset', {
        name: this._presetName(p), edits: p.edits || {}, dest,
      });
      if (r && r.error) throw new Error(r.error);
      const n = (r.unsupported || []).length;
      App.toast(t('pePresetXmpExported') +
        (n ? t('pePresetXmpUnsupported', { n }) : ''), n ? 'warning' : 'success');
    } catch (e) {
      App.toast(t('pePresetXmpExportFail') + (e.message || ''), 'error');
    }
  },

  async _deletePreset() {
    const id = this._presetApplied || '';
    if (!id || !id.startsWith('u:')) return;
    const p = this._presets.find(x => x.id === id);
    const confirmed = await Modal.showConfirm(
      t('pePresetDelTitle'),
      t('pePresetDelConfirm', { name: p ? this._presetName(p) : '' })
    );
    if (!confirmed) return;
    try {
      const r = await window.api.sendCommand('delete_editor_preset', { id });
      if (r && r.error) { App.toast(t('pePresetSaveFail') + r.error, 'error'); return; }
      if (this._presetApplied === id) {
        this._presetApplied = null;
        this._presetBase = null;
        this._presetResolvedEdits = null;
        this._presetFitToken++;
      }
      delete this._presetThumbs[id];
      await this._loadPresets();
      App.toast(t('pePresetDeleted'), 'info');
    } catch (e) { App.toast(t('pePresetSaveFail') + e.message, 'error'); }
  },

  async _export() {
    if (!this._path) return;
    // Export must not be a third save model: commit and persist first.
    this._commitChange();
    const saved = await this._flushSave();
    if (!saved) App.toast(t('peExportSaveWarn'), 'warning');
    const act = (typeof Activity !== 'undefined')
      ? Activity.start({ title: t('actExportOne'), detail: this._fileList[this._fileIndex]?.name || this._path, total: 1 })
      : '';
    try {
      const r = await window.api.sendCommand('apply_edits', {
        path: this._path, edits: this._readEdits(), preview: false,
      });
      if (r && r.error) throw new Error(r.error);
      if (r && r.path) {
        App.toast(t('peExported') + r.path, 'success');
        if (this._callbacks.onExport) this._callbacks.onExport(r.path);
      }
      if (act && typeof Activity !== 'undefined') Activity.finish(act, { detail: r && r.path ? r.path : '' });
    } catch (e) {
      App.toast(t('peExportFail') + e.message, 'error');
      if (act && typeof Activity !== 'undefined') Activity.fail(act, e.message || '');
    }
  },

  // ── 污点修复/仿制/红眼（P4-2，协议见后端 local_spots.py）──

  _spotRadiusNorm() {
    const layer = document.getElementById('pe-spot-layer');
    const r = layer.getBoundingClientRect();
    const minSide = Math.min(r.width, r.height) || 1;
    return Math.max(0.005, this._spotRadius / minSide);  // 半径相对短边（分辨率无关）
  },

  _spotToggle(force) {
    const on = force != null ? !!force : !this._spotOn;
    if (on) {
      if (this._cropMode) this._exitCrop(true);
      if (this._maskMode) this._maskExitMode();
      if (this._baMode) this._toggleBA();
      if (this._zoom) this._zoomFit();
    }
    this._spotOn = on;
    const btn = document.getElementById('pe-spot-btn');
    const bar = document.getElementById('pe-spot-bar');
    const layer = document.getElementById('pe-spot-layer');
    if (btn) btn.classList.toggle('pe-active', on);
    if (bar) bar.classList.toggle('hidden', !on);
    if (layer) layer.classList.toggle('active', on);
    if (!on) this._spotSel = -1;
    this._syncToolRail(on ? 'spot' : 'edit');
    this._spotResize();
  },

  _spotResize() {
    const layer = document.getElementById('pe-spot-layer');
    const cv = document.getElementById('pe-spot-canvas');
    if (!layer || !cv) return;
    const w = layer.clientWidth, h = layer.clientHeight;
    if (!w || !h) return;
    const dpr = window.devicePixelRatio || 1;
    const pw = Math.round(w * dpr), ph = Math.round(h * dpr);
    if (cv.width !== pw || cv.height !== ph) { cv.width = pw; cv.height = ph; }
    this._spotDraw();
  },

  _spotDraw() {
    const cv = document.getElementById('pe-spot-canvas');
    if (!cv || !cv.width) return;
    const ctx = cv.getContext('2d');
    const W = cv.width, H = cv.height;
    ctx.clearRect(0, 0, W, H);
    if (!this._spotOn) return;
    const X = v => v * W, Y = v => v * H;
    const scale = Math.min(W, H);
    this._spots.forEach((s, i) => {
      const sel = i === this._spotSel;
      const col = s.mode === 'redeye' ? 'rgba(229,72,77,.95)' : 'rgba(255,255,255,.95)';
      const rad = (s.r || 0.03) * scale;
      ctx.strokeStyle = col;
      ctx.lineWidth = sel ? 2.5 : 1.5;
      ctx.beginPath();
      ctx.arc(X(s.x), Y(s.y), rad, 0, Math.PI * 2);
      ctx.stroke();
      ctx.strokeStyle = 'rgba(0,0,0,.55)';  // 深色描边保证亮暗背景都可见
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.arc(X(s.x), Y(s.y), rad + 1.5, 0, Math.PI * 2);
      ctx.stroke();
      if (s.mode === 'clone' && s.sx != null) {
        ctx.strokeStyle = col;
        ctx.setLineDash([4, 4]);
        ctx.beginPath(); ctx.moveTo(X(s.x), Y(s.y)); ctx.lineTo(X(s.sx), Y(s.sy)); ctx.stroke();
        ctx.setLineDash([]);
        ctx.strokeRect(X(s.sx) - 4, Y(s.sy) - 4, 8, 8);  // 源点方块
      }
      if (sel) {
        ctx.fillStyle = col;
        ctx.beginPath(); ctx.arc(X(s.x), Y(s.y), 2.5, 0, Math.PI * 2); ctx.fill();
      }
    });
  },

  _spotHit(rel) {
    const layer = document.getElementById('pe-spot-layer');
    const r = layer.getBoundingClientRect();
    const scale = Math.min(r.width, r.height);
    for (let i = this._spots.length - 1; i >= 0; i--) {
      const s = this._spots[i];
      const d = Math.hypot((s.x - rel[0]) * r.width, (s.y - rel[1]) * r.height);
      if (d < Math.max(12, (s.r || 0.03) * scale)) return { i, src: false };
      if (s.mode === 'clone' && s.sx != null) {
        const ds = Math.hypot((s.sx - rel[0]) * r.width, (s.sy - rel[1]) * r.height);
        if (ds < 12) return { i, src: true };
      }
    }
    return null;
  },

  _spotPointerDown(e) {
    if (!this._spotOn) return;
    e.preventDefault();
    const layer = document.getElementById('pe-spot-layer');
    const r = layer.getBoundingClientRect();
    if (!r.width || !r.height) return;
    const toRel = (ev) => [
      Math.max(0, Math.min(1, (ev.clientX - r.left) / r.width)),
      Math.max(0, Math.min(1, (ev.clientY - r.top) / r.height)),
    ];
    const rel = toRel(e);
    const hit = this._spotHit(rel);
    const rn = this._spotRadiusNorm();
    if (!hit) {
      const s = { id: Date.now() % 10000000, mode: this._spotKind,
        x: rel[0], y: rel[1], r: rn };
      if (this._spotKind === 'clone') { s.sx = rel[0]; s.sy = Math.max(0, rel[1] - 3 * rn); }
      this._spots.push(s);
      this._spotSel = this._spots.length - 1;
      this._spotDrag = { idx: this._spotSel, src: false };
    } else {
      this._spotSel = hit.i;
      this._spotDrag = { idx: hit.i, src: hit.src };
    }
    this._spotDraw();
    const move = (ev) => {
      if (!this._spotDrag) return;
      const [x, y] = toRel(ev);
      const s = this._spots[this._spotDrag.idx];
      if (!s) return;
      if (this._spotDrag.src) { s.sx = x; s.sy = y; }
      else { s.x = x; s.y = y; }
      this._spotDraw();
      this._schedulePreview();
    };
    const up = () => {
      this._spotDrag = null;
      document.removeEventListener('pointermove', move);
      document.removeEventListener('pointerup', up);
      this._schedulePreview();
    };
    document.addEventListener('pointermove', move);
    document.addEventListener('pointerup', up);
  },

  _spotDblClick(e) {
    if (!this._spotOn) return;
    const layer = document.getElementById('pe-spot-layer');
    const r = layer.getBoundingClientRect();
    if (!r.width) return;
    const hit = this._spotHit([(e.clientX - r.left) / r.width, (e.clientY - r.top) / r.height]);
    if (hit) {
      this._spots.splice(hit.i, 1);
      this._spotSel = -1;
      this._spotDraw();
      this._schedulePreview();
    }
  },

  // ── 历史面板（P4-4：会话级快照，保存点粒度；持久审计走 edit_journal）──

  _histPush(editsArg, prevArg) {
    if (!this._path) return;
    try {
      const edits = JSON.parse(JSON.stringify(editsArg || this._readEdits()));
      const json = JSON.stringify(edits);
      const arr = this._hist[this._path] || (this._hist[this._path] = []);
      if (arr.length && arr[arr.length - 1].json === json) {
        arr[arr.length - 1].ts = Date.now();
        return;
      }
      const prev = prevArg
        ? JSON.parse(JSON.stringify(prevArg))
        : (arr.length ? arr[arr.length - 1].edits : this._defaultEdits());
      arr.push({ ts: Date.now(), edits, json, label: this._histLabel(prev, edits) });
      if (arr.length > 60) arr.shift();
      this._histRender();
      const wrap = document.getElementById('pe-hist-wrap');
      if (wrap && wrap.classList.contains('open')) this._journalRefresh();
    } catch (_) { /* ignore */ }
  },

  _histLabel(prev, cur) {
    const LABEL = {
      exposure: 'peExposure', contrast: 'peContrast', saturation: 'peSaturation',
      vibrance: 'peVibrance', wb_temp: 'peWbTemp', wb_tint: 'peWbTint',
      shadows: 'peShadows', highlights: 'peHighlights', clarity: 'peClarity',
      texture: 'peTexture', dehaze: 'peDehaze', sharpness: 'peSharpness',
      levels: 'peLevels', curves: 'peCurveSection', hsl: 'peHslSection',
      color_grading: 'peGradingSection', masks: 'peMaskSection', optics: 'peOptics',
      calibration: 'peCalSection',
      spots: 'peSpotTool', redeye: 'peSpotRedeye', crop: 'peCrop',
      rotate_deg: 'peStraighten', auto_tone: 'peAiToneDistill', denoise: 'peDenoise',
    };
    const changed = Object.keys(cur).filter(k =>
      cur[k] != null && JSON.stringify(cur[k]) !== JSON.stringify(prev[k]));
    const names = changed.slice(0, 3).map(k => t(LABEL[k] || k));
    return (names.join('、') + (changed.length > 3 ? ` +${changed.length - 3}` : '')) || '—';
  },

  _histRender() {
    const el = document.getElementById('pe-hist-list');
    if (!el) return;
    const arr = this._hist[this._path] || [];
    if (!arr.length) {
      el.innerHTML = `<div class="pe-curve-hint">${t('peHistEmpty')}</div>`;
      return;
    }
    el.innerHTML = '';
    for (let i = arr.length - 1; i >= 0; i--) {
      const entry = arr[i];
      const row = document.createElement('div');
      row.className = 'pe-hist-item';
      const time = new Date(entry.ts);
      const hh = String(time.getHours()).padStart(2, '0');
      const mm = String(time.getMinutes()).padStart(2, '0');
      row.innerHTML = `<span class="pe-hist-ts">${hh}:${mm}</span>` +
        `<span>${this._escHtml(entry.label || '—')}</span>`;
      row.addEventListener('click', () => this._histRestore(i));
      el.appendChild(row);
    }
  },

  _histRestore(idx) {
    const arr = this._hist[this._path] || [];
    const entry = arr[idx];
    if (!entry) return;
    this._pushUndoSnapshot(this._readEdits());
    this._edits = JSON.parse(JSON.stringify(entry.edits));
    this._presetBase = null;
    this._presetApplied = null;
    this._presetResolvedEdits = null;
    this._presetFitToken++;
    this._setSliders(this._edits);
    this._renderPresetWall();
    this._loadPresetThumbs();
    this._lastCommitted = JSON.parse(JSON.stringify(this._edits));
    this._lastCommittedJson = JSON.stringify(this._edits);
    this._updatePreview();
    this._scheduleAutoSave();
    this._setSaveStatus('peSaving');
  },

  // ── 持久记录（P3-10 双日志 UI 合并）：操作审计 journal + 个性化记忆，同面板展示 ──

  async _journalRefresh() {
    const el = document.getElementById('pe-journal-list');
    if (!el || !this._path) return;
    const JR_OP = { save: 'peJrSave', reset: 'peJrReset', batch: 'peJrBatch' };
    const MEM_KIND = { suggested: 'peJrSuggested', edit: 'peJrEdit' };
    const rows = [];
    const fmtTs = ts => {
      const d = ts ? new Date(ts * 1000) : null;
      return d ? `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}` : '';
    };
    try {
      const r = await window.api.sendCommand('edit_journal_list', { path: this._path, limit: 20 });
      (r && r.entries || []).forEach(e => {
        const detail = e.op === 'batch' && e.count ? ` ×${e.count}`
          : (e.keys && e.keys.length ? ` · ${e.keys.slice(0, 4).join(', ')}${e.keys.length > 4 ? '…' : ''}` : '');
        rows.push({ ts: e.ts, html: `<span class="pe-hist-ts">${fmtTs(e.ts)}</span>` +
          `<span class="pe-jr-chip pe-jr-audit">${t('peHistAudit')}</span>` +
          `<span>${this._escHtml(t(JR_OP[e.op] || e.op))}${this._escHtml(detail)}</span>` });
      });
    } catch (_) { /* 面板为空即可 */ }
    try {
      const m = await window.api.sendCommand('memory_status', { path: this._path });
      (m && m.entries || []).slice(0, 10).forEach(e => {
        rows.push({ ts: e.ts, html: `<span class="pe-hist-ts">${fmtTs(e.ts)}</span>` +
          `<span class="pe-jr-chip pe-jr-mem">${t('peHistMemory')}</span>` +
          `<span>${this._escHtml(t(MEM_KIND[e.kind] || e.kind))}</span>` });
      });
    } catch (_) { /* 记忆层关闭或不可用 */ }
    if (!rows.length) {
      el.innerHTML = `<div class="pe-curve-hint">${t('peHistEmpty')}</div>`;
      return;
    }
    rows.sort((a, b) => (b.ts || 0) - (a.ts || 0));
    el.innerHTML = '';
    rows.slice(0, 24).forEach(rw => {
      const row = document.createElement('div');
      row.className = 'pe-hist-item';
      row.innerHTML = rw.html;
      el.appendChild(row);
    });
  },

  // ── 命名版本（P6-1，LR Virtual Copies 轻量版：跨会话具名快照）──

  async _vcRefresh() {
    if (!this._path) return;
    try {
      const r = await window.api.sendCommand('vcopy_list', { path: this._path });
      const el = document.getElementById('pe-vc-list');
      if (!el) return;
      const copies = (r && r.copies) || [];
      if (!copies.length) {
        el.innerHTML = `<div class="pe-curve-hint">${t('peVcEmpty')}</div>`;
        return;
      }
      el.innerHTML = '';
      copies.forEach(c => {
        const row = document.createElement('div');
        row.className = 'pe-hist-item';
        const time = c.ts ? new Date(c.ts * 1000) : null;
        const hh = time
          ? `${String(time.getHours()).padStart(2, '0')}:${String(time.getMinutes()).padStart(2, '0')}`
          : '';
        row.innerHTML = `<span class="pe-hist-ts">${hh}</span>` +
          `<span>${this._escHtml(c.name || '')}</span>` +
          `<button class="pe-mask-chip-x" style="margin-left:auto" title="✕">✕</button>`;
        row.addEventListener('click', async (e) => {
          if (e.target.closest('.pe-mask-chip-x')) {
            try {
              await window.api.sendCommand('vcopy_delete', { path: this._path, vcid: c.id });
            } catch (_) { /* ignore */ }
            this._vcRefresh();
            return;
          }
          this._vcApply(c);
        });
        el.appendChild(row);
      });
    } catch (_) { /* ignore */ }
  },

  async _vcSaveCurrent() {
    if (!this._path) return;
    const name = await Modal.showInput(t('peVcNameTitle'), t('peVcNamePrompt'));
    if (!name || !name.trim()) return;
    try {
      const r = await window.api.sendCommand('vcopy_create',
        { path: this._path, name: name.trim(), edits: this._readEdits() });
      if (r && r.error) { App.toast(t('peVcFail') + r.error, 'error'); return; }
      App.toast(t('peVcSaved', { name: name.trim() }), 'success');
      this._vcRefresh();
    } catch (e) { App.toast(t('peVcFail') + e.message, 'error'); }
  },

  async _vcApply(c) {
    try {
      await this._saveSilent();  // 当前状态先落主 sidecar
      const r = await window.api.sendCommand('vcopy_apply', { path: this._path, vcid: c.id });
      if (!r || r.error || !r.edits) {
        App.toast(t('peVcFail') + ((r && r.error) || ''), 'error');
        return;
      }
      this._edits = { ...this._defaultEdits(), ...r.edits };
      this._presetBase = null;
      this._presetApplied = null;
      this._presetResolvedEdits = null;
      this._presetFitToken++;
      this._setSliders(this._edits);
      this._renderPresetWall();
      this._loadPresetThumbs();
      this._updatePreview();
      App.toast(t('peVcApplied', { name: r.name || c.name || '' }), 'success');
    } catch (e) { App.toast(t('peVcFail') + e.message, 'error'); }
  },

  // ── DNG 内嵌镜头档案（P6-4）──

  async _importDngProfile() {
    if (!this._path) return;
    try {
      const r = await window.api.sendCommand('read_dng_lens_profile', { path: this._path });
      if (!r || r.error) { App.toast(t('peDngFail') + ((r && r.error) || ''), 'error'); return; }
      if (!r.found) { App.toast(t('peDngNone'), 'warning'); return; }
      const set = (id, v) => {
        const el = document.getElementById(id);
        if (el) {
          el.value = v;
          const l = document.getElementById(id + '-val');
          if (l) l.textContent = v;
        }
      };
      set('pe-optics-distortion', r.distortion || 0);
      set('pe-optics-vig', r.vig || 0);
      this._schedulePreview();
      App.toast(t('peDngOk', { d: r.distortion || 0, v: r.vig || 0 }) +
        (r.skipped ? ` (${t('peDngSkip', { n: r.skipped })})` : ''), 'success');
    } catch (e) { App.toast(t('peDngFail') + e.message, 'error'); }
  },
};
