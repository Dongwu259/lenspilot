/**
 * file-browser.js — Local + Cloud file browser with album management
 */

const FileBrowser = {
  mode: 'local',
  folder: null,
  files: [],
  _allFiles: [], // unfiltered source array
  selected: new Set(),
  cloudPhotos: [],
  albums: [],
  currentAlbumId: null,
  currentAlbumPhotos: [],
  albumSelected: new Set(),
  _abortCtrl: null,
  _loading: false,

  init() { this.bindEvents(); },

  bindEvents() {
    // Tab switching
    document.querySelectorAll('[data-fb-tab]').forEach(btn => {
      btn.addEventListener('click', () => {
        this._abort(); // cancel in-flight requests
        document.querySelectorAll('[data-fb-tab]').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        this.mode = btn.dataset.fbTab;
        this.selected.clear();
        this.albumSelected.clear();
        this.currentAlbumId = null;
        PhotoLightbox.close();
        this._updateModeUI();
        this.checkCloudAccess();
        if (this.mode === 'cloud') this.loadCloudPhotos();
        else if (this.mode === 'albums') this.loadAlbums();
        else this.render();
      });
    });

    document.getElementById('btn-fb-browse').addEventListener('click', async () => {
      const f = await window.api.selectFolder();
      if (f) { this.folder = f; document.getElementById('fb-folder').value = f; document.getElementById('btn-fb-explorer').disabled = false; this.loadLocalFolder(); }
    });
    document.getElementById('btn-fb-explorer').addEventListener('click', () => {
      if (this.folder) window.api.openFolder(this.folder);
    });
    // Search
    let searchTimer;
    document.getElementById('fb-search').addEventListener('input', () => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => { this._applyFilters(); this.render(); }, 250);
    });
    // Sort
    document.getElementById('fb-sort').addEventListener('change', () => { this._applySort(); this.render(); });
    // Select all / clear (visible buttons; Ctrl+A & Esc still work)
    document.getElementById('btn-fb-select-all').addEventListener('click', () => this.selectAllVisible());
    document.getElementById('btn-fb-select-clear').addEventListener('click', () => this.clearSelection());
    document.getElementById('btn-fb-export').addEventListener('click', () => this.exportSelected());

    // Cloud action buttons
    document.getElementById('btn-fb-cat-excellent').addEventListener('click', () => this.batchCategorizeCloud('excellent'));
    document.getElementById('btn-fb-cat-pending').addEventListener('click', () => this.batchCategorizeCloud('pending'));
    document.getElementById('btn-fb-cat-reject').addEventListener('click', () => this.batchCategorizeCloud('reject'));
    document.getElementById('btn-fb-cloud-delete').addEventListener('click', () => {
      if (this.mode === 'local') this.deleteSelected(); else this.batchDeleteCloud();
    });
    document.getElementById('btn-fb-add-to-album').addEventListener('click', () => this.batchAddToAlbum());
    document.getElementById('btn-fb-back-albums').addEventListener('click', () => this.backToAlbumList());
    document.getElementById('btn-fb-new-album').addEventListener('click', () => this.createAlbum());

    // Global keyboard shortcuts (lightbox handles its own keys via PhotoLightbox)
    document.addEventListener('keydown', (e) => {
      if (typeof App === 'undefined' || App.currentPage !== 'file-browser') return;
      const lb = document.getElementById('photo-lightbox');
      if (lb && !lb.classList.contains('hidden')) return; // PhotoLightbox handles its own keys
      if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
      if (typeof Keymap !== 'undefined') {
        if (Keymap.is(e, 'fbDelete') && this.selected.size > 0) { e.preventDefault(); this.deleteSelected(); }
        else if (Keymap.is(e, 'fbSelectAll')) { e.preventDefault(); this._selectAll(); }
        else if (Keymap.is(e, 'fbClearSelection')) { this.selected.clear(); this.render(); this._updateCloudActions(); }
        return;
      }
      if (e.key === 'Delete' && this.selected.size > 0) { e.preventDefault(); this.deleteSelected(); }
      if ((e.ctrlKey || e.metaKey) && e.key === 'a') { e.preventDefault(); this._selectAll(); }
      if (e.key === 'Escape') { this.selected.clear(); this.render(); this._updateCloudActions(); }
    });
    // Right-click context menu
    this._ctxTarget = null; // { file, index } of the right-clicked file
    this._createContextMenu();
    const fbGrid = document.getElementById('fb-grid');
    if (fbGrid) fbGrid.addEventListener('contextmenu', (e) => this._showContextMenu(e));
  },

  _createContextMenu() {
    if (document.getElementById('fb-context-menu')) return;
    const menu = document.createElement('div');
    menu.id = 'fb-context-menu';
    menu.className = 'context-menu';
    menu.innerHTML = `
      <div class="context-item" data-action="preview"><i class="ph ph-eye"></i> ${t('fbCtxPreview')}</div>
      <div class="context-item" data-action="edit"><i class="ph ph-sliders-horizontal"></i> ${t('fbCtxEdit')}</div>
      <div class="context-item" data-action="agent"><i class="ph ph-sparkle"></i> ${t('fbCtxAgent')}</div>
      <div class="context-sep"></div>
      <div class="context-item context-item-danger" data-action="delete"><i class="ph ph-trash"></i> ${t('delete')}</div>
    `;
    menu.querySelectorAll('.context-item').forEach(item => {
      item.addEventListener('click', () => {
        this._handleContextAction(item.dataset.action);
        this._closeContextMenu();
      });
    });
    document.body.appendChild(menu);
    // Close on outside click / Escape
    document.addEventListener('click', (e) => {
      if (!e.target.closest('#fb-context-menu')) this._closeContextMenu();
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') this._closeContextMenu();
    });
  },

  _showContextMenu(e) {
    e.preventDefault();
    const menu = document.getElementById('fb-context-menu');
    if (!menu) return;

    // Determine target: a specific file item, or the grid background
    const itemEl = e.target.closest('.fb-item');
    if (itemEl) {
      const idx = parseInt(itemEl.dataset.index);
      if (!isNaN(idx) && this.files[idx]) {
        this._ctxTarget = { file: this.files[idx], index: idx };
      } else {
        this._ctxTarget = null;
      }
    } else {
      this._ctxTarget = null;
    }

    // Update menu item states
    const isLocal = this.mode === 'local';
    const hasTarget = !!this._ctxTarget;
    const hasFolder = !!this.folder;

    menu.querySelector('[data-action="preview"]').classList.toggle('disabled', !hasTarget);
    menu.querySelector('[data-action="edit"]').classList.toggle('disabled', !isLocal || !hasTarget);
    menu.querySelector('[data-action="agent"]').classList.toggle('disabled', !hasFolder || typeof Agent === 'undefined');
    menu.querySelector('[data-action="delete"]').classList.toggle('disabled', !hasTarget && this.selected.size === 0);

    // Position menu (keep within viewport)
    let x = e.clientX, y = e.clientY;
    menu.classList.add('open'); // make visible temporarily to measure
    const mw = menu.offsetWidth, mh = menu.offsetHeight;
    if (x + mw > window.innerWidth) x = window.innerWidth - mw - 4;
    if (y + mh > window.innerHeight) y = window.innerHeight - mh - 4;
    menu.style.left = x + 'px';
    menu.style.top = y + 'px';
  },

  _closeContextMenu() {
    const menu = document.getElementById('fb-context-menu');
    if (menu) menu.classList.remove('open');
    this._ctxTarget = null;
  },

  async _handleContextAction(action) {
    const t = this._ctxTarget;
    switch (action) {
    case 'preview':
      if (t && t.file) {
        this._openLightbox(t.index);
      }
      break;
    case 'agent':
      if (!this.folder || typeof Agent === 'undefined') return;
      let selectedPaths = [];
      try { selectedPaths = [...(this.selected || [])].map(i => (this.files[i] && this.files[i].path) || null).filter(Boolean); } catch (_) {}
      Agent.quickLaunch({ folder: this.folder, selectedPaths });
      break;
    case 'edit':
      if (t && t.file && t.file.path && typeof PhotoEdit !== 'undefined') {
        // If multiple files selected, navigate only within selected ones
        let editList, editIdx;
        if (this.selected.size > 1 && this.selected.has(t.index)) {
          editList = [...this.selected].sort((a, b) => a - b).map(i => this.files[i]).filter(Boolean);
          editIdx = editList.findIndex(f => f === t.file);
        } else {
          editList = this.files;
          editIdx = t.index;
        }
        if (typeof Workbench !== 'undefined') {
          const photos = editList.map(f => ({ path: f.path, name: f.name, thumb: f.thumb || '' }));
          // 批量作用域：多选时只用选中的；单选时只用当前这张，绝不默认整个文件夹
          const selectedPaths = (this.selected.size > 1 && this.selected.has(t.index))
            ? photos.map(f => f.path)
            : [t.file.path];
          Workbench.setPhotos(photos, { source: 'file-browser', active: t.file.path });
          Workbench.setSelection(selectedPaths);
          editList = Workbench.getPhotos();
          editIdx = Math.max(0, editList.findIndex(f => f.path === t.file.path));
        }
        PhotoEdit.open(t.file.path, {
          fileList: editList,
          index: editIdx >= 0 ? editIdx : 0,
          onSaved: () => this.render(),
          onReset: () => this.render(),
        });
      }
      break;
    case 'delete':
      if (t && t.file) {
        if (!(await Modal.showConfirm(window.t('fbDeleteFile'), window.t('fbConfirmDeleteFile', { name: t.file.name })))) return;
        if (this.mode === 'cloud') {
          this._deleteCloudFile(t.index);
        } else {
          this._deleteLocalFile(t.index);
        }
      } else if (this.selected.size > 0) {
        this.deleteSelected();
      }
      break;
    }
  },

  async _deleteLocalFile(index) {
    const f = this.files[index];
    if (!f) return;
    try {
      await window.api.sendCommand('delete_file', { path: f.path });
      this._removeFileFromList(index, f);
      App.toast(t('fbDeleted', { name: f.name }), 'success');
    } catch (e) { App.toast(t('fbDeleteFailed', { msg: f.name }), 'error'); }
  },

  async _deleteCloudFile(index) {
    const f = this.files[index];
    if (!f) return;
    try {
      const resp = await fetch(`${App.serverUrl}/api/photos/${f.id}`, { method: 'DELETE', headers: { 'x-studio-token': App.token } });
      if (!resp.ok) throw new Error((await resp.json().catch(()=>({}))).error || 'Failed');
      this._removeFileFromList(index, f);
      App.toast(t('fbDeleted', { name: f.name }), 'success');
    } catch (e) { App.toast(t('fbDeleteFailed', { msg: f.name }), 'error'); }
  },

  _removeFileFromList(index, fileObj) {
    this.files.splice(index, 1);
    const ai = this._allFiles.indexOf(fileObj);
    if (ai >= 0) this._allFiles.splice(ai, 1);
    this.selected.delete(index);
    // Re-index remaining selected
    const newSel = new Set();
    for (const i of this.selected) { if (i > index) newSel.add(i - 1); else newSel.add(i); }
    this.selected = newSel;
    this.render();
    this._updateCloudActions();
  },

  _abort() { if (this._abortCtrl) { this._abortCtrl.abort(); this._abortCtrl = null; } },
  _selectAll() {
    if (this.selected.size === this.files.length) { this.selected.clear(); }
    else { this.files.forEach((_, i) => this.selected.add(i)); }
    this.render();
    this._updateCloudActions();
  },
  // 文件页“全选”按钮：始终选中当前筛选结果，而不是切换
  selectAllVisible() {
    if (!this.files.length) return;
    this.files.forEach((_, i) => this.selected.add(i));
    this.render();
    this._updateCloudActions();
  },
  clearSelection() {
    if (!this.selected.size) return;
    this.selected.clear();
    this.render();
    this._updateCloudActions();
  },
  // 本地文件支持直接批量导出；云端照片需先下载，不在此入口暴露
  exportSelected() {
    if (this.mode !== 'local') return;
    const paths = [...this.selected]
      .map(i => this.files[i] && this.files[i].path)
      .filter(Boolean);
    if (!paths.length) return;
    if (typeof BatchExport !== 'undefined') BatchExport.open(paths);
  },
  _applyFilters() {
    const q = (document.getElementById('fb-search')?.value || '').toLowerCase();
    if (!q) { this.files = [...this._allFiles]; return; }
    this.files = this._allFiles.filter(f => f.name.toLowerCase().includes(q));
    this.selected.clear();
  },
  _applySort() {
    const sort = document.getElementById('fb-sort')?.value || 'name';
    if (sort === 'date') {
      this._allFiles.sort((a, b) => {
        const ta = a.mtime || (a.uploadedAt ? new Date(a.uploadedAt).getTime() : 0);
        const tb = b.mtime || (b.uploadedAt ? new Date(b.uploadedAt).getTime() : 0);
        return tb - ta;
      });
    } else if (sort === 'size') {
      this._allFiles.sort((a, b) => (b.size || 0) - (a.size || 0));
    } else {
      this._allFiles.sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true }));
    }
    this._applyFilters();
  },

  // ── Mode UI ──
  _updateModeUI() {
    const isLocal = this.mode === 'local';
    const isAlbums = this.mode === 'albums';
    document.getElementById('fb-local-controls').style.display = isLocal ? '' : 'none';
    document.getElementById('fb-toolbar').style.display = isAlbums ? 'none' : '';
    document.getElementById('fb-cloud-actions').classList.toggle('hidden', this.mode !== 'cloud');
    document.getElementById('fb-grid').classList.toggle('hidden', isAlbums);
    document.getElementById('fb-album-list').classList.toggle('hidden', !isAlbums || !!this.currentAlbumId);
    document.getElementById('fb-album-detail').classList.toggle('hidden', !this.currentAlbumId);
    document.getElementById('btn-fb-back-albums').classList.toggle('hidden', !this.currentAlbumId);
    document.getElementById('btn-fb-new-album').classList.toggle('hidden', !isAlbums || !!this.currentAlbumId);
    const title = isAlbums ? (this.currentAlbumId ? t('fbAlbumPhotos') : t('fbAlbumList')) : t('fbFileList');
    document.getElementById('fb-section-title').textContent = title;
  },

  checkCloudAccess() {
    const lock = document.getElementById('fb-sub-lock');
    if ((this.mode === 'cloud' || this.mode === 'albums') && (!App.hasActiveSub || !App.hasActiveSub())) {
      if (lock) lock.classList.remove('hidden');
    } else {
      if (lock) lock.classList.add('hidden');
    }
  },

  // ── Local ──
  async loadLocalFolder() {
    if (!this.folder) return;
    this._loading = true;
    this._showLoading();
    this._abort();
    try {
      const r = await window.api.sendCommand('list_images_in_folder', { folder: this.folder });
      const images = r.images || [];
      this._allFiles = images.map(p => {
        // Backward compat: p can be a string (old backend) or {path,size,mtime} (new backend)
        const filePath = typeof p === 'string' ? p : p.path;
        const name = filePath.split('\\').pop().split('/').pop();
        const ext = name.split('.').pop().toLowerCase();
        return {
          path: filePath, name, ext, isLocal: true,
          mtime: (typeof p === 'object' ? p.mtime : 0) || 0,
          size: (typeof p === 'object' ? p.size : 0) || 0,
          thumb: '',
        };
      });
      this._applySort();
    } catch (e) { App.toast(t('fbLoadFail'), 'error'); }
    this._loading = false;
    this.render();
    // Load thumbnails asynchronously in background (batch by batch)
    this._loadThumbnails();
  },

  async _loadThumbnails() {
    const needsThumb = this._allFiles.filter(f => !f.thumb);
    if (!needsThumb.length) return;
    this._thumbFailNotified = false;
    const batchSize = 30;
    for (let i = 0; i < needsThumb.length; i += batchSize) {
      const batch = needsThumb.slice(i, i + batchSize);
      try {
        const r = await window.api.sendCommand('get_thumbnails', {
          paths: batch.map(f => f.path)
        });
        if (r && r.thumbs) {
          for (const [path, thumbPath] of Object.entries(r.thumbs)) {
            const file = this._allFiles.find(f => f.path === path);
            if (file) file.thumb = thumbPath;
          }
          this.render();
        }
      } catch (_) {
        // Notify once per load run so users don't stare at permanent placeholder icons
        if (!this._thumbFailNotified) {
          this._thumbFailNotified = true;
          App.toast(t('fbThumbLoadFail'), 'warning');
        }
      }
    }
  },

  _showLoading() {
    const grid = document.getElementById('fb-grid');
    if (grid) grid.innerHTML = '<div class="empty-state"><i class="ph ph-spinner" style="font-size:28px;animation:spin 1s linear infinite"></i><p>' + t('fbLoading') + '</p></div>';
  },

  // ── Cloud ──
  async loadCloudPhotos(page = 0) {
    if (!App.hasActiveSub || !App.hasActiveSub()) { this.cloudPhotos = []; this.render(); return; }
    this._loading = true;
    this._showLoading();
    this._abort();
    this._abortCtrl = new AbortController();
    try {
      const limit = 200;
      const resp = await fetch(`${App.serverUrl}/api/photos?limit=${limit}&offset=${page*limit}`, {
        headers: { 'x-studio-token': App.token },
        signal: this._abortCtrl.signal,
      });
      if (resp.status === 401) {
        // Mirror App.refreshUser: clear the dead token and show the login overlay
        App.toast(t('loginExpired'), 'error');
        App.user = null; App.token = '';
        App._removeToken();
        App.updateCloudConnect();
        document.getElementById('login-overlay').classList.remove('hidden');
        this._loading = false;
        return;
      }
      if (!resp.ok) throw new Error('Failed');
      const data = await resp.json();
      this.cloudPhotos = (data.photos || []).map(p => ({
        id: p.id, name: p.originalName, size: p.size, score: p.score,
        thumbUrl: p.thumbUrl, viewUrl: p.viewUrl, category: p.category,
        uploadedAt: p.uploadedAt, isLocal: false,
      }));
      this._allFiles = this.cloudPhotos;
      this.files = [...this.cloudPhotos];
      this.render();
      // Pagination controls
      const hasMore = data.hasMore;
      this._renderCloudPagination(page, hasMore);
    } catch (e) {
      if (e.name === 'AbortError') return;
      App.toast(t('fbCloudFail'), 'error');
    }
    this._loading = false;
  },

  _renderCloudPagination(page, hasMore) {
    let pg = document.getElementById('fb-cloud-pagination');
    if (!pg) {
      pg = document.createElement('div');
      pg.id = 'fb-cloud-pagination';
      pg.style.cssText = 'display:flex;justify-content:center;align-items:center;gap:8px;padding:8px 0';
      const grid = document.getElementById('fb-grid');
      if (grid) grid.parentNode.insertBefore(pg, grid.nextSibling);
    }
    pg.innerHTML = `
      <button class="btn btn-sm btn-outline" ${page === 0 ? 'disabled' : ''} onclick="FileBrowser.loadCloudPhotos(${page - 1})">${t('fbPrevPage')}</button>
      <span style="font-size:12px;color:var(--text2)">${t('fbPage').replace('N', page + 1)}</span>
      <button class="btn btn-sm btn-outline" ${!hasMore ? 'disabled' : ''} onclick="FileBrowser.loadCloudPhotos(${page + 1})">${t('fbNextPage')}</button>
    `;
  },

  // ── Render ──
  render() {
    document.getElementById('fb-count').textContent = this.files.length;
    const grid = document.getElementById('fb-grid');
    if (!this.files.length) {
      grid.innerHTML = '<div class="empty-state"><span class="empty-icon"><i class="ph ph-folder-open"></i></span><span class="empty-text">' + t('fbNoFiles') + '</span></div>';
      return;
    }

    if (this.mode === 'cloud') {
      grid.innerHTML = this.files.map((f, i) => `
        <div class="fb-item ${this.selected.has(i) ? 'selected' : ''}" data-index="${i}" style="animation-delay:${Math.min(i*20, 400)}ms">
          <img class="fb-thumb" src="${f.thumbUrl ? App.serverUrl + f.thumbUrl : ''}" onerror="this.style.display='none'" loading="lazy" style="width:40px;height:30px;object-fit:cover;border-radius:4px;flex-shrink:0">
          <div class="fb-name" title="${f.name}">${f.name}</div>
          <span style="font-size:11px;color:var(--text3);flex-shrink:0">${f.score ? t('fbScorePts', { score: f.score.toFixed(1) }) : ''}</span>
          <span style="font-size:11px;flex-shrink:0">${this._catIcon(f.category)}</span>
        </div>`).join('');
    } else {
      grid.innerHTML = this.files.map((f, i) => {
        const isRaw = ['arw','cr2','cr3','nef','dng','orf','rw2','raf'].includes(f.ext);
        // Show real thumbnail if available, otherwise file type icon (prevents loading full-size files)
        const hasThumb = f.thumb && f.isLocal;
        const thumbSrc = hasThumb ? `local-image://x/?p=${encodeURIComponent(f.thumb)}` : '';
        const iconHtml = hasThumb ? '' : `<i class="ph ${isRaw ? 'ph-film-strip' : 'ph-image'}" style="color:var(--text3);font-size:20px"></i>`;
        return `
        <div class="fb-item ${this.selected.has(i) ? 'selected' : ''}" data-index="${i}" style="animation-delay:${Math.min(i*20, 400)}ms">
          <div class="fb-thumb-placeholder" style="width:36px;height:27px;flex-shrink:0;display:flex;align-items:center;justify-content:center;background:var(--bg3);border-radius:4px;overflow:hidden">
            ${thumbSrc ? `<img src="${thumbSrc}" onerror="this.style.display='none';this.nextElementSibling.style.display=''" style="width:100%;height:100%;object-fit:cover">` : ''}
            <span class="fb-icon-fallback" style="display:${hasThumb ? 'none' : 'block'}">${iconHtml}</span>
          </div>
          <div class="fb-name" title="${f.name}">${f.name}</div>
          ${f.size ? `<span style="font-size:10px;color:var(--text3);flex-shrink:0">${Fmt.size(f.size)}</span>` : ''}
          <button class="btn btn-ghost btn-sm fb-rename-btn" data-index="${i}" title="${t('fbRename')}"><i class="ph ph-pencil-simple"></i></button>
        </div>`;
      }).join('');
    }

    // Click to select, double-click to preview
    grid.querySelectorAll('.fb-item').forEach(el => {
      el.addEventListener('click', (e) => {
        if (e.target.closest('button') || e.target.closest('a')) return;
        const i = parseInt(el.dataset.index);
        if (this.selected.has(i)) this.selected.delete(i); else this.selected.add(i);
        // Toggle CSS class directly (no full re-render)
        el.classList.toggle('selected', this.selected.has(i));
        this._updateCloudActions();
      });
      el.addEventListener('dblclick', (e) => {
        const i = parseInt(el.dataset.index);
        this._openLightbox(i);
      });
    });

    // Rename (local only)
    if (this.mode === 'local') {
      grid.querySelectorAll('.fb-rename-btn').forEach(btn => {
        btn.addEventListener('click', async (e) => {
          e.stopPropagation();
          const i = parseInt(btn.dataset.index);
          const f = this.files[i];
          const nn = await Modal.showInput(t('fbRename'), t('fbRenameCurrent', { name: f.name }));
          if (!nn) return;
          try {
            // Replace only filename portion, not path segments
            const dir = f.path.substring(0, f.path.lastIndexOf(f.name));
            const np = dir + nn;
            await window.api.sendCommand('rename_file', { oldPath: f.path, newPath: np });
            f.path = np; f.name = nn; this.render(); App.toast(t('fbRenamed'), 'success');
          } catch (e) { App.toast(t('fbRenameFailed'), 'error'); }
        });
      });
    }
  },

  _catIcon(c) {
    if (c === 'excellent') return '<i class="ph ph-star" style="color:var(--green);font-size:14px"></i>';
    if (c === 'reject') return '<i class="ph ph-trash" style="color:var(--red);font-size:14px"></i>';
    return '<i class="ph ph-clock" style="color:var(--text3);font-size:14px"></i>';
  },

  _updateCloudActions() {
    const count = this.selected.size;
    const bar = document.getElementById('fb-cloud-actions');
    const badge = document.getElementById('fb-cloud-selected-count');
    if (this.mode === 'local') {
      // Local mode: hide cloud-specific buttons, show only export + delete + count
      bar.classList.toggle('hidden', count === 0);
      document.getElementById('btn-fb-cat-excellent').style.display = 'none';
      document.getElementById('btn-fb-cat-pending').style.display = 'none';
      document.getElementById('btn-fb-cat-reject').style.display = 'none';
      document.getElementById('btn-fb-add-to-album').style.display = 'none';
      document.getElementById('btn-fb-export').style.display = '';
    } else {
      bar.classList.toggle('hidden', count === 0);
      document.getElementById('btn-fb-cat-excellent').style.display = '';
      document.getElementById('btn-fb-cat-pending').style.display = '';
      document.getElementById('btn-fb-cat-reject').style.display = '';
      document.getElementById('btn-fb-add-to-album').style.display = '';
      document.getElementById('btn-fb-export').style.display = 'none';
    }
    if (count > 0) {
      badge.textContent = t('fbSelected', { count });
      badge.classList.remove('hidden');
    } else {
      badge.classList.add('hidden');
    }
  },

  async deleteSelected() {
    const indices = [...this.selected];
    if (!indices.length) return;
    if (!(await Modal.showConfirm(t('fbBatchDelete'), t('fbConfirmDeleteFiles', { count: indices.length })))) return;
    let d = 0;
    for (const i of indices.sort((a,b) => b-a)) {
      try {
        if (this.mode === 'cloud') {
          const resp = await fetch(`${App.serverUrl}/api/photos/${this.files[i].id}`, { method: 'DELETE', headers: { 'x-studio-token': App.token } });
          if (!resp.ok) throw new Error((await resp.json().catch(()=>({}))).error || 'Failed');
        } else {
          await window.api.sendCommand('delete_file', { path: this.files[i].path });
        }
        // Remove from both arrays
        const deleted = this.files[i];
        this.files.splice(i, 1);
        const ai = this._allFiles.indexOf(deleted);
        if (ai >= 0) this._allFiles.splice(ai, 1);
        d++;
      } catch (e) { App.toast(t('fbDeleteFailed', { msg: this.files[i].name }), 'error'); }
    }
    this.selected.clear(); this.render();
    this._updateCloudActions();
    if (d > 0) App.toast(t('fbDeletedFiles', { count: d }), 'success');
  },

  // ── Cloud Batch Operations ──
  _getSelectedCloudIds() {
    return [...this.selected].map(i => this.files[i]?.id).filter(Boolean);
  },

  async batchCategorizeCloud(category) {
    const ids = this._getSelectedCloudIds();
    if (!ids.length) return;
    try {
      const resp = await fetch(`${App.serverUrl}/api/photos/batch`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'x-studio-token': App.token },
        body: JSON.stringify({ action: 'categorize', ids, category }),
      });
      if (!resp.ok) throw new Error((await resp.json().catch(()=>({}))).error || 'Failed');
      // Update local state
      for (const i of this.selected) {
        if (this.files[i]) this.files[i].category = category;
      }
      this.selected.clear();
      this.render();
      this._updateCloudActions();
      App.toast(t('fbCategorized', { cat: category === 'excellent' ? '⭐' + t('fbExcellent') : category === 'reject' ? '🗑' + t('fbReject') : '⏳' + t('fbPending') }), 'success');
    } catch (e) { App.toast(t('fbCatFailed', { msg: e.message }), 'error'); }
  },

  async batchDeleteCloud() {
    const ids = this._getSelectedCloudIds();
    if (!ids.length) return;
    if (!(await Modal.showConfirm(t('fbDeleteCloudTitle'), t('fbConfirmDeleteCloud', { count: ids.length })))) return;
    try {
      const resp = await fetch(`${App.serverUrl}/api/photos/batch`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'x-studio-token': App.token },
        body: JSON.stringify({ action: 'delete', ids }),
      });
      if (!resp.ok) throw new Error((await resp.json().catch(()=>({}))).error || 'Failed');
      // Remove from local arrays
      const idSet = new Set(ids);
      this.cloudPhotos = this.cloudPhotos.filter(p => !idSet.has(p.id));
      this.files = this.cloudPhotos;
      this.selected.clear();
      this.render();
      this._updateCloudActions();
      App.toast(t('fbDeletedPhotos', { count: ids.length }), 'success');
    } catch (e) { App.toast(t('fbDeleteFailed', { msg: e.message }), 'error'); }
  },

  async batchAddToAlbum() {
    const ids = this._getSelectedCloudIds();
    if (!ids.length) { App.toast(t('fbSelectPhotosFirst'), 'warning'); return; }
    // Load albums for selection
    try {
      const resp = await fetch(`${App.serverUrl}/api/albums`, { headers: { 'x-studio-token': App.token } });
      if (!resp.ok) throw new Error('Failed');
      const albums = await resp.json();
      if (!albums.length) {
        // No albums exist — prompt to create one
        const title = await Modal.showInput(t('fbNewAlbum'), t('fbNoAlbumsPrompt'));
        if (!title) return;
        const password = await Modal.showInput(t('fbSetPassword'), t('fbPasswordOptional')) || '';
        await this._createAlbumAndAdd(title, password, ids);
        return;
      }
      // Let user pick an album or create new
      const albumNames = albums.map((a, i) => `${i + 1}. ${a.title} (${t('fbPhotosCount', { count: a.photoCount })})`).join('\n');
      const choice = await Modal.showInput(t('fbAddAlbum'), t('fbPickAlbumPrompt', { list: albumNames }));
      if (!choice) return;
      if (choice.toLowerCase() === 'new') {
        const title = await Modal.showInput(t('fbNewAlbum'), t('fbNewAlbumPrompt'));
        if (!title) return;
        const password = await Modal.showInput(t('fbSetPassword'), t('fbPasswordOptional')) || '';
        await this._createAlbumAndAdd(title, password, ids);
      } else {
        const idx = parseInt(choice) - 1;
        if (isNaN(idx) || idx < 0 || idx >= albums.length) { App.toast(t('fbInvalidChoice'), 'error'); return; }
        await this._addToExistingAlbum(albums[idx].id, ids);
      }
    } catch (e) { App.toast(t('fbLoadAlbumsFailedMsg', { msg: e.message }), 'error'); }
  },

  async _createAlbumAndAdd(title, password, ids) {
    try {
      const album = await App.createAlbum(title, password, ids);
      this.selected.clear();
      this.render();
      this._updateCloudActions();
      Modal.showMessage(t('fbAlbumCreatedMsg', { title, url: album.shareUrl, pwd: password ? t('fbPasswordSuffix', { pwd: password }) : '' }), 'success');
    } catch (e) { App.toast(t('fbCreateAlbumFailed', { msg: e.message }), 'error'); }
  },

  async _addToExistingAlbum(albumId, ids) {
    try {
      const resp = await fetch(`${App.serverUrl}/api/albums/${albumId}/photos`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json', 'x-studio-token': App.token },
        body: JSON.stringify({ photoIds: ids }),
      });
      if (!resp.ok) throw new Error((await resp.json().catch(()=>({}))).error || 'Failed');
      this.selected.clear();
      this.render();
      this._updateCloudActions();
      App.toast(t('fbAddedToAlbum', { count: ids.length }), 'success');
    } catch (e) { App.toast(t('fbAddToAlbumFailed', { msg: e.message }), 'error'); }
  },

  // ── Photo Preview (PhotoLightbox: local + cloud) ──

  _openLightbox(index, fileList) {
    const files = fileList || this.files;
    const isCloud = this.mode === 'cloud' || !!this.currentAlbumId;
    PhotoLightbox.open({
      files,
      index,
      mode: isCloud ? 'cloud' : 'local',
      serverUrl: isCloud ? App.serverUrl : '',
      getViewUrl: isCloud ? (f) => f.viewUrl || '' : null,
      onEdit: (f, idx) => {
        if (!f.path) return;
        PhotoLightbox.close();
        if (typeof PhotoEdit !== 'undefined') {
          PhotoEdit.open(f.path, { fileList: this.files, index: idx, onSaved: () => this.render(), onReset: () => this.render() });
        }
      },
      renderButtons: isCloud ? (el, f) => {
        const file = f; // capture for event handlers
        el.innerHTML = `
          <button class="btn btn-sm" data-cat="excellent">⭐ ${t('fbExcellent')}</button>
          <button class="btn btn-sm" data-cat="pending">⏳ ${t('fbPending')}</button>
          <button class="btn btn-sm" data-cat="reject">🗑 ${t('fbReject')}</button>
          <button class="btn btn-sm btn-primary" data-download>⬇ ${t('fbDownload')}</button>
        `;
        el.querySelectorAll('[data-cat]').forEach(btn => {
          btn.addEventListener('click', (e) => { e.stopPropagation(); this.lightboxSetCategory(btn.dataset.cat, file); });
        });
        el.querySelector('[data-download]').addEventListener('click', (e) => { e.stopPropagation(); this.lightboxDownload(file); });
      } : null,
    });
  },

  async lightboxSetCategory(category, file) {
    if (!file || !file.id) return;
    try {
      const resp = await fetch(`${App.serverUrl}/api/photos/${file.id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json', 'x-studio-token': App.token },
        body: JSON.stringify({ category }),
      });
      if (!resp.ok) throw new Error((await resp.json().catch(()=>({}))).error || 'Failed');
      file.category = category;
      App.toast(t('fbCategorized', { cat: category === 'excellent' ? '⭐' + t('fbExcellent') : category === 'reject' ? '🗑' + t('fbReject') : '⏳' + t('fbPending') }), 'success');
      this.render();
    } catch (e) { App.toast(t('fbCatFailed', { msg: e.message }), 'error'); }
  },

  lightboxDownload(file) {
    if (!file || !file.viewUrl) return;
    window.api.openUrl(App.serverUrl + file.viewUrl);
  },

  // ── Albums ──
  async loadAlbums() {
    if (!App.hasActiveSub || !App.hasActiveSub()) {
      document.getElementById('fb-album-list').innerHTML = '<div class="empty-state"><span class="empty-text">' + t('fbNeedSub') + '</span></div>';
      return;
    }
    try {
      const resp = await fetch(`${App.serverUrl}/api/albums`, { headers: { 'x-studio-token': App.token } });
      if (!resp.ok) throw new Error('Failed');
      this.albums = await resp.json();
      this.renderAlbumList();
    } catch (e) { App.toast(t('fbLoadAlbumsFailed'), 'error'); }
  },

  renderAlbumList() {
    const container = document.getElementById('fb-album-list');
    document.getElementById('fb-count').textContent = this.albums.length;

    if (!this.albums.length) {
      container.innerHTML = '<div class="empty-state"><span class="empty-icon"><i class="ph ph-folder-open"></i></span><span class="empty-text">' + t('fbNoAlbums') + '</span><p style="margin-top:8px">' + t('fbNoAlbumsHint') + '</p></div>';
      return;
    }

    container.innerHTML = this.albums.map(a => `
      <div class="fb-album-item" data-album-id="${a.id}">
        <div class="fb-album-icon">📁</div>
        <div class="fb-album-info">
          <div class="fb-album-title">${this._esc(a.title)}</div>
          <div class="fb-album-meta">${t('fbAlbumMeta', { count: a.photoCount, code: a.accessCode, date: new Date(a.createdAt).toLocaleDateString('zh-CN') })}</div>
        </div>
        <div class="fb-album-actions">
          <button class="btn btn-sm btn-outline fb-album-copy-link" data-code="${a.accessCode}" title="${t('fbCopyLink')}"><i class="ph ph-link"></i></button>
          <button class="btn btn-sm btn-outline fb-album-delete" data-id="${a.id}" title="${t('fbDeleteAlbum')}" style="color:var(--red)"><i class="ph ph-trash"></i></button>
        </div>
      </div>
    `).join('');

    // Click album to view photos
    container.querySelectorAll('.fb-album-item').forEach(el => {
      el.addEventListener('click', (e) => {
        if (e.target.closest('button')) return;
        this.viewAlbum(el.dataset.albumId);
      });
    });

    // Copy link buttons
    container.querySelectorAll('.fb-album-copy-link').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        this.copyAlbumLink(btn.dataset.code);
      });
    });

    // Delete album buttons
    container.querySelectorAll('.fb-album-delete').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        this.deleteAlbum(btn.dataset.id);
      });
    });
  },

  async viewAlbum(albumId) {
    try {
      const resp = await fetch(`${App.serverUrl}/api/albums/${albumId}`, { headers: { 'x-studio-token': App.token } });
      if (!resp.ok) throw new Error('Failed');
      const album = await resp.json();
      this.currentAlbumId = albumId;
      this.currentAlbumPhotos = (album.photos || []).map(p => ({
        id: p.id, name: p.name, score: p.score, thumbUrl: p.thumbUrl, viewUrl: `/api/photos/${p.id}/view`,
        isLocal: false,
      }));
      this.files = this.currentAlbumPhotos;
      this.selected.clear();
      this._updateModeUI();
      this.renderAlbumPhotos();
    } catch (e) { App.toast(t('fbLoadAlbumDetailFailed'), 'error'); }
  },

  renderAlbumPhotos() {
    const container = document.getElementById('fb-album-detail');
    document.getElementById('fb-count').textContent = this.currentAlbumPhotos.length;

    if (!this.currentAlbumPhotos.length) {
      container.innerHTML = '<div class="empty-state"><span class="empty-text">' + t('fbAlbumEmpty') + '</span></div>';
      return;
    }

    container.innerHTML = `<div class="photo-grid">${this.currentAlbumPhotos.map((p, i) => `
      <div class="photo-card ${this.selected.has(i) ? 'selected' : ''}" data-index="${i}">
        <img src="${p.thumbUrl ? App.serverUrl + p.thumbUrl : ''}" alt="${this._esc(p.name)}" loading="lazy" onerror="this.style.background='var(--bg3)';this.style.minHeight='120px'">
        <div class="card-info">
          <div class="card-name">${this._esc(p.name)}</div>
          ${p.score > 0 ? `<span class="score-badge ${p.score >= 7 ? 'score-high' : p.score >= 5 ? 'score-mid' : 'score-low'}">${p.score.toFixed(1)}</span>` : ''}
        </div>
      </div>
    `).join('')}</div>`;

    container.querySelectorAll('.photo-card').forEach(card => {
      card.addEventListener('click', (e) => {
        const i = parseInt(card.dataset.index);
        if (e.ctrlKey || e.metaKey) {
          if (this.selected.has(i)) this.selected.delete(i); else this.selected.add(i);
          container.querySelectorAll('.photo-card').forEach(c => {
            c.classList.toggle('selected', this.selected.has(parseInt(c.dataset.index)));
          });
        } else {
          this._openLightbox(i, this.currentAlbumPhotos);
        }
      });
    });
  },

  backToAlbumList() {
    this.currentAlbumId = null;
    this.currentAlbumPhotos = [];
    this.files = [];
    this.selected.clear();
    PhotoLightbox.close();
    this._updateModeUI();
    this.renderAlbumList();
    document.getElementById('fb-count').textContent = this.albums.length;
  },

  async createAlbum() {
    const title = await Modal.showInput(t('fbNewAlbum'), t('fbAlbumNamePrompt'));
    if (!title) return;
    const password = await Modal.showInput(t('fbSetPassword'), t('fbPasswordOptional')) || '';
    try {
      const resp = await fetch(`${App.serverUrl}/api/albums`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'x-studio-token': App.token },
        body: JSON.stringify({ title, photoIds: [], password }),
      });
      if (!resp.ok) throw new Error((await resp.json().catch(()=>({}))).error || 'Failed');
      const album = await resp.json();
      const shareUrl = `${App.serverUrl}/album/${album.accessCode}`;
      App.toast(t('fbAlbumCreated'), 'success');
      Modal.showMessage(t('fbAlbumCreatedSimple', { url: shareUrl, pwd: password ? t('fbPasswordSuffix', { pwd: password }) : '' }), 'success');
      this.loadAlbums();
    } catch (e) { App.toast(t('fbCreateAlbumFailed', { msg: e.message }), 'error'); }
  },

  async deleteAlbum(albumId) {
    if (!(await Modal.showConfirm(t('fbDeleteAlbum'), t('fbConfirmDeleteAlbum')))) return;
    try {
      const resp = await fetch(`${App.serverUrl}/api/albums/${albumId}`, {
        method: 'DELETE',
        headers: { 'x-studio-token': App.token },
      });
      if (!resp.ok) throw new Error((await resp.json().catch(()=>({}))).error || 'Failed');
      App.toast(t('fbAlbumDeleted'), 'success');
      if (this.currentAlbumId === albumId) this.backToAlbumList();
      else this.loadAlbums();
    } catch (e) { App.toast(t('fbDeleteAlbumFailed', { msg: e.message }), 'error'); }
  },

  copyAlbumLink(accessCode) {
    // Check if album has a password via stored album data
    const album = this.albums.find(a => a.accessCode === accessCode);
    const hasPassword = album && album.hasPassword;
    const baseUrl = `${App.serverUrl}/album/${accessCode}`;

    if (hasPassword) {
      // Prompt user whether to include password in link
      Modal.showInput(t('fbCopyShareLink'), t('fbShareLinkPwdPrompt'))
        .then(pwd => {
          const url = pwd ? `${baseUrl}?pwd=${encodeURIComponent(pwd)}` : baseUrl;
          navigator.clipboard.writeText(url).then(() => {
            App.toast(pwd ? t('fbShareLinkPwdCopied') : t('fbShareLinkCopied'), 'success');
          }).catch(() => Modal.showMessage(t('fbShareLinkMsg', { url }), 'info'));
        });
    } else {
      navigator.clipboard.writeText(baseUrl).then(() => {
        App.toast(t('fbShareLinkCopiedClipboard'), 'success');
      }).catch(() => {
        Modal.showMessage(t('fbShareLinkMsg', { url: baseUrl }), 'info');
      });
    }
  },

  _esc(s) { return UI._escapeHtml(s); },

  setFolder(path) {
    this.folder = path;
    document.getElementById('fb-folder').value = path;
    document.getElementById('btn-fb-explorer').disabled = false;
    if (App.currentPage !== 'file-browser') App.setPage('file-browser');
    this.loadLocalFolder();
  },
};

document.addEventListener('DOMContentLoaded', () => FileBrowser.init());
