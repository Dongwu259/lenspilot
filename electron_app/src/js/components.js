/**
 * components.js — Reusable UI component helpers
 */

const UI = {
  /**
   * Add a log entry to a log container.
   */
  addLog(containerId, message, level = 'info') {
    const container = document.getElementById(containerId);
    if (!container) return;

    const now = new Date();
    const time = now.toTimeString().slice(0, 8);

    const entry = document.createElement('div');
    entry.className = 'log-entry';
    entry.innerHTML = `
      <span class="log-time">[${time}]</span>
      <span class="log-msg ${level}">${this._escapeHtml(message)}</span>
    `;
    container.appendChild(entry);
    container.scrollTop = container.scrollHeight;
  },

  /**
   * Clear log container.
   */
  clearLog(containerId) {
    const container = document.getElementById(containerId);
    if (container) container.innerHTML = '';
  },

  /**
   * Update a progress bar. 到 100% 时加 .done 触发一次完成脉冲（回落后移除）。
   */
  updateProgress(fillId, textId, percent, text) {
    const fill = document.getElementById(fillId);
    const label = document.getElementById(textId);
    if (fill) {
      const p = Math.min(100, Math.max(0, percent));
      fill.style.width = p + '%';
      if (p >= 100) fill.classList.add('done');
      else fill.classList.remove('done');
    }
    if (label) label.textContent = text || '';
  },

  /**
   * Set button enabled/disabled state.
   */
  setButton(id, enabled) {
    const btn = document.getElementById(id);
    if (btn) btn.disabled = !enabled;
  },

  /**
   * Format bytes to human-readable size.
   */
  formatSize(bytes) { return Fmt.size(bytes); },
  formatSpeed(bps) { return Fmt.speed(bps); },
  formatETA(seconds) { return Fmt.duration(seconds); },

  _escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  },
};

// ═══════════════════════════════════════════════════════════════════
// Modal Dialog (replaces prompt() in Electron)
// ═══════════════════════════════════════════════════════════════════

// 退场动画：先播 modal-closing，180ms 后再隐藏；重开时取消未完成的关闭
function _animCloseModal(overlay) {
  if (overlay._closeTimer) return;
  overlay.classList.add('modal-closing');
  overlay._closeTimer = setTimeout(() => {
    overlay._closeTimer = null;
    overlay.classList.add('hidden');
    overlay.classList.remove('modal-closing');
  }, 180);
}
function _cancelModalClose(overlay) {
  if (overlay._closeTimer) { clearTimeout(overlay._closeTimer); overlay._closeTimer = null; }
  overlay.classList.remove('modal-closing');
}

const Modal = {
  /**
   * Show a text input dialog.
   * Returns the entered text, or null if cancelled.
   */
  showInput(title, message) {
    return new Promise((resolve) => {
      const overlay = document.getElementById('input-modal');
      const titleEl = document.getElementById('modal-title');
      const messageEl = document.getElementById('modal-message');
      const inputEl = document.getElementById('modal-input');
      const okBtn = document.getElementById('modal-ok');
      const cancelBtn = document.getElementById('modal-cancel');

      titleEl.textContent = title;
      messageEl.textContent = message;
      inputEl.value = '';

      const cleanup = () => {
        _animCloseModal(overlay);
        okBtn.removeEventListener('click', onOk);
        cancelBtn.removeEventListener('click', onCancel);
        inputEl.removeEventListener('keydown', onKey);
      };

      const onOk = () => {
        const value = inputEl.value.trim();
        cleanup();
        resolve(value || null);
      };

      const onCancel = () => {
        cleanup();
        resolve(null);
      };

      const onKey = (e) => {
        if (e.key === 'Enter') onOk();
        if (e.key === 'Escape') onCancel();
      };

      okBtn.addEventListener('click', onOk);
      cancelBtn.addEventListener('click', onCancel);
      inputEl.addEventListener('keydown', onKey);

      _cancelModalClose(overlay);
      overlay.classList.remove('hidden');
      inputEl.focus();
    });
  },

  /**
   * Show a confirmation dialog with custom button text.
   * Returns true for confirm, false for cancel.
   */
  showConfirm(title, message, confirmText, cancelText) {
    if (!confirmText) confirmText = t('ok');
    if (!cancelText) cancelText = t('cancel');
    return new Promise((resolve) => {
      const overlay = document.getElementById('input-modal');
      const titleEl = document.getElementById('modal-title');
      const messageEl = document.getElementById('modal-message');
      const inputEl = document.getElementById('modal-input');
      const okBtn = document.getElementById('modal-ok');
      const cancelBtn = document.getElementById('modal-cancel');

      titleEl.textContent = title;
      messageEl.textContent = message;
      inputEl.classList.add('hidden');
      okBtn.textContent = confirmText;
      cancelBtn.textContent = cancelText;

      const cleanup = () => {
        _animCloseModal(overlay);
        okBtn.removeEventListener('click', onOk);
        cancelBtn.removeEventListener('click', onCancel);
        document.removeEventListener('keydown', onKey);
        inputEl.classList.remove('hidden');
        okBtn.textContent = t('ok');
        cancelBtn.textContent = t('cancel');
        overlay._activeModal = null;
      };

      const onOk = () => { cleanup(); resolve(true); };
      const onCancel = () => { cleanup(); resolve(false); };
      const onKey = (e) => {
        if (e.key === 'Enter') onOk();
        if (e.key === 'Escape') onCancel();
      };

      okBtn.addEventListener('click', onOk);
      cancelBtn.addEventListener('click', onCancel);
      document.addEventListener('keydown', onKey);

      // Prevent overlapping modals
      if (overlay._activeModal && overlay._activeModal !== 'confirm') {
        resolve(false); return;
      }
      overlay._activeModal = 'confirm';
      _cancelModalClose(overlay);
      overlay.classList.remove('hidden');
      okBtn.focus();
    });
  },

  /**
   * Show a message dialog.
   */
  showMessage(message, type) {
    if (typeof App !== 'undefined' && App.toast) {
      App.toast(message, type || 'info');
      return Promise.resolve();
    }
    if (window.api && window.api.showMessage) {
      return window.api.showMessage(message, type);
    }
    return Promise.resolve();
  },

  /**
   * 以动画显示一个 modal-overlay（兼容 .hidden 类和 style.display 两种旧约定）。
   */
  animatedShow(el) {
    if (!el) return;
    if (el._closeTimer) { clearTimeout(el._closeTimer); el._closeTimer = null; }
    el.classList.remove('modal-closing');
    el.classList.remove('hidden');
    if (el.style.display === 'none') el.style.display = 'flex';
  },

  /**
   * 以退场动画隐藏一个 modal-overlay（播完统一加 .hidden，两种约定都生效）。
   */
  animatedHide(el) {
    if (!el || el._closeTimer) return;
    if (el.classList.contains('hidden') || el.style.display === 'none') return;
    el.classList.add('modal-closing');
    el._closeTimer = setTimeout(() => {
      el._closeTimer = null;
      el.classList.remove('modal-closing');
      el.classList.add('hidden');
    }, 180);
  },
};

