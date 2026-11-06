/**
 * uploader.js — Cloud Upload page logic
 */

const CloudUpload = {
  uploading: false,
  uploadQueue: [],       // [{filePath, score, category}]
  _lastPct: 0,           // monotonic guard — the bar never moves backwards
  _cancelRequested: false,
  _uploadTask: '',

  init() {
    this.bindEvents();
    this._loadOfflineQueue();
    // Auto-connect from App state
    if (App.token) {
      const st = document.getElementById('cloud-status');
      if (st) { st.innerHTML = `<i class="ph ph-check-circle"></i> ${t('cloudConnected')}`; st.style.color = 'var(--success)'; }
      document.getElementById('btn-upload-start').disabled = false;
    }
  },

  bindEvents() {
    document.getElementById('btn-upload-start').addEventListener('click', () => this.startUpload());
    document.getElementById('btn-upload-cancel').addEventListener('click', () => this.cancelUpload());
    document.getElementById('btn-open-cloud').addEventListener('click', () => {
      window.api.openUrl(`${App.serverUrl}/index.html#auth=${encodeURIComponent(App.token)}`);
    });

    // Add files directly
    document.getElementById('btn-upload-add-files').addEventListener('click', async () => {
      try {
        const files = await window.api.selectFiles();
        if (files && files.length > 0) {
          const scores = {};
          files.forEach(f => { scores[f] = 0; });
          this.addToQueue(files, scores);
        }
      } catch (e) { this.addLog(t('upAddFilesFail', { msg: e.message }), 'error'); }
    });

    // File drag-and-drop from filesystem onto upload queue
    const queueList = document.getElementById('upload-queue-list');
    if (queueList) {
      queueList.addEventListener('dragover', (e) => { e.preventDefault(); e.stopPropagation(); queueList.classList.add('drag-over'); });
      queueList.addEventListener('dragleave', () => queueList.classList.remove('drag-over'));
      queueList.addEventListener('drop', async (e) => {
        e.preventDefault(); e.stopPropagation();
        queueList.classList.remove('drag-over');
        const files = [];
        if (e.dataTransfer.items) {
          for (const item of e.dataTransfer.items) {
            if (item.kind === 'file') {
              const entry = item.webkitGetAsEntry ? item.webkitGetAsEntry() : null;
              if (entry && entry.isDirectory) {
                // Read directory recursively
                const dirFiles = await this._readDirEntry(entry);
                files.push(...dirFiles);
              } else {
                const f = item.getAsFile();
                if (f) files.push(f.path);
              }
            }
          }
        }
        if (files.length) {
          const scores = {};
          files.forEach(f => { scores[f] = 0; });
          this.addToQueue(files, scores);
        }
      });
    }

    // Add folder
    document.getElementById('btn-upload-add-folder').addEventListener('click', async () => {
      const folder = await window.api.selectFolder();
      if (!folder) return;
      try {
        const scanResult = await window.api.sendCommand('list_images_in_folder', { folder });
        if (scanResult && scanResult.images) {
          const scores = {};
          scanResult.images.forEach(f => {
            const p = typeof f === 'string' ? f : f.path;
            scores[p] = 0;
          });
          this.addToQueue(scanResult.images, scores);
        }
      } catch (e) {
        this.addLog(t('upScanFolderFail', { msg: e.message }), 'error');
      }
    });
  },

  addToQueue(filePaths, scores) {
    for (const fp of filePaths) {
      // Normalize: fp can be a string (old format) or {path, size, mtime} (new format)
      const pathStr = typeof fp === 'string' ? fp : fp.path;
      if (!pathStr) continue;
      // Avoid duplicates
      if (this.uploadQueue.find(q => q.filePath === pathStr)) continue;
      this.uploadQueue.push({
        filePath: pathStr,
        score: (scores[pathStr] || scores[fp] || 0),
        category: 'pending',
      });
    }
    this.renderQueue();
    this.addLog(t('cloudAdded').replace('N', filePaths.length), 'info');
  },

  renderQueue() {
    const count = document.getElementById('upload-queue-count');
    const list = document.getElementById('upload-queue-list');
    const empty = document.getElementById('upload-empty');

    count.textContent = this.uploadQueue.length;
    if (this.uploadQueue.length === 0) {
      empty.style.display = 'block';
      list.innerHTML = '';
      document.getElementById('btn-upload-start').disabled = !App.token;
      return;
    }

    empty.style.display = 'none';
    document.getElementById('btn-upload-start').disabled = !App.token;

    list.innerHTML = this.uploadQueue.map((q, i) => {
      const name = q.filePath.split('\\').pop().split('/').pop();
      const ext = name.split('.').pop().toLowerCase();
      const isRaw = ['arw','cr2','cr3','nef','dng','orf','rw2'].includes(ext);
      const icon = isRaw ? '<i class="ph ph-film-strip"></i>' : '<i class="ph ph-camera"></i>';
      const sizeStr = this._formatFileSize(q.filePath);
      return `
      <div class="upload-queue-item" draggable="true" data-index="${i}">
        <span class="drag-handle">⋮⋮</span>
        <span style="font-size:18px">${icon}</span>
        <span style="flex:1;font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${q.filePath}">${name}</span>
        <span style="font-size:11px;color:var(--text3)">${sizeStr}</span>
        ${q.score > 0 ? `<span style="font-size:11px;color:var(--green)">${t('upScorePoints', { score: q.score.toFixed(1) })}</span>` : ''}
        <button class="btn btn-ghost btn-sm" onclick="CloudUpload.removeFromQueue(${i})" title="${t('upRemove')}"><i class="ph ph-x"></i></button>
      </div>`;
    }).join('');

    // Drag & drop
    list.querySelectorAll('.upload-queue-item').forEach(item => {
      item.addEventListener('dragstart', (e) => {
        e.dataTransfer.setData('text/plain', item.dataset.index);
        item.classList.add('dragging');
      });
      item.addEventListener('dragend', () => item.classList.remove('dragging'));
      item.addEventListener('dragover', (e) => { e.preventDefault(); item.classList.add('drag-over'); });
      item.addEventListener('dragleave', () => item.classList.remove('drag-over'));
      item.addEventListener('drop', (e) => {
        e.preventDefault(); item.classList.remove('drag-over');
        const from = parseInt(e.dataTransfer.getData('text/plain'));
        const to = parseInt(item.dataset.index);
        if (from !== to) {
          const [moved] = this.uploadQueue.splice(from, 1);
          this.uploadQueue.splice(to, 0, moved);
          this.renderQueue();
        }
      });
    });
  },

  removeFromQueue(index) {
    this.uploadQueue.splice(index, 1);
    this.renderQueue();
  },

  _formatFileSize(path) {
    // File size not available in renderer without IPC round-trip.
    // Show file extension as a lightweight type hint instead.
    const ext = (path || '').split('.').pop().toUpperCase();
    return ext || '?';
  },

  async startUpload() {
    if (this.uploading) { this.addLog(t('cloudUploading'), 'warn'); return; }
    if (this.uploadQueue.length === 0) {
      this.addLog(t('cloudQueueEmpty'), 'warn');
      return;
    }
    if (!App.token) {
      this.addLog(t('cloudNeedLogin'), 'warn');
      return;
    }

    this.uploading = true;
    this._cancelRequested = false;
    this._lastPct = 0;
    document.getElementById('btn-upload-start').disabled = true;
    document.getElementById('btn-upload-cancel').disabled = false;
    UI.clearLog('upload-log');
    UI.updateProgress('upload-progress-fill', 'upload-progress-text', 0, t('cloudPreparing'));

    const filePaths = this.uploadQueue.map(q => q.filePath);
    if (typeof Activity !== 'undefined') {
      this._uploadTask = Activity.start({
        title: t('actUpload'),
        detail: t('actPhotosN', { count: filePaths.length }),
        total: filePaths.length,
        cancelable: true,
        onCancel: () => this._doCancelUpload(),
      });
    }
    const scores = {};
    this.uploadQueue.forEach(q => { scores[q.filePath] = q.score; });
    const batchKey = Date.now().toString(36) + Math.random().toString(36).substring(2, 10);
    // Per-file idempotency key: allows independent retry of failed files within a batch
    const idempotencyKeys = {};
    filePaths.forEach((fp, i) => { idempotencyKeys[fp] = batchKey + '_' + i; });

    // Watchdog: if upload_done doesn't fire within 30 min, reset UI
    const watchdog = setTimeout(() => {
      if (this.uploading) {
        this.addLog(t('upTimeout'), 'error');
        if (this._uploadTask && typeof Activity !== 'undefined') {
          Activity.fail(this._uploadTask, t('upTimeout'));
          this._uploadTask = '';
        }
        this._resetUploadUI();
      }
    }, 1800000);

    try {
      await window.api.sendCommand('upload_photos', {
        server_url: App.serverUrl,
        token: App.token,
        file_paths: filePaths,
        scores: scores,
        idempotency_key: batchKey,
        idempotency_keys: idempotencyKeys,
      });
    } catch (e) {
      this._resetUploadUI();
      this.addLog(t('upError', { msg: e.message }), 'error');
      if (this._uploadTask && typeof Activity !== 'undefined') {
        Activity.fail(this._uploadTask, e.message || '');
        this._uploadTask = '';
      }
    } finally {
      clearTimeout(watchdog);
    }
  },

  _resetUploadUI() {
    this.uploading = false;
    document.getElementById('btn-upload-start').disabled = !App.token;
    document.getElementById('btn-upload-cancel').disabled = true;
  },

  cancelUpload() {
    if (this._uploadTask && typeof Activity !== 'undefined') {
      Activity.cancel(this._uploadTask);
      return;
    }
    this._doCancelUpload();
  },

  _doCancelUpload() {
    this._cancelRequested = true;
    window.api.sendCommand('cancel_upload').catch(() => {});
    this.uploading = false;
    this._uploadTask = '';
    document.getElementById('btn-upload-start').disabled = !App.token;
    document.getElementById('btn-upload-cancel').disabled = true;
    this.addLog(t('upCancelled'), 'warn');
  },

  async _showCreateAlbumPrompt() {
    const title = await Modal.showInput(t('upCreateAlbumTitle'), t('upAlbumNamePrompt'));
    if (!title) return;
    const password = await Modal.showInput(t('upSetPasswordTitle'), t('upPasswordOptional')) || '';
    // P3-8: 精修张数上限（影楼套餐张数；空 = 不限），客户选片页会实时计数并在服务端硬校验
    const maxPicksRaw = await Modal.showInput(t('upMaxPicksTitle'), t('upMaxPicksPrompt')) || '';
    const maxPicks = Math.max(0, parseInt(maxPicksRaw, 10) || 0);
    try {
      const album = await App.createAlbum(title, password, [], maxPicks);
      this.addLog(t('upAlbumCreated', { title }), 'success');
      this.addLog(t('upShareLink', { url: album.shareUrl }), 'info');
      Modal.showMessage(t('upAlbumCreatedMsg', { url: album.shareUrl, pw: password ? t('upPasswordLine', { pw: password }) : '' }), 'success');
    } catch (e) { this.addLog(t('upCreateAlbumFail', { msg: e.message }), 'error'); }
  },

  handleEvent(eventName, data) {
    switch (eventName) {
      case 'upload_progress':
        let pct = data.total > 0 ? (data.current / data.total * 100) : 0;
        // Add chunk-level granularity for large files (chunk events carry
        // batch-level current/total plus the in-file chunk position)
        if (data.chunk_part && data.chunk_total) {
          const chunkPct = data.chunk_part / data.chunk_total;
          pct = ((data.current - 1) + chunkPct) / data.total * 100;
        }
        // Monotonic guard: stale/concurrent events must not move the bar backwards
        if (pct < this._lastPct) pct = this._lastPct;
        this._lastPct = pct;
        const statusMsg = data.chunk_total
          ? t('upUploadingChunks', { current: data.current, total: data.total, part: data.chunk_part, chunks: data.chunk_total })
          : t('upUploading', { current: data.current, total: data.total });
        UI.updateProgress('upload-progress-fill', 'upload-progress-text', pct, statusMsg);
        if (this._uploadTask && typeof Activity !== 'undefined') {
          Activity.update(this._uploadTask, {
            current: data.current || 0,
            total: data.total || 0,
            detail: statusMsg,
          });
        }
        document.getElementById('upload-progress-stats').textContent =
          `${t('cloudSuccess')} ${data.success} | ${t('cloudFail')} ${data.failed} | ${t('upCurrent')} ${data.current_file || '--'}`;
        if (data.current_file) {
          if (data.error) {
            this.addLog(`✗ ${data.current_file}: ${data.error}`, 'error');
          } else if (data.chunk_total && data.chunk_part < data.chunk_total) {
            // Chunk progress — only log first chunk of each file
            if (data.chunk_part === 1) {
              this.addLog(`⏳ ${data.current_file}`, 'info');
            }
          } else {
            // File fully uploaded (no chunks, or last chunk)
            this.addLog(`✓ ${data.current_file}`, 'success');
          }
        }
        break;

      case 'upload_done':
        // Distinguish a cancelled batch: backend flag, or a local cancel whose
        // tally (success + failed) never reached the batch total.
        const cancelled = !!data.cancelled ||
          (this._cancelRequested && (data.success + data.failed) < data.total);
        this._cancelRequested = false;
        this._resetUploadUI();
        if (this._uploadTask && typeof Activity !== 'undefined') {
          if (cancelled) {
            Activity.markCancelled(this._uploadTask);
          } else {
            Activity.finish(this._uploadTask, {
              detail: t('upDoneSynced', { count: data.success || 0 }),
            });
          }
          this._uploadTask = '';
        }
        if (cancelled) {
          // Cancelled: no 100% completion state, no album prompt
          const donePct = Math.max(this._lastPct,
            data.total > 0 ? ((data.success + data.failed) / data.total * 100) : 0);
          UI.updateProgress('upload-progress-fill', 'upload-progress-text', donePct, t('upCancelled'));
        } else {
          UI.updateProgress('upload-progress-fill', 'upload-progress-text', 100, t('cloudUploadDone'));
        }
        document.getElementById('upload-progress-stats').textContent =
          `${t('cloudSuccess')} ${data.success} / ${data.total} | ${t('cloudFail')} ${data.failed}`;

        // Only remove successfully uploaded files; keep failed ones for retry
        if (data.uploaded_paths && data.uploaded_paths.length) {
          const okSet = new Set(data.uploaded_paths);
          this.uploadQueue = this.uploadQueue.filter(q => !okSet.has(q.filePath));
        }
        // If ALL failed, keep queue intact
        this.renderQueue();

        if (cancelled) {
          this.addLog(data.success > 0
            ? `${t('upCancelled')} — ${t('upSyncedCount', { count: data.success })}`
            : t('upCancelled'), 'warn');
          if (data.success > 0) {
            App.saveStat('uploaded', (parseInt(document.getElementById('stat-uploaded')?.textContent) || 0) + data.success);
          }
        } else if (data.success > 0) {
          this.addLog(t('upDoneSynced', { count: data.success }), 'success');
          App.saveStat('uploaded', (parseInt(document.getElementById('stat-uploaded')?.textContent) || 0) + data.success);
          App.addHistory(t('upDoneTitle'), t('upPhotosToServer', { count: data.success, server: App.serverUrl }));
          App.notify(t('upDoneTitle'), t('upSyncedCount', { count: data.success }));
          // Only prompt once per session
          if (!this._albumPrompted) {
            this._albumPrompted = true;
            this._showCreateAlbumPrompt();
          }
        }
        // Notify automation engine
        if (typeof Automation !== 'undefined') {
          Automation.onStepComplete('upload', { success: data.success, total: data.total });
        }
        break;

      case 'log':
        this.addLog(data.message, data.level || 'info');
        break;
    }
  },

  async _readDirEntry(dirEntry) {
    const files = [];
    const reader = dirEntry.createReader();
    const readAll = () => new Promise((resolve) => {
      const all = [];
      const read = () => reader.readEntries((entries) => {
        if (!entries.length) { resolve(all); return; }
        all.push(...entries); read();
      });
      read();
    });
    const entries = await readAll();
    for (const entry of entries) {
      if (entry.isFile) {
        files.push(entry.fullPath);
      } else if (entry.isDirectory) {
        try {
          const sub = await this._readDirEntry(entry);
          files.push(...sub);
        } catch (_) {}
      }
    }
    return files;
  },

  addLog(message, level) {
    UI.addLog('upload-log', message, level);
    // Cap log entries to prevent unbounded DOM growth
    const logEl = document.getElementById('upload-log');
    if (logEl && logEl.children.length > 500) {
      while (logEl.children.length > 300) logEl.firstChild.remove();
    }
  },

  // ── Offline Queue ──
  _offlineQueue: [],
  _offlineLoaded: false,

  _loadOfflineQueue() {
    if (this._offlineLoaded) return;
    this._offlineLoaded = true;
    try {
      const raw = localStorage.getItem('offline_upload_queue');
      this._offlineQueue = raw ? JSON.parse(raw) : [];
    } catch (e) { this._offlineQueue = []; }
    if (this._offlineQueue.length) {
      App.toast(t('upOfflineWaiting', { count: this._offlineQueue.length }), 'warning');
      this._updateOfflineBadge();
    }
    // Listen for reconnect
    window.addEventListener('online', () => this._retryOffline());
  },

  _saveOfflineQueue() {
    try {
      localStorage.setItem('offline_upload_queue', JSON.stringify(this._offlineQueue));
    } catch (e) { /* storage full — silently fail */ }
    this._updateOfflineBadge();
  },

  _updateOfflineBadge() {
    const badge = document.getElementById('offline-queue-badge');
    if (badge) {
      badge.textContent = this._offlineQueue.length;
      badge.classList.toggle('hidden', !this._offlineQueue.length);
    }
  },

  enqueueOffline(filePath, score, category) {
    this._loadOfflineQueue();
    // Avoid duplicate
    if (!this._offlineQueue.find(q => q.filePath === filePath)) {
      this._offlineQueue.push({ filePath, score, category });
      this._saveOfflineQueue();
      this.addLog(t('upAddedOffline', { name: filePath.split('\\').pop() }), 'info');
    }
  },

  async _retryOffline() {
    if (!this._offlineQueue.length || !navigator.onLine || !App.token) return;
    this.addLog(t('upRetrying', { count: this._offlineQueue.length }), 'info');
    App.toast(t('upUploadingOffline', { count: this._offlineQueue.length }), 'info');

    const toRetry = [...this._offlineQueue];
    const okPaths = [];
    const failed = [];
    for (const item of toRetry) {
      try {
        const r = await window.api.sendCommand('upload_photos', {
          server_url: App.serverUrl,
          token: App.token,
          file_paths: [item.filePath],
          scores: { [item.filePath]: item.score || 0 },
        });
        // Backend reports per-file failures in the result instead of throwing
        // — only truly uploaded paths leave the offline queue.
        if (r && r.uploaded_paths && r.uploaded_paths.includes(item.filePath)) {
          okPaths.push(item.filePath);
          this._offlineQueue = this._offlineQueue.filter(q => q.filePath !== item.filePath);
        } else {
          failed.push(item);
          this.addLog(t('upError', { msg: item.filePath.split('\\').pop() }), 'error');
        }
      } catch (e) {
        const msg = (e.message || '').toLowerCase();
        if (!navigator.onLine || msg.includes('network') || msg.includes('timeout') || msg.includes('econnrefused')) {
          failed.push(item);
        }
      }
    }
    this._saveOfflineQueue();
    if (okPaths.length) {
      App.toast(t('upOfflineUploaded', { count: okPaths.length }), 'success');
    }
    if (this._offlineQueue.length) {
      App.toast(t('upOfflineRemaining', { count: this._offlineQueue.length }), 'warning');
    }
  },
};

document.addEventListener('DOMContentLoaded', () => CloudUpload.init());
