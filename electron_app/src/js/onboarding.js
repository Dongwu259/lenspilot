/**
 * onboarding.js — First-run three-step onboarding wizard
 *
 * Step 1: 拷卡目标目录 → App.saveConfig({ copy_target_path })（与拷卡页/设置页同源）
 * Step 2: AI 模型后端（ONNX=CPU / PyTorch=GPU）→ get_backend_pref / set_backend_pref
 * Step 3: 试评一个文件夹 → 跳 AI 选片页并自动开始评分（复用 PhotoSelect 公共入口）
 *
 * 首启检测：localStorage 'onboarding_done'；完成或跳过都会写入。
 * 设置页可经 #btn-reopen-onboarding 重新打开（Onboarding.open()）。
 */

const Onboarding = {
  STORAGE_KEY: 'onboarding_done',
  step: 0,
  state: { targetPath: '', backend: 'onnx', cudaAvailable: false, folder: '' },
  _overlay: null,
  _probed: false,

  shouldShow() {
    try { return !localStorage.getItem(this.STORAGE_KEY); } catch (e) { return false; }
  },

  // 向导当前是否打开（App 用它抑制自动模型预加载，而非 shouldShow —
  // 否则未登录期间 flag 未写入，preload 会被永久抑制）
  isOpen() {
    return !!this._overlay;
  },

  _markDone() {
    try { localStorage.setItem(this.STORAGE_KEY, '1'); } catch (e) { /* ignore */ }
  },

  // ── Open / Close ──

  open(step = 0) {
    if (this._overlay) return;
    this.step = step;
    // 预填已有配置（老用户从设置页重新打开时）
    try {
      this.state.targetPath =
        (App.config && App.config.copy_target_path) ||
        (App.loadPrefs && App.loadPrefs().copyPath) || '';
    } catch (e) { this.state.targetPath = ''; }
    this._buildOverlay();
    this._render();
  },

  close(opts = {}) {
    this._markDone();
    const ov = this._overlay;
    this._overlay = null;
    if (ov) {
      ov.classList.add('modal-closing');
      setTimeout(() => ov.remove(), 200);
    }
    // 引导展示期间 App 的自动模型预加载被抑制（避免与第 2 步的后端选择弹窗打架），
    // 引导结束（含跳过）后在这里补一次；deferPreload 时由调用方自行负责
    //（试评路径要 await 加载完成后启动评分，不能在此处并发抢跑 _loadingModel）。
    if (!opts.deferPreload && typeof PhotoSelect !== 'undefined' && !PhotoSelect.modelLoaded && !PhotoSelect._loadingModel) {
      PhotoSelect.preloadModel();
    }
  },

  // ── DOM ──

  _buildOverlay() {
    const ov = document.createElement('div');
    ov.className = 'modal-overlay ob-overlay';
    ov.id = 'onboarding-overlay';
    ov.innerHTML = `
      <div class="modal-card ob-card">
        <div class="ob-head">
          <div>
            <div class="ob-title">${t('obTitle')}</div>
            <div class="ob-subtitle">${t('obSubtitle')}</div>
          </div>
          <button class="btn btn-ghost btn-sm" data-ob="skip">${t('obSkip')}</button>
        </div>
        <div class="ob-progress" data-ob="progress"></div>
        <div class="ob-body" data-ob="body"></div>
        <div class="ob-footer">
          <span class="ob-step-count" data-ob="count"></span>
          <div class="ob-footer-btns">
            <button class="btn btn-outline" data-ob="back">${t('obBack')}</button>
            <button class="btn btn-primary" data-ob="next">${t('obNext')}</button>
          </div>
        </div>
      </div>`;
    document.body.appendChild(ov);
    this._overlay = ov;
    ov.querySelector('[data-ob="skip"]').addEventListener('click', () => this.close());
    ov.querySelector('[data-ob="back"]').addEventListener('click', () => this._go(-1));
    ov.querySelector('[data-ob="next"]').addEventListener('click', () => this._go(1));
  },

  _render() {
    const body = this._overlay.querySelector('[data-ob="body"]');
    const steps = [t('obNav1'), t('obNav2'), t('obNav3')];
    this._overlay.querySelector('[data-ob="progress"]').innerHTML = steps.map((label, i) => `
      <div class="ob-dot-wrap${i === this.step ? ' current' : ''}${i < this.step ? ' done' : ''}">
        <span class="ob-dot">${i < this.step ? '✓' : i + 1}</span><span class="ob-dot-label">${label}</span>
      </div>${i < 2 ? '<span class="ob-dot-line"></span>' : ''}`).join('');
    this._overlay.querySelector('[data-ob="count"]').textContent = t('obStepOf', { n: this.step + 1 });
    this._overlay.querySelector('[data-ob="back"]').style.visibility = this.step === 0 ? 'hidden' : '';
    this._overlay.querySelector('[data-ob="next"]').textContent = this.step === 2 ? t('obFinish') : t('obNext');

    if (this.step === 0) this._renderStepTarget(body);
    else if (this.step === 1) this._renderStepBackend(body);
    else this._renderStepFolder(body);
  },

  _renderStepTarget(body) {
    body.innerHTML = `
      <div class="ob-step-title">${t('obStep1Title')}</div>
      <div class="ob-step-desc">${t('obStep1Desc')}</div>
      <div class="ob-path-row">
        <input type="text" id="ob-target-path" placeholder="${t('obStep1Placeholder')}" value="">
        <button class="btn btn-outline btn-sm" id="ob-browse-target">${t('browse')}</button>
      </div>`;
    const input = body.querySelector('#ob-target-path');
    input.value = this.state.targetPath;
    input.addEventListener('input', () => { this.state.targetPath = input.value.trim(); });
    body.querySelector('#ob-browse-target').addEventListener('click', async () => {
      const folder = await window.api.selectFolder();
      if (folder) { this.state.targetPath = folder; input.value = folder; }
    });
  },

  _renderStepBackend(body) {
    const cuda = this.state.cudaAvailable;
    const gpuDisabled = this._probed && !cuda;
    const recBackend = cuda ? 'pytorch' : 'onnx';
    if (!this._probed && this.state.backend !== 'pytorch') this.state.backend = 'onnx';
    const opt = (value, name, size, desc) => {
      const active = this.state.backend === value;
      const disabled = value === 'pytorch' && gpuDisabled;
      const rec = this._probed && value === recBackend;
      return `
        <label class="ps-be-opt ob-be-opt${active ? ' active' : ''}${disabled ? ' disabled' : ''}" data-be="${value}">
          <input type="radio" name="ob-be" value="${value}" ${active ? 'checked' : ''} ${disabled ? 'disabled' : ''} style="margin-right:8px">
          <div>
            <div style="font-weight:600">${name}
              <span style="font-weight:400;font-size:11px;color:var(--text3);margin-left:6px">${size}</span>
              ${rec ? `<span class="ob-rec-badge">${t('obRecommended')}</span>` : ''}
            </div>
            <div style="font-size:11px;color:var(--text3)">${desc}</div>
          </div>
        </label>`;
    };
    body.innerHTML = `
      <div class="ob-step-title">${t('obStep2Title')}</div>
      <div class="ob-step-desc">${t('obStep2Desc')}</div>
      <div style="display:flex;flex-direction:column;gap:10px">
        ${opt('onnx', 'ONNX Runtime (CPU)', '≈45MB', t('obCpuDesc'))}
        ${opt('pytorch', 'PyTorch (GPU)', '≈130MB', gpuDisabled ? t('obGpuNoCuda') : (this._probed ? t('obGpuDesc') : t('obGpuDetecting')))}
      </div>`;
    body.querySelectorAll('.ob-be-opt').forEach(el => {
      el.addEventListener('click', (e) => {
        if (el.classList.contains('disabled')) { e.preventDefault(); return; }
        body.querySelectorAll('.ob-be-opt').forEach(o => o.classList.remove('active'));
        el.classList.add('active');
        el.querySelector('input').checked = true;
        this.state.backend = el.dataset.be;
      });
    });
    // 进入本步时探测一次 CUDA / 已有偏好，完成后重绘（打上「推荐」标）
    if (!this._probed) {
      this._probeBackend().then(() => {
        if (this._overlay && this.step === 1) this._render();
      });
    }
  },

  _renderStepFolder(body) {
    body.innerHTML = `
      <div class="ob-step-title">${t('obStep3Title')}</div>
      <div class="ob-step-desc">${t('obStep3Desc')}</div>
      <div class="ob-path-row">
        <input type="text" id="ob-trial-folder" placeholder="${t('obStep3Placeholder')}" value="">
        <button class="btn btn-outline btn-sm" id="ob-browse-folder">${t('browse')}</button>
      </div>`;
    const input = body.querySelector('#ob-trial-folder');
    input.value = this.state.folder;
    input.addEventListener('input', () => { this.state.folder = input.value.trim(); });
    body.querySelector('#ob-browse-folder').addEventListener('click', async () => {
      const folder = await window.api.selectFolder();
      if (folder) { this.state.folder = folder; input.value = folder; }
    });
  },

  // ── Backend probe (step 2) ──

  async _probeBackend() {
    if (this._probed) return;
    try {
      const pref = await window.api.sendCommand('get_backend_pref', {});
      if (pref) {
        this.state.cudaAvailable = !!pref.cuda_available;
        if (pref.backend) this.state.backend = pref.backend;
        else if (!this.state.cudaAvailable) this.state.backend = 'onnx';
        this._probed = true;
        return;
      }
    } catch (e) { /* 后端未就绪 → 退回单项 CUDA 探测 */ }
    try {
      const r = await window.api.sendCommand('get_cuda_available', {});
      this.state.cudaAvailable = !!(r && r.cuda_available);
    } catch (e) { /* 保持默认 CPU */ }
    this._probed = true;
  },

  // ── Navigation ──

  async _go(dir) {
    if (this._navBusy) return;
    if (dir < 0) {
      if (this.step > 0) { this.step--; this._render(); }
      return;
    }
    this._navBusy = true;  // 下一步含异步保存，防连点跳两步
    try {
      if (this.step === 0) {
        if (!this.state.targetPath) { App.toast(t('obPathRequired'), 'warning'); return; }
        await this._saveTargetPath();
        this.step = 1; this._render();
      } else if (this.step === 1) {
        await this._saveBackend();
        this.step = 2; this._render();
      } else {
        const folder = this.state.folder;
        this.close({ deferPreload: !!folder });
        if (folder) this._startTrialScoring(folder);
      }
    } finally { this._navBusy = false; }
  },

  async _saveTargetPath() {
    const p = this.state.targetPath;
    App.savePrefs({ copyPath: p });
    await App.saveConfig({ copy_target_path: p });
    // 同步拷卡页/设置页已渲染的展示位（若已在 DOM 中）
    const ccInput = document.getElementById('copy-target-path');
    if (ccInput) ccInput.value = p;
    const settingDisplay = document.getElementById('setting-path-display');
    if (settingDisplay) settingDisplay.textContent = p;
  },

  async _saveBackend() {
    try {
      await window.api.sendCommand('set_backend_pref', { backend: this.state.backend });
    } catch (e) {
      // 后端尚未就绪：不阻塞流程，首次评分时 photo-select 会再次询问
      App.toast(t('obBackendSaveFail'), 'warning');
    }
  },

  // ── Step 3 finish: jump to AI 选片 and start scoring ──

  async _startTrialScoring(folder) {
    App.setPage('photo-select');
    if (typeof PhotoSelect === 'undefined') return;
    if (!PhotoSelect.initialized) PhotoSelect.init();
    PhotoSelect.sourceFolder = folder;
    const input = document.getElementById('select-folder');
    if (input) input.value = folder;
    // 首次需下载/加载模型；加载完成后自动开始评分（复用 PhotoSelect 公共入口）
    await PhotoSelect.preloadModel();
    if (PhotoSelect.modelLoaded && !PhotoSelect.processing && PhotoSelect.sourceFolder === folder) {
      PhotoSelect.startScoring({ copy_on_score: false });
    }
  },
};
