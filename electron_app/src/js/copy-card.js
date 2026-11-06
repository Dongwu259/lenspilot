/**
 * copy-card.js — Auto Copy Card page logic
 */

const CopyCard = {
  monitoring: false,
  copying: false,
  _copyTimeout: null,    // safety timeout: auto-reset copying if backend dies
  _COPY_MAX_MS: 30 * 60 * 1000,  // 30 minutes max for a copy operation
  presets: {},
  currentPreset: '',
  pendingCards: [],

  // ── Initialization ──

  init() {
    this.bindEvents();
    // Scan drives immediately on page load (no need to wait for "开始检测")
    this.scanDrives();
  },

  applyConfig(config) {
    if (!config) return;

    if (config.copy_target_path) {
      document.getElementById('copy-target-path').value = config.copy_target_path;
    }
    if (config.copy_folder_name) {
      document.getElementById('copy-folder-name').value = config.copy_folder_name;
    }
    if (config.presets) {
      this.presets = config.presets;
      this.currentPreset = config.current_preset || Object.keys(config.presets)[0] || '';
      this.refreshPresetList();
    }
    if (config.file_filter) {
      document.getElementById('copy-file-filter').value = config.file_filter;
    }
    this.updateScanHint();
    // Scan drives when config is fully loaded (presets may affect scan_path)
    this.scanDrives();
  },

  bindEvents() {
    // Browse target path
    document.getElementById('btn-browse-target').addEventListener('click', async () => {
      const folder = await window.api.selectFolder();
      if (folder) {
        document.getElementById('copy-target-path').value = folder;
        App.saveConfig({ copy_target_path: folder });
      }
    });

    // Folder name preview — sync to settings
    document.getElementById('copy-folder-name').addEventListener('input', () => {
      this.updateFolderPreview();
      App.savePrefs({ folderTemplate: document.getElementById('copy-folder-name').value });
    });

    // Preset selection
    document.getElementById('copy-preset').addEventListener('change', () => {
      this.currentPreset = document.getElementById('copy-preset').value;
      App.saveConfig({ current_preset: this.currentPreset });
      this.updateScanHint();
      this.scanDrives();
    });

    // Add preset
    document.getElementById('btn-add-preset').addEventListener('click', () => this.addPreset());

    // File filter — sync to settings
    document.getElementById('copy-file-filter').addEventListener('change', () => {
      App.savePrefs({ fileFilter: document.getElementById('copy-file-filter').value });
      App.saveConfig({ file_filter: document.getElementById('copy-file-filter').value });
    });

    // Start/Stop monitor
    document.getElementById('btn-start-monitor').addEventListener('click', () => this.startMonitor());
    document.getElementById('btn-stop-monitor').addEventListener('click', () => this.stopMonitor());

    // Cancel copy
    document.getElementById('btn-cancel-copy').addEventListener('click', () => this.cancelCopy());

    // Clear queue
    document.getElementById('btn-clear-queue').addEventListener('click', () => this.clearQueue());

    // ── FTP / Watch Tab ──
    document.querySelectorAll('[data-transfer-tab]').forEach(btn => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('[data-transfer-tab]').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        const tab = btn.dataset.transferTab;
        document.getElementById('transfer-usb-content').classList.toggle('hidden', tab !== 'usb');
        document.getElementById('transfer-ftp-content').classList.toggle('hidden', tab !== 'ftp');
        document.getElementById('transfer-watch-content').classList.toggle('hidden', tab !== 'watch');
        if (tab === 'ftp') this.checkFtpStatus();
        if (tab === 'watch') this.checkWatchStatus();
      });
    });

    document.getElementById('btn-ftp-start').addEventListener('click', () => this.ftpStart());
    document.getElementById('btn-ftp-stop').addEventListener('click', () => this.ftpStop());
    document.getElementById('btn-ftp-browse-folder').addEventListener('click', async () => {
      const f = await window.api.selectFolder();
      if (f) document.getElementById('ftp-receive-folder').value = f;
    });
    document.getElementById('btn-ftp-copy').addEventListener('click', () => {
      const addr = document.getElementById('ftp-address').value;
      navigator.clipboard.writeText(addr).then(() => App.toast(t('ccAddrCopied'), 'success'));
    });

    // ── Watch Tab ──
    document.getElementById('btn-watch-start').addEventListener('click', () => this.startWatch());
    document.getElementById('btn-watch-stop').addEventListener('click', () => this.stopWatch());
    document.getElementById('btn-watch-browse').addEventListener('click', async () => {
      const f = await window.api.selectFolder();
      if (f) {
        document.getElementById('watch-folder').value = f;
        // 默认输出目录 = 监听目录的同级「精选」目录
        if (!document.getElementById('watch-output-folder').value) {
          const parent = f.replace(/[\\/][^\\/]+$/, '');
          document.getElementById('watch-output-folder').value = parent + '/精选照片';
        }
        this.saveWatchConfig();
      }
    });
    document.getElementById('btn-watch-output-browse').addEventListener('click', async () => {
      const f = await window.api.selectFolder();
      if (f) { document.getElementById('watch-output-folder').value = f; this.saveWatchConfig(); }
    });
    document.getElementById('btn-watch-score').addEventListener('click', () => this.startManualScore());
    ['watch-mode', 'watch-filter', 'watch-quiet', 'watch-threshold', 'watch-auto-upload', 'watch-process-existing']
      .forEach(id => document.getElementById(id).addEventListener('change', () => this.saveWatchConfig()));
    // 阈值同步到全局 pref
    document.getElementById('watch-threshold').addEventListener('change', () => {
      App.savePrefs({ scoreThreshold: parseFloat(document.getElementById('watch-threshold').value) });
    });

    this.loadWatchConfig();
    this.updateFolderPreview();
  },

  // ── Preset Management ──

  refreshPresetList() {
    const select = document.getElementById('copy-preset');
    select.innerHTML = '';
    Object.keys(this.presets).forEach(name => {
      const opt = document.createElement('option');
      opt.value = name;
      opt.textContent = name;
      if (name === this.currentPreset) opt.selected = true;
      select.appendChild(opt);
    });
    if (!this.currentPreset && Object.keys(this.presets).length > 0) {
      this.currentPreset = Object.keys(this.presets)[0];
      select.value = this.currentPreset;
    }
  },

  updateScanHint() {
    const preset = this.presets[this.currentPreset];
    const scanPath = preset ? preset.photo_scan_path : 'DCIM';
    document.getElementById('scan-hint').textContent =
      t('ccScanHint', { path: scanPath.replace(/\\/g, '/') });
  },

  updateFolderPreview() {
    const name = document.getElementById('copy-folder-name').value;
    const today = new Date().toISOString().slice(0, 10);
    const resolved = name.replace('{date}', today);
    document.getElementById('folder-preview').textContent = '→ ' + resolved;
  },

  async addPreset() {
    try {
      const name = await Modal.showInput(t('ccAddPresetTitle'), t('ccAddPresetPrompt'));
      if (!name) return;
      if (this.presets[name]) {
        await Modal.showMessage(t('ccPresetExists', { name }), 'warning');
        return;
      }

      await Modal.showMessage(
        t('ccSelectPhotoDirMsg'),
        'info'
      );
      const photoDir = await window.api.selectFolder();
      if (!photoDir) return;

      await Modal.showMessage(
        t('ccSelectVideoDirMsg'),
        'info'
      );
      const videoDir = await window.api.selectFolder();
      if (!videoDir) return;

      // Extract relative paths
      const photoRel = this.extractRelativePath(photoDir);
      const videoRel = this.extractRelativePath(videoDir);

      this.presets[name] = {
        photo_scan_path: photoRel,
        video_scan_path: videoRel,
      };
      this.currentPreset = name;
      this.refreshPresetList();
      App.saveConfig({ presets: this.presets, current_preset: name });
      this.addLog(t('ccPresetAdded', { name, photo: photoRel, video: videoRel }), 'success');
    } catch (err) {
      this.addLog(t('ccAddPresetFail', { msg: err.message }), 'error');
      console.error('addPreset error:', err);
    }
  },

  extractRelativePath(fullPath) {
    // Extract path relative to drive root
    // Windows: "D:\DCIM\100MSDCF" → "DCIM\100MSDCF" (去掉盘符 "D:\")
    // macOS:   "/Volumes/CARD/DCIM" → "DCIM"
    // Linux:   "/media/user/CARD/DCIM" → "DCIM"
    //          "/run/media/user/CARD/DCIM" → "DCIM"
    //          "/mnt/CARD/DCIM" → "DCIM"
    const normalized = fullPath.replace(/\\/g, '/');
    if (/^[A-Za-z]:[/\\]/.test(fullPath)) {
      // Windows: strip drive letter prefix
      return fullPath.slice(3).replace(/^[\\/]/, '');
    }
    // Mac/Linux: strip mount point prefix
    const parts = normalized.split('/');
    let skip = 0;
    if (parts[1] === 'Volumes') {
      skip = 3;              // /Volumes/LABEL/...
    } else if (parts[1] === 'media') {
      skip = 4;              // /media/USER/LABEL/...
    } else if (parts[1] === 'run' && parts[2] === 'media') {
      skip = 5;              // /run/media/USER/LABEL/...
    } else if (parts[1] === 'mnt') {
      skip = 3;              // /mnt/LABEL/...
    }
    if (skip > 0 && parts.length > skip) {
      return parts.slice(skip).join('/');
    }
    // Fallback: return last 1-2 segments
    return parts.slice(-2).join('/');
  },

  // ── Monitor ──

  async startMonitor() {
    const targetPath = document.getElementById('copy-target-path').value;
    if (!targetPath) {
      App.toast(t('ccSelectTargetFirst'), 'warning');
      const folder = await window.api.selectFolder();
      if (!folder) return;
      document.getElementById('copy-target-path').value = folder;
      App.saveConfig({ copy_target_path: folder });
    }

    const preset = this.presets[this.currentPreset];
    const scanPath = preset ? preset.photo_scan_path : 'DCIM';

    try {
      await window.api.sendCommand('start_monitor', {
        scan_path: scanPath,
        target_path: document.getElementById('copy-target-path').value,
        preset_name: this.currentPreset,
        file_filter: document.getElementById('copy-file-filter').value,
      });

      this.monitoring = true;
      UI.setButton('btn-start-monitor', false);
      UI.setButton('btn-stop-monitor', true);
      this.addLog(t('ccMonitorStarted', { path: scanPath.replace(/\\/g, '/') }), 'info');
    } catch (e) {
      this.addLog(t('ccMonitorStartFail', { msg: e.message }), 'error');
    }
  },

  async stopMonitor() {
    try {
      await window.api.sendCommand('stop_monitor');
    } catch (e) { /* ignore */ }

    this.monitoring = false;
    UI.setButton('btn-start-monitor', true);
    UI.setButton('btn-stop-monitor', false);
    this.addLog(t('ccMonitorStopped'), 'info');
  },

  // ── Drive Operations ──

  async scanDrives() {
    try {
      // 传当前预设的照片目录，否则自定义预设下后端按默认 'DCIM' 判断，开始拷贝按钮不出现
      const preset = this.presets[this.currentPreset];
      const scanPath = preset ? preset.photo_scan_path : 'DCIM';
      const result = await window.api.sendCommand('list_drives', { scan_path: scanPath });
      this.renderDriveList(result.drives);
    } catch (e) {
      console.error('scanDrives failed:', e.message);
    }
  },

  renderDriveList(drives) {
    const container = document.getElementById('drive-list');
    const countEl = document.getElementById('drive-count');

    if (!drives || drives.length === 0) {
      container.innerHTML = '<div class="empty-state">' + t('ccWaitDeviceDots') + '</div>';
      countEl.textContent = '0';
      return;
    }

    countEl.textContent = drives.length;
    container.innerHTML = drives.map(d => `
      <div class="drive-item" data-drive-letter="${this._escapeHtml(d.letter)}" data-src-path="${this._escapeHtml(d.src_path || '')}" data-drive-label="${this._escapeHtml(d.label || '')}">
        <span class="drive-icon">${d.has_scan_path ? '<i class="ph ph-floppy-disk"></i>' : '<i class="ph ph-disc"></i>'}</span>
        <div class="drive-info">
          <div class="drive-letter">${this._formatDriveDisplay(d.letter)} <span style="color:var(--text-muted);font-weight:400">[${d.label || t('ccNoLabel')}]</span></div>
          <div class="drive-path">${d.has_scan_path ? '✓ ' + d.scan_path : t('ccNoTargetFolder')}</div>
        </div>
        <div class="drive-action">
          ${d.has_scan_path ? '<button class="btn btn-primary btn-sm btn-start-copy">' + t('ccStartCopy') + '</button>' : ''}
        </div>
      </div>
    `).join('');
    // Use event delegation instead of inline onclick to avoid path backslash corruption
    container.querySelectorAll('.btn-start-copy').forEach(btn => {
      btn.addEventListener('click', () => {
        const item = btn.closest('.drive-item');
        this.confirmCopy(
          item.dataset.driveLetter,
          item.dataset.srcPath,
          item.dataset.driveLabel
        );
      });
    });
  },

  async confirmCopy(letter, srcPath, label) {
    const targetPath = document.getElementById('copy-target-path').value;
    const folderName = this.resolveFolderName();
    const filterKey = document.getElementById('copy-file-filter').value;
    const filterLabel = document.getElementById('copy-file-filter').selectedOptions[0].textContent;

    const msg = [
      t('ccCopyDetectMsg', { drive: this._formatDriveDisplay(letter), label: label ? `(${label})` : '' }),
      ``,
      t('ccCopyMsgModel', { name: this.currentPreset }),
      t('ccCopyMsgFilter', { filter: filterLabel }),
      t('ccCopyMsgSrc', { path: srcPath }),
      t('ccCopyMsgTarget', { path: this._formatDisplayPath(targetPath, folderName) }),
      ``,
      t('ccCopyMsgConfirm'),
    ].join('\n');

    if (await Modal.showConfirm(t('ccStartCopy'), msg)) {
      this.startCopy(letter, srcPath, folderName, label);
    } else {
      this.addLog(t('ccCopyCancelledByUser', { drive: this._formatDriveDisplay(letter) }), 'warn');
    }
  },

  async startCopy(letter, srcPath, folderName, label) {
    if (this.copying) {
      // Add to queue
      this.pendingCards.push({ letter, srcPath, folderName, label });
      this.renderQueue();
      this.addLog(t('ccCopyQueued', { letter: this._formatDriveDisplay(letter), pos: this.pendingCards.length }), 'warn');
      return;
    }

    const targetPath = document.getElementById('copy-target-path').value;
    const filterKey = document.getElementById('copy-file-filter').value;

    this.copying = true;
    this._armCopyTimeout();  // safety: auto-reset if backend dies
    UI.setButton('btn-cancel-copy', true);
    UI.updateProgress('copy-progress-fill', 'copy-progress-text', 0, t('ccPreparing'));
    UI.clearLog('copy-log');
    this.addLog(t('ccCopyStarted', { src: srcPath, dest: this._formatDisplayPath(targetPath, folderName) }), 'info');

    try {
      const prefs = JSON.parse(localStorage.getItem('prefs_' + (App.user?.id || 'default')) || '{}');
      const r = await window.api.sendCommand('start_copy', {
        letter,
        src_path: srcPath,
        target_path: targetPath,
        folder_name: folderName,
        file_filter: filterKey,
        threads: prefs.copyThreads || 1,
      });
      // Backend reports scan/queue failures as a normal {"error": ...} result —
      // no copy thread starts and no events follow. Treat it as a thrown error
      // so the UI resets instead of waiting on progress that will never come.
      if (r && r.error) throw new Error(r.error);
    } catch (e) {
      this.addLog(t('ccCopyStartFail', { msg: e.message }), 'error');
      this.copying = false;
      UI.setButton('btn-cancel-copy', false);
    }
  },

  _armCopyTimeout() {
    this._clearCopyTimeout();
    this._copyTimeout = setTimeout(() => {
      if (this.copying) {
        this.addLog(t('ccCopyTimeout'), 'warn');
        this._resetCopyState();
      }
    }, this._COPY_MAX_MS);
  },

  _clearCopyTimeout() {
    if (this._copyTimeout) { clearTimeout(this._copyTimeout); this._copyTimeout = null; }
  },

  _resetCopyState() {
    this.copying = false;
    this._clearCopyTimeout();
    UI.setButton('btn-cancel-copy', false);
    // Don't process pending queue on timeout — it might be stale
  },

  cancelCopy() {
    window.api.sendCommand('cancel_copy').catch(() => {});
    this.addLog(t('ccCancelling'), 'warn');
  },

  // ── Queue ──

  renderQueue() {
    const section = document.getElementById('queue-section');
    const list = document.getElementById('queue-list');
    const count = document.getElementById('queue-count');

    if (this.pendingCards.length === 0) {
      section.classList.add('hidden');
      return;
    }

    section.classList.remove('hidden');
    count.textContent = this.pendingCards.length;
    list.innerHTML = this.pendingCards.map(c =>
      `<div style="padding:4px 0;font-size:12px;color:var(--text-secondary)">
        <i class="ph ph-clipboard-text"></i> ${this._formatDriveDisplay(c.letter)} ${c.label ? '(' + c.label + ')' : ''} — ${c.srcPath.replace(/\\/g, '/').split('/').pop()}
      </div>`
    ).join('');
  },

  clearQueue() {
    if (this.pendingCards.length === 0) return;
    const count = this.pendingCards.length;
    this.pendingCards = [];
    this.renderQueue();
    this.addLog(t('ccQueueCleared', { count }), 'warn');
  },

  processNextPending() {
    if (this.copying || this.pendingCards.length === 0) return;
    const next = this.pendingCards.shift();
    this.renderQueue();
    this.addLog(t('ccQueueDequeue', { letter: this._formatDriveDisplay(next.letter) }), 'info');
    this.startCopy(next.letter, next.srcPath, next.folderName, next.label);
  },

  // ── Event Handlers ──

  handleEvent(eventName, data) {
    switch (eventName) {
      case 'drive_detected':
        this.addLog(t('ccDriveDetected', { drive: this._formatDriveDisplay(data.letter), path: data.src_path }), 'info');
        this.scanDrives();
        this.confirmCopy(data.letter, data.src_path, data.label || '');
        break;

      case 'drives_changed':
        this.scanDrives();
        // Remove disconnected drives from queue
        if (data.current_drives) {
          const current = new Set(data.current_drives);
          const before = this.pendingCards.length;
          this.pendingCards = this.pendingCards.filter(c => current.has(c.letter));
          if (this.pendingCards.length < before) {
            this.renderQueue();
          }
        }
        break;

      case 'copy_progress': {
        const pct = data.total > 0 ? (data.done / data.total * 100) : 0;
        UI.updateProgress('copy-progress-fill', 'copy-progress-text',
          Math.min(pct, 95), t('ccCopying'));

        let stats = t('ccCopyStats', { done: data.done, total: data.total });
        if (data.skipped > 0) stats += t('ccCopyStatsSkipped', { count: data.skipped });
        if (data.speed > 0) {
          stats += t('ccCopyStatsSpeed', { speed: UI.formatSpeed(data.speed) });
          const remainingBytes = data.total_bytes - data.copied_bytes;
          const eta = data.speed > 0 ? remainingBytes / data.speed : -1;
          stats += t('ccCopyStatsEta', { eta: UI.formatETA(eta) });
        }
        document.getElementById('copy-progress-stats').textContent = stats;
        break;
      }

      case 'verify_progress':
        UI.updateProgress('copy-progress-fill', 'copy-progress-text',
          95 + (data.done / data.total * 5),
          data.final ? t('ccVerifyDone') : t('ccVerifying', { done: data.done, total: data.total }));
        document.getElementById('copy-progress-stats').textContent =
          t('ccVerifyStats', { done: data.done, total: data.total, count: data.mismatches || 0 });
        break;

      case 'copy_done': {
        this.copying = false;
        this._clearCopyTimeout();
        UI.updateProgress('copy-progress-fill', 'copy-progress-text', 100, t('ccDoneExclaim'));
        UI.setButton('btn-cancel-copy', false);

        const summary = [
          t('ccCopyDoneSummary', { copied: data.copied, skipped: data.skipped, dest: data.dest }),
          data.mismatches > 0
            ? t('ccMd5Warn', { verified: data.verified, mismatches: data.mismatches })
            : t('ccMd5Ok', { verified: data.verified }),
        ].join('\n');
        this.addLog(summary, data.mismatches > 0 ? 'warn' : 'success');

        document.getElementById('copy-progress-stats').textContent = [
          t('ccCopyStatsDone', { copied: data.copied, skipped: data.skipped }),
          data.mismatches > 0
            ? t('ccVerifyStatsDone', { verified: data.verified, mismatches: data.mismatches })
            : t('ccVerifyAllOk', { verified: data.verified }),
        ].join('  |  ');

        // Save dest folder for workflow
        localStorage.setItem('last_copy_dest', data.dest);
        App.saveStat('photos', data.copied);
        App.saveStat('verified', data.verified);
        App.addHistory(t('ccCopyDoneTitle'), t('ccCopyDoneHistory', { count: data.copied, dest: data.dest }));
        App.notify(t('ccCopyDoneTitle'), t('ccCopyDoneNotify', { copied: data.copied, verified: data.verified }));

        // Prompt AI scoring
        if (App.token && App.currentPage !== 'photo-select') {
          App.toast(t('ccCopyDoneToast'), 'info');
        }
        // Notify automation engine
        if (typeof Automation !== 'undefined') {
          Automation.onStepComplete('copy', { dest: data.dest, copied: data.copied, files: data.files });
        }
        setTimeout(() => this.processNextPending(), 500);
        break;
      }

      case 'copy_cancelled':
        this.copying = false;
        this._clearCopyTimeout();
        UI.updateProgress('copy-progress-fill', 'copy-progress-text', 0, t('ccCancelled'));
        UI.setButton('btn-cancel-copy', false);
        this.addLog(t('ccCopyCancelledMsg', { copied: data.copied, dest: data.dest }), 'warn');
        setTimeout(() => this.processNextPending(), 500);
        break;

      case 'copy_error':
        this.copying = false;
        this._clearCopyTimeout();
        UI.setButton('btn-cancel-copy', false);
        this.addLog(t('ccCopyFailed', { drive: this._formatDriveDisplay(data.letter), msg: data.error }), 'error');
        setTimeout(() => this.processNextPending(), 500);
        break;
    }
  },

  // ── Utilities ──

  addLog(message, level = 'info') {
    UI.addLog('copy-log', message, level);
  },

  resolveFolderName() {
    const name = document.getElementById('copy-folder-name').value;
    const today = new Date().toISOString().slice(0, 10);
    return name.replace('{date}', today);
  },

  _escapeHtml(text) {
    return UI._escapeHtml(text);
  },

  _escapeAttr(text) {
    // Kept for backward compatibility; prefer _escapeHtml for HTML contexts
    return (text || '').replace(/'/g, "\\'").replace(/"/g, '\\"').replace(/\\/g, '\\\\');
  },

  // 格式化驱动器标识符用于显示
  // Windows 盘符是单字母 'E' → 显示 'E:'
  // Mac/Linux 挂载路径 '/Volumes/CARD' → 保持原样
  _formatDriveDisplay(key) {
    return (key && key.length === 1) ? key + ':' : key;
  },

  // 格式化路径用于显示（统一用 / 分隔）
  _formatDisplayPath(...parts) {
    return parts.join('/');
  },

  // ── FTP Server ──
  _ftpFiles: [],

  async ftpStart() {
    const port = parseInt(document.getElementById('ftp-port').value) || 2121;
    const folder = document.getElementById('ftp-receive-folder').value
      || document.getElementById('copy-target-path').value
      || '';
    if (!folder) { App.toast(t('ccFtpSetFolderFirst'), 'warning'); return; }
    try {
      const r = await window.api.sendCommand('ftp_start', { port, folder });
      if (r.error) { App.toast(r.error, 'error'); return; }
      // 新一轮接收：清空上一轮残留的文件列表与计数
      this._ftpFiles = [];
      this._renderFtpFileList();
      // Server binds the LAN interface — show the dialable LAN IP, not the wildcard.
      const addr = `${r.lan_ip || r.ip}:${r.port}`;
      document.getElementById('ftp-address').value = addr;
      document.getElementById('ftp-server-info').style.display = '';
      this._updateFtpUI(true);
      this.addFtpLog(t('ccFtpStarted', { addr }));
      // 凭据日志由后端 ftp_credentials 事件统一记录（app.js 路由），此处不重复
      this.addFtpLog(t('ccFtpRecvFolder', { folder }));
    } catch (e) { App.toast(t('ccStartFail', { msg: e.message }), 'error'); }
  },

  async ftpStop() {
    try {
      await window.api.sendCommand('ftp_stop', {});
      this._updateFtpUI(false);
      this.addFtpLog(t('ccFtpStopped'));
    } catch (e) { App.toast(t('ccStopFail', { msg: e.message }), 'error'); }
  },

  async checkFtpStatus() {
    try {
      const r = await window.api.sendCommand('ftp_status', {});
      if (r.running) {
        document.getElementById('ftp-address').value = `${r.ip}:${r.port}`;
        document.getElementById('ftp-server-info').style.display = '';
        document.getElementById('ftp-port').value = r.port;
      }
      this._updateFtpUI(r.running);
    } catch (e) { /* ignore */ }
  },

  _updateFtpUI(running) {
    const dot = document.getElementById('ftp-status-dot');
    const text = document.getElementById('ftp-status-text');
    const startBtn = document.getElementById('btn-ftp-start');
    const stopBtn = document.getElementById('btn-ftp-stop');

    if (running) {
      dot.className = 'status-dot online';
      text.textContent = t('running');
      startBtn.classList.add('hidden');
      stopBtn.classList.remove('hidden');
    } else {
      dot.className = 'status-dot offline';
      text.textContent = t('ccWatchNotStarted');
      startBtn.classList.remove('hidden');
      stopBtn.classList.add('hidden');
    }
  },

  // Called by App when ftp_file_received event arrives
  onFtpFileReceived(data) {
    this._ftpFiles.push(data);
    this.addFtpLog(t('ccFtpFileReceived', { name: data.name, size: UI.formatSize(data.size) }));
    this._renderFtpFileList();
    // Update dashboard stats
    const stats = JSON.parse(localStorage.getItem('dash_stats') || '{}');
    stats.copiedFiles = (stats.copiedFiles || 0) + 1;
    localStorage.setItem('dash_stats', JSON.stringify(stats));
    // Notify automation
    if (window.Automation) {
      Automation.onFtpStepComplete(data);
    }
  },

  onFtpLog(data) {
    this.addFtpLog(data.message);
  },

  _renderFtpFileList() {
    const el = document.getElementById('ftp-file-list');
    document.getElementById('ftp-file-count').textContent = this._ftpFiles.length;
    if (!this._ftpFiles.length) {
      el.innerHTML = '<div class="empty-state"><span class="empty-icon">📡</span><span class="empty-text">' + t('ccFtpWait') + '</span></div>';
      return;
    }
    // Show last 20 files
    const files = this._ftpFiles.slice(-20).reverse();
    el.innerHTML = files.map(f => `
      <div class="drive-card">
        <div class="drive-icon"><i class="ph ph-image"></i></div>
        <div class="drive-info">
          <div class="drive-label" title="${f.path}">${f.name}</div>
          <div class="drive-meta">${UI.formatSize(f.size)}</div>
        </div>
      </div>
    `).join('');
  },

  addFtpLog(message, level = 'info') {
    UI.addLog('ftp-log', `[FTP] ${message}`, level);
  },

  handleFtpEvent(eventName, data) {
    switch (eventName) {
      case 'ftp_file_received': this.onFtpFileReceived(data); break;
      case 'ftp_server_started':
        document.getElementById('ftp-address').value = `${data.ip}:${data.port}`;
        document.getElementById('ftp-server-info').style.display = '';
        this._updateFtpUI(true);
        this.addFtpLog(t('ccFtpServerStarted', { addr: `${data.ip}:${data.port}` }));
        break;
      case 'ftp_server_stopped':
        this._updateFtpUI(false);
        this.addFtpLog(t('ccFtpServerStopped'));
        break;
    }
  },

  _ftpUIReady: true,

  // ── Folder Watch ──
  watching: false,
  _watchFiles: [],      // {path,name,size,score,selected} 渲染网格
  _watchPending: [],    // manual 模式待评分文件路径
  _watchStats: { received: 0, scored: 0, selected: 0 },

  loadWatchConfig() {
    const p = App.loadPrefs();
    const c = p.watchConfig || {};
    if (c.folder) document.getElementById('watch-folder').value = c.folder;
    if (c.outputFolder) document.getElementById('watch-output-folder').value = c.outputFolder;
    if (c.filter) document.getElementById('watch-filter').value = c.filter;
    if (c.mode) document.getElementById('watch-mode').value = c.mode;
    if (c.quiet) document.getElementById('watch-quiet').value = c.quiet;
    document.getElementById('watch-threshold').value = c.threshold != null ? c.threshold : (p.scoreThreshold != null ? p.scoreThreshold : 6.0);
    document.getElementById('watch-auto-upload').checked = !!c.autoUpload;
    document.getElementById('watch-process-existing').checked = !!c.processExisting;
    this._updateWatchModeUI();
  },

  saveWatchConfig() {
    const c = {
      folder: document.getElementById('watch-folder').value,
      outputFolder: document.getElementById('watch-output-folder').value,
      filter: document.getElementById('watch-filter').value,
      mode: document.getElementById('watch-mode').value,
      quiet: parseInt(document.getElementById('watch-quiet').value) || 5,
      threshold: parseFloat(document.getElementById('watch-threshold').value) || 6.0,
      autoUpload: document.getElementById('watch-auto-upload').checked,
      processExisting: document.getElementById('watch-process-existing').checked,
    };
    App.savePrefs({ watchConfig: c });
    this._updateWatchModeUI();
  },

  _updateWatchModeUI() {
    const mode = document.getElementById('watch-mode').value;
    document.getElementById('btn-watch-score').classList.toggle('hidden', mode !== 'manual');
    document.getElementById('watch-quiet').parentElement.style.opacity = mode === 'batch' ? '1' : '0.5';
  },

  _watchParams() {
    return {
      folder: document.getElementById('watch-folder').value,
      mode: document.getElementById('watch-mode').value,
      filter: document.getElementById('watch-filter').value,
      interval: 2.0,
      stability_ms: 1500,
      quiet_ms: (parseInt(document.getElementById('watch-quiet').value) || 5) * 1000,
      output_folder: document.getElementById('watch-output-folder').value,
      threshold: parseFloat(document.getElementById('watch-threshold').value) || 6.0,
      autoUpload: document.getElementById('watch-auto-upload').checked,
      process_existing: document.getElementById('watch-process-existing').checked,
      recursive: true,
    };
  },

  async startWatch() {
    const params = this._watchParams();
    if (!params.folder) { App.toast(t('ccWatchSelectFolder'), 'warning'); return; }
    if (!params.output_folder) { App.toast(t('ccWatchSelectOutput'), 'warning'); return; }
    try {
      await window.api.sendCommand('start_folder_watch', params);
      // 新一轮监听：重置文件列表/待评分队列/统计，避免在旧值上累加、新旧混显
      this._watchFiles = [];
      this._watchPending = [];
      this._watchStats = { received: 0, scored: 0, selected: 0 };
      this._renderWatchList();
      this.watching = true;
      this._updateWatchUI(true);
      this.addWatchLog(t('ccWatchStarted', { folder: params.folder, mode: params.mode }));
    } catch (e) { App.toast(t('ccWatchStartFail', { msg: e.message }), 'error'); }
  },

  async stopWatch() {
    try {
      await window.api.sendCommand('stop_folder_watch', {});
      this.watching = false;
      this._updateWatchUI(false);
      this.addWatchLog(t('ccWatchStopped'));
    } catch (e) { App.toast(t('ccStopFail', { msg: e.message }), 'error'); }
  },

  async checkWatchStatus() {
    try {
      const r = await window.api.sendCommand('folder_watch_status', {});
      this._updateWatchUI(r.running);
    } catch (e) { /* ignore */ }
  },

  _updateWatchUI(running) {
    const dot = document.getElementById('watch-status-dot');
    const text = document.getElementById('watch-status-text');
    const startBtn = document.getElementById('btn-watch-start');
    const stopBtn = document.getElementById('btn-watch-stop');
    if (running) {
      dot.className = 'status-dot online';
      text.textContent = t('ccWatching');
      startBtn.classList.add('hidden');
      stopBtn.classList.remove('hidden');
    } else {
      dot.className = 'status-dot offline';
      text.textContent = t('ccWatchNotStarted');
      startBtn.classList.remove('hidden');
      stopBtn.classList.add('hidden');
    }
  },

  // Called by App for watch_* events
  handleWatchEvent(eventName, data) {
    switch (eventName) {
      case 'watch_started':
        this.watching = true;
        this._updateWatchUI(true);
        this.addWatchLog(t('ccWatchStartedEvent', { folder: data.folder, mode: data.mode }));
        break;
      case 'watch_stopped':
        this.watching = false;
        this._updateWatchUI(false);
        this.addWatchLog(t('ccWatchStopped'));
        break;
      case 'watch_file_received': this.onWatchFileReceived(data); break;
      case 'watch_batch_ready': this.onWatchBatchReady(data); break;
      case 'watch_score_progress': this.onWatchScoreProgress(data); break;
      case 'watch_score_done': this.onWatchScoreDone(data); break;
    }
  },

  onWatchFileReceived(data) {
    const mode = document.getElementById('watch-mode').value;
    this._watchStats.received += 1;
    this._watchFiles.push({ path: data.path, name: data.name, size: data.size, score: null, selected: false });
    this._renderWatchList();
    this.addWatchLog(t('ccWatchNewPhoto', { name: data.name, size: UI.formatSize(data.size) }));

    if (mode === 'realtime') {
      // 立即单独评分
      const params = this._watchParams();
      window.api.sendCommand('score_files', {
        files: [data.path], threshold: params.threshold,
        output_folder: params.output_folder, batch_id: data.batch_id,
      }).catch(e => this.addWatchLog(t('ccScoreFail', { msg: e.message }), 'error'));
    } else if (mode === 'manual') {
      this._watchPending.push(data.path);
    }
    // batch 模式：等 watch_batch_ready
  },

  onWatchBatchReady(data) {
    const params = this._watchParams();
    this._watchStats.received += data.files.length;
    this.addWatchLog(t('ccBatchScoring', { count: data.files.length }));
    const files = data.files.map(f => f.path);
    files.forEach(f => {
      if (!this._watchFiles.find(x => x.path === f)) {
        const fi = data.files.find(x => x.path === f);
        this._watchFiles.push({ path: f, name: fi.name, size: fi.size, score: null, selected: false });
      }
    });
    this._renderWatchList();
    window.api.sendCommand('score_files', {
      files, threshold: params.threshold,
      output_folder: params.output_folder, batch_id: data.batch_id,
    }).catch(e => this.addWatchLog(t('ccBatchScoreFail', { msg: e.message }), 'error'));
  },

  onWatchScoreProgress(data) {
    const pct = data.total > 0 ? (data.current / data.total * 100) : 0;
    UI.updateProgress('watch-progress-fill', 'watch-progress-text', pct, t('ccScoring', { current: data.current, total: data.total }));
    let stats = t('ccScoreStats', { current: data.current, total: data.total });
    if (data.last_score !== undefined && data.last_score >= 0) stats += t('ccScoreStatsLast', { score: data.last_score.toFixed(1) });
    if (data.selected > 0) stats += t('ccScoreStatsSelected', { count: data.selected });
    document.getElementById('watch-progress-stats').textContent = stats;

    // 更新网格中对应文件的分数（按完整路径匹配，避免不同子目录同名文件错配）
    if (data.last_file && data.last_score >= 0) {
      const lastPath = data.image && data.image.path;
      const item = lastPath
        ? this._watchFiles.find(x => x.path === lastPath)
        : this._watchFiles.slice().reverse().find(x => x.name === data.last_file && x.score === null);
      if (item) {
        item.score = data.last_score;
        item.selected = data.last_score >= (parseFloat(document.getElementById('watch-threshold').value) || 6);
        this._renderWatchList();
      }
      this.addWatchLog(
        item && item.selected ? t('ccScoreLogSelected', { file: data.last_file, score: data.last_score.toFixed(1) }) : t('ccScoreLogNormal', { file: data.last_file, score: data.last_score.toFixed(1) }),
        item && item.selected ? 'success' : 'info'
      );
    }
  },

  onWatchScoreDone(data) {
    UI.updateProgress('watch-progress-fill', 'watch-progress-text', 100, t('ccScoreDone'));
    this._watchStats.scored += data.total;
    this._watchStats.selected += data.selected;
    this._renderWatchList();
    this.addWatchLog(t('ccBatchDone', { selected: data.selected, total: data.total, avg: data.avg_score || '--' }), 'success');
    App.saveStat('scored', (parseInt(JSON.parse(localStorage.getItem('dash_stats') || '{}').scored) || 0) + data.total);
    App.saveStat('excellent', (parseInt(JSON.parse(localStorage.getItem('dash_stats') || '{}').excellent) || 0) + data.selected);

    // 自动上传精选照片
    const autoUpload = document.getElementById('watch-auto-upload').checked;
    if (autoUpload && App.token && typeof CloudUpload !== 'undefined' && data.output_folder) {
      window.api.sendCommand('list_images_in_folder', { folder: data.output_folder }).then(r => {
        if (r && r.images && r.images.length) {
          const scores = {}; r.images.forEach(f => {
            const p = typeof f === 'string' ? f : f.path;
            scores[p] = 0;
          });
          CloudUpload.addToQueue(r.images, scores);
          App.toast(t('ccUploadQueued', { count: r.images.length }), 'success');
        }
      }).catch(() => {});
    }
  },

  startManualScore() {
    if (!this._watchPending.length) { App.toast(t('ccNoPendingPhotos'), 'warning'); return; }
    const params = this._watchParams();
    const files = this._watchPending.slice();
    const batchId = 'manual_' + Date.now();
    this.addWatchLog(t('ccManualScoring', { count: files.length }));
    window.api.sendCommand('score_files', {
      files, threshold: params.threshold,
      output_folder: params.output_folder, batch_id: batchId,
    }).catch(e => this.addWatchLog(t('ccScoreFail', { msg: e.message }), 'error'))
      // 成功/失败（含长命令超时）都清掉本次已提交的文件，避免下次重复评分旧文件；
      // 评分期间新到的文件不在快照内，保留待下轮
      .finally(() => { this._watchPending = this._watchPending.filter(p => !files.includes(p)); });
  },

  _renderWatchList() {
    const el = document.getElementById('watch-file-list');
    document.getElementById('watch-file-count').textContent = this._watchFiles.length;
    document.getElementById('watch-stats').textContent =
      t('ccWatchStatsText', { received: this._watchStats.received, scored: this._watchStats.scored, selected: this._watchStats.selected });
    if (!this._watchFiles.length) {
      el.innerHTML = '<div class="empty-state"><span class="empty-icon">📷</span><span class="empty-text">' + t('ccWatchWait') + '</span><span class="empty-hint">' + t('ccWatchHint') + '</span></div>';
      return;
    }
    const files = this._watchFiles.slice(-60).reverse();
    el.innerHTML = files.map(f => {
      const scoreTxt = f.score === null ? '<span class="drive-meta">' + t('ccPendingScore') + '</span>'
        : `<span class="drive-meta">${t('ccScorePoints', { score: f.score.toFixed(1) })} ${f.selected ? t('ccPickedMark') : ''}</span>`;
      const color = f.selected ? 'var(--brand)' : (f.score === null ? 'var(--text3)' : 'var(--text2)');
      return `<div class="drive-card">
        <div class="drive-icon"><i class="ph ph-image"></i></div>
        <div class="drive-info">
          <div class="drive-label" title="${f.path}">${f.name}</div>
          <div class="drive-meta" style="color:${color}">${UI.formatSize(f.size)}</div>
          ${scoreTxt}
        </div>
      </div>`;
    }).join('');
  },

  addWatchLog(message, level = 'info') {
    UI.addLog('watch-log', t('ccWatchTag') + ' ' + message, level);
  },
};

// Initialize when DOM is ready
document.addEventListener('DOMContentLoaded', () => CopyCard.init());
