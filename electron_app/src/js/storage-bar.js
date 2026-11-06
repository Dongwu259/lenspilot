/**
 * storage-bar.js — Shared cloud storage usage bar.
 *
 * Usage:
 *   StorageBar.render({ barId:'el1', textId:'el2', used:5GB, limit:512GB })
 */

const StorageBar = {
  /**
   * Update a storage bar + text label.
   * @param {object} opts
   *   barId   — element id for the progress-fill div
   *   textId  — element id for the text span (optional)
   *   used    — bytes used
   *   limit   — bytes limit (total)
   */
  render(opts) {
    const { barId, textId, used, limit } = opts;
    const usedBytes = used || 0;
    const limitBytes = limit || 2 * 1024 * 1024 * 1024;

    const bar = document.getElementById(barId);
    if (bar) {
      bar.style.width = Math.min(100, (usedBytes / limitBytes * 100)).toFixed(1) + '%';
    }
    if (textId) {
      const text = document.getElementById(textId);
      if (text) {
        text.textContent = Fmt.size(usedBytes) + ' / ' + Fmt.size(limitBytes);
      }
    }
  },

  /** Convenience: render from a subscription object */
  renderSub(opts) {
    const { barId, textId, subscription } = opts;
    const sub = subscription || {};
    this.render({
      barId, textId,
      used: sub.storageUsed || 0,
      limit: sub.storageLimit || 2 * 1024 * 1024 * 1024,
    });
  },
};
