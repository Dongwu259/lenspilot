/**
 * photo-dedup.js — 智能查重面板（选片页「查重」按钮入口）
 *
 * 两种模式：
 *  - burst  连拍优选：dedup_burst dry_run=true 预览分组，后端按 美学分+锐度+睁眼
 *            推荐每组最佳一张；确认后可 dry_run=false 把未选中移入 _duplicates/。
 *  - similar 相似照片：deep_dedup（pHash 粗筛 + 深度特征精判），对当前结果列表
 *            （无结果则退化为整个文件夹的 paths）做纯相似分组，无推荐。
 *
 * 每组恰好一张「保留」：burst 默认用后端推荐，similar 默认取已评分最高者，
 * 点击组内任意缩略图即可切换。「标记结果」把保留/排除写进 set_manual_keep，
 * 与选片页阈值过滤、XMP 导出链路完全联动。
 */

const PhotoDedup = {
  mode: 'burst',        // 'burst' | 'similar'
  folder: '',
  running: false,
  groups: [],           // [{id, paths:[fullpath], keeper, recKeeper|null, thumbs:{path:thumb}}]
  deepUsed: null,       // similar 模式: 深度特征是否启用（null=非 similar 结果）
  _runId: 0,            // 递增使迟到结果被忽略（面板关闭/重跑）
  _elapsedTimer: null,
  _elapsedStart: 0,

  // ── 面板开关 ──

  open() {
    const folder = (typeof PhotoSelect !== 'undefined' && PhotoSelect.sourceFolder)
      || (document.getElementById('select-folder').value || '').trim();
    if (!folder) { App.toast(t('ddNeedFolder'), 'warning'); return; }
    if (typeof PhotoSelect !== 'undefined' && PhotoSelect.processing) {
      App.toast(t('psScoringWait'), 'warning'); return;
    }
    this.folder = folder;
    this.groups = [];
    this.deepUsed = null;
    this._runId++;
    this._stopElapsed();
    this.running = false;
    this.setMode(this.mode);          // 刷新 tab/参数区/说明文案
    this._renderIdle();
    Modal.animatedShow(document.getElementById('dedup-panel'));
  },

  close() {
    const wasRunning = this.running;
    this._runId++;                    // 使进行中的调用结果失效
    this.running = false;
    this._stopElapsed();
    Modal.animatedHide(document.getElementById('dedup-panel'));
    if (wasRunning) App.toast(t('ddClosedRunning'), 'info');
  },

  isOpen() {
    const el = document.getElementById('dedup-panel');
    return el && !el.classList.contains('hidden');
  },

  setMode(mode) {
    if (this.running) return;
    this.mode = mode === 'similar' ? 'similar' : 'burst';
    document.getElementById('dd-tab-burst').classList.toggle('active', this.mode === 'burst');
    document.getElementById('dd-tab-similar').classList.toggle('active', this.mode === 'similar');
    document.getElementById('dd-param-window').classList.toggle('hidden', this.mode !== 'burst');
    document.getElementById('dd-param-threshold').classList.toggle('hidden', this.mode !== 'similar');
    document.getElementById('dd-mode-desc').textContent =
      t(this.mode === 'burst' ? 'ddModeBurstDesc' : 'ddModeSimilarDesc');
    // 切模式后旧结果属于另一种模式，直接清掉避免误操作
    this.groups = [];
    this.deepUsed = null;
    if (this.isOpen()) this._renderIdle();
  },

  bindEvents() {
    document.getElementById('dd-close').addEventListener('click', () => this.close());
    document.getElementById('dd-tab-burst').addEventListener('click', () => this.setMode('burst'));
    document.getElementById('dd-tab-similar').addEventListener('click', () => this.setMode('similar'));
    document.getElementById('dd-run').addEventListener('click', () => this.run());
    document.getElementById('dd-apply-marks').addEventListener('click', () => this.applyMarks());
    document.getElementById('dd-apply-move').addEventListener('click', () => this.applyMove());
    const win = document.getElementById('dd-time-window');
    win.addEventListener('input', () => {
      document.getElementById('dd-time-window-val').textContent = win.value + 's';
    });
    const th = document.getElementById('dd-sim-threshold');
    th.addEventListener('input', () => {
      document.getElementById('dd-sim-threshold-val').textContent = parseFloat(th.value).toFixed(2);
    });
    // Esc 关闭（不干扰选片页 lightbox 的键盘流：仅当面板打开时）
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && this.isOpen()
          && document.getElementById('ps-lightbox').classList.contains('hidden')) {
        this.close();
      }
    });
  },

  // ── 运行 ──

  async run() {
    if (this.running || !this.folder) return;
    this.groups = [];
    this.deepUsed = null;
    this._renderGroups();             // 清空旧结果
    const runId = ++this._runId;
    this._setRunning(true);
    try {
      if (this.mode === 'burst') await this._runBurst(runId);
      else await this._runSimilar(runId);
    } catch (e) {
      if (runId === this._runId) {
        App.toast(t('ddRunFail', { msg: e.message }), 'error');
        this._renderIdle();
      }
    } finally {
      if (runId === this._runId) this._setRunning(false);
    }
  },

  async _runBurst(runId) {
    const timeWindow = parseInt(document.getElementById('dd-time-window').value, 10) || 3;
    const r = await window.api.sendCommand('dedup_burst', {
      folder: this.folder, time_window: timeWindow, dry_run: true,
    });
    if (runId !== this._runId) return;   // 面板已关/已重跑 → 丢弃迟到结果
    if (r && r.skipped === 'no_cached_thumbnails') {
      App.toast(t('ddBurstSkipped'), 'warning');
      this._renderIdle();
      return;
    }
    const details = (r && r.group_details) || [];
    this.groups = details.map((g, i) => {
      // moved_paths 为后端补的全路径；老后端无此字段时退化为 folder+basename
      // （find_images 递归，子目录文件会错位 —— 仅兼容兜底，新后端不会走到）
      const movedPaths = g.moved_paths
        || (g.moved || []).map(name => this.folder.replace(/[\\/]+$/, '') + '/' + name);
      const keeper = g.kept_path;
      return {
        id: i,
        paths: [keeper, ...movedPaths].sort(),
        keeper,
        recKeeper: keeper,
        thumbs: {},
      };
    });
    await this._fillThumbs(runId);
    if (runId !== this._runId) return;
    this._renderGroups();
  },

  async _runSimilar(runId) {
    const threshold = parseFloat(document.getElementById('dd-sim-threshold').value) || 0.95;
    const paths = await this._collectPaths();
    if (runId !== this._runId) return;
    if (paths.length < 2) {
      App.toast(t('ddTooFew'), 'warning');
      this._renderIdle();
      return;
    }
    const r = await window.api.sendCommand('deep_dedup', {
      paths, similarity_threshold: threshold,
    });
    if (runId !== this._runId) return;
    this.deepUsed = !!(r && r.deep_features_used);
    // 默认保留项：已在选片页评分的取分最高者，否则按路径序第一张
    const scoreOf = {};
    (typeof PhotoSelect !== 'undefined' ? PhotoSelect.results || [] : []).forEach(x => {
      scoreOf[x.path] = x.score;
    });
    this.groups = ((r && r.groups) || []).map((gp, i) => {
      const sorted = gp.slice().sort();
      let keeper = sorted[0];
      let best = -Infinity;
      sorted.forEach(p => {
        const s = scoreOf[p];
        if (typeof s === 'number' && s >= 0 && s > best) { best = s; keeper = p; }
      });
      return { id: i, paths: sorted, keeper, recKeeper: null, thumbs: {} };
    });
    await this._fillThumbs(runId);
    if (runId !== this._runId) return;
    this._renderGroups();
  },

  // 相似模式的路径来源：优先当前评分结果列表，否则列出整个文件夹
  async _collectPaths() {
    const rs = (typeof PhotoSelect !== 'undefined' && PhotoSelect.results) || [];
    if (rs.length) return rs.map(x => x.path);
    const r = await window.api.sendCommand('list_images_in_folder', { folder: this.folder });
    return ((r && r.images) || []).map(f => (typeof f === 'string' ? f : f.path));
  },

  // 缩略图：先白嫖选页已生成的 thumb_path，缺的批量 get_thumbnails 补齐
  async _fillThumbs(runId) {
    const byPath = {};
    (typeof PhotoSelect !== 'undefined' && PhotoSelect.results || []).forEach(x => {
      if (x.thumb_path) byPath[x.path] = x.thumb_path;
    });
    const missing = [];
    this.groups.forEach(g => g.paths.forEach(p => {
      if (byPath[p]) g.thumbs[p] = byPath[p];
      else missing.push(p);
    }));
    // 先渲染已有缩略图，缺失的后补（大文件夹下 RAW 解码慢，避免干等）
    if (runId === this._runId) this._renderGroups();
    if (!missing.length) return;
    try {
      const r = await window.api.sendCommand('get_thumbnails', { paths: missing });
      if (runId !== this._runId) return;
      const thumbs = (r && r.thumbs) || {};
      this.groups.forEach(g => g.paths.forEach(p => {
        if (thumbs[p]) g.thumbs[p] = thumbs[p];
      }));
      this._renderGroups();
    } catch (e) { /* 缩略图失败不阻塞分组展示（占位图） */ }
  },

  // ── 渲染 ──

  _renderIdle() {
    document.getElementById('dd-empty').classList.remove('hidden');
    document.getElementById('dd-loading').classList.add('hidden');
    document.getElementById('dd-groups').innerHTML = '';
    this._renderSummary();
  },

  _setRunning(on) {
    this.running = on;
    document.getElementById('dd-run').disabled = on;
    document.getElementById('dd-tab-burst').disabled = on;
    document.getElementById('dd-tab-similar').disabled = on;
    document.getElementById('dd-empty').classList.add('hidden');
    document.getElementById('dd-loading').classList.toggle('hidden', !on);
    document.getElementById('dd-apply-marks').disabled = true;
    document.getElementById('dd-apply-move').disabled = true;
    if (on) {
      this._elapsedStart = Date.now();
      document.getElementById('dd-elapsed').textContent = '0s';
      this._elapsedTimer = setInterval(() => {
        const s = Math.floor((Date.now() - this._elapsedStart) / 1000);
        const el = document.getElementById('dd-elapsed');
        if (el) el.textContent = s + 's';
      }, 1000);
    } else {
      this._stopElapsed();
      document.getElementById('dd-loading').classList.add('hidden');
    }
    this._renderSummary();
  },

  _stopElapsed() {
    if (this._elapsedTimer) { clearInterval(this._elapsedTimer); this._elapsedTimer = null; }
  },

  _counts() {
    const n = this.groups.length;
    let m = 0;
    this.groups.forEach(g => { m += g.paths.length; });
    return { groups: n, photos: m, dups: m - n };
  },

  _renderSummary() {
    const c = this._counts();
    const el = document.getElementById('dd-summary');
    if (!c.groups) { el.textContent = ''; }
    else {
      el.textContent = t('ddSummary', { groups: c.groups, photos: c.photos, dups: c.dups })
        + (this.deepUsed === true ? ' · ' + t('ddDeepUsed')
          : this.deepUsed === false ? ' · ' + t('ddDeepNotUsed') : '');
    }
    const has = c.groups > 0 && !this.running;
    document.getElementById('dd-apply-marks').disabled = !has;
    document.getElementById('dd-apply-move').disabled = !has || this.mode !== 'burst';
  },

  _renderGroups() {
    const wrap = document.getElementById('dd-groups');
    wrap.innerHTML = '';
    const has = this.groups.length > 0;
    document.getElementById('dd-empty').classList.toggle('hidden', has || this.running);
    if (!has) { this._renderSummary(); return; }
    const frag = document.createDocumentFragment();
    this.groups.forEach((g, gi) => {
      const card = document.createElement('div');
      card.className = 'dd-group';

      const head = document.createElement('div');
      head.className = 'dd-group-head';
      head.textContent = t('ddGroupTitle', { n: gi + 1, size: g.paths.length });
      card.appendChild(head);

      const strip = document.createElement('div');
      strip.className = 'dd-strip';
      g.paths.forEach(p => {
        const name = p.split(/[\\/]/).pop();
        const item = document.createElement('div');
        item.className = 'dd-item' + (p === g.keeper ? ' keeper' : '');
        item.title = t('ddKeepThis');
        item.dataset.path = p;

        const imgWrap = document.createElement('div');
        imgWrap.className = 'dd-thumb';
        if (g.thumbs[p]) {
          const img = document.createElement('img');
          img.src = 'local-image://x/?p=' + encodeURIComponent(g.thumbs[p]);
          img.loading = 'lazy';
          imgWrap.appendChild(img);
        } else {
          const ph = document.createElement('div');
          ph.className = 'dd-thumb-ph';
          ph.innerHTML = '<i class="ph ph-image"></i>';
          imgWrap.appendChild(ph);
        }
        if (p === g.keeper) {
          const badge = document.createElement('div');
          badge.className = 'dd-keeper-badge';
          badge.innerHTML = '<i class="ph ph-check"></i>';
          imgWrap.appendChild(badge);
        }
        if (p === g.recKeeper) {
          const rec = document.createElement('div');
          rec.className = 'dd-rec-badge';
          rec.textContent = t('ddRecommended');
          imgWrap.appendChild(rec);
        }
        item.appendChild(imgWrap);

        const label = document.createElement('div');
        label.className = 'dd-name';
        label.textContent = name;
        label.title = name;
        item.appendChild(label);

        item.addEventListener('click', () => this.pickKeeper(g.id, p));
        strip.appendChild(item);
      });
      card.appendChild(strip);
      frag.appendChild(card);
    });
    wrap.appendChild(frag);
    this._renderSummary();
  },

  pickKeeper(gid, path) {
    if (this.running) return;
    const g = this.groups.find(x => x.id === gid);
    if (!g || g.keeper === path) return;
    g.keeper = path;
    this._renderGroups();
  },

  // ── 动作 a：标记结果（set_manual_keep 联动选片页） ──

  async applyMarks() {
    if (!this.groups.length || this.running) return;
    const markMap = new Map();   // path -> keep
    this.groups.forEach(g => g.paths.forEach(p => markMap.set(p, p === g.keeper ? 1 : -1)));
    await this._applyMarkList(markMap, { pushUndo: true });
    const c = this._counts();
    App.toast(t('ddMarksDone', { kept: c.groups, rejected: c.dups }), 'success');
    this.close();
  },

  // 批量 set_manual_keep + 写回 PhotoSelect.results + 重绘网格
  async _applyMarkList(markMap, { pushUndo = false } = {}) {
    const results = (typeof PhotoSelect !== 'undefined' && PhotoSelect.results) || [];
    const prev = [];
    results.forEach(r => { if (markMap.has(r.path)) prev.push({ path: r.path, keep: r.manual_keep || 0 }); });
    // 后端逐张落库（IPC 串行处理，这里并发发出即可）
    await Promise.all([...markMap].map(([path, keep]) =>
      window.api.sendCommand('set_manual_keep', { path, keep }).catch(() => {})
    ));
    // 前端视图同步：manual_keep 驱动阈值过滤/选中态/XMP 导出
    results.forEach(r => { if (markMap.has(r.path)) r.manual_keep = markMap.get(r.path); });
    if (typeof PhotoSelect !== 'undefined' && results.length) {
      PhotoSelect.recomputeSelection();
      PhotoSelect.renderGrid();
    }
    if (pushUndo && prev.length && typeof App !== 'undefined' && App.pushUndo) {
      const prevMap = new Map(prev.map(x => [x.path, x.keep]));
      App.pushUndo(t('ddMarksUndo', { count: prev.length }), async () => {
        await Promise.all([...prevMap].map(([path, keep]) =>
          window.api.sendCommand('set_manual_keep', { path, keep }).catch(() => {})
        ));
        const rs = (typeof PhotoSelect !== 'undefined' && PhotoSelect.results) || [];
        rs.forEach(r => { if (prevMap.has(r.path)) r.manual_keep = prevMap.get(r.path); });
        if (typeof PhotoSelect !== 'undefined' && rs.length) {
          PhotoSelect.recomputeSelection();
          PhotoSelect.renderGrid();
        }
      });
    }
  },

  // ── 动作 b：移动未选中到 _duplicates（仅连拍模式） ──

  async applyMove() {
    if (this.mode !== 'burst' || !this.groups.length || this.running) return;
    const c = this._counts();
    const overrides = this.groups.filter(g => g.recKeeper && g.keeper !== g.recKeeper).length;
    // 移动操作由后端按算法推荐重新分组执行；用户改动的保留项只影响「标记结果」
    let msg = t('ddMoveConfirmMsg', { count: c.dups });
    if (overrides) msg += '\n' + t('ddMoveOverrideNote', { count: overrides });
    const ok = await Modal.showConfirm(t('ddMoveConfirmTitle'), msg, t('ddMoveConfirmBtn'), t('cancel'));
    if (!ok) return;

    const runId = ++this._runId;
    this._setRunning(true);
    try {
      const timeWindow = parseInt(document.getElementById('dd-time-window').value, 10) || 3;
      const r = await window.api.sendCommand('dedup_burst', {
        folder: this.folder, time_window: timeWindow, dry_run: false,
      });
      // 注意：即使面板中途被关（runId 失效），文件已被移动——结果仍要落到网格与提示
      const moved = new Set();
      ((r && r.group_details) || []).forEach(g => (g.moved_paths || []).forEach(p => moved.add(p)));
      if (moved.size && typeof PhotoSelect !== 'undefined' && PhotoSelect.results) {
        // 已移走的照片从选片结果中剔除，避免对不存在的路径继续标记/导出
        PhotoSelect.results = PhotoSelect.results.filter(x => !moved.has(x.path));
        PhotoSelect.recomputeSelection();
        PhotoSelect.renderGrid();
        PhotoSelect.refreshModelInfo();
      }
      App.toast(t('ddMoveDone', { count: (r && r.duplicates) || 0 }), 'success');
      if (runId === this._runId) {
        this.groups = [];
        this._setRunning(false);   // 先于 close()，避免误报「结果将被忽略」
        this._renderIdle();
        this.close();
      }
    } catch (e) {
      App.toast(t('ddRunFail', { msg: e.message }), 'error');
    } finally {
      if (runId === this._runId) this._setRunning(false);
      else this._stopElapsed();
    }
  },
};
