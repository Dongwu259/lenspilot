/**
 * fmt.js — Shared formatting utilities.
 *
 * Usage: Fmt.size(bytes), Fmt.speed(bps), Fmt.duration(seconds), Fmt.date(iso)
 */

const Fmt = {
  /** Format bytes to human-readable size: B → KB → MB → GB → TB */
  size(bytes) {
    if (!bytes || bytes < 0) return '0 B';
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    if (bytes < 1024 * 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
    if (bytes < 1024 * 1024 * 1024 * 1024) return (bytes / (1024 * 1024 * 1024)).toFixed(2) + ' GB';
    return (bytes / (1024 * 1024 * 1024 * 1024)).toFixed(2) + ' TB';
  },

  /** Format bytes-per-second to human-readable speed */
  speed(bps) {
    if (!bps || bps < 0) return '0 B/s';
    if (bps < 1024) return bps.toFixed(1) + ' B/s';
    if (bps < 1024 * 1024) return (bps / 1024).toFixed(1) + ' KB/s';
    return (bps / (1024 * 1024)).toFixed(1) + ' MB/s';
  },

  /** Format seconds to human-readable duration */
  duration(seconds) {
    if (!seconds || seconds < 0) return '--';
    if (seconds < 60) return Math.floor(seconds) + 's';
    if (seconds < 3600) {
      const m = Math.floor(seconds / 60);
      const s = Math.floor(seconds % 60);
      return m + 'm ' + s + 's';
    }
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    return h + 'h ' + m + 'm';
  },

  /** Format ISO date string to locale date */
  date(iso) {
    if (!iso) return '--';
    return new Date(iso).toLocaleDateString('zh-CN');
  },

  /** Format ISO date string to locale datetime */
  datetime(iso) {
    if (!iso) return '--';
    const d = new Date(iso);
    return d.toLocaleDateString('zh-CN') + ' ' + d.toTimeString().slice(0, 8);
  },

  /** Format a percentage (0-1 or 0-100) */
  percent(val) {
    if (val == null) return '--';
    return (val * 100).toFixed(0) + '%';
  },
};
