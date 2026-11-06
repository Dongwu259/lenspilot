/**
 * photo-lightbox.js — Shared fullscreen photo viewer.
 *
 * Self-contained lightbox used by file-browser, photo-select, etc.
 * Provides: open/close/prev/next, image display, keyboard nav, edit hook.
 *
 * Usage:
 *   PhotoLightbox.open({
 *     files: [{path, thumb?, name?, score?, ...}],
 *     index: 0,
 *     mode: 'local' | 'cloud',             // local → local-image://, cloud → server URL
 *     serverUrl: 'https://...',            // for cloud mode
 *     getViewUrl: (file) => '...',         // optional custom URL builder
 *     onClose: () => {},
 *     onEdit: (file, index) => {},         // called when edit button clicked
 *     onNavigate: (file, index) => {},     // called on prev/next
 *     renderButtons: (container, file) => {},  // optional: add custom buttons below image
 *   });
 *   PhotoLightbox.close();
 */

const PhotoLightbox = {
  _files: [],
  _index: -1,
  _mode: 'local',
  _callbacks: {},
  _bound: false,

  open(opts) {
    this._files = opts.files || [];
    this._index = opts.index || 0;
    this._mode = opts.mode || 'local';
    this._callbacks = opts;

    this._bind();
    this._render();
    document.getElementById('photo-lightbox').classList.remove('hidden');
  },

  close() {
    document.getElementById('photo-lightbox').classList.add('hidden');
    const cb = this._callbacks.onClose;
    this._files = [];
    this._index = -1;
    this._callbacks = {};
    if (cb) cb();
  },

  _bind() {
    if (this._bound) return;
    this._bound = true;
    const $ = id => document.getElementById(id);

    $('pl-close').addEventListener('click', () => this.close());
    $('pl-backdrop').addEventListener('click', () => this.close());
    $('pl-prev').addEventListener('click', () => this._navigate(-1));
    $('pl-next').addEventListener('click', () => this._navigate(1));
    $('pl-edit').addEventListener('click', () => {
      const f = this._files[this._index];
      if (f && this._callbacks.onEdit) this._callbacks.onEdit(f, this._index);
    });

    document.addEventListener('keydown', (e) => {
      const lb = document.getElementById('photo-lightbox');
      if (!lb || lb.classList.contains('hidden')) return;
      if (typeof Keymap !== 'undefined') {
        if (Keymap.is(e, 'lbClose')) this.close();
        else if (Keymap.is(e, 'lbPrev')) { e.preventDefault(); this._navigate(-1); }
        else if (Keymap.is(e, 'lbNext')) { e.preventDefault(); this._navigate(1); }
        return;
      }
      if (e.key === 'Escape') { this.close(); }
      else if (e.key === 'ArrowLeft') { e.preventDefault(); this._navigate(-1); }
      else if (e.key === 'ArrowRight') { e.preventDefault(); this._navigate(1); }
    });
  },

  _navigate(dir) {
    const n = this._files.length;
    if (n === 0) return;
    this._index = ((this._index + dir) % n + n) % n;
    this._render();
    if (this._callbacks.onNavigate) {
      this._callbacks.onNavigate(this._files[this._index], this._index);
    }
  },

  _render() {
    const f = this._files[this._index];
    if (!f) return;

    // Image
    const img = document.getElementById('pl-img');
    this._hideRawPlaceholder();
    img.style.display = '';
    if (this._mode === 'local' && f.path) {
      // RAW/HEIC → use thumbnail if available
      const ext = (f.ext || (f.path || '').split('.').pop() || '').toLowerCase();
      const RAW_EXTS = new Set(['heic','heif','arw','cr2','cr3','nef','nrw','raf','rw2','orf',
        'pef','dng','srw','x3f','raw','iiq','3fr','mef','mos','mrw','erf','dcr','kdc']);
      if (RAW_EXTS.has(ext)) {
        if (f.thumb) {
          img.src = 'local-image://x/?p=' + encodeURIComponent(f.thumb);
        } else {
          // RAW without thumbnail: Chromium can't decode the original bytes —
          // show a placeholder (icon + filename) instead of a broken image
          img.src = '';
          img.style.display = 'none';
          this._showRawPlaceholder(f);
        }
      } else {
        img.src = 'local-image://x/?p=' + encodeURIComponent(f.path);
      }
    } else if (f.viewUrl) {
      img.src = (this._callbacks.serverUrl || '') + f.viewUrl;
    } else if (this._callbacks.getViewUrl) {
      img.src = this._callbacks.getViewUrl(f);
    }

    // Info
    document.getElementById('pl-name').textContent = f.name || f.filename || '';
    const scoreEl = document.getElementById('pl-score');
    const score = f.score || 0;
    if (score > 0) {
      scoreEl.textContent = score.toFixed(1);
      scoreEl.className = 'score-badge ' + (score >= 7 ? 'score-high' : score >= 5 ? 'score-mid' : 'score-low');
      scoreEl.classList.remove('hidden');
    } else {
      scoreEl.classList.add('hidden');
    }

    // Counter
    document.getElementById('pl-counter').textContent = (this._index + 1) + ' / ' + this._files.length;

    // Nav state
    document.getElementById('pl-prev').disabled = this._files.length <= 1;
    document.getElementById('pl-next').disabled = this._files.length <= 1;

    // Edit button: show when file has a local path
    const hasLocalPath = !!(this._mode === 'local' && f.path);
    document.getElementById('pl-edit').style.display = hasLocalPath ? '' : 'none';

    // Custom action buttons from caller (e.g., cloud category/download)
    const actionsEl = document.getElementById('pl-actions');
    actionsEl.innerHTML = '';
    if (this._callbacks.renderButtons) {
      this._callbacks.renderButtons(actionsEl, f, this._index);
    }

    // Image info (color space + EXIF)
    this._loadImageInfo(f);
  },

  _showRawPlaceholder(f) {
    let ph = document.getElementById('pl-raw-placeholder');
    if (!ph) {
      ph = document.createElement('div');
      ph.id = 'pl-raw-placeholder';
      ph.style.cssText = 'display:none;flex-direction:column;align-items:center;justify-content:center;gap:12px;' +
        'min-height:30vh;color:rgba(255,255,255,.55);text-align:center;padding:0 24px';
      const content = document.querySelector('#photo-lightbox .pl-content');
      (content || document.getElementById('photo-lightbox')).appendChild(ph);
    }
    ph.innerHTML = '<i class="ph ph-film-strip" style="font-size:72px"></i>' +
      '<div style="font-size:14px;word-break:break-all">' + UI._escapeHtml(f.name || f.filename || '') + '</div>' +
      '<div style="font-size:12px;opacity:.7">' + t('lbRawNoPreview') + '</div>';
    ph.style.display = 'flex';
  },

  _hideRawPlaceholder() {
    const ph = document.getElementById('pl-raw-placeholder');
    if (ph) ph.style.display = 'none';
  },

  async _loadImageInfo(f) {
    const el = document.getElementById('pl-exif');
    el.innerHTML = '';
    if (!f || !f.path) return;

    try {
      const r = await window.api.sendCommand('get_image_info', { path: f.path });
      if (!r || r.error) return;

      const parts = [];
      if (r.colorSpace) {
        parts.push('<span title="' + t('lbColorSpace') + '"><i class="ph ph-palette"></i> ' + r.colorSpace + '</span>');
      }
      if (r.width && r.height) {
        parts.push('<span>' + r.width + '×' + r.height + '</span>');
      }
      if (r.exif && Object.keys(r.exif).length > 0) {
        const e = r.exif;
        if (e.cameraModel) parts.push('<span title="' + t('lbCamera') + '"><i class="ph ph-camera"></i> ' + e.cameraModel + '</span>');
        if (e.lensModel) parts.push('<span title="' + t('lbLens') + '">' + e.lensModel + '</span>');
        if (e.focalLength) parts.push('<span>' + e.focalLength + '</span>');
        if (e.aperture) parts.push('<span>' + e.aperture + '</span>');
        if (e.shutter) parts.push('<span>' + e.shutter + '</span>');
        if (e.iso) parts.push('<span>' + e.iso + '</span>');
        if (e.dateTaken) parts.push('<span title="' + t('lbDateTaken') + '"><i class="ph ph-calendar"></i> ' + e.dateTaken.replace('T', ' ').slice(0, 19) + '</span>');
      }
      el.innerHTML = parts.join(' <span style="opacity:.3">|</span> ');
    } catch (_) { /* ignore */ }
  },
};
