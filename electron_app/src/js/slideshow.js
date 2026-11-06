/**
 * slideshow.js — Fullscreen photo slideshow
 *
 * Usage: Slideshow.start(photos, {interval: 3, transition: 'fade'})
 *   photos: [{path, filename, score, thumb_path}]
 *   keyboard: ← → navigate, Space pause/resume, Escape exit
 */

const Slideshow = {
  _photos: [],
  _index: 0,
  _timer: null,
  _running: false,
  _interval: 3,       // seconds between slides
  _loop: true,
  _hideTimer: null,

  start(photos, opts = {}) {
    if (!photos || !photos.length) { App.toast(t('ssNoPhotos'), 'warning'); return; }
    this._photos = photos;
    this._index = 0;
    this._running = true;
    this._interval = opts.interval || 3;
    this._loop = opts.loop !== false;

    this._showOverlay();
    this._render();
    this._armTimer();
  },

  _showOverlay() {
    const ov = document.getElementById('slideshow-overlay');
    ov.classList.remove('hidden');
    ov.addEventListener('mousemove', this._onMove);
    document.addEventListener('keydown', this._onKey);
    document.body.style.overflow = 'hidden';
    this._showControls(true);
  },

  _hideOverlay() {
    const ov = document.getElementById('slideshow-overlay');
    ov.classList.add('hidden');
    ov.removeEventListener('mousemove', this._onMove);
    document.removeEventListener('keydown', this._onKey);
    document.body.style.overflow = '';
    this.stop();
  },

  // ── Event handlers (arrow functions for stable listener references) ──

  _onKey: (e) => {
    if (typeof Keymap !== 'undefined') {
      if (Keymap.is(e, 'ssClose')) Slideshow._hideOverlay();
      else if (Keymap.is(e, 'ssPrev')) Slideshow.prev();
      else if (Keymap.is(e, 'ssNext')) Slideshow.next();
      else if (Keymap.is(e, 'ssPlayPause')) { e.preventDefault(); Slideshow.toggle(); }
      return;
    }
    if (e.key === 'Escape') Slideshow._hideOverlay();
    else if (e.key === 'ArrowLeft') Slideshow.prev();
    else if (e.key === 'ArrowRight') Slideshow.next();
    else if (e.key === ' ') { e.preventDefault(); Slideshow.toggle(); }
  },

  _onMove: () => {
    Slideshow._showControls(true);
    clearTimeout(Slideshow._hideTimer);
    Slideshow._hideTimer = setTimeout(() => Slideshow._showControls(false), 3000);
  },

  _showControls(show) {
    const ctrl = document.getElementById('ss-controls');
    if (ctrl) ctrl.classList.toggle('ss-hidden', !show);
  },

  _render() {
    const p = this._photos[this._index];
    if (!p) return;
    const img = document.getElementById('ss-img');
    // Fade transition
    img.style.opacity = '0';
    setTimeout(() => {
      img.src = PhotoSelect.thumbUrl(p.path || p.thumb_path);
      img.onload = () => { img.style.opacity = '1'; };
      img.onerror = () => { img.style.opacity = '1'; }; // show broken-image fallback
    }, 200);

    document.getElementById('ss-name').textContent = p.filename || '';
    const scoreEl = document.getElementById('ss-score');
    if (p.score >= 0) {
      scoreEl.textContent = t('ssScore', { score: p.score.toFixed(1) });
      scoreEl.className = 'score-badge ' + (PhotoSelect.scoreClass ? PhotoSelect.scoreClass(p.score) : '');
      scoreEl.classList.remove('hidden');
    } else {
      scoreEl.classList.add('hidden');
    }
    document.getElementById('ss-counter').textContent = `${this._index + 1} / ${this._photos.length}`;
  },

  _armTimer() {
    this._disarm();
    if (!this._running) return;
    this._timer = setTimeout(() => {
      if (this._index >= this._photos.length - 1) {
        if (this._loop) { this._index = 0; this._render(); this._armTimer(); }
        else this._hideOverlay();
      } else {
        this._index++;
        this._render();
        this._armTimer();
      }
    }, this._interval * 1000);
  },

  _disarm() {
    if (this._timer) { clearTimeout(this._timer); this._timer = null; }
  },

  // ── Public controls ──

  next() {
    if (!this._photos.length) return;
    this._index = (this._index + 1) % this._photos.length;
    this._render();
    if (this._running) { this._disarm(); this._armTimer(); }
  },

  prev() {
    if (!this._photos.length) return;
    this._index = (this._index - 1 + this._photos.length) % this._photos.length;
    this._render();
    if (this._running) { this._disarm(); this._armTimer(); }
  },

  toggle() {
    this._running = !this._running;
    document.getElementById('ss-play-icon').className =
      'ph ' + (this._running ? 'ph-pause' : 'ph-play');
    if (this._running) this._armTimer(); else this._disarm();
  },

  stop() {
    this._running = false;
    this._disarm();
    clearTimeout(this._hideTimer);
  },

  // ── Init (called once from photo-select) ──

  _initDone: false,
  init() {
    if (this._initDone) return;
    this._initDone = true;

    const $ = id => document.getElementById(id);

    // Control buttons
    $('ss-prev').addEventListener('click', () => this.prev());
    $('ss-next').addEventListener('click', () => this.next());
    $('ss-play').addEventListener('click', () => this.toggle());
    $('ss-close').addEventListener('click', () => this._hideOverlay());

    // Interval toggle: cycle 2s → 3s → 5s → 10s → 2s
    const intervals = [2, 3, 5, 10];
    let intIdx = intervals.indexOf(this._interval);
    $('ss-interval').addEventListener('click', () => {
      intIdx = (intIdx + 1) % intervals.length;
      this._interval = intervals[intIdx];
      $('ss-interval').textContent = this._interval + 's';
      if (this._running) { this._disarm(); this._armTimer(); }
    });

    // Click on image → next
    $('ss-img').addEventListener('click', () => this.next());
  },
};
