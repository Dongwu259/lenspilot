/**
 * toast.js — Shared toast notification system.
 *
 * Usage:
 *   Toast.show('message')
 *   Toast.show('msg', 'success')
 *   Toast.ok('done'), Toast.error('failed')
 */

const Toast = {
  _icons: { success: 'ph-check-circle', error: 'ph-x-circle', warning: 'ph-warning', info: 'ph-info' },

  show(msg, level) {
    level = level || 'info';
    const container = document.getElementById('toast-container');
    if (!container) return;
    const el = document.createElement('div');
    el.className = 'toast toast-' + level;
    const icon = this._icons[level] || this._icons.info;
    el.innerHTML = '<span><i class="ph ' + icon + '"></i></span> ' + msg;
    container.appendChild(el);
    // 退场：先播 toastOut 动画，结束后再移除节点
    setTimeout(() => {
      if (!el.parentNode) return;
      el.classList.add('toast-out');
      setTimeout(() => { if (el.parentNode) el.remove(); }, 240);
    }, 3760);
  },

  ok(msg) { this.show(msg, 'success'); },
  error(msg) { this.show(msg, 'error'); },
  warn(msg) { this.show(msg, 'warning'); },
};
