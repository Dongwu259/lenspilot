/**
 * app.js — Main application controller
 * Dashboard, theme, toast, auth, tabs, settings
 */

const App = {
  currentPage: 'dashboard',
  backendReady: false,
  config: {},
  token: null,
  user: null,
  serverUrl: 'https://studio.dwphoto.top',

  // ── Secure token storage (safeStorage-encrypted with localStorage fallback) ──
  async _loadToken() {
    // Try secure store first (Electron safeStorage), then localStorage for migration
    try {
      if (window.api && window.api.secureStoreGet) {
        const tok = await window.api.secureStoreGet('studio_token');
        if (tok) return tok;
      }
    } catch (_) { /* fall through to localStorage */ }
    // Legacy migration: read from localStorage, promote to secure store
    const legacy = localStorage.getItem('studio_token');
    if (legacy) {
      this._saveToken(legacy);  // promote to secure store
      localStorage.removeItem('studio_token');  // clean legacy localStorage only
      return legacy;
    }
    return null;
  },
  async _saveToken(tok) {
    if (tok) {
      // 先同步写 localStorage(agent.js 等多模块从这里读 cloud_token,
      // 不能等异步 secureStore 完——调用方没有 await _saveToken 导致竞态)
      localStorage.setItem('studio_token', tok);
      // 安全存储异步写入(可慢,不影响 token 即时可用)
      try {
        if (window.api && window.api.secureStoreSet) {
          await window.api.secureStoreSet('studio_token', tok);
        }
      } catch (_) { /* ignore */ }
    }
  },
  async _removeToken() {
    this.token = null;
    this.user = null;
    try {
      if (window.api && window.api.secureStoreDelete) {
        await window.api.secureStoreDelete('studio_token');
      }
    } catch (_) { /* ignore */ }
    localStorage.removeItem('studio_token');
  },

  async init() {
    this._setupErrorReporting();
    this.initTheme();
    this.setupTabs();
    if (typeof Keymap !== 'undefined') Keymap.init();
    if (typeof Activity !== 'undefined') Activity.init();
    if (typeof BatchExport !== 'undefined') BatchExport.init();
    this._initWorkbench();
    if (typeof CommandPalette !== 'undefined') CommandPalette.init();
    this.setupBackendEvents();
    if (typeof Agent !== 'undefined') Agent.init();
    this.setupLoginUI();
    this.setupSettings();
    await this.checkLogin();
    await this.loadConfig();
    this.applyPrefs();
    const startPage = this.loadPrefs().startPage || 'dashboard';
    this.setPage(startPage);
    if (typeof I18N !== 'undefined') I18N.applyDOM();
    this.checkUpdate();
    // Global Ctrl+Z undo. The photo editor owns its own undo stack while it is open.
    document.addEventListener('keydown', (e) => {
      const hit = (typeof Keymap !== 'undefined')
        ? Keymap.is(e, 'undo')
        : (e.ctrlKey && e.key.toLowerCase() === 'z' && !e.shiftKey);
      if (!hit) return;
      if (e.target.closest('input,textarea,[contenteditable]')) return;
      const editor = document.getElementById('photo-edit-overlay');
      if (editor && !editor.classList.contains('hidden')) return;
      e.preventDefault(); this.undoLast();
    });

    // First-run onboarding wizard — 只在已登录云账号时出现（init 时后台恢复成功，
    // 或之后 doLogin/doRegister 成功）。引导期间抑制自动模型预加载，
    // 避免 photo-select 的首启后端选择弹窗与向导第 2 步互相打架（引导结束时会补预加载）
    const showOnboarding = this._maybeOpenOnboarding();

    // Preload AI model after startup
    if (!showOnboarding && typeof PhotoSelect !== 'undefined' && !PhotoSelect.modelLoaded) {
      PhotoSelect.preloadModel();
    }
  },

  // 新手引导只在登录云账号后出现（向导含云端设置步骤）；init(后台恢复成功)与
  // doLogin/doRegister 成功后都会调这里；shouldShow 只对未完成的用户为 true，
  // 完成后写 flag 不再弹。
  _maybeOpenOnboarding() {
    if (typeof Onboarding === 'undefined' || !Onboarding.shouldShow()) return false;
    if (!this.user) return false;
    Onboarding.open();
    return true;
  },

  // ── Auto Update ──
  compareVersions(a, b) {
    const parse = (v) => String(v || '').replace(/^v/i, '').split(/[.-]/).map(part => {
      const n = parseInt(part, 10);
      return Number.isFinite(n) ? n : 0;
    });
    const left = parse(a);
    const right = parse(b);
    const len = Math.max(left.length, right.length);
    for (let i = 0; i < len; i++) {
      const diff = (left[i] || 0) - (right[i] || 0);
      if (diff !== 0) return diff > 0 ? 1 : -1;
    }
    return 0;
  },

  async checkUpdate() {
    try {
      const local = await window.api.getVersion();
      const resp = await fetch(`${this.serverUrl}/version.json?ts=${Date.now()}`, { cache: 'no-store' });
      if (!resp.ok) return;
      const remote = await resp.json();
      const remoteVersion = remote.desktopVersion || remote.appVersion || remote.version;
      if (!remoteVersion || this.compareVersions(remoteVersion, local) <= 0) return;

      // Determine if model update is needed (model version changed)
      const localModelVer = localStorage.getItem('model_version') || '';
      const remoteModelVer = remote.modelVersion || '';
      const modelChanged = remoteModelVer && localModelVer !== remoteModelVer;

      // Show update notification bar
      const bar = document.getElementById('update-bar');
      if (bar) {
        const sizeHint = modelChanged ? t('appUpdSizeModel', { size: remote.modelSize || '130MB' }) : t('appUpdSizeNoModel');
        bar.innerHTML = `
          <span>${t('appUpdFound', { remote: remoteVersion, local, hint: sizeHint })}</span>
          <button class="btn btn-primary btn-sm" id="btn-download-update">${t('appUpdDownload')}</button>
          <button class="btn btn-ghost btn-sm" id="btn-dismiss-update">✕</button>`;
        bar.classList.add('show');
        document.getElementById('btn-download-update').addEventListener('click', () => this.downloadUpdate({ ...remote, version: remoteVersion, modelChanged }));
        document.getElementById('btn-dismiss-update').addEventListener('click', () => bar.classList.remove('show'));
      }
    } catch (e) { /* offline */ }
  },

  async downloadUpdate(remote) {
    const bar = document.getElementById('update-bar');
    const modelChanged = remote.modelChanged;

    // Use the version.json URL for the update (GPU or CPU, platform-aware)
    const isGPU = localStorage.getItem('model_type') === 'pytorch';
    const isMac = (window.api && window.api.platform) === 'darwin';
    const isLinux = (window.api && window.api.platform) === 'linux';
    const platformShort = isMac ? 'mac' : (isLinux ? 'linux' : 'win');

    // Select the right download URL for this platform + backend
    let url;
    if (isGPU) {
      url = isMac ? (remote.desktopUpdateUrlMacGPU || remote.desktopUpdateUrlMac || remote.desktopUpdateUrlGPU || remote.desktopUpdateUrl)
        : (isLinux ? (remote.desktopUpdateUrlLinuxGPU || remote.desktopUpdateUrlGPU || remote.desktopUpdateUrl)
                   : (remote.desktopUpdateUrlGPU || remote.desktopUpdateUrl));
    } else {
      url = isMac ? (remote.desktopUpdateUrlMac || remote.desktopUpdateUrl)
        : (isLinux ? (remote.desktopUpdateUrlLinux || remote.desktopUpdateUrl)
                   : (remote.desktopUpdateUrl || remote.desktopUpdateUrlGPU));
    }
    if (!url) {
      throw new Error(`未找到 ${platformShort} 平台的更新包链接，请联系管理员更新 version.json。`);
    }

    // 平台校验码预检（只看本平台字段，不回退 Windows sha —— dmg 永远对不上 exe 的
    // 哈希）。缺失时（如 Mac 未发布 sha256）优雅降级为浏览器手动下载，而不是
    // 白下几百 MB 再在校验阶段 fail-closed 拒绝。
    const platformSha = isGPU
      ? (isMac ? remote.desktopSha256MacGPU : (isLinux ? (remote.desktopSha256LinuxGPU || remote.desktopSha256GPU) : remote.desktopSha256GPU))
      : (isMac ? remote.desktopSha256Mac : (isLinux ? (remote.desktopSha256Linux || remote.desktopSha256) : remote.desktopSha256));
    if (!(platformSha || '').trim()) {
      this.setStatus(t('appUpdManualOnly'), 'warning');
      this.toast(t('appUpdManualOnly'), 'warning');
      try { window.api.openUrl(url); } catch (_) { /* open-url 不可用时忽略 */ }
      bar.classList.remove('show');
      return;
    }

    // Pre-check: HEAD request to get file size and server reachability
    let totalSize = 0;
    try {
      const headResp = await fetch(url, { method: 'HEAD' });
      if (!headResp.ok) throw new Error(`服务器返回 ${headResp.status}`);
      totalSize = parseInt(headResp.headers.get('content-length') || '0');
      const sizeText = totalSize ? `(${(totalSize / 1e6).toFixed(0)} MB)` : '';
      this.setStatus(t('appUpdDlStart', { size: sizeText }), 'info');
      bar.innerHTML = `<span>${t('appUpdDlBarSize', { size: sizeText, model: modelChanged ? t('appUpdModelInc') : t('appUpdModelExc') })}</span>`;
    } catch (e) {
      // HEAD failed — try GET anyway, server might not support HEAD
      this.setStatus(t('updateDownloading'), 'info');
      bar.innerHTML = `<span>${t('appUpdDlBar', { model: modelChanged ? t('appUpdModelInc') : t('appUpdModelExc') })}</span>`;
    }

    try {
      const resp = await fetch(url);
      if (!resp.ok) throw new Error(t('appErrDlHttp', { status: resp.status }));

      const total = parseInt(resp.headers.get('content-length') || totalSize || '0');
      const reader = resp.body.getReader();
      const chunks = [];
      let loaded = 0;

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        chunks.push(value);
        loaded += value.length;
        if (total) {
          const pct = Math.round(loaded / total * 100);
          this.setStatus(t('appUpdDlPct', { pct, loaded: (loaded/1e6).toFixed(0), total: (total/1e6).toFixed(0) }), 'info');
        }
      }

      // Pick the checksum matching the chosen platform (Mac/Win/Linux, CPU/GPU).
      const blob = new Blob(chunks);
      let expectedSha = '';
      if (isGPU) {
        expectedSha = isMac ? (remote.desktopSha256MacGPU || remote.desktopSha256GPU || '')
          : (isLinux ? (remote.desktopSha256LinuxGPU || remote.desktopSha256GPU || '')
                     : (remote.desktopSha256GPU || ''));
      } else {
        expectedSha = isMac ? (remote.desktopSha256Mac || remote.desktopSha256 || '')
          : (isLinux ? (remote.desktopSha256Linux || remote.desktopSha256 || '')
                     : (remote.desktopSha256 || ''));
      }
      expectedSha = expectedSha.trim().toLowerCase();
      if (!expectedSha) {
        throw new Error(t('appErrNoChecksum'));
      }
      try {
          const buf = await blob.arrayBuffer();
          const digest = await crypto.subtle.digest('SHA-256', buf);
          const actual = Array.from(new Uint8Array(digest)).map(b => b.toString(16).padStart(2, '0')).join('');
          if (actual !== expectedSha) {
            throw new Error(t('appErrChecksum'));
          }
        } catch (e) {
          this.setStatus(t('appUpdVerifyFail'), 'error');
          this.toast(t('appUpdVerifyRejected', { msg: e.message }), 'error');
          return;
        }

      // Convert blob to base64 via FileReader (handles chunking internally, safe for large files)
      const base64 = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => {
          const dataUrl = reader.result;
          const comma = dataUrl.indexOf(',');
          resolve(dataUrl.substring(comma + 1));
        };
        reader.onerror = reject;
        reader.readAsDataURL(blob);
      });

      // Save to temp directory via IPC (auto-update flow)
      this.setStatus(t('appUpdSaving'), 'info');
      const defaultExt = isMac ? '.dmg' : '.exe';
      const defaultFilename = `LensPilot-v${remote.version}${defaultExt}`;
      const filename = remote.filename || (isMac ? (remote.macFilename || defaultFilename) : defaultFilename);
      const tempPath = await window.api.saveTempFile(base64, filename);

      // Store model version if included in this update
      if (modelChanged && remote.modelVersion) {
        localStorage.setItem('model_version', remote.modelVersion);
      }

      // Show install button in the update bar
      bar.innerHTML = `
        <span>${t('appUpdReady', { version: remote.version })}</span>
        <button class="btn btn-primary btn-sm" id="btn-install-update">${t('appUpdInstall')}</button>
        <button class="btn btn-ghost btn-sm" id="btn-dismiss-update">✕</button>`;
      bar.classList.add('show');
      document.getElementById('btn-install-update').addEventListener('click', async () => {
        bar.innerHTML = `<span>${t('appUpdLaunching')}</span>`;
        try {
          await window.api.installAndQuit(tempPath);
        } catch (e) {
          this.toast(t('appUpdLaunchFail', { msg: e.message }), 'error');
        }
      });
      document.getElementById('btn-dismiss-update').addEventListener('click', () => {
        bar.classList.remove('show');
        this.setStatus(t('ready'), 'success');
      });

      this.setStatus(t('appUpdReadyHint'), 'success');
      App.notify(t('appUpdReadyTitle'), t('appUpdReadyBody', { version: remote.version }) + (modelChanged ? t('appUpdModelNote') : ''));
      this.toast(t('appUpdReadyToast', { version: remote.version }), 'success');
      this.addHistory(t('appHistUpdate'), `v${remote.version}`);
    } catch (e) {
      this.setStatus(t('appUpdDlFail'), 'error');
      bar.innerHTML = `
        <span>${t('appUpdDlFailMsg', { msg: e.message })}</span>
        <button class="btn btn-primary btn-sm" id="btn-retry-update">${t('appUpdRetry')}</button>
        <button class="btn btn-ghost btn-sm" id="btn-dismiss-update">✕</button>`;
      bar.classList.add('show');
      document.getElementById('btn-retry-update').addEventListener('click', () => this.downloadUpdate(remote));
      document.getElementById('btn-dismiss-update').addEventListener('click', () => {
        bar.classList.remove('show');
        this.setStatus(t('ready'), 'success');
      });
      this.toast(t('appUpdDlFailToast', { msg: e.message }), 'error');
    }
  },

  // ── Theme ──
  initTheme() {
    const saved = localStorage.getItem('theme') || 'light';
    this.applyTheme(saved);
    // Listen for system theme changes
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
      if (localStorage.getItem('theme') === 'system') this.applyTheme('system');
    });
  },
  applyTheme(mode) {
    let theme = mode;
    if (mode === 'system') {
      theme = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    }
    document.documentElement.setAttribute('data-theme', theme);
    localStorage.setItem('theme', mode);
  },
  setTheme(mode) {
    this.applyTheme(mode);
    document.getElementById('setting-theme').value = mode;
  },

  // ── Reduce Motion ──
  // 系统 prefers-reduced-motion 由 CSS 媒体查询自动生效；这里处理设置页手动开关
  applyMotionPref() {
    const p = this.loadPrefs();
    if (p.reduceMotion) document.documentElement.setAttribute('data-motion', 'reduced');
    else document.documentElement.removeAttribute('data-motion');
  },

  // ── Toast ──
  toast(msg, level) { Toast.show(msg, level); },

  // ── Model Download Modal (global, shown on first-run auto-download) ──
  _modelDlTotal: 0,
  _modelDlFiles: {},
  showModelDownloadModal() {
    if (document.getElementById('model-dl-modal')) return;
    const m = document.createElement('div');
    m.id = 'model-dl-modal';
    m.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:9999;display:flex;align-items:center;justify-content:center';
    m.innerHTML = `
      <div style="background:var(--bg2,#fff);border-radius:12px;padding:28px 32px;min-width:380px;max-width:90vw;box-shadow:0 8px 32px rgba(0,0,0,.2)">
        <div style="font-size:15px;font-weight:600;margin-bottom:6px">${t('appModelDlTitle')}</div>
        <div style="font-size:12px;color:var(--text3,#888);margin-bottom:16px">${t('appModelDlDesc')}</div>
        <div id="model-dl-file" style="font-size:13px;margin-bottom:8px;color:var(--text2,#666)">${t('appModelDlPrep')}</div>
        <div style="background:var(--bg3,#eee);border-radius:6px;height:10px;overflow:hidden;margin-bottom:6px">
          <div id="model-dl-bar" style="height:100%;width:0%;background:var(--brand,#3b82f6);transition:width .2s"></div>
        </div>
        <div id="model-dl-pct" style="font-size:12px;color:var(--text3,#888);text-align:right">0%</div>
      </div>`;
    document.body.appendChild(m);
  },
  updateModelDownloadProgress(data) {
    this.showModelDownloadModal();
    if (data.total) this._modelDlTotal = data.total;
    const fileEl = document.getElementById('model-dl-file');
    const barEl = document.getElementById('model-dl-bar');
    const pctEl = document.getElementById('model-dl-pct');
    if (fileEl) fileEl.textContent = t('appModelDlFile', { file: data.file || t('appModelFile') });
    const pct = Math.round(data.pct || 0);
    if (barEl) barEl.style.width = pct + '%';
    if (pctEl) {
      const mb = data.downloaded ? (data.downloaded / 1048576).toFixed(0) : 0;
      const totalMb = this._modelDlTotal ? (this._modelDlTotal / 1048576).toFixed(0) : '?';
      pctEl.textContent = `${mb}/${totalMb}MB · ${pct}%`;
    }
  },
  hideModelDownloadModal(success) {
    const m = document.getElementById('model-dl-modal');
    if (m) {
      if (success) {
        const fileEl = document.getElementById('model-dl-file');
        if (fileEl) fileEl.textContent = t('appModelDlDone');
        const barEl = document.getElementById('model-dl-bar');
        if (barEl) barEl.style.width = '100%';
        setTimeout(() => m.remove(), 800);
      } else {
        m.remove();
      }
    }
    this._modelDlTotal = 0;
  },

  // ── AI 自动修图（photo_s auto-tone）权重管理 ──
  AutoTone: {
    _status: null,
    async refresh() {
      const desc = document.getElementById('at-status-desc');
      const btn = document.getElementById('btn-at-download');
      const prog = document.getElementById('at-progress-desc');
      if (!desc) return;
      try {
        const s = await window.api.sendCommand('auto_tone_status', {});
        this._status = s;
        const eng = s.engine || {};
        if (s.ready) {
          desc.textContent = t('atStatusReady');
          if (btn) btn.style.display = 'none';
          if (prog) prog.style.display = 'none';
        } else if (eng.reason === 'torch' || eng.plugin === false) {
          // CPU 版/插件缺失: 无 AI 引擎,管线自动走规则层,无需下载
          desc.textContent = t('atStatusNoEngine');
          if (btn) btn.style.display = 'none';
          if (prog) prog.style.display = 'none';
        } else {
          const mb = s.required_total_bytes ? Math.round(s.required_total_bytes / 1048576) : 0;
          const readyMb = s.required_ready_bytes ? Math.round(s.required_ready_bytes / 1048576) : 0;
          desc.textContent = eng.reason === 'tower'
            ? t('atStatusNeedTower', { total: mb })
            : t('atStatusNeedWeights', { total: mb });
          if (btn && !s.downloading) btn.style.display = '';
        }
      } catch (e) {
        desc.textContent = t('atStatusError');
        if (btn) btn.style.display = 'none';
      }
    },
    async download() {
      try { await window.api.sendCommand('auto_tone_download', {}); } catch (e) {}
      const btn = document.getElementById('btn-at-download');
      if (btn) btn.style.display = 'none';
      const prog = document.getElementById('at-progress-desc');
      if (prog) { prog.style.display = ''; prog.textContent = t('atDlStarting'); }
    },
    handleEvent(eventName, data) {
      const prog = document.getElementById('at-progress-desc');
      if (eventName === 'auto_tone_download_progress' && prog) {
        prog.style.display = '';
        const pct = Math.round(data.pct || 0);
        const mb = data.downloaded ? (data.downloaded / 1048576).toFixed(0) : 0;
        const totalMb = data.total ? (data.total / 1048576).toFixed(0) : '?';
        prog.textContent = `${data.file || ''} · ${mb}/${totalMb}MB · ${pct}%`;
      } else if (eventName === 'auto_tone_download_done') {
        if (typeof App !== 'undefined') App.toast(t('atDlDone'), 'success');
        if (typeof App !== 'undefined') App.AutoTone.refresh();
      } else if (eventName === 'auto_tone_download_error' && prog) {
        prog.style.display = '';
        prog.textContent = t('atDlError') + (data.error ? `: ${data.error}` : '');
        if (typeof App !== 'undefined') App.toast(t('atDlError'), 'error');
        if (typeof App !== 'undefined') App.AutoTone.refresh();
      }
    },
  },

  // ── 个性化记忆层（edit_memory.py）：采纳率 + 管理开关/清除（P1-2 / P2-6）──
  Memory: {
    async refresh() {
      const desc = document.getElementById('ms-adoption-desc');
      const badge = document.getElementById('ms-adoption-rate');
      const entries = document.getElementById('ms-entries-desc');
      const toggle = document.getElementById('setting-memory-enabled');
      if (desc) desc.textContent = t('msChecking');
      try {
        const s = await window.api.sendCommand('memory_stats', {});
        if (desc) {
          desc.textContent = (s && s.settled) ? t('msAdoptionDesc', {
            sug: s.suggestions || 0, acc: s.accepted || 0,
            set: s.settled || 0, mem: s.count || 0 }) : t('msNoData');
        }
        if (badge) {
          badge.textContent = (s && s.adoption_rate != null)
            ? Math.round(s.adoption_rate * 100) + '%' : '—';
        }
        if (entries) {
          const k = (s && s.kinds) || {};
          entries.textContent = t('msEntriesDesc', {
            total: (s && s.count) || 0,
            sug: k.suggested || 0, edit: k.edit || 0 });
        }
        if (toggle && s && typeof s.enabled === 'boolean') toggle.checked = s.enabled;
      } catch (e) {
        if (desc) desc.textContent = t('msError');
        if (badge) badge.textContent = '—';
        if (entries) entries.textContent = t('msError');
      }
    },
    async setEnabled(on) {
      try {
        await App.saveConfig({ edit_memory_enabled: !!on });
        App.toast(on ? t('msToggleOn') : t('msToggleOff'), 'info');
      } catch (e) {
        App.toast(t('msError'), 'error');
      }
      App.Memory.refresh();
    },
    async clearAll() {
      const ok = await Modal.showConfirm(t('msClearAll'), t('msClearAllConfirm'));
      if (!ok) return;
      try {
        const r = await window.api.sendCommand('memory_clear', {});
        App.toast(t('msClearDone', { n: (r && r.removed) || 0 }), 'success');
      } catch (e) { App.toast(t('msClearFail') + (e.message || ''), 'error'); }
      App.Memory.refresh();
    },
    async clearDir() {
      let folder = null;
      try { folder = await window.api.selectFolder(); } catch (_) { return; }
      if (!folder) return;
      const ok = await Modal.showConfirm(t('msClearDir'),
        t('msClearDirConfirm', { dir: folder }));
      if (!ok) return;
      try {
        const r = await window.api.sendCommand('memory_clear', { batch: folder });
        App.toast(t('msClearDone', { n: (r && r.removed) || 0 }), 'success');
      } catch (e) { App.toast(t('msClearFail') + (e.message || ''), 'error'); }
      App.Memory.refresh();
    },
  },

  // ── 主体分割模型（segment_models.py，P2-7）：状态行 + 下载 ──
  Segment: {
    async refresh() {
      const desc = document.getElementById('seg-status-desc');
      const btn = document.getElementById('btn-seg-warmup');
      if (!desc) return;
      try {
        const s = await window.api.sendCommand('segment_model_status', {});
        if (s && s.loaded) {
          desc.textContent = t('segStatusReady');
          if (btn) btn.style.display = 'none';
        } else if (s && s.cached) {
          desc.textContent = t('segStatusCached');
          if (btn) btn.style.display = 'none';
        } else if (s && s.error) {
          desc.textContent = t('segStatusError') + String(s.error).slice(0, 80);
          if (btn) btn.style.display = '';
        } else {
          desc.textContent = t('segStatusMissing');
          if (btn) btn.style.display = '';
        }
      } catch (e) {
        desc.textContent = t('segStatusError');
        if (btn) btn.style.display = 'none';
      }
    },
    async warmup() {
      const btn = document.getElementById('btn-seg-warmup');
      if (btn) btn.disabled = true;
      const desc = document.getElementById('seg-status-desc');
      if (desc) desc.textContent = t('segDownloading');
      try { await window.api.sendCommand('segment_warmup', {}); }
      catch (e) { /* 状态由 refresh 呈现 */ }
      if (btn) btn.disabled = false;
      App.Segment.refresh();
    },
  },

  // ── Desktop Notification ──
  notify(title, body) {
    const p = this.loadPrefs();
    if (p.notify === false) return;
    if (p.sound !== 'off') {
      try {
        const audio = new Audio('data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEARKwAAIhYAQACABAAZGF0YQAAAAA=');
        audio.volume = 0.3; audio.play().catch(()=>{});
      } catch(e){}
    }
    if (Notification.permission === 'granted') {
      new Notification(title, { body });
    } else if (Notification.permission !== 'denied') {
      Notification.requestPermission().then(p => { if (p === 'granted') new Notification(title, { body }); });
    }
  },

  // ── Operation History ──
  addHistory(action, detail) {
    const h = JSON.parse(localStorage.getItem('op_history') || '[]');
    h.unshift({ time: new Date().toISOString(), action, detail });
    if (h.length > 100) h.length = 100;
    localStorage.setItem('op_history', JSON.stringify(h));
  },

  renderHistory() {
    const hist = JSON.parse(localStorage.getItem('op_history') || '[]');
    const hList = document.getElementById('history-list');
    if (!hList) return;
    if (!hist.length) {
      hList.innerHTML = `<span style="color:var(--text3)">${t('noRecords')}</span>`;
      return;
    }
    hList.innerHTML = hist.slice(0, 50).map(h =>
      `<div style="display:flex;gap:8px;padding:2px 0"><span style="color:var(--text3);flex-shrink:0">${new Date(h.time).toLocaleString('zh-CN')}</span><span>${h.action}: ${h.detail}</span></div>`
    ).join('');
  },

  // ── Tabs ──

  // Dropdown group definitions: group → { icon, pages: [{id, label, icon}] }
  _navGroups: {
    'shooting': {
      pages: [
        { id: 'tethered',     i18nKey: 'dashTether',  icon: 'ph-camera' },
        { id: 'copy-card',    i18nKey: 'dashCopy',    icon: 'ph-floppy-disk' },
      ],
    },
    'culling': {
      pages: [
        { id: 'photo-select', i18nKey: 'dashAIScore', icon: 'ph-brain' },
        { id: 'file-browser', i18nKey: 'dashFiles',   icon: 'ph-folder-open' },
        { id: 'photo-edit',   i18nKey: 'navPhotoEdit', icon: 'ph-sliders-horizontal' },
        { id: 'editor-workspace', i18nKey: 'navEditorWorkspace', icon: 'ph-layout' },
        { id: 'batch-export', i18nKey: 'navBatchExport', icon: 'ph-export' },
      ],
    },
    'ai-smart': {
      pages: [
        { id: 'agent',      i18nKey: 'dashAgent',    icon: 'ph-sparkle' },
        { id: 'pipeline',   i18nKey: 'dashPipeline', icon: 'ph-user-circle-gear' },
        { id: 'automation', i18nKey: 'dashAuto',     icon: 'ph-lightning' },
      ],
    },
  },
  _activeGroup: null,

  setupTabs() {
    // Regular page tabs (those with data-page)
    document.querySelectorAll('.nav-tab[data-page]').forEach(tab => {
      tab.addEventListener('click', () => this.setPage(tab.dataset.page));
    });
    // Dropdown toggles
    document.querySelectorAll('.nav-dropdown-toggle').forEach(tab => {
      tab.addEventListener('click', (e) => {
        e.stopPropagation();
        this._toggleSubNav(tab.dataset.group);
      });
    });
    // Click outside closes sub-nav
    document.addEventListener('click', (e) => {
      if (!e.target.closest('.nav-dropdown-toggle') && !e.target.closest('.sub-navbar')) {
        this._closeSubNav();
      }
    });
  },

  _initWorkbench() {
    if (typeof Workbench === 'undefined') return;
    Workbench.loadMeta();
    const chip = document.getElementById('workbench-chip');
    const text = document.getElementById('workbench-chip-text');
    if (chip) chip.addEventListener('click', () => this.setPage('photo-edit'));
    const render = () => {
      const n = Workbench.count();
      if (text) text.textContent = t('wbChip', { photos: n.photos, selected: n.selected });
      if (chip) chip.classList.toggle('hidden', n.photos === 0);
    };
    Workbench.onChange(render);
    render();
  },

  _toggleSubNav(group) {
    if (this._activeGroup === group) { this._closeSubNav(); return; }

    // If switching groups, close current first for clean animation
    if (this._activeGroup) {
      this._closeSubNav();
      setTimeout(() => this._openSubNav(group), 320);
      return;
    }
    this._openSubNav(group);
  },

  _openSubNav(group) {
    this._activeGroup = group;

    // Update toggle active states
    document.querySelectorAll('.nav-dropdown-toggle').forEach(t => {
      t.classList.toggle('open', t.dataset.group === group);
    });

    // Build sub-nav buttons
    const inner = document.getElementById('sub-nav-inner');
    const def = this._navGroups[group];
    if (!def) return;
    inner.innerHTML = def.pages.map((p, i) =>
      `<button class="sub-nav-btn${p.id === this.currentPage ? ' active-page' : ''}" data-page="${p.id}" style="--fly-delay:${i * 50}ms">
        <i class="ph ${p.icon}"></i> ${t(p.i18nKey)}
      </button>`
    ).join('');

    // Open sub-navbar (trigger slide-down)
    const bar = document.getElementById('sub-navbar');
    bar.classList.add('open');

    // Trigger fly-in animation after a frame
    requestAnimationFrame(() => {
      inner.querySelectorAll('.sub-nav-btn').forEach(btn => btn.classList.add('fly-in'));
    });

    // Bind sub-button clicks
    inner.querySelectorAll('.sub-nav-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        this.setPage(btn.dataset.page);
        this._closeSubNav();
      });
    });
  },

  _closeSubNav() {
    if (!this._activeGroup) return;
    this._activeGroup = null;
    document.querySelectorAll('.nav-dropdown-toggle').forEach(t => t.classList.remove('open'));
    const bar = document.getElementById('sub-navbar');
    bar.classList.remove('open');
    // Clear after transition ends so it doesn't flash
    setTimeout(() => {
      if (!this._activeGroup) document.getElementById('sub-nav-inner').innerHTML = '';
    }, 300);
  },

  setPage(page) {
    // Leaving the workspace page must not leave a full-screen editor behind.
    if (this.currentPage === 'editor-workspace' && page !== 'editor-workspace' &&
        typeof PhotoEdit !== 'undefined' && PhotoEdit._workspaceMode) {
      PhotoEdit.close();
    }
    this.currentPage = page;
    // Update regular tab active state
    document.querySelectorAll('.nav-tab[data-page]').forEach(t => t.classList.toggle('active', t.dataset.page === page));
    // If the new page belongs to a dropdown group, highlight the group toggle
    let parentGroup = null;
    for (const [gid, gdef] of Object.entries(this._navGroups)) {
      if (gdef.pages.some(p => p.id === page)) { parentGroup = gid; break; }
    }
    document.querySelectorAll('.nav-dropdown-toggle').forEach(t => {
      t.classList.toggle('active', parentGroup && t.dataset.group === parentGroup);
    });
    document.querySelectorAll('.page').forEach(p => p.classList.toggle('active', p.id === `page-${page}`));
    // Run page-specific init BEFORE i18n so any JS-injected text gets translated
    if (page === 'dashboard') this.refreshDashboard();
    if (page === 'photo-select' && typeof PhotoSelect !== 'undefined' && !PhotoSelect.initialized) PhotoSelect.init();
    if (page === 'settings') { this.refreshUser(); this.refreshSettings(); if (typeof Keymap !== 'undefined') Keymap._buildSettings(); if (this.AutoTone) this.AutoTone.refresh(); if (this.Memory) this.Memory.refresh(); if (this.Segment) this.Segment.refresh(); }
    if (page === 'agent' && typeof Agent !== 'undefined') Agent.onPageEnter();
    if (page === 'photo-edit' && typeof PhotoEditPage !== 'undefined') PhotoEditPage.onPageEnter();
    if (page === 'batch-export' && typeof BatchExport !== 'undefined') BatchExport.onPageEnter();
    if (page === 'editor-workspace') {
      const opened = typeof PhotoEdit !== 'undefined' && PhotoEdit.openWorkspace();
      if (!opened) {
        this.toast(t('peWsEmpty'), 'warning');
        this.setPage('photo-edit');
        return;
      }
    }
    if (page === 'pipeline' && typeof PipelineUI !== 'undefined') PipelineUI.onPageEnter();
    if (page === 'tethered' && typeof Tethered !== 'undefined') Tethered.onPageEnter();
    if (page === 'cloud') { this.refreshUser(); this.checkSubAccess(); }
    if (page === 'file-browser') { this.refreshUser(); if (this.config.copy_target_path && typeof FileBrowser !== 'undefined' && !FileBrowser.folder) FileBrowser.setFolder(this.config.copy_target_path); }
    // Re-apply i18n AFTER page init to catch any JS-injected text
    if (typeof I18N !== 'undefined') I18N.applyDOM();
  },

  // ── Cloud account ──
  // Open-source build: every local feature is unlocked. Login is only needed for
  // the optional cloud services (backup / albums / client delivery); storage
  // quota is enforced server-side and merely displayed here.
  hasActiveSub() {
    return !!this.user;
  },

  /** Feature flags — kept as an interface so call sites stay stable; everything unlocked */
  getFeatureFlags() {
    return {
      plan: 'open',
      isPro: true,
      isAiSelect: true,
      paid: true,
      canSchedule: true,
      canWatchFolder: true,
      digitalEmployeeDailyLimit: Infinity,
      digitalEmployeeAutonomy: ['L2', 'L3'],
      canCreateCustomTemplate: true,
      officialApiQuota: Infinity,
      aiSelectUnlimited: true,
    };
  },

  getUserStorageLimit() {
    const sub = this.user?.subscription;
    if (sub && sub.storageLimit) return sub.storageLimit;
    return 2 * 1024 * 1024 * 1024;  // server-side default quota, display only
  },

  async refreshUser() {
    if (!this.token) return;
    try {
      const resp = await fetch(`${this.serverUrl}/api/auth/me`, { headers: { 'x-studio-token': this.token } });
      if (resp.ok) { this.user = await resp.json(); this.checkSubAccess(); this.updateCloudConnect(); }
      else if (resp.status === 401) {
        this.toast(t('loginExpired'), 'warning');
        this.user = null; this.token = '';
        this._removeToken();
        this.updateCloudConnect();
      }
    } catch (e) { /* network error — keep existing user data */ }
  },

  checkSubAccess() {
    const lock = document.getElementById('cloud-sub-lock');
    const inner = document.getElementById('cloud-page-inner');
    if (!lock || !inner) return;
    // Open-source build: cloud features just need a (free) account login;
    // storage quota is enforced server-side and displayed only.
    if (this.token) {
      lock.classList.add('hidden');
      inner.style.pointerEvents = ''; inner.style.opacity = '';
    } else {
      lock.classList.remove('hidden');
      inner.style.pointerEvents = 'none'; inner.style.opacity = '0.5';
    }
  },

  // ── Dashboard ──
  refreshDashboard() {
    const s = JSON.parse(localStorage.getItem('dash_stats') || '{}');
    document.getElementById('stat-photos').textContent = s.photos || 0;
    document.getElementById('stat-verified').textContent = s.verified || 0;
    document.getElementById('stat-scored').textContent = s.scored || 0;
    document.getElementById('stat-excellent').textContent = s.excellent || 0;
    document.getElementById('stat-uploaded').textContent = s.uploaded || 0;
    // Automation card (automation.js 按账号存 'automation_<userId>', schedule.type 语义: 'none' = 未定时)
    const cfg = JSON.parse(localStorage.getItem('automation_' + (this.user?.id || 'default')) || '{}');
    document.getElementById('stat-auto-status').innerHTML = {full:`<i class="ph ph-robot"></i>${t('autoFull')}`,semi:`<i class="ph ph-arrows-clockwise"></i>${t('autoSemi')}`,manual:`<i class="ph ph-hand"></i>${t('dashManual')}`}[cfg.strategy||'manual'];
    document.getElementById('stat-auto-next').textContent = ((cfg.schedule||{}).type && cfg.schedule.type !== 'none') ? t('dashScheduled') : t('dashNoSchedule');
    // Refresh the dashboard hero API-key gate
    if (typeof Agent !== 'undefined') Agent.refreshApiKeyGate();
  },

  saveStat(key, val) {
    const s = JSON.parse(localStorage.getItem('dash_stats') || '{}');
    s[key] = val;
    localStorage.setItem('dash_stats', JSON.stringify(s));
  },

  // ── Undo System ──
  _undoStack: [],
  _undoMax: 20,
  _undoToast: null,

  /** Push an undoable action. action = {label, undo: async fn} */
  pushUndo(label, undoFn) {
    this._undoStack.push({ label, undo: undoFn, time: Date.now() });
    if (this._undoStack.length > this._undoMax) this._undoStack.shift();
    // Show undo toast with click-to-undo
    const container = document.getElementById('toast-container');
    if (!container) return;
    this._removeUndoToast();
    const toast = document.createElement('div');
    toast.className = 'toast toast-info';
    toast.innerHTML = `<span>${label}</span> <a href="#" style="color:var(--brand);margin-left:8px;font-weight:600">${t('undo')}</a>`;
    toast.querySelector('a').addEventListener('click', (e) => { e.preventDefault(); this.undoLast(); });
    container.appendChild(toast);
    this._undoToast = toast;
    toast._timer = setTimeout(() => this._removeUndoToast(), 5000);
  },

  _removeUndoToast() {
    if (!this._undoToast) return;
    clearTimeout(this._undoToast._timer);
    if (this._undoToast.parentNode) this._undoToast.remove();
    this._undoToast = null;
  },

  /** Undo the most recent action. */
  async undoLast() {
    const action = this._undoStack.pop();
    if (!action) { this.toast(t('undoNone'), 'info'); return; }
    try {
      await action.undo();
      this.toast(t('appUndoneLabel', { label: action.label }), 'success');
    } catch (e) {
      this.toast(t('appUndoFail', { msg: e.message }), 'error');
    }
    this._removeUndoToast();
  },

  // ── Backend Events ──
  setupBackendEvents() {
    window.api.onPythonEvent((eventName, data) => {
      switch (eventName) {
        case 'drives_changed': case 'drive_detected': case 'copy_progress':
        case 'verify_progress': case 'copy_done': case 'copy_cancelled': case 'copy_error':
          if (typeof CopyCard !== 'undefined') CopyCard.handleEvent(eventName, data); break;
        case 'scan_complete': case 'score_progress': case 'score_done': case 'export_progress':
          if (typeof PhotoSelect !== 'undefined') PhotoSelect.handleEvent(eventName, data);
          if (eventName === 'score_done') App.syncFeedback();
          break;
        case 'tag_progress': case 'tag_done':
          if (typeof PhotoTagger !== 'undefined') PhotoTagger.handleEvent(eventName, data); break;
        case 'model_download_progress':
          App.updateModelDownloadProgress(data);
          if (typeof PhotoSelect !== 'undefined') PhotoSelect.onModelDownloadProgress(data); break;
        case 'model_download_done':
          App.hideModelDownloadModal(true);
          if (typeof PhotoSelect !== 'undefined') PhotoSelect.onModelDownloadDone(data); break;
        case 'model_download_error':
          // Per-file retry errors during download — don't toast (Python retries
          // up to 3x). Only surface a hard failure via model_error at the end.
          if (typeof PhotoSelect !== 'undefined') PhotoSelect.onModelDownloadError(data); break;
        case 'auto_tone_download_progress': case 'auto_tone_download_done': case 'auto_tone_download_error':
          this.AutoTone.handleEvent(eventName, data); break;
        case 'model_loaded':
          App.hideModelDownloadModal(true);
          if (typeof PhotoSelect !== 'undefined') PhotoSelect.handleEvent(eventName, data); break;
        case 'model_error':
          App.hideModelDownloadModal(false);
          if (typeof PhotoSelect !== 'undefined') PhotoSelect.handleEvent(eventName, data); break;
        case 'upload_progress': case 'upload_done':
          if (typeof CloudUpload !== 'undefined') CloudUpload.handleEvent(eventName, data); break;
        case 'batch_export_progress':
          if (typeof BatchExport !== 'undefined') BatchExport.handleEvent(eventName, data); break;
        case 'ftp_file_received': case 'ftp_server_started': case 'ftp_server_stopped':
          if (typeof CopyCard !== 'undefined' && CopyCard._ftpUIReady) CopyCard.handleFtpEvent(eventName, data); break;
        case 'ftp_credentials':
          // 自动生成的 FTP 密码只经此事件下发一次——转发到拷卡页 FTP 日志
          if (typeof CopyCard !== 'undefined') CopyCard.addFtpLog(t('ccFtpCredentials', { user: data.username, pwd: data.password || '' }));
          break;
        case 'ftp_log':
          if (typeof CopyCard !== 'undefined') CopyCard.onFtpLog(data); break;
        case 'watch_started': case 'watch_stopped': case 'watch_file_received':
        case 'watch_batch_ready':
        case 'watch_score_progress': case 'watch_score_done':
          if (typeof Tethered !== 'undefined' && Tethered.isActive()) Tethered.handleEvent(eventName, data);
          else if (typeof CopyCard !== 'undefined') CopyCard.handleWatchEvent(eventName, data);
          break;
        case 'coach_advice': case 'tether_started': case 'tether_stopped': case 'liveview_frame':
          if (typeof Tethered !== 'undefined') Tethered.handleEvent(eventName, data); break;
        case 'log': this.routeLog(data); break;
        case 'agent_thinking': case 'agent_tool_call': case 'agent_tool_progress':
        case 'agent_tool_result': case 'agent_permission_request': case 'agent_permission_resolved':
        case 'agent_iteration': case 'agent_done': case 'agent_error': case 'agent_log':
        case 'agent_work_folder_changed': case 'agent_ask_user':
        case 'agent_watch_started': case 'agent_watch_stopped':
        case 'agent_watch_progress': case 'agent_watch_fired': case 'agent_local_download':
          if (typeof Agent !== 'undefined') Agent.handleEvent(eventName, data); break;
        // 数字员工 Pipeline
        case 'pipeline_started': case 'pipeline_phase_start': case 'pipeline_phase_done':
        case 'pipeline_step_start': case 'pipeline_step': case 'pipeline_progress':
        case 'pipeline_step_skip':
        case 'pipeline_gate': case 'pipeline_paused': case 'pipeline_resumed':
        case 'pipeline_log': case 'pipeline_done': case 'pipeline_error':
          if (typeof PipelineUI !== 'undefined') PipelineUI.handleEvent(eventName, data); break;
      }
    });
    window.api.onPythonStatus((status) => {
      if (status.ready) { this.backendReady = true; this.updateBackendStatus(true); this.loadConfig(); if (typeof PhotoSelect !== 'undefined' && !(typeof Onboarding !== 'undefined' && Onboarding.isOpen())) PhotoSelect.preloadModel(); if (typeof Agent !== 'undefined') { Agent.refreshApiKeyGate(); Agent.armAll(); } }
      else if (status.running === false) { this.backendReady = false; this.updateBackendStatus(false); }
    });
  },
  updateBackendStatus(connected) {
    const dot = document.getElementById('backend-status'); const txt = document.getElementById('backend-text');
    dot.className = 'status-dot ' + (connected ? 'connected' : 'disconnected');
    txt.textContent = connected ? t('backendOn') : t('backendOff');
  },
  routeLog(data) {
    const page = this.currentPage;
    if (page === 'copy-card') { if (typeof CopyCard !== 'undefined') CopyCard.addLog(data.message, data.level || 'info'); }
    else if (page === 'cloud') { if (typeof CloudUpload !== 'undefined') CloudUpload.addLog(data.message, data.level || 'info'); }
    else if (page === 'photo-select' && typeof PhotoSelect !== 'undefined') PhotoSelect.addLog(data.message, data.level || 'info');
  },

  // ── Config ──
  async loadConfig() {
    if (!this.backendReady) return;
    try {
      const result = await window.api.sendCommand('get_config');
      this.config = result.config || result;
      if (typeof CopyCard !== 'undefined') CopyCard.applyConfig(this.config);
      document.getElementById('setting-path-display').textContent = this.config.copy_target_path || t('unset');
    } catch (e) { console.log('Config load deferred:', e.message); }
  },
  async saveConfig(updates = {}) {
    Object.assign(this.config, updates);
    try { await window.api.sendCommand('save_config', { config: this.config }); } catch (e) {
      App.toast(t('appSaveSettingsFail'), 'error');
    }
  },

  // ── Data flywheel: opt-in selection feedback ──
  async refreshFeedbackStatus() {
    const el = document.getElementById('feedback-share-status');
    if (!el) return;
    try {
      const s = await window.api.sendCommand('feedback_get_status');
      if (!s || !s.enabled) { el.textContent = ''; return; }
      el.textContent = t('appFeedbackStatus', { count: s.uploaded || 0 }) + (s.pending ? t('appFeedbackPending', { count: s.pending }) : '');
    } catch (e) { el.textContent = ''; }
  },
  async syncFeedback() {
    // Best-effort: only meaningful when opted-in and logged in.
    if (!this.config || !this.config.feedback_share_enabled || !this.token) return;
    try {
      await window.api.sendCommand('feedback_sync', { cloud_token: this.token, server_url: this.serverUrl });
      this.refreshFeedbackStatus();
    } catch (e) { /* offline queue keeps records for next time */ }
  },

  // ── Settings ──
  _prefsKey() { return 'prefs_' + (this.user?.id || 'default'); },

  loadPrefs() {
    try { return JSON.parse(localStorage.getItem(this._prefsKey()) || '{}'); } catch(e) { return {}; }
  },
  savePrefs(updates) {
    const p = this.loadPrefs(); Object.assign(p, updates);
    localStorage.setItem(this._prefsKey(), JSON.stringify(p));
  },

  /** Flash a setting row green briefly to confirm save */
  flashRow(el) {
    const row = el.closest('.setting-row');
    if (!row) return;
    row.classList.add('saved-flash');
    setTimeout(() => row.classList.remove('saved-flash'), 600);
  },

  setupSettings() {
    // Theme
    document.getElementById('setting-theme').addEventListener('change', e => this.setTheme(e.target.value));
    // Reduce motion（启动时先应用一次）
    this.applyMotionPref();
    document.getElementById('setting-reduce-motion').addEventListener('change', e => {
      this.savePrefs({ reduceMotion: e.target.checked });
      this.applyMotionPref();
      this.flashRow(e.target);
    });
    // Startup page
    document.getElementById('setting-start-page').addEventListener('change', e => this.savePrefs({ startPage: e.target.value }));
    // Storage path
    document.getElementById('btn-setting-path').addEventListener('click', async () => {
      const f = await window.api.selectFolder(); if (f) { this.savePrefs({ copyPath: f }); await this.saveConfig({ copy_target_path: f }); document.getElementById('setting-path-display').textContent = f; this.toast(t('pathUpdated'),'success'); }
    });
    // Folder template
    document.getElementById('setting-folder-template').addEventListener('change', e => this.savePrefs({ folderTemplate: e.target.value }));
    // File filter
    document.getElementById('setting-file-filter').addEventListener('change', e => this.savePrefs({ fileFilter: e.target.value }));
    // Score threshold
    document.getElementById('setting-threshold').addEventListener('input', e => {
      const v = parseFloat(e.target.value); document.getElementById('setting-threshold-label').textContent = v.toFixed(1);
      this.savePrefs({ scoreThreshold: v });
    });
    // Output name
    document.getElementById('setting-output-name').addEventListener('change', e => this.savePrefs({ outputName: e.target.value }));
    // Language
    document.getElementById('setting-lang').addEventListener('change', e => {
      if (typeof I18N !== 'undefined') { I18N.setLang(e.target.value); I18N.applyDOM(); }
      // 动态创建的悬浮图表标题/状态徽标不在 data-i18n 覆盖范围内
      if (typeof PhotoEdit !== 'undefined' && PhotoEdit._updateScopeBadges) {
        PhotoEdit._updateScopeBadges();
      }
      if (typeof Keymap !== 'undefined') Keymap._buildSettings();
      this.toast(t('langChanged'), 'success');
    });
    // Auto-login
    document.getElementById('setting-auto-login').addEventListener('change', e => this.savePrefs({ autoLogin: e.target.checked }));
    // Re-open onboarding wizard
    const obReopen = document.getElementById('btn-reopen-onboarding');
    if (obReopen) obReopen.addEventListener('click', () => { if (typeof Onboarding !== 'undefined') Onboarding.open(); });
    // Keyboard shortcuts: restore defaults
    const kmReset = document.getElementById('btn-keymap-reset');
    if (kmReset) kmReset.addEventListener('click', () => {
      if (typeof Keymap !== 'undefined') Keymap.reset();
      this.toast(t('kmResetDone'), 'success');
    });
    const kmResetLayer = document.getElementById('btn-keymap-reset-layer');
    if (kmResetLayer) kmResetLayer.addEventListener('click', () => {
      if (typeof Keymap !== 'undefined') Keymap.resetLayer(Keymap._activeLayer);
      this.toast(t('kmResetLayerDone'), 'success');
    });
    // Data flywheel — opt-in selection feedback (backend config, not localStorage)
    const fbToggle = document.getElementById('setting-feedback-share');
    if (fbToggle) fbToggle.addEventListener('change', async e => {
      await this.saveConfig({ feedback_share_enabled: e.target.checked });
      this.toast(e.target.checked ? t('settingsFeedbackOn') : t('settingsFeedbackOff'), 'info');
      this.refreshFeedbackStatus();
      if (e.target.checked) this.syncFeedback();
    });
    // 个性化记忆层：总开关 + 清除（P2-6）
    const memToggle = document.getElementById('setting-memory-enabled');
    if (memToggle) memToggle.addEventListener('change', e => App.Memory.setEnabled(e.target.checked));
    const memClearAll = document.getElementById('btn-memory-clear-all');
    if (memClearAll) memClearAll.addEventListener('click', () => App.Memory.clearAll());
    const memClearDir = document.getElementById('btn-memory-clear-dir');
    if (memClearDir) memClearDir.addEventListener('click', () => App.Memory.clearDir());
    // Advanced settings
    document.getElementById('setting-copy-threads').addEventListener('change', e => this.savePrefs({ copyThreads: parseInt(e.target.value) }));
    document.getElementById('setting-wm-enabled').addEventListener('change', e => this.savePrefs({ wmEnabled: e.target.checked }));
    document.getElementById('setting-wm-text').addEventListener('change', e => this.savePrefs({ wmText: e.target.value }));
    document.getElementById('setting-wm-pos').addEventListener('change', e => this.savePrefs({ wmPos: e.target.value }));
    // Logo image watermark
    document.getElementById('btn-wm-image-browse').addEventListener('click', async () => {
      const paths = await window.api.selectFiles([{ name: '图片', extensions: ['png','jpg','jpeg','webp','bmp'] }]);
      if (paths && paths.length) {
        document.getElementById('setting-wm-image').value = paths[0];
        this.savePrefs({ wmImage: paths[0] });
      }
    });
    document.getElementById('btn-wm-image-clear').addEventListener('click', () => {
      document.getElementById('setting-wm-image').value = '';
      this.savePrefs({ wmImage: '' });
    });
    document.getElementById('setting-wm-opacity').addEventListener('input', e => {
      const v = e.target.value; document.getElementById('setting-wm-opacity-label').textContent = v + '%';
      this.savePrefs({ wmOpacity: parseInt(v) });
    });
    document.getElementById('btn-clear-cache').addEventListener('click', () => this.clearRawCache());
    document.getElementById('btn-clear-score-cache').addEventListener('click', () => this.clearScoreCache());
    document.getElementById('btn-clear-user-data').addEventListener('click', () => this.clearUserData());
    document.getElementById('setting-sound').addEventListener('change', e => { this.savePrefs({ sound: e.target.value }); this.flashRow(e.target); this.toast(t('settingsSoundSaved'), 'success'); });
    document.getElementById('setting-dup-days').addEventListener('change', e => { this.savePrefs({ dupDays: parseInt(e.target.value) }); this.flashRow(e.target); this.toast(t('settingsDedupSaved'), 'success'); });
    document.getElementById('setting-timeout').addEventListener('change', e => { this.savePrefs({ timeout: parseInt(e.target.value) }); this.flashRow(e.target); this.toast(t('settingsTimeoutSaved'), 'success'); });
    // Notifications
    document.getElementById('setting-notify').addEventListener('change', e => this.savePrefs({ notify: e.target.checked }));
    // Logout button
    document.getElementById('btn-settings-logout').addEventListener('click', async () => {
      if (await Modal.showConfirm(t('appLogoutTitle'), t('logoutConfirm'))) this.logout();
    });
    // Clear history
    document.getElementById('btn-clear-history').addEventListener('click', async () => {
      if (await Modal.showConfirm(t('appClearHistTitle'), t('settingsClearDataConfirm'))) { localStorage.removeItem('op_history'); this.renderHistory(); this.toast(t('settingsHistoryCleared'), 'success'); }
    });
  },

  refreshSettings() {
    const p = this.loadPrefs();
    document.getElementById('setting-auto-login').checked = p.autoLogin !== false;
    document.getElementById('setting-reduce-motion').checked = !!p.reduceMotion;
    this.applyMotionPref(); // 登录后按当前账号的 prefs 重刷动效开关
    document.getElementById('setting-lang').value = I18N.current;
    document.getElementById('setting-theme').value = localStorage.getItem('theme') || 'light';
    document.getElementById('setting-start-page').value = p.startPage || 'dashboard';
    document.getElementById('setting-path-display').textContent = p.copyPath || this.config.copy_target_path || t('unset');
    document.getElementById('setting-folder-template').value = p.folderTemplate || '{date}';
    document.getElementById('setting-file-filter').value = p.fileFilter || 'all';
    document.getElementById('setting-threshold').value = p.scoreThreshold || 5.0;
    document.getElementById('setting-threshold-label').textContent = (p.scoreThreshold || 5.0).toFixed(1);
    document.getElementById('setting-output-name').value = p.outputName || '优秀照片';
    document.getElementById('setting-notify').checked = p.notify !== false;
    // Data flywheel opt-in (from backend config)
    const fbToggle = document.getElementById('setting-feedback-share');
    if (fbToggle) fbToggle.checked = !!(this.config && this.config.feedback_share_enabled);
    this.refreshFeedbackStatus();
    // Advanced
    document.getElementById('setting-copy-threads').value = p.copyThreads || 1;
    document.getElementById('setting-wm-text').value = p.wmText || '© LensPilot';
    document.getElementById('setting-wm-image').value = p.wmImage || '';
    document.getElementById('setting-wm-pos').value = p.wmPos || 'bottom-right';
    document.getElementById('setting-wm-enabled').checked = p.wmEnabled === true;
    document.getElementById('setting-wm-opacity').value = p.wmOpacity || 30;
    document.getElementById('setting-wm-opacity-label').textContent = (p.wmOpacity || 30) + '%';
    document.getElementById('setting-sound').value = p.sound || 'on';
    document.getElementById('setting-dup-days').value = p.dupDays || 0;
    document.getElementById('setting-timeout').value = p.timeout || 10;
    this.updateCacheInfo();
    this.refreshPackageHint();
    // Version from package.json
    if (window.api && window.api.getVersion) {
      window.api.getVersion().then(v => {
        const el = document.getElementById('app-version-text');
        if (el && v) el.textContent = 'v' + v;
      }).catch(() => {});
    }
    // Operation history
    this.renderHistory();
  },

  // Apply prefs to other pages
  applyPrefs() {
    const p = this.loadPrefs();
    if (p.copyPath) { document.getElementById('copy-target-path').value = p.copyPath; }
    if (p.folderTemplate) { document.getElementById('copy-folder-name').value = p.folderTemplate; }
    if (p.fileFilter) { document.getElementById('copy-file-filter').value = p.fileFilter; }
    if (p.scoreThreshold) { document.getElementById('select-threshold').value = p.scoreThreshold; document.getElementById('threshold-label').textContent = p.scoreThreshold.toFixed(1); }
    else { document.getElementById('select-threshold').value = 5.0; document.getElementById('threshold-label').textContent = '5.0'; }
    if (p.outputName) { document.getElementById('select-output-name').value = p.outputName; }
    // AI 选片输出模式（文件夹 / 星标）与星标分数段
    if (p.psMode) { document.getElementById('ps-mode').value = p.psMode; }
    if (Array.isArray(p.psStarThresholds) && p.psStarThresholds.length === 5) {
      // 存储顺序 [★5, ★4, ★3, ★2, ★1]
      for (let s = 5; s >= 1; s--) {
        const v = parseFloat(p.psStarThresholds[5 - s]);
        if (isFinite(v)) document.getElementById('ps-star-' + s).value = v;
      }
    }
    if (typeof PhotoSelect !== 'undefined') {
      PhotoSelect.outputBase = p.psOutputBase || '';
      if (PhotoSelect._renderOutputBaseHint) PhotoSelect._renderOutputBaseHint();
      if (PhotoSelect._applyModeUI) PhotoSelect._applyModeUI();
    }
  },

  // ── Login UI ──
  setupLoginUI() {
    document.querySelectorAll('.login-tab').forEach(tab => {
      tab.addEventListener('click', () => {
        document.querySelectorAll('.login-tab').forEach(t => t.classList.remove('active'));
        tab.classList.add('active');
        const isLogin = tab.dataset.tab === 'login';
        document.getElementById('form-login').classList.toggle('hidden', !isLogin);
        document.getElementById('form-register').classList.toggle('hidden', isLogin);
      });
    });
    document.getElementById('form-login').addEventListener('submit', async (e) => { e.preventDefault(); await this.doLogin(); });
    document.getElementById('form-register').addEventListener('submit', async (e) => { e.preventDefault(); await this.doRegister(); });

    // Forgot Password
    document.getElementById('btn-forgot-pw').addEventListener('click', () => this.showForgotPassword());
    document.getElementById('btn-reset-cancel').addEventListener('click', () => this.hideForgotPassword());
    document.getElementById('form-forgot').addEventListener('submit', async (e) => { e.preventDefault(); await this.doResetPassword(); });
    // Forgot password mode toggle (email / phone)
    document.querySelectorAll('#form-forgot .reg-toggle-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('#form-forgot .reg-toggle-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        const mode = btn.dataset.fpmode;
        document.getElementById('forgot-email-section').classList.toggle('hidden', mode !== 'email');
        document.getElementById('forgot-phone-section').classList.toggle('hidden', mode !== 'phone');
      });
    });
    document.getElementById('btn-send-reset-code').addEventListener('click', async () => {
      const email = document.getElementById('forgot-email').value.trim();
      const msg = document.getElementById('forgot-msg');
      if (!email || !email.includes('@')) { msg.textContent = t('appInvalidEmail'); msg.style.color = 'var(--red)'; msg.style.display = 'block'; return; }
      try {
        document.getElementById('btn-send-reset-code').disabled = true;
        await fetch(`${this.serverUrl}/api/auth/forgot-password`, {
          method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({email}),
        });
        msg.textContent = t('appCodeSent'); msg.style.color = 'var(--green)'; msg.style.display = 'block';
        document.getElementById('forgot-code').focus();
      } catch(e) { msg.textContent = t('appSendFail', { msg: e.message }); msg.style.color = 'var(--red)'; msg.style.display = 'block'; }
      finally { document.getElementById('btn-send-reset-code').disabled = false; }
    });

    const setupToggle = (formId, emailId, phoneId) => {
      document.querySelectorAll(`#${formId} .reg-toggle-btn`).forEach(btn => {
        btn.addEventListener('click', () => {
          document.querySelectorAll(`#${formId} .reg-toggle-btn`).forEach(b => b.classList.remove('active'));
          btn.classList.add('active');
          const mode = btn.dataset.mode;
          document.getElementById(emailId).classList.toggle('hidden', mode !== 'email');
          document.getElementById(phoneId).classList.toggle('hidden', mode !== 'phone');
        });
      });
    };
    setupToggle('form-login', 'login-email', 'login-phone');
    setupToggle('form-register', 'reg-email', 'reg-phone');

    document.getElementById('btn-logout').addEventListener('click', () => this.logout());
    document.getElementById('nav-user-name').addEventListener('click', () => this.showAccountModal());
    document.getElementById('btn-open-login').addEventListener('click', () => {
      document.getElementById('login-overlay').classList.remove('hidden');
    });
    document.getElementById('acct-close').addEventListener('click', () => Modal.animatedHide(document.getElementById('account-modal')));
    document.getElementById('account-modal').addEventListener('click', (e) => {
      if (e.target === document.getElementById('account-modal')) Modal.animatedHide(document.getElementById('account-modal'));
    });
    document.getElementById('acct-change-pw').addEventListener('click', () => {
      Modal.animatedHide(document.getElementById('account-modal'));
      this.showChangePasswordDialog();
    });
  },

  async checkLogin() {
    // Open-source build: the app starts directly, no login required. If a saved
    // token exists, the cloud session is restored silently in the background —
    // never blocking, never prompting. Login only matters for the optional
    // cloud features (backup / albums / delivery).
    const saved = await this._loadToken();
    if (!saved) {
      document.getElementById('btn-open-login')?.classList.remove('hidden');
      return;
    }
    this.token = saved;
    try {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 8000);
      const resp = await fetch(`${this.serverUrl}/api/auth/me`, {
        headers: { 'x-studio-token': saved }, signal: controller.signal
      });
      clearTimeout(timer);
      if (resp.ok) {
        this.user = await resp.json();
        this.showUserInNav();
        this.updateCloudConnect();
      } else if (resp.status === 401 || resp.status === 403) {
        // Token invalid — clear it silently, stay in local mode
        await this._removeToken();
      }
    } catch (e) { /* offline / server unreachable — stay in local mode */ }
  },

  async doLogin() {
    const activeBtn = document.querySelector('#form-login .reg-toggle-btn.active');
    if (!activeBtn) return this.showLoginError(t('appLoginFormNotReady'));
    const mode = activeBtn.dataset.mode;
    const login = mode === 'email' ? document.getElementById('login-email').value.trim() : document.getElementById('login-phone').value.trim();
    const password = document.getElementById('login-password').value;
    if (!login) return this.showLoginError(mode === 'email' ? t('emailOnly') : t('phoneOnly'));
    if (!password) return this.showLoginError(t('appPwRequired'));
    if (password.length < 6) return this.showLoginError(t('appPwMin6'));

    this.showLoading(t('logining')); this.setStatus(t('waitingServer')); document.getElementById('login-overlay').classList.add('hidden');
    try {
      const body = { password }; if (mode === 'email') body.email = login; else body.phone = login;
      const resp = await fetch(`${this.serverUrl}/api/auth/login`, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body) });
      if (!resp.ok) { const err = await resp.json().catch(()=>({})); throw new Error(err.error||t('appLoginFail')); }
      const data = await resp.json();
      this.token = data.token; this.user = data.user || {};
      this._saveToken(data.token);
      this.hideLoading(); this.setStatus(t('ready'),'success'); this.showUserInNav(); this.updateCloudConnect(); this.checkSubAccess(); this.applyPrefs(); this.toast(t('loginSuccess'),'success');
      this._maybeOpenOnboarding();
    } catch (e) { this.hideLoading(); this.setStatus(t('appLoginFail'),'error'); document.getElementById('login-overlay').classList.remove('hidden'); this.showLoginError(e.message); }
  },

  async doRegister() {
    const name = document.getElementById('reg-name').value.trim();
    const activeBtn = document.querySelector('#form-register .reg-toggle-btn.active');
    if (!activeBtn) return this.showLoginError(t('appRegFormNotReady'));
    const mode = activeBtn.dataset.mode;
    const email = mode === 'email' ? document.getElementById('reg-email').value.trim() : '';
    const phone = mode === 'phone' ? document.getElementById('reg-phone').value.trim() : '';
    const password = document.getElementById('reg-password').value;
    if (mode === 'email' && !email) return this.showLoginError(t('emailOnly'));
    if (mode === 'phone' && !phone) return this.showLoginError(t('phoneOnly'));
    if (!password || password.length < 6) return this.showLoginError(t('pwMinLen'));
    if (!document.getElementById('reg-agree').checked) return this.showLoginError(t('appAgreeRequired'));

    this.showLoading(t('reging')); this.setStatus(t('waitingServer')); document.getElementById('login-overlay').classList.add('hidden');
    try {
      const resp = await fetch(`${this.serverUrl}/api/auth/register`, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({email,phone,password,displayName:name}) });
      if (!resp.ok) { const err = await resp.json().catch(()=>({})); throw new Error(err.error||t('appRegFail')); }
      const data = await resp.json();
      this.token = data.token; this.user = data.user;
      this._saveToken(data.token);
      this.hideLoading(); this.setStatus(t('ready'),'success'); this.showUserInNav(); this.updateCloudConnect(); this.applyPrefs(); this.toast(t('regSuccess'),'success');
      this._maybeOpenOnboarding();
    } catch (e) { this.hideLoading(); this.setStatus(t('appRegFail'),'error'); document.getElementById('login-overlay').classList.remove('hidden'); this.showLoginError(e.message); }
  },

  logout() {
    this.token = null; this.user = null; this._removeToken();
    document.getElementById('nav-user').classList.add('hidden');
    document.getElementById('btn-open-login').classList.remove('hidden');
    document.getElementById('cloud-status').textContent = t('cloudNeedLogin');
    document.getElementById('btn-upload-start').disabled = true;
    this.toast(t('logoutMsg'),'info');
  },

  // ── Shared: Create a cloud album (used by file-browser & uploader) ──
  async createAlbum(title, password, photoIds, maxPicks) {
    if (!this.token) throw new Error(t('appNeedLogin'));
    const resp = await fetch(`${this.serverUrl}/api/albums`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'x-studio-token': this.token },
      body: JSON.stringify({
        title, photoIds: photoIds || [], password: password || '',
        maxPicks: Math.max(0, parseInt(maxPicks, 10) || 0),
      }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.error || t('appCreateFail'));
    }
    const album = await resp.json();
    album.shareUrl = `${this.serverUrl}/album/${album.accessCode}`;
    return album;
  },

  showUserInNav() {
    if (!this.user) return;
    document.getElementById('nav-user').classList.remove('hidden');
    document.getElementById('btn-open-login').classList.add('hidden');
    document.getElementById('nav-user-name').textContent = this.user.displayName || this.user.email;
  },
  showLoginError(msg) { const el=document.getElementById('login-error'); el.textContent=msg; el.classList.remove('hidden'); setTimeout(()=>el.classList.add('hidden'),4000); },
  updateCloudConnect() {
    if (this.token) {
      const st = document.getElementById('cloud-status');
      if (st) { st.innerHTML=`<i class="ph ph-check-circle"></i> ${t('cloudConnected')}`; st.style.color='var(--green)'; }
      document.getElementById('btn-upload-start').disabled = false;
      this.updateStorageDisplay();
    }
  },
  async showChangePasswordDialog() {
    const oldPw = await Modal.showInput(t('changePW'), t('oldPW')); if(!oldPw)return;
    const newPw = await Modal.showInput(t('changePW'), t('newPW')); if(!newPw||newPw.length<6)return this.showLoginError(t('appNewPwMin'));
    this.showLoading(t('appChanging')); this.setStatus(t('waitingServer'));
    try {
      const resp=await fetch(`${this.serverUrl}/api/auth/password`,{method:'PUT',headers:{'Content-Type':'application/json','x-studio-token':this.token},body:JSON.stringify({oldPassword:oldPw,newPassword:newPw})});
      if(!resp.ok){const err=await resp.json().catch(()=>({}));throw new Error(err.error||t('appChangeFail'));}
      const data=await resp.json(); this.token=data.token; this._saveToken(data.token); this.updateCloudConnect();
      this.hideLoading(); this.setStatus(t('ready'),'success'); this.toast(t('pwChanged'),'success');
    } catch(e){ this.hideLoading(); this.setStatus(t('appChangeFail'),'error'); this.showLoginError(e.message); }
  },

  showAccountModal() {
    if (!this.user) return;
    document.getElementById('acct-email').textContent = this.user.email || '—';
    document.getElementById('acct-phone').textContent = this.user.phone || '—';
    // Open-source build: no subscription tiers — show the server-side storage quota only.
    const sub = this.user.subscription;
    StorageBar.render({
      barId: 'acct-storage-bar', textId: 'acct-storage',
      used: sub?.storageUsed || 0, limit: this.getUserStorageLimit(),
    });
    document.getElementById('acct-storage-row').classList.remove('hidden');
    Modal.animatedShow(document.getElementById('account-modal'));
  },

  updateStorageDisplay() {
    if (!this.user) return;
    StorageBar.render({
      barId: 'cloud-storage-bar', textId: 'cloud-storage-text',
      used: this.user.subscription?.storageUsed || 0, limit: this.getUserStorageLimit(),
    });
  },

  showForgotPassword() {
    document.getElementById('form-login').classList.add('hidden');
    document.getElementById('form-register').classList.add('hidden');
    document.querySelector('.login-tabs').classList.add('hidden');
    document.getElementById('form-forgot').classList.remove('hidden');
    document.getElementById('login-error').classList.add('hidden');
  },

  hideForgotPassword() {
    document.getElementById('form-login').classList.remove('hidden');
    document.querySelector('.login-tabs').classList.remove('hidden');
    document.getElementById('form-forgot').classList.add('hidden');
    document.getElementById('forgot-email').value = '';
    document.getElementById('forgot-code').value = '';
    document.getElementById('forgot-new-pw').value = '';
    document.getElementById('forgot-msg').style.display = 'none';
  },

  async doResetPassword() {
    const email = document.getElementById('forgot-email').value.trim();
    const code = document.getElementById('forgot-code').value.trim();
    const password = document.getElementById('forgot-new-pw').value;
    const msg = document.getElementById('forgot-msg');
    if (!email || !code || !password) { msg.textContent = t('appFillAll'); msg.style.color = 'var(--red)'; msg.style.display = 'block'; return; }
    if (password.length < 8) { msg.textContent = t('appPwMin8'); msg.style.color = 'var(--red)'; msg.style.display = 'block'; return; }
    try {
      const resp = await fetch(`${this.serverUrl}/api/auth/reset-password`, {
        method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({email, code, password}),
      });
      if (!resp.ok) { const err = await resp.json().catch(()=>({})); throw new Error(err.error||t('appResetFail')); }
      msg.textContent = t('appResetDone'); msg.style.color = 'var(--green)'; msg.style.display = 'block';
      document.getElementById('login-email').value = email;
      document.getElementById('login-password').value = '';
      setTimeout(() => this.hideForgotPassword(), 2000);
    } catch(e) { msg.textContent = e.message; msg.style.color = 'var(--red)'; msg.style.display = 'block'; }
  },

  async updateCacheInfo() {
    try {
      const info = await window.api.sendCommand('get_cache_info', {});
      document.getElementById('setting-cache-info').textContent =
        info.count ? t('appCacheInfo', { count: info.count, size: (info.size/1024/1024).toFixed(1) }) : t('appCacheEmpty');
    } catch(e) { document.getElementById('setting-cache-info').textContent = t('appCacheCalc'); }
  },

  // 显示安装包类型（CPU 版仅 ONNX / GPU 版可切 PyTorch）于模型状态卡与设置页。
  // 安装包类型由打包时是否捆绑 PyTorch 运行时决定，运行时不可改。
  async refreshPackageHint() {
    const setHint = (text, badgeText) => {
      const mt = document.getElementById('model-package-text');
      const sd = document.getElementById('setting-package-desc');
      const sb = document.getElementById('setting-package-badge');
      if (mt) mt.textContent = text;
      if (sd) sd.innerHTML = `${text}<br><span style="font-size:11px;color:var(--text3)">${t('appPackageDesc')}</span>`;
      if (sb) sb.textContent = badgeText;
    };
    if (!this.backendReady) { setHint(t('appBackendNotReady'), t('psDetecting')); return; }
    try {
      const pref = await window.api.sendCommand('get_backend_pref', {});
      if (pref && pref.package_type === 'gpu') {
        setHint(t('appPkgGpuDesc'), t('appPkgGpu'));
      } else {
        setHint(t('appPkgCpuDesc'), t('appPkgCpu'));
      }
    } catch (e) { setHint(t('appDetectFail'), t('psDetecting')); }
  },

  async clearRawCache() {
    try {
      const r = await window.api.sendCommand('clear_raw_cache', {});
      this.toast(t('appCacheCleared', { count: r.deleted || 0 }), 'success');
      this.updateCacheInfo();
    } catch(e) { this.toast(t('appCacheClearFail'), 'error'); }
  },

  async clearScoreCache() {
    const confirmed = await Modal.showConfirm(t('appClearScoreTitle'),
      t('appClearScoreConfirm')
    );
    if (!confirmed) return;
    try {
      const r = await window.api.sendCommand('clear_score_cache', {});
      if (r && r.ok) this.toast(t('appScoreCacheCleared'), 'success');
      else this.toast(t('appScoreCacheClearFail'), 'error');
    } catch(e) { this.toast(t('appScoreCacheClearFail'), 'error'); }
  },

  async clearUserData() {
    const confirmed = await Modal.showConfirm(t('appClearUserTitle'),
      t('appClearUserConfirm')
    );
    if (!confirmed) return;

    try {
      // 1. Clear backend data (models, cache, config, scores.db)
      const r = await window.api.sendCommand('clear_user_data', {});
      const count = (r.deleted_paths || []).length;
      this.toast(t('appUserDataDeleted', { count }), 'success');
      console.log('[clearUserData] deleted:', r.deleted_paths);

      // 2. Clear frontend localStorage
      localStorage.clear();

      // 3. Relaunch
      setTimeout(() => {
        window.api.relaunchApp();
      }, 500);
    } catch (e) {
      this.toast(t('appClearDataFail', { msg: e.message }), 'error');
      console.error('[clearUserData] error:', e);
    }
  },

  // ── Loading ──
  showLoading(text) { const el=document.getElementById('loading-overlay'); if(el){el.querySelector('.loading-text').textContent=text||t('loading');el.classList.remove('hidden');} },
  hideLoading() { const el=document.getElementById('loading-overlay'); if(el)el.classList.add('hidden'); },
  setStatus(msg,level) { const bar=document.getElementById('status-bar'); if(bar){bar.textContent=msg;bar.className='status-bar';if(level)bar.classList.add(level);} },

  // ── Error Reporting ──
  _setupErrorReporting() {
    if (this._errReporting) return;
    this._errReporting = true;
    window.addEventListener('error', (e) => {
      this._reportError('js_error', { message: e.message, file: e.filename, line: e.lineno, col: e.colno });
    });
    window.addEventListener('unhandledrejection', (e) => {
      this._reportError('unhandled_rejection', { message: e.reason?.message || String(e.reason) });
    });
  },
  async _reportError(type, data) {
    if (!this.token || !this.serverUrl) return;
    try {
      await fetch(`${this.serverUrl}/api/client-error`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'x-studio-token': this.token },
        body: JSON.stringify({ type, ...data, version: this._version || 'unknown', time: new Date().toISOString() }),
      });
    } catch (e) { /* silent */ }
  },
};

document.addEventListener('DOMContentLoaded', () => App.init());

