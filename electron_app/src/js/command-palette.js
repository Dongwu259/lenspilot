/**
 * command-palette.js — global command palette (Ctrl+K)
 *
 * One entry point for pages, workbench and the open editor. This is the first
 * step toward the command-centric interaction layer: the same action is reachable
 * from the keyboard without hunting through pages/panels.
 */
const CommandPalette = {
  _bound: false,
  _items: [],
  _sel: 0,

  init() {
    if (this._bound) return;
    this._bound = true;
    document.getElementById('btn-command-palette')?.addEventListener('click', () => this.open());
    document.querySelector('#command-palette .cmd-backdrop')?.addEventListener('click', () => this.close());
    const input = document.getElementById('cmd-input');
    if (input) {
      input.addEventListener('input', () => this._render());
      input.addEventListener('keydown', (e) => {
        const paletteKey = (typeof Keymap !== 'undefined')
          ? Keymap.is(e, 'commandPalette')
          : ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k');
        if (paletteKey) { e.preventDefault(); this.close(); }
        else if (e.key === 'ArrowDown') { e.preventDefault(); this._move(1); }
        else if (e.key === 'ArrowUp') { e.preventDefault(); this._move(-1); }
        else if (e.key === 'Enter') { e.preventDefault(); this._execute(this._sel); }
        else if (e.key === 'Escape') { e.preventDefault(); this.close(); }
      });
    }
    document.addEventListener('keydown', (e) => {
      const hit = (typeof Keymap !== 'undefined')
        ? Keymap.is(e, 'commandPalette')
        : ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k');
      if (!hit) return;
      const tag = (e.target && e.target.tagName) || '';
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
      e.preventDefault();
      if (!document.getElementById('command-palette').classList.contains('hidden')) this.close();
      else this.open();
    });
  },

  open() {
    const overlay = document.getElementById('command-palette');
    if (!overlay) return;
    overlay.classList.remove('hidden');
    const input = document.getElementById('cmd-input');
    if (input) { input.value = ''; setTimeout(() => input.focus(), 20); }
    this._sel = 0;
    this._render();
  },

  close() {
    document.getElementById('command-palette')?.classList.add('hidden');
  },

  _build() {
    const items = [];
    const add = (id, label, icon, run, hint) => items.push({ id, label, icon, run, hint: hint || '' });
    const page = (id, labelKey, icon) => add('page:' + id, t(labelKey), icon, () => {
      if (typeof App !== 'undefined') App.setPage(id);
    });

    page('dashboard', 'dashHome', 'ph-house');
    page('copy-card', 'dashCopy', 'ph-floppy-disk');
    page('tethered', 'dashTether', 'ph-camera');
    page('photo-select', 'dashAIScore', 'ph-brain');
    page('file-browser', 'dashFiles', 'ph-folder-open');
    page('photo-edit', 'navPhotoEdit', 'ph-sliders-horizontal');
    page('editor-workspace', 'navEditorWorkspace', 'ph-layout');
    page('batch-export', 'navBatchExport', 'ph-export');
    page('cloud', 'dashCloud', 'ph-cloud');
    page('agent', 'dashAgent', 'ph-sparkle');
    page('pipeline', 'dashPipeline', 'ph-user-circle-gear');
    page('automation', 'dashAuto', 'ph-lightning');
    page('settings', 'dashSettings', 'ph-gear');

    if (typeof Workbench !== 'undefined') {
      add('wb:open', t('navPhotoEdit'), 'ph-stack', () => {
        if (App) App.setPage('photo-edit');
      }, Workbench.count().photos + ' photos');
      add('wb:all', t('pepSelAll'), 'ph-check-square', () => Workbench.selectAll(), Workbench.count().selected + ' selected');
      add('wb:clear-sel', t('pepSelClear'), 'ph-x-square', () => Workbench.clearSelection());
      add('wb:clear', t('pepWbClear'), 'ph-trash', () => {
        if (typeof PhotoEditPage !== 'undefined' && PhotoEditPage.clearWorkbench) PhotoEditPage.clearWorkbench();
        else Workbench.clear();
      });
      const active = Workbench.getActive();
      if (active) {
        add('wb:edit-active', t('edit'), 'ph-sliders', () => {
          if (typeof PhotoEdit !== 'undefined') PhotoEdit.open(active, { fileList: Workbench.getPhotos() });
        }, active.split(/[\\/]/).pop());
      }
    }

    if (typeof Activity !== 'undefined' && Activity.tasks.length) {
      add('act:open', t('actTitle'), 'ph-pulse', () => Activity.toggle(),
        t('actRunningN', { count: Activity.running().length }));
    }

    const editor = document.getElementById('photo-edit-overlay');
    if (editor && !editor.classList.contains('hidden') && typeof PhotoEdit !== 'undefined') {
      const keyHint = (id) => (typeof Keymap !== 'undefined') ? Keymap.getLabel(id) : '';
      const tool = (id, key, icon, km) => add('ed:' + id, t(key), icon, () => PhotoEdit._setTool(id), keyHint(km));
      tool('edit', 'peToolEdit', 'ph-sliders', 'toolEdit');
      tool('crop', 'peToolCrop', 'ph-crop', 'toolCrop');
      tool('mask', 'peToolMask', 'ph-mask-sad', 'toolMask');
      tool('spot', 'peToolSpot', 'ph-eraser', 'toolSpot');
      tool('history', 'peToolHistory', 'ph-clock-counter-clockwise', 'toolHistory');
      tool('version', 'peToolVersion', 'ph-bookmark-simple', 'toolVersion');
      add('ed:undo', t('peUndo'), 'ph-arrow-u-up-left', () => PhotoEdit._undo(), keyHint('undo'));
      add('ed:redo', t('peRedo'), 'ph-arrow-u-up-right', () => PhotoEdit._redo(), keyHint('redo'));
      add('ed:save', t('peSave'), 'ph-check', () => PhotoEdit._save());
      add('ed:export', t('peExport'), 'ph-download-simple', () => PhotoEdit._export());
      add('ed:close', t('close'), 'ph-x', () => PhotoEdit.close(), keyHint('close'));
    }
    return items;
  },

  _render() {
    const input = document.getElementById('cmd-input');
    const q = (input ? input.value : '').trim().toLowerCase();
    const all = this._build();
    this._items = !q ? all : all.filter(it =>
      (it.label || '').toLowerCase().includes(q) ||
      (it.id || '').toLowerCase().includes(q) ||
      (it.hint || '').toLowerCase().includes(q));
    if (this._sel >= this._items.length) this._sel = Math.max(0, this._items.length - 1);
    const list = document.getElementById('cmd-list');
    if (!list) return;
    if (!this._items.length) {
      list.innerHTML = `<div class="cmd-empty">${t('cmdNoResults')}</div>`;
      return;
    }
    list.innerHTML = this._items.map((it, i) =>
      `<button class="cmd-item${i === this._sel ? ' active' : ''}" data-i="${i}">
        <i class="ph ${it.icon || 'ph-command'}"></i>
        <span class="cmd-label">${this._esc(it.label)}</span>
        ${it.hint ? `<span class="cmd-hint">${this._esc(it.hint)}</span>` : ''}
      </button>`).join('');
    list.querySelectorAll('.cmd-item').forEach(el => {
      el.addEventListener('click', () => this._execute(parseInt(el.dataset.i, 10)));
    });
    const active = list.querySelector('.cmd-item.active');
    if (active) active.scrollIntoView({ block: 'nearest' });
  },

  _move(d) {
    if (!this._items.length) return;
    this._sel = (this._sel + d + this._items.length) % this._items.length;
    this._render();
  },

  _execute(i) {
    const item = this._items[i];
    if (!item) return;
    this.close();
    try { item.run(); } catch (e) {
      if (typeof App !== 'undefined' && App.toast) App.toast(String(e.message || e), 'error');
    }
  },

  _esc(s) {
    const d = document.createElement('div');
    d.textContent = String(s ?? '');
    return d.innerHTML;
  },
};
