/**
 * automation.js — 自动化引擎 + 定时任务（按账号隔离存储）
 */

const Automation = {
  strategy: 'manual',
  steps: { copy:{auto:false,config:{}}, score:{auto:false,config:{}}, upload:{auto:false,config:{}} },
  deliverWhen: 'round1',  // 交付时机: round1=粗筛后立即上传 / round2=终选后（需先「导入修图结果」）
  schedule: { type:'none', custom:{ freq:'daily', dayOfWeek:'1', hour:'9', minute:'0', multiTimes:[] } },
  _timer: null, _refreshTimer: null, _running: false,
  _state: { currentStep:null, startTime:null, results:{}, history:[] },
  _savedSemi: null, // preserve semi-auto settings when switching to full

  // ── Storage key (per account) ──
  _key() { return 'automation_' + (App.user?.id || 'default'); },

  init() { this.load(); this.bindUI(); this.render(); if (this.schedule.type !== 'none') this.startSchedule(); },

  load() {
    try { const s = JSON.parse(localStorage.getItem(this._key()) || 'null'); if (s) { this.strategy = s.strategy || 'manual'; this.steps = s.steps || this.steps; this.deliverWhen = s.deliverWhen || 'round1'; this.schedule = s.schedule || this.schedule; this._state.history = s.history || []; } } catch(e){}
  },
  save() { localStorage.setItem(this._key(), JSON.stringify({ strategy:this.strategy, steps:this.steps, deliverWhen:this.deliverWhen, schedule:this.schedule, history:this._state.history })); },

  // ── UI ──
  bindUI() {
    document.getElementById('auto-strategy')?.addEventListener('change', e => { this.setStrategy(e.target.value); });
    ['copy','score','upload'].forEach(step => {
      document.getElementById(`auto-step-${step}`)?.addEventListener('change', e => { this.steps[step].auto = e.target.checked; this.save(); this.render(); });
    });
    document.getElementById('auto-schedule-type')?.addEventListener('change', e => {
      this.schedule.type = e.target.value; this.save(); this.render(); this.restartSchedule();
    });
    document.getElementById('auto-deliver-when')?.addEventListener('change', e => { this.deliverWhen = e.target.value; this.save(); });
    document.getElementById('auto-custom-freq')?.addEventListener('change', e => {
      this.schedule.custom.freq = e.target.value; this.save(); this.renderCustom();
    });
    document.getElementById('auto-custom-day')?.addEventListener('change', e => { this.schedule.custom.dayOfWeek = e.target.value; this.save(); });
    document.getElementById('auto-custom-hour')?.addEventListener('change', e => { this.schedule.custom.hour = e.target.value; this.save(); });
    document.getElementById('auto-custom-minute')?.addEventListener('change', e => { this.schedule.custom.minute = e.target.value; this.save(); });
    document.getElementById('auto-multi-count')?.addEventListener('change', e => {
      const n = parseInt(e.target.value); this.schedule.custom.multiTimes.length = Math.max(0, n);
      while (this.schedule.custom.multiTimes.length < n) this.schedule.custom.multiTimes.push('09:00');
      this.save(); this.renderCustom();
    });
    document.getElementById('btn-auto-start')?.addEventListener('click', () => this.startNow());
    document.getElementById('btn-auto-stop')?.addEventListener('click', () => this.stop());
  },

  setStrategy(mode) {
    this.strategy = mode;
    if (mode === 'full') {
      this._savedSemi = JSON.parse(JSON.stringify(this.steps)); // preserve semi settings
      this.steps.copy.auto = this.steps.score.auto = this.steps.upload.auto = true;
    } else if (mode === 'manual') {
      this.steps.copy.auto = this.steps.score.auto = this.steps.upload.auto = false;
    } else if (mode === 'semi' && this._savedSemi) {
      this.steps = JSON.parse(JSON.stringify(this._savedSemi));
    }
    this.save(); this.render();
  },

  // ── Rendering ──
  render() {
    const sel = document.getElementById('auto-strategy'); if (sel) sel.value = this.strategy;
    const dw = document.getElementById('auto-deliver-when'); if (dw) dw.value = this.deliverWhen;
    ['copy','score','upload'].forEach(step => {
      const cb = document.getElementById(`auto-step-${step}`); if (cb) cb.checked = this.steps[step].auto;
    });
    const sch = document.getElementById('auto-schedule-type'); if (sch) sch.value = this.schedule.type;
    this.renderCustom();
    this.renderHistory();
  },

  renderCustom() {
    const panel = document.getElementById('auto-custom-panel');
    if (!panel) return;
    if (this.schedule.type !== 'custom') { panel.classList.add('hidden'); return; }
    panel.classList.remove('hidden');

    const freq = document.getElementById('auto-custom-freq'); if (freq) freq.value = this.schedule.custom.freq;
    const day = document.getElementById('auto-custom-day-row');
    const time = document.getElementById('auto-custom-time-row');
    const multi = document.getElementById('auto-custom-multi-row');

    if (day) day.classList.toggle('hidden', this.schedule.custom.freq !== 'weekly');
    if (time) time.classList.toggle('hidden', this.schedule.custom.freq === 'multiDaily');
    if (multi) {
      multi.classList.toggle('hidden', this.schedule.custom.freq !== 'multiDaily');
      if (this.schedule.custom.freq === 'multiDaily') this.renderMultiTimes();
    }

    const h = document.getElementById('auto-custom-hour'); if (h) h.value = this.schedule.custom.hour;
    const m = document.getElementById('auto-custom-minute'); if (m) m.value = this.schedule.custom.minute;
    const d = document.getElementById('auto-custom-day'); if (d) d.value = this.schedule.custom.dayOfWeek;
    const cnt = document.getElementById('auto-multi-count'); if (cnt) cnt.value = this.schedule.custom.multiTimes.length;
    this.updateNextRunLabel();
  },

  renderMultiTimes() {
    const container = document.getElementById('auto-multi-times');
    if (!container) return;
    const n = this.schedule.custom.multiTimes.length;
    container.innerHTML = this.schedule.custom.multiTimes.map((timeVal, i) => `
      <div style="display:flex;align-items:center;gap:8px;padding:4px 0">
        <span style="font-size:12px;color:var(--text2)">${t('autoNthTime', { n: i + 1 })}</span>
        <input type="time" value="${timeVal}" class="multi-time-input" data-index="${i}"
               style="padding:6px 10px;background:var(--bg3);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:13px">
      </div>
    `).join('');
    container.querySelectorAll('.multi-time-input').forEach(inp => {
      inp.addEventListener('change', e => {
        this.schedule.custom.multiTimes[parseInt(e.target.dataset.index)] = e.target.value;
        this.save(); this.restartSchedule();
      });
    });
  },

  renderHistory() {
    const el = document.getElementById('auto-history');
    if (!el) return;
    const h = this._state.history.slice(-10);
    if (!h.length) { el.innerHTML = `<div style="font-size:12px;color:var(--text3);padding:8px">${t('autoNoHistory')}</div>`; return; }
    el.innerHTML = h.map(r => `
      <div style="font-size:12px;color:var(--text2);padding:4px 0;border-bottom:1px solid var(--border)">
        <span style="color:${r.success ? 'var(--green)' : 'var(--red)'}">${r.success ? '✓' : '✗'}</span>
        ${new Date(r.time).toLocaleString('zh-CN', {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'})}
        · ${r.steps.map(s => this._stepLabel(s)).join('→')}
        ${r.error ? ` · ${r.error}` : ''}
      </div>
    `).join('');
  },

  // ── Next run label ──
  updateNextRunLabel() {
    const el = document.getElementById('auto-next-run');
    if (!el || this.schedule.type === 'none') { if (el) el.textContent = ''; return; }
    const next = this._calcNextRun();
    if (next) el.textContent = `${t('nextRun')}: ${next.toLocaleString('zh-CN', {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit', weekday:'short'})}`;
  },

  // ── Scheduling ──
  _calcNextRun() {
    return Scheduler.calcNextRun(this.schedule, this.intervalMinutes);
  },

  startSchedule() {
    this.stopSchedule();
    if (this.schedule.type === 'none') { this.updateNextRunLabel(); return; }
    this._armScheduleTimer();
    // Periodic refresh: re-evaluate every hour to compensate for sleep/hibernate
    if (!this._refreshTimer) {
      this._refreshTimer = setInterval(() => {
        if (this.schedule.type === 'none') { this.stopSchedule(); return; }
        this._armScheduleTimer(); // re-arm, not just relabel — corrects drift after sleep/hibernate
      }, 3600000);
    }
  },

  _armScheduleTimer() {
    if (this._timer) { clearTimeout(this._timer); this._timer = null; }
    const next = this._calcNextRun();
    if (!next) { this._nextRunAt = null; this.updateNextRunLabel(); return; }
    this._nextRunAt = next.getTime();
    const delay = Math.max(1000, this._nextRunAt - Date.now());
    // setTimeout can't wait past ~24.8 days; cap at 24h and re-check on fire —
    // a weekly task must not run just because the 24h cap elapsed.
    this._timer = setTimeout(() => this._onScheduleTimer(), Math.min(delay, 86400000));
    this.updateNextRunLabel();
  },

  _onScheduleTimer() {
    this._timer = null;
    if (this.schedule.type === 'none') return;
    // Fired early (24h cap): target still ahead — re-arm and keep waiting.
    if (this._nextRunAt && Date.now() < this._nextRunAt - 5000) { this._armScheduleTimer(); return; }
    this.startNow();
    this.startSchedule();
  },

  stopSchedule() {
    if (this._timer) { clearTimeout(this._timer); this._timer = null; }
    if (this._refreshTimer) { clearInterval(this._refreshTimer); this._refreshTimer = null; }
  },
  restartSchedule() { this.stopSchedule(); this.startSchedule(); },

  // ── Execution ──
  startNow() {
    if (this._running) { App.toast(t('autoRunning'), 'warning'); return; }
    this._running = true;
    // 保留 history——_recordHistory/renderHistory 依赖它，整体替换会丢
    this._state = { currentStep:'start', startTime:Date.now(), results:{}, stepsExecuted:[], history: this._state.history || [] };
    document.getElementById('auto-status').textContent = t('running');
    this._updateProgress(t('autoStarting'));
    App.toast(t('autoStarted'), 'info');

    // Determine enabled steps
    const enabled = [];
    if (this.steps.copy.auto) enabled.push('copy');
    if (this.steps.score.auto) enabled.push('score');
    if (this.steps.upload.auto) enabled.push('upload');

    if (enabled.length === 0) {
      this._recordHistory(false, [], t('autoNoSteps'));
      this.stop();
      return;
    }

    this._state.enabledSteps = enabled;
    this._runNextStep();
  },

  _runNextStep() {
    if (!this._running) return;
    const enabled = this._state.enabledSteps;
    if (!enabled || enabled.length === 0) { this._finishAll(); return; }
    const step = enabled.shift();
    this._state.currentStep = step;
    this._updatePipeline(step);
    this._updateProgress(`${t('autoExecuting')} ${this._stepLabel(step)}`);

    switch (step) {
      case 'copy':
        this._executeCopy();
        break;
      case 'score':
        this._executeScore();
        break;
      case 'upload':
        this._executeUpload();
        break;
      default:
        this._runNextStep(); // skip unknown, continue chain
    }
  },

  _stepLabel(s) { return s === 'copy' ? t('autoCopyStep') : s === 'score' ? t('autoScoreStep') : s === 'upload' ? t('autoUploadStep') : s; },

  _executeCopy() {
    if (typeof CopyCard !== 'undefined') {
      if (!CopyCard.monitoring) CopyCard.startMonitor();
    }
    // Copy completion is event-driven — when copy finishes, CopyCard calls Automation.onStepComplete('copy', data)
    // Watchdog fallback: advance only if no copy actually started (no card / start error).
    this._copyWatchdog = setTimeout(() => {
      this._copyWatchdog = null;
      if (!this._running || this._state.currentStep !== 'copy') return;
      // A real copy (multi-GB) runs far longer than 10s — once it has started,
      // disarm and let copy_done drive onStepComplete instead of advancing early.
      if (typeof CopyCard !== 'undefined' && CopyCard.copying) return;
      this.addLog(t('autoCopyNoDevice'));
      this._state.stepsExecuted.push('copy');
      this._runNextStep();
    }, 10000);
  },

  _executeScore() {
    const dest = this._state.results.copy?.dest
      || localStorage.getItem('last_copy_dest')
      || (typeof PhotoSelect !== 'undefined' ? PhotoSelect.sourceFolder : '')
      || '';
    if (!dest) { this._state.stepsExecuted.push('score'); this._runNextStep(); return; }
    if (typeof PhotoSelect !== 'undefined') {
      if (!PhotoSelect.modelLoaded) {
        this.addLog(t('autoModelLoading'));
        PhotoSelect.preloadModel().then(() => {
          if (this._running) this._doScore(dest);
        }).catch(() => {
          this._state.stepsExecuted.push('score');
          this._runNextStep();
        });
        return;
      }
      this._doScore(dest);
    } else {
      this._state.stepsExecuted.push('score');
      this._runNextStep();
    }
  },

  _doScore(folder) {
    document.getElementById('select-folder').value = folder;
    if (typeof PhotoSelect !== 'undefined') {
      PhotoSelect.sourceFolder = folder;
      App.setPage('photo-select');
      setTimeout(() => PhotoSelect.startScoring({ copy_on_score: true }), 800);
      // Watchdog fallback (same pattern as the copy step): once scoring has
      // actually started, disarm and let score_done drive onStepComplete —
      // advance only if it never started (start error emits no score_done).
      this._scoreWatchdog = setTimeout(() => {
        this._scoreWatchdog = null;
        if (!this._running || this._state.currentStep !== 'score') return;
        if (typeof PhotoSelect !== 'undefined' && PhotoSelect.processing) return;
        this.addLog(t('autoScoreNoRun'));
        this._state.stepsExecuted.push('score');
        this._runNextStep();
      }, 10000);
    }
  },

  _executeUpload() {
    if (!App.hasActiveSub || !App.hasActiveSub()) {
      this.addLog(t('autoUploadNeedSub'));
      this._state.stepsExecuted.push('upload');
      this._runNextStep();
      return;
    }
    // 交付时机「终选后」：需先在选片页「导入修图结果」完成二次筛选
    // （photo-select.importRetouched 按 sourceFolder 写 automation_deliver_round2_<folder> 标记；
    //   评分输出在 优秀照片 子目录时两种 folder 都查）
    if (this.deliverWhen === 'round2') {
      const srcFolder = (typeof PhotoSelect !== 'undefined' ? PhotoSelect.sourceFolder : '') || '';
      const outFolder = this._state.results.score?.output_folder || '';
      const marker = [outFolder, srcFolder].filter(Boolean)
        .map(f => localStorage.getItem('automation_deliver_round2_' + encodeURIComponent(f)))
        .find(Boolean);
      if (!marker) {
        this.addLog(t('autoWaitRound2'), 'warn');
        this._state.stepsExecuted.push('upload');
        this._runNextStep();
        return;
      }
    }
    // Collect scored photos from output folder
    const outputFolder = this._state.results.score?.output_folder
      || (typeof PhotoSelect !== 'undefined' ? PhotoSelect.sourceFolder : '');
    if (outputFolder && typeof CloudUpload !== 'undefined') {
      // Expand the folder into files first — addToQueue expects file paths, not directories
      window.api.sendCommand('list_images_in_folder', { folder: outputFolder }).then(r => {
        if (!this._running) return;
        if (r && r.images && r.images.length) {
          const scores = {}; r.images.forEach(f => { scores[typeof f === 'string' ? f : f.path] = 0; });
          CloudUpload.addToQueue(r.images, scores);
          if (CloudUpload.uploadQueue.length > 0) {
            App.setPage('cloud');
            setTimeout(() => CloudUpload.startUpload(), 1000);
            return;
          }
        }
        this.addLog(t('autoQueueEmptySkip'));
        this._state.stepsExecuted.push('upload');
        this._runNextStep();
      }).catch(e => {
        if (!this._running) return;
        this.onStepFailed('upload', e.message);
      });
      return;
    }
    this.addLog(t('autoQueueEmptySkip'));
    this._state.stepsExecuted.push('upload');
    this._runNextStep();
  },

  _updatePipeline(currentStep) {
    const stages = ['copy','score','upload'];
    const allDone = currentStep === null;
    stages.forEach(s => {
      const el = document.getElementById(`auto-stage-${s}`);
      if (!el) return;
      const badge = el.querySelector('.stage-badge');
      const isEnabled = this.steps[s]?.auto;
      if (!isEnabled && !allDone) {
        el.classList.remove('active','done');
        el.classList.add('disabled');
        if (badge) { badge.textContent = t('autoSkip'); badge.style.background = ''; }
      } else if (this._state.stepsExecuted.includes(s) || allDone) {
        el.classList.remove('active','disabled'); el.classList.add('done');
        if (badge) { badge.textContent = t('done'); badge.style.background = 'var(--green)'; }
      } else if (s === currentStep) {
        el.classList.remove('done','disabled'); el.classList.add('active');
        if (badge) { badge.textContent = t('autoInProgress'); badge.style.background = 'var(--accent)'; }
      } else {
        el.classList.remove('active','done','disabled');
        if (badge) { badge.textContent = t('autoWait'); badge.style.background = ''; }
      }
    });
  },

  _finishAll() {
    this._running = false;
    if (this._copyWatchdog) { clearTimeout(this._copyWatchdog); this._copyWatchdog = null; }
    if (this._scoreWatchdog) { clearTimeout(this._scoreWatchdog); this._scoreWatchdog = null; }
    document.getElementById('auto-status').textContent = t('done');
    this._updateProgress(t('autoAllDone'));
    this._updatePipeline(null); // mark all remaining as done
    App.toast(t('autoAllDoneMsg'), 'success');
    App.notify(t('autoFinish'), t('autoAllStepsDone'));
    this._recordHistory(true, this._state.stepsExecuted);
    this.restartSchedule();
    this.renderHistory();
  },

  _updateProgress(msg) {
    const el = document.getElementById('auto-progress');
    if (el) el.textContent = msg;
    this.addLog(msg);
  },

  stop() {
    this._running = false;
    if (this._copyWatchdog) { clearTimeout(this._copyWatchdog); this._copyWatchdog = null; }
    if (this._scoreWatchdog) { clearTimeout(this._scoreWatchdog); this._scoreWatchdog = null; }
    document.getElementById('auto-status').textContent = t('autoStopped');
    this._updateProgress(t('autoStopped'));
    this._clearPipeline();
    App.toast(t('autoStoppedToast'), 'info');
    this._recordHistory(false, this._state.stepsExecuted || [], t('autoManualStop'));
    this.renderHistory();
  },

  _clearPipeline() {
    ['copy','score','upload'].forEach(s => {
      const el = document.getElementById(`auto-stage-${s}`);
      if (!el) return;
      el.classList.remove('active','done','disabled');
      const badge = el.querySelector('.stage-badge');
      if (badge) { badge.textContent = t('autoWait'); badge.style.background = ''; }
    });
  },

  _recordHistory(success, steps, error) {
    this._state.history.push({
      time: Date.now(), success, steps: steps || [],
      error: error || (success ? '' : t('autoAborted')),
    });
    if (this._state.history.length > 50) this._state.history = this._state.history.slice(-50);
    this.save();
  },

  // ── Event hooks (called by other modules) ──
  onStepComplete(step, data) {
    if (!this._running) return;
    this._state.results[step] = data;
    this._state.stepsExecuted.push(step);
    if (this._copyWatchdog && step === 'copy') { clearTimeout(this._copyWatchdog); this._copyWatchdog = null; }
    if (this._scoreWatchdog && step === 'score') { clearTimeout(this._scoreWatchdog); this._scoreWatchdog = null; }
    this.addLog(t('autoStepDone', { step: this._stepLabel(step) }));
    this._runNextStep();
  },

  onStepFailed(step, error) {
    if (!this._running) return;
    this.addLog(t('autoStepFailed', { step: this._stepLabel(step), error }), 'error');
    // Continue chain despite failure (don't block the pipeline)
    this._state.stepsExecuted.push(step);
    this._runNextStep();
  },

  // FTP trigger — goes directly to score (no copy step needed)
  onFtpStepComplete(data) {
    if (!this._running || !this.steps.score.auto) return;
    this._state.results['ftp'] = data;
    this._state.currentStep = 'score';
    App.toast(t('autoFtpDone'), 'info');
    const path = data.path || '';
    const sep = path.includes('\\') ? '\\' : '/';
    const lastSep = path.lastIndexOf(sep);
    const folder = lastSep > 0 ? path.substring(0, lastSep) : (document.getElementById('ftp-receive-folder')?.value || '');
    if (folder && typeof PhotoSelect !== 'undefined') {
      document.getElementById('select-folder').value = folder;
      PhotoSelect.sourceFolder = folder;
      App.setPage('photo-select');
      setTimeout(() => { if (PhotoSelect.modelLoaded) PhotoSelect.startScoring(); else PhotoSelect.preloadModel(); }, 800);
    }
  },

  addLog(msg, level) {
    if (typeof UI !== 'undefined' && UI.addLog) UI.addLog('auto-log', msg, level || 'info');
  },
};

document.addEventListener('DOMContentLoaded', () => Automation.init());
