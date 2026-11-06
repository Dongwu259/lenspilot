const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('api', {
  // ── Platform info (for OS-specific UI, e.g. titlebar) ──
  platform: process.platform,   // 'darwin' | 'win32' | 'linux'

  // ── Python Commands ──
  sendCommand: (command, params) => {
    return ipcRenderer.invoke('python-command', command, params);
  },

  // ── Events from Python ──
  onPythonEvent: (callback) => {
    const handler = (_, eventName, data) => callback(eventName, data);
    ipcRenderer.on('python-event', handler);
    return () => ipcRenderer.removeListener('python-event', handler);
  },

  onPythonStatus: (callback) => {
    const handler = (_, status) => callback(status);
    ipcRenderer.on('python-status', handler);
    return () => ipcRenderer.removeListener('python-status', handler);
  },

  // ── Native Dialogs ──
  selectFolder: () => ipcRenderer.invoke('select-folder'),

  selectFiles: (opts) => ipcRenderer.invoke('select-files', opts),

  selectSave: (options) => ipcRenderer.invoke('select-save', options),

  openFolder: (folderPath) => ipcRenderer.invoke('open-folder', folderPath),

  openUrl: (url) => ipcRenderer.invoke('open-url', url),

  showMessage: (message, type) => ipcRenderer.invoke('show-message', message, type),

  showInput: (title, message) => ipcRenderer.invoke('show-input', title, message),

  // ── Window Controls ──
  minimizeWindow: () => ipcRenderer.invoke('window-minimize'),
  maximizeWindow: () => ipcRenderer.invoke('window-maximize'),
  closeWindow: () => ipcRenderer.invoke('window-close'),
  isMaximized: () => ipcRenderer.invoke('window-is-maximized'),
  relaunchApp: () => ipcRenderer.invoke('relaunch-app'),

  getVersion: () => ipcRenderer.invoke('get-version'),
  showItemInFolder: (path) => ipcRenderer.invoke('show-item-in-folder', path),

  // Auto-update: save downloaded installer to temp & launch
  saveTempFile: (data, filename) => ipcRenderer.invoke('save-temp-file', { data, filename }),
  installAndQuit: (filePath) => ipcRenderer.invoke('install-and-quit', filePath),

  // ── Agent conversation history export/import ──
  exportHistory: (content, defaultName) => ipcRenderer.invoke('agent-export-history', { content, defaultName }),
  importHistory: () => ipcRenderer.invoke('agent-import-history'),

  // ── Secure Storage (encrypted localStorage for API keys/tokens) ──
  secureStoreSet: (key, value) => ipcRenderer.invoke('secure-store-set', key, value),
  secureStoreGet: (key) => ipcRenderer.invoke('secure-store-get', key),
  secureStoreDelete: (key) => ipcRenderer.invoke('secure-store-delete', key),
});