// ═══════════════════════════════════════════════════════════════════
// Scheduler — shared scheduling logic (used by automation.js & agent.js)
// ═══════════════════════════════════════════════════════════════════

const Scheduler = {
  /**
   * Calculate the next run time for a schedule config.
   * @param {object} schedule - {type:'none'|'hourly'|'custom', custom:{freq,hour,minute,dayOfWeek,multiTimes}}
   * @param {number} [intervalMinutes=30] - for freq='interval'
   * @returns {Date|null}
   */
  calcNextRun(schedule, intervalMinutes) {
    if (!schedule || schedule.type === 'none') return null;
    const now = new Date();
    if (schedule.type === 'hourly') {
      return new Date(now.getFullYear(), now.getMonth(), now.getDate(), now.getHours() + 1, 0, 0);
    }
    if (schedule.type === 'custom') {
      const c = schedule.custom || {};
      if (c.freq === 'interval') {
        const mins = parseInt(intervalMinutes) || 30;
        return new Date(Date.now() + mins * 60000);
      }
      const h = parseInt(c.hour) || 9, m = parseInt(c.minute) || 0;
      if (c.freq === 'daily') {
        const t = new Date(now.getFullYear(), now.getMonth(), now.getDate(), h, m, 0);
        if (t <= now) t.setDate(t.getDate() + 1);
        return t;
      }
      if (c.freq === 'weekly') {
        const day = parseInt(c.dayOfWeek) || 1;
        const t = new Date(now.getFullYear(), now.getMonth(), now.getDate(), h, m, 0);
        const jsDay = t.getDay();
        let target = day % 7;
        let diff = target - jsDay;
        if (diff < 0 || (diff === 0 && t <= now)) diff += 7;
        t.setDate(t.getDate() + diff);
        return t;
      }
      if (c.freq === 'multiDaily') {
        const times = (c.multiTimes || []).filter(Boolean);
        if (!times.length) return null;
        const sorted = [...times].sort((a, b) => {
          const [ah, am] = a.split(':').map(Number);
          const [bh, bm] = b.split(':').map(Number);
          return (ah * 60 + am) - (bh * 60 + bm);
        });
        const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
        for (const ts of sorted) {
          const [th, tm] = ts.split(':').map(Number);
          const t = new Date(today.getFullYear(), today.getMonth(), today.getDate(), th, tm, 0);
          if (t > now) return t;
        }
        const [th, tm] = sorted[0].split(':').map(Number);
        return new Date(today.getFullYear(), today.getMonth(), today.getDate() + 1, th, tm, 0);
      }
    }
    return null;
  },

  /**
   * Format a schedule into a human-readable string.
   */
  fmtSchedule(schedule, intervalMinutes) {
    if (!schedule || schedule.type === 'none') return t('schedNoSchedule');
    if (schedule.type === 'hourly') return t('schedHourly');
    const c = schedule.custom || {};
    const n = intervalMinutes || 30;
    if (c.freq === 'interval') return t('schedEveryNMin').replace('N', n);
    const hh = String(c.hour != null ? c.hour : 9).padStart(2, '0');
    const mm = String(c.minute != null ? c.minute : 0).padStart(2, '0');
    if (c.freq === 'daily') return `${t('freqDaily')} ${hh}:${mm}`;
    if (c.freq === 'weekly') {
      const days = ['', t('dayMon'), t('dayTue'), t('dayWed'), t('dayThu'), t('dayFri'), t('daySat'), t('daySun')];
      return `${t('freqWeekly')}${days[parseInt(c.dayOfWeek) || 1]} ${hh}:${mm}`;
    }
    if (c.freq === 'multiDaily') return t('schedMulti');
    return t('schedNoSchedule');
  },
};
