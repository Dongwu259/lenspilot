/* tethered.js - 联机拍摄页 (0.8.6 M1)
 *
 * 独立页 page-tethered(摄影工作流 → 联机拍摄)。会话卡 + 实时叠层 + 教练卡。
 * 复用 folder_watcher 的 watch_* 协议:收到 watch_file_received 触发 score_files
 * (镜像 copy-card.js 的 realtime 流),收到 watch_score_progress 更新叠层。
 * shooting_coach 在后端订阅同一 watch_score_progress,在分数掉落/每 10 张时
 * 触发 coach_advise -> coach_advice 事件 -> 本模块渲染教练卡。
 *
 * 动画复用 .page.active/pageIn(style.css),组件复用 .card/.page-inner/.badge/.log-view。
 * M1 叠层为文本(无缩略图);缩略图/直方图渲染留 M2。
 */
const Tethered = {
  _active: false,
  _bound: false,
  _shots: [],          // [{path, name, score}]
  _session: null,

  isActive() { return this._active; },

  onPageEnter() {
    if (!this._bound) this._bind();
    this._refreshStatus();
    this._refreshRefStats();
    // 恢复切页时被暂停的实时参数轮询(仅当实时取景仍在进行)
    if (this._settingsPollPaused) {
      this._settingsPollPaused = false;
      const lvStop = document.getElementById('btn-tether-liveview-stop');
      if (lvStop && !lvStop.disabled && !this._settingsTimer) this._startSettingsPolling();
    }
  },

  _bind() {
    this._bound = true;
    document.getElementById('btn-tether-browse').addEventListener('click', async () => {
      const f = await window.api.selectFolder();
      if (f) document.getElementById('tether-folder').value = f;
    });
    document.getElementById('btn-tether-start').addEventListener('click', () => this.start());
    document.getElementById('btn-tether-stop').addEventListener('click', () => this.stop());
    document.getElementById('btn-tether-coach').addEventListener('click', () => this._manualCoach());
    document.getElementById('tether-coach-freq').addEventListener('change', () => this._onCoachFreqChange());
    document.getElementById('btn-tether-liveview-start').addEventListener('click', () => this._startLiveview());
    document.getElementById('btn-tether-liveview-stop').addEventListener('click', () => this._stopLiveview());
    document.getElementById('btn-tether-capture').addEventListener('click', () => this._capture());
    document.getElementById('btn-ref-import').addEventListener('click', () => this._importRefs());
    document.getElementById('btn-ref-clear').addEventListener('click', () => this._clearRefs());
    // 点击空白处收起参数下拉
    document.addEventListener('click', () => this._ddCloseAll());
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') this._ddCloseAll(); });
    // 滚动/缩放窗口时收起(fixed 定位不跟随容器滚动); 但列表内部滚动不收起
    window.addEventListener('scroll', (e) => {
      if (e.target && e.target.closest && e.target.closest('.tether-dd-list')) return;
      this._ddCloseAll();
    }, true);
    window.addEventListener('resize', () => this._ddCloseAll());
    this._ddResetAll();
  },

  // ── 自定义参数下拉(原生 select 弹层高度不可控,快门 56 档会顶出屏幕) ──
  _ddBuild(el, values, current) {
    const list = (values || []).map(v =>
      `<div class="tether-dd-item" data-v="${UI._escapeHtml(v)}">${UI._escapeHtml(v)}</div>`).join('');
    el.innerHTML =
      `<button type="button" class="tether-dd-btn" ${values && values.length ? '' : 'disabled'}>` +
      `<span class="tether-dd-val">${UI._escapeHtml(current || '-')}</span><i class="ph ph-caret-down"></i></button>` +
      `<div class="tether-dd-list">${list}</div>`;
    const btn = el.querySelector('.tether-dd-btn');
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const wasOpen = el.classList.contains('open');
      this._ddCloseAll();
      if (!wasOpen) {
        el.classList.add('open');
        this._ddPosition(el);
      }
    });
    el.querySelectorAll('.tether-dd-item').forEach(item => {
      item.addEventListener('click', (e) => {
        e.stopPropagation();
        this._ddCloseAll();
        this._onSettingPick(el, item.dataset.v);
      });
    });
  },

  _ddCloseAll() {
    document.querySelectorAll('.tether-dd.open').forEach(d => d.classList.remove('open'));
  },

  _ddPosition(el) {
    // 按按钮的视口坐标放弹层(fixed 定位, 不被卡片层叠/裁剪挡住); 下方不够高就向上翻
    const btn = el.querySelector('.tether-dd-btn');
    const list = el.querySelector('.tether-dd-list');
    if (!btn || !list) return;
    const r = btn.getBoundingClientRect();
    const w = Math.max(120, r.width + 16);
    let left = r.right - w;  // 右对齐按钮
    left = Math.max(8, Math.min(left, window.innerWidth - w - 8));
    list.style.width = w + 'px';
    list.style.left = left + 'px';
    const listH = Math.min(224, list.scrollHeight || 224);
    const spaceBelow = window.innerHeight - r.bottom;
    if (spaceBelow < listH + 8 && r.top > listH + 8) {
      list.style.top = (r.top - listH - 4) + 'px';
    } else {
      list.style.top = (r.bottom + 4) + 'px';
    }
  },

  _ddSetValue(el, v) {
    const span = el.querySelector('.tether-dd-val');
    if (span && v != null && v !== '') span.textContent = v;
    el.querySelectorAll('.tether-dd-item').forEach(i => i.classList.toggle('active', i.dataset.v === v));
  },

  _ddResetAll() {
    document.querySelectorAll('.tether-dd').forEach(el => this._ddBuild(el, [], null));
  },

  async _onSettingPick(el, value) {
    const key = el.dataset.key;
    if (!key || !value) return;
    this._ddSetValue(el, value);  // 先乐观显示,失败会被轮询纠正
    const btn = el.querySelector('.tether-dd-btn');
    if (btn) btn.disabled = true;
    try {
      const r = await window.api.sendCommand('tether_set_setting', { key, value });
      if (r && r.error) {
        App.toast(t('tetherSetFailed', { msg: r.error }), 'error');
      } else if (r && r.applied === false) {
        App.toast(r.note || t('tetherSetNotApplied'), 'warning');
      }
    } catch (err) {
      App.toast(t('tetherSetFailed', { msg: err.message }), 'error');
    } finally {
      if (btn) btn.disabled = false;
      this._refreshSettings();  // 回读相机实际值(可能被档位取整)
    }
  },

  async start() {
    const folder = document.getElementById('tether-folder').value.trim();
    if (!folder) { App.toast(t('tetherNeedFolder'), 'warning'); return; }
    const genre = document.getElementById('tether-genre').value;
    const lighting = document.getElementById('tether-lighting').value;
    document.getElementById('btn-tether-start').disabled = true;
    try {
      const r = await window.api.sendCommand('tether_start', { folder, genre, lighting, mode: 'realtime', ...this._coachFreqParams() });
      if (r && r.ok) {
        this._active = true;
        this._session = r.session_id;
        this._shots = [];
        document.getElementById('btn-tether-stop').disabled = false;
        document.getElementById('btn-tether-coach').disabled = false;
        const caps = r.caps || [];
        const hasControl = caps.includes('liveview') || caps.includes('capture');
        const statusEl = document.getElementById('tether-status');
        document.getElementById('tether-backend-badge').textContent =
          r.error_code === 'camera_not_found' ? t('tetherNoCameraBadge') :
          r.needs_driver ? t('tetherNeedDriverBadge') : (r.backend || 'folder_watch');
        if (r.needs_driver || r.error_code === 'camera_not_found') {
          // gphoto2 saw no camera (Windows: WinUSB not installed via Zadig) ->
          // folder-watch fallback. Offer the driver-install guide inline.
          statusEl.innerHTML = t('tetherNoCameraWin')
            + `<a href="#" id="tether-driver-link" style="color:var(--brand);font-weight:600">${t('tetherDriverGuideLink')}</a>`;
          if (r.error_code === 'camera_not_found') {
            this._wireDriverActions(r.help_url || 'https://studio.dwphoto.top/tethered-setup#mac');
            this._refreshStatus();
            this._logCoach(t('tetherNoCameraMacLog'), 'warn');
          } else {
            this._wireDriverActions(r.help_url || 'https://studio.dwphoto.top/tethered-setup');
            this._refreshStatus();
            this._logCoach(t('tetherNoCameraWinLog'), 'warn');
          }
        } else {
          statusEl.textContent = hasControl
            ? t('tetherStatusControl', { folder, backend: r.backend, caps: caps.join('/') })
            : t('tetherStatusWatch', { folder, backend: r.backend });
          this._logCoach(t('tetherSessionStarted', { backend: r.backend, caps: caps.join('/') || t('tetherCapsNone') }), 'info');
        }
        document.getElementById('tether-shot-count').textContent = '0';
        this._applyCaps(caps);
      } else {
        document.getElementById('btn-tether-start').disabled = false;
        App.toast((r && r.error) || t('tetherStartFailed'), 'error');
      }
    } catch (e) {
      document.getElementById('btn-tether-start').disabled = false;
      App.toast(t('tetherStartFailedMsg', { msg: e.message }), 'error');
    }
  },

  async stop() {
    try { await window.api.sendCommand('tether_stop', {}); } catch (e) {}
    this._resetUI(t('tetherStopped'));
  },

  _resetUI(statusText) {
    this._active = false;
    this._session = null;
    this._choicesLoaded = false;
    if (this._settingsTimer) { clearInterval(this._settingsTimer); this._settingsTimer = null; }
    this._settingsPollPaused = false;
    document.getElementById('btn-tether-start').disabled = false;
    document.getElementById('btn-tether-stop').disabled = true;
    document.getElementById('btn-tether-coach').disabled = true;
    document.getElementById('btn-tether-liveview-start').disabled = true;
    document.getElementById('btn-tether-liveview-stop').disabled = true;
    document.getElementById('btn-tether-capture').disabled = true;
    document.getElementById('tether-status').textContent = statusText;
    this._clearLiveviewFrame();
    const sv = document.getElementById('tether-liveview-status');
    if (sv) sv.textContent = t('tetherLvIdle');
    document.querySelectorAll('.tether-dd').forEach(el => this._ddBuild(el, [], null));
    // 停止联机后清残留:拍摄列表/当前片叠层/最近列表/教练卡恢复初始空态
    this._shots = [];
    document.getElementById('tether-shot-count').textContent = '0';
    const cur = document.getElementById('tether-current');
    if (cur) {
      cur.classList.add('empty-state');
      cur.innerHTML = `<span class="empty-icon">📷</span><span class="empty-text" data-i18n="thWaitShot">${t('thWaitShot')}</span><span class="empty-hint" data-i18n="thWaitShotHint">${t('thWaitShotHint')}</span>`;
    }
    const recent = document.getElementById('tether-recent');
    if (recent) recent.innerHTML = '';
    const coach = document.getElementById('tether-coach');
    if (coach) coach.innerHTML = `<div class="empty-state"><span class="empty-text" data-i18n="thCoachEmpty">${t('thCoachEmpty')}</span></div>`;
  },

  async _refreshStatus() {
    try {
      const r = await window.api.sendCommand('tether_status', {});
      if (r && r.running) {
        this._active = true;
        document.getElementById('btn-tether-start').disabled = true;
        document.getElementById('btn-tether-stop').disabled = false;
        document.getElementById('btn-tether-coach').disabled = false;
        document.getElementById('tether-backend-badge').textContent =
          r.error_code === 'camera_not_found' ? t('tetherNoCameraBadge') :
          r.needs_driver ? t('tetherNeedDriverBadge') : (r.backend || '?');
        const baseStatus = t('tetherStatusStats', { folder: r.folder, shots: r.shots, avg: r.avg_score == null ? '--' : r.avg_score });
        if (r.needs_driver || r.error_code === 'camera_not_found') {
          if (r.error_code === 'camera_not_found') {
            document.getElementById('tether-status').innerHTML =
              t('tetherNoCameraMacStatus', { status: baseStatus })
              + `<a href="#" id="tether-driver-link" style="color:var(--brand);font-weight:600">${t('tetherGuideLink')}</a>`;
            this._wireDriverActions('https://studio.dwphoto.top/tethered-setup#mac');
          } else {
            document.getElementById('tether-status').innerHTML =
              t('tetherNeedDriverStatus', { status: baseStatus })
              + `<button id="tether-install-driver-btn" style="padding:2px 10px;font-size:13px;border:1px solid var(--brand);background:var(--brand);color:#fff;border-radius:6px;cursor:pointer">${t('tetherInstallDriverBtn')}</button> `
              + `<a href="#" id="tether-driver-link" style="color:var(--brand);font-weight:600">${t('tetherGuideShort')}</a>`;
            this._wireDriverActions('https://studio.dwphoto.top/tethered-setup');
          }
        } else {
          document.getElementById('tether-status').textContent = baseStatus;
        }
        document.getElementById('tether-shot-count').textContent = String(r.shots || 0);
        this._applyCaps(r.caps || []);
      } else {
        this._resetUI(t('tetherNotStarted'));
      }
    } catch (e) { /* backend not ready yet */ }
  },

  _wireDriverActions(guideUrl) {
    // Wire the "一键安装驱动" button + "指南" link rendered into #tether-status
    // when needs_driver. Re-bound on every refresh (old elements are GC'd).
    const link = document.getElementById('tether-driver-link');
    if (link) link.onclick = (e) => {
      e.preventDefault();
      window.api.openUrl(guideUrl || 'https://studio.dwphoto.top/tethered-setup');
    };
    const btn = document.getElementById('tether-install-driver-btn');
    if (btn) btn.onclick = () => this._installDriver();
  },

  async _installDriver() {
    // Launch the elevated WinUSB installer (libwdi/wdi-simple). A UAC prompt
    // appears; the helper runs in its own console. After it finishes the user
    // re-clicks "开始联机" and gphoto2 can see the camera. If wdi-simple.exe
    // isn't bundled yet, the backend returns ready:false -> open the manual guide.
    try {
      App.toast(t('tetherDriverLaunching'), 'info');
      const r = await window.api.sendCommand('tether_install_driver', {});
      if (r && r.ready) {
        this._logCoach(t('tetherDriverLaunched'), 'info');
      } else if (r && !r.ready) {
        App.toast(r.reason || t('tetherDriverNotReady'), 'warning');
        window.api.openUrl(r.guide_url || 'https://studio.dwphoto.top/tethered-setup');
      } else if (r && r.error) {
        App.toast(t('tetherStartFailedMsg', { msg: r.error }), 'error');
      }
    } catch (e) {
      App.toast(t('tetherStartFailedMsg', { msg: e.message }), 'error');
    }
  },

  _coachFreqParams() {
    // 频率下拉框 -> 后端 {coach_every, coach_drop}
    const v = (document.getElementById('tether-coach-freq') || {}).value || '10';
    if (v === 'drop') return { coach_every: 0, coach_drop: true };
    if (v === 'manual') return { coach_every: 0, coach_drop: false };
    return { coach_every: parseInt(v, 10) || 10, coach_drop: true };
  },

  async _onCoachFreqChange() {
    if (!this._active) return;  // 下次开始联机时生效
    try {
      await window.api.sendCommand('tether_coach_config', {
        every: this._coachFreqParams().coach_every,
        drop: this._coachFreqParams().coach_drop,
      });
    } catch (e) { /* ignore */ }
  },

  _manualCoach() {
    const last = this._shots[this._shots.length - 1];
    if (!last) { App.toast(t('tetherNoShots'), 'warning'); return; }
    this._logCoach(t('tetherCoachAsking'), 'info');
    window.api.sendCommand('coach_advise', { shot_path: last.path, trigger: 'manual' })
      .catch(e => this._logCoach(t('tetherCoachReqFailed', { msg: e.message }), 'error'));
  },

  handleEvent(name, data) {
    switch (name) {
      case 'tether_started': break;            // 已在 start() 处理
      case 'tether_stopped': this._resetUI(t('tetherSessionStopped')); break;
      case 'liveview_frame': this._onLiveviewFrame(data); break;
      case 'watch_file_received': this._onFileReceived(data); break;
      case 'watch_score_progress': this._onScoreProgress(data); break;
      case 'watch_score_done': break;
      case 'coach_advice': this._onCoachAdvice(data); break;
    }
  },

  // ── USB control (gPhoto2): liveview / capture / settings ──
  _applyCaps(caps) {
    const c = caps || [];
    document.getElementById('btn-tether-liveview-start').disabled = !c.includes('liveview');
    document.getElementById('btn-tether-capture').disabled = !c.includes('capture');
    if (c.includes('settings_read')) this._refreshSettings();
    if (c.includes('settings_write')) this._loadSettingChoices();
  },

  async _loadSettingChoices() {
    // 取相机各参数的可选值填充下拉框(镜头/档位决定列表,每会话拉一次)
    if (this._choicesLoaded) return;
    try {
      const r = await window.api.sendCommand('tether_get_setting_choices', {});
      if (!r || !r.choices) {
        console.warn('[tethered] get_setting_choices unavailable:', r);
        this._logCoach(t('tetherChoicesFailed', { msg: (r && r.error) || t('tetherBackendNoCmd') }), 'error');
        return;
      }
      const map = { 'tether-set-aperture': 'f-number', 'tether-set-shutter': 'shutterspeed', 'tether-set-iso': 'iso' };
      let any = false;
      for (const [id, cname] of Object.entries(map)) {
        const el = document.getElementById(id);
        const list = r.choices[cname];
        if (!el || !Array.isArray(list) || !list.length) continue;
        this._ddBuild(el, list, null);
        any = true;
      }
      if (any) {
        this._choicesLoaded = true;
        this._refreshSettings();
        const n = (r.choices['f-number'] || []).length;
        this._logCoach(t('tetherChoicesLoaded', { ap: n, sh: (r.choices['shutterspeed'] || []).length, iso: (r.choices['iso'] || []).length }), 'info');
      } else {
        console.warn('[tethered] get_setting_choices returned no usable lists:', r.choices);
        this._logCoach(t('tetherChoicesFailed', { msg: t('tetherNoChoices') }), 'error');
      }
    } catch (e) {
      console.warn('[tethered] load setting choices failed:', e);
      this._logCoach(t('tetherChoicesFailed', { msg: e.message }), 'error');
    }
  },

  _onLiveviewFrame(data) {
    if (!data || !data.frame) return;
    const img = document.getElementById('tether-liveview');
    const empty = document.getElementById('tether-liveview-empty');
    img.src = 'data:image/jpeg;base64,' + data.frame;
    img.style.display = 'block';
    if (empty) empty.style.display = 'none';
  },

  async _startLiveview() {
    try {
      const r = await window.api.sendCommand('tether_liveview_start', {});
      if (r && r.ok) {
        document.getElementById('btn-tether-liveview-start').disabled = true;
        document.getElementById('btn-tether-liveview-stop').disabled = false;
        const sv = document.getElementById('tether-liveview-status');
        if (sv) sv.textContent = t('tetherLvActive');
        this._refreshSettings();
        this._startSettingsPolling();
      } else {
        App.toast((r && r.error) || t('tetherLvCannotStart'), 'error');
      }
    } catch (e) { App.toast(t('tetherLvFailed', { msg: e.message }), 'error'); }
  },

  _startSettingsPolling() {
    // 轮询实时参数(用户在机身上改光圈/快门/ISO 时同步显示)
    if (this._settingsTimer) clearInterval(this._settingsTimer);
    this._settingsTimer = setInterval(() => {
      // App.setPage 没有"离开页面"钩子(app.js 不可改),定时器自检:切走即停,
      // 不再每 3 秒打扰相机;回到本页时由 onPageEnter 恢复
      if (typeof App !== 'undefined' && App.currentPage !== 'tethered') {
        clearInterval(this._settingsTimer);
        this._settingsTimer = null;
        this._settingsPollPaused = true;
        return;
      }
      this._refreshSettings();
    }, 3000);
  },

  _clearLiveviewFrame() {
    // 停止取景/停止会话时清掉残留的最后一帧，避免画面冻结在框内
    const img = document.getElementById('tether-liveview');
    const empty = document.getElementById('tether-liveview-empty');
    if (img) { img.src = ''; img.style.display = 'none'; }
    if (empty) empty.style.display = '';
  },

  async _stopLiveview() {
    try { await window.api.sendCommand('tether_liveview_stop', {}); } catch (e) {}
    document.getElementById('btn-tether-liveview-start').disabled = false;
    document.getElementById('btn-tether-liveview-stop').disabled = true;
    const sv = document.getElementById('tether-liveview-status');
    if (sv) sv.textContent = t('tetherLvIdle');
    this._clearLiveviewFrame();
    if (this._settingsTimer) { clearInterval(this._settingsTimer); this._settingsTimer = null; }
    this._settingsPollPaused = false;
  },

  async _capture() {
    try {
      const r = await window.api.sendCommand('tether_capture', {});
      if (r && r.error) App.toast(r.error, 'error');
      // 拍到的文件落到监听文件夹 -> folder_watcher -> score_files -> watch_score_progress（自动）
    } catch (e) { App.toast(t('tetherCaptureFailed', { msg: e.message }), 'error'); }
  },

  async _refreshSettings() {
    try {
      const r = await window.api.sendCommand('tether_get_settings', {});
      const setV = (id, v) => {
        const el = document.getElementById(id);
        if (!el || v == null || v === '') return;
        if (el.classList.contains('open')) return;  // 用户正在浏览下拉,不打扰
        this._ddSetValue(el, String(v));
      };
      if (r && r.aperture != null) {
        setV('tether-set-aperture', r.aperture);
        setV('tether-set-shutter', r.shutter);
        setV('tether-set-iso', r.iso);
      }
    } catch (e) { /* ignore */ }
  },

  _onFileReceived(data) {
    // 触发评分(镜像 copy-card.js realtime 流);后端 shooting_coach 订阅 watch_score_progress 触发教练。
    window.api.sendCommand('score_files', {
      files: [data.path], threshold: 6.0, batch_id: data.batch_id || '',
    }).catch(e => this._logCoach(t('tetherScoreFailed', { msg: e.message }), 'error'));
  },

  _onScoreProgress(data) {
    const img = data.image || {};
    const path = img.path;
    const score = data.last_score;
    if (!path || score == null || score < 0) return;
    let item = this._shots.find(x => x.path === path);
    if (!item) {
      item = { path, name: img.filename || data.last_file || '', score: null };
      this._shots.push(item);
    }
    item.score = score;
    document.getElementById('tether-shot-count').textContent = String(this._shots.length);
    // 当前片叠层
    const cur = document.getElementById('tether-current');
    cur.classList.remove('empty-state');
    const color = score >= 7 ? 'var(--brand)' : score >= 5 ? 'var(--text2)' : 'var(--text3)';
    cur.innerHTML = `<div style="display:flex;gap:14px;align-items:center">
      <div style="min-width:0"><div style="font-size:13px;color:var(--text3);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${item.name}</div></div>
      <div style="font-size:30px;font-weight:700;color:${color}">${score.toFixed(1)}</div>
      <div style="font-size:12px;color:var(--text3)">${t('tetherScoreUnit')}</div>
    </div>`;
    this._renderRecent();
  },

  _renderRecent() {
    const el = document.getElementById('tether-recent');
    const recent = this._shots.slice(-8).reverse();
    el.innerHTML = recent.map(s => {
      const sc = s.score == null ? '…' : s.score.toFixed(1);
      const color = s.score == null ? 'var(--text3)' :
        (s.score >= 7 ? 'var(--brand)' : s.score >= 5 ? 'var(--text2)' : 'var(--text3)');
      return `<div style="display:flex;justify-content:space-between;padding:4px 8px;border-bottom:1px solid var(--border)">
        <span style="font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:72%">${s.name}</span>
        <span style="font-size:13px;font-weight:600;color:${color}">${sc}</span>
      </div>`;
    }).join('');
  },

  _onCoachAdvice(data) {
    const s = data.settings || {};
    // 实时参数与 EXIF 字段形状不同:aperture 可能已带 "f/" 前缀,快门在
    // shutter_str(EXIF)/shutter(gphoto2)/shutter_s(秒) 三个字段之一
    const ap = s.aperture != null ? String(s.aperture).replace(/^f\/?/i, '') : '';
    const sh = s.shutter_str || s.shutter || (s.shutter_s != null ? s.shutter_s + 's' : '?');
    const exifStr = ap
      ? `f/${ap} · ${sh} · ISO${s.iso == null ? '?' : s.iso}`
      : (s.fallback === 'exif' ? t('tetherNoExif') : '');
    const rules = (data.rules_fired || []).map(r => `<li>${r}</li>`).join('');
    const refs = (data.refs || []).map(r =>
      `<span class="badge" style="margin-right:4px">${r.caption || ('#' + r.id)}</span>`).join('');
    const html = `<div style="padding:10px;border-bottom:1px solid var(--border)">
      <div style="font-size:12px;color:var(--text3);margin-bottom:4px">${exifStr}${data.llm_used ? ' · LLM' : t('tetherRuleBased')}</div>
      <div style="font-size:13px;line-height:1.6">${data.advice || ''}</div>
      ${rules ? `<ul style="margin:6px 0 0 16px;font-size:12px;color:var(--text2)">${rules}</ul>` : ''}
      ${refs ? `<div style="margin-top:6px;font-size:11px">${t('tetherRefs')}${refs}</div>` : ''}
    </div>`;
    const el = document.getElementById('tether-coach');
    const empty = el.querySelector('.empty-state');
    if (empty) el.innerHTML = '';
    el.insertAdjacentHTML('afterbegin', html);
  },

  _logCoach(msg, level) {
    const el = document.getElementById('tether-coach');
    const empty = el.querySelector('.empty-state');
    if (empty) el.innerHTML = '';
    const color = level === 'error' ? 'var(--danger, #e53935)' : 'var(--text3)';
    el.insertAdjacentHTML('afterbegin',
      `<div style="padding:8px;font-size:12px;color:${color}">${msg}</div>`);
  },

  // ── 参考样片库(0.8.8):手动导入/统计/按题材清空,供教练 RAG 检索 ──
  _refT(key, zh, vars) {
    // 新键尚未入 i18n.js:先用中文兜底,键补齐后自动切换
    let s = t(key);
    if (s === key) s = zh;
    if (vars) for (const k of Object.keys(vars)) s = s.split('{' + k + '}').join(String(vars[k]));
    return s;
  },

  _refGenreLabel(genre) {
    // 题材值 -> 复用联机页现有 i18n 键('' = 通用)
    const keys = { '': 'thGeneral', wedding: 'thGenreWedding', portrait: 'thGenrePortrait', product: 'thGenreProduct', realestate: 'thGenreRealestate' };
    return keys[genre] ? t(keys[genre]) : (genre || t('thGeneral'));
  },

  async _importRefs() {
    const genre = document.getElementById('ref-genre').value;
    const lighting = document.getElementById('ref-lighting').value;
    const paths = await window.api.selectFiles();
    if (!paths || !paths.length) return;
    const btn = document.getElementById('btn-ref-import');
    btn.disabled = true;
    try {
      const r = await window.api.sendCommand('reference_import', { paths, genre, lighting });
      if (r && r.error) {
        App.toast(r.error, 'error');
      } else if (r) {
        App.toast(this._refT('refImportDone', '导入完成: {ok}/{total} 成功, {failed} 失败',
          { ok: r.imported, total: r.total, failed: r.failed }), r.failed ? 'warning' : 'success');
        this._logCoach(this._refT('refImportLog', '参考样片导入「{genre}」: {ok}/{total} 成功',
          { genre: this._refGenreLabel(genre), ok: r.imported, total: r.total }), 'info');
      }
    } catch (e) {
      App.toast(this._refT('refImportFailed', '导入失败: {msg}', { msg: e.message }), 'error');
    } finally {
      btn.disabled = false;
      this._refreshRefStats();
    }
  },

  async _clearRefs() {
    const genre = document.getElementById('ref-genre').value;
    const label = this._refGenreLabel(genre);
    const ok = await Modal.showConfirm(
      this._refT('refClearTitle', '清空参考样片'),
      this._refT('refClearMsg', '将删除「{genre}」题材库的全部样片,不可恢复。继续?', { genre: label }));
    if (!ok) return;
    try {
      const r = await window.api.sendCommand('reference_clear', { genre });
      if (r && r.error) {
        App.toast(r.error, 'error');
      } else if (r) {
        App.toast(this._refT('refClearDone', '已删除 {n} 条,库剩余 {m} 条',
          { n: r.deleted, m: r.remaining }), 'success');
      }
    } catch (e) {
      App.toast(this._refT('refClearFailed', '清空失败: {msg}', { msg: e.message }), 'error');
    } finally {
      this._refreshRefStats();
    }
  },

  async _refreshRefStats() {
    const el = document.getElementById('ref-stats');
    if (!el) return;
    try {
      const r = await window.api.sendCommand('reference_stats', {});
      if (!r || r.error) { el.textContent = (r && r.error) || ''; return; }
      const byGenre = r.by_genre || {};
      const order = ['', 'wedding', 'portrait', 'product', 'realestate'];
      const parts = [];
      for (const g of order) {
        if (byGenre[g]) parts.push(`${this._refGenreLabel(g)} ${byGenre[g]}`);
      }
      for (const g of Object.keys(byGenre)) {  // 固定顺序之外的历史题材也列出
        if (!order.includes(g) && byGenre[g]) parts.push(`${g} ${byGenre[g]}`);
      }
      el.textContent = this._refT('refStatsLine', '共 {total} 条样片', { total: r.total || 0 })
        + (parts.length ? ' · ' + parts.join(' · ') : '');
    } catch (e) { /* backend not ready yet */ }
  },
};
