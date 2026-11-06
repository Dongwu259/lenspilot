/**
 * pipeline-ui.js — M1 数字员工前端 UI（0.8.4）
 */
const PipelineUI = (() => {
  let _active = false, _runId = null, _state = { status: 'idle' };
  let _phases = [], _artifacts = {}, _logCount = 0;
  let _pendingGateId = null;  // gate_id for the final review (stored separately from modal dataset)
  let _templates = [];        // cached template list from backend

  const $ = (id) => document.getElementById(id);
  let dom = {};

  function cacheDom() {
    dom = {
      folder: $('pipeline-folder'), goal: $('pipeline-goal'),
      template: $('pipeline-template'), autonomy: $('pipeline-autonomy'),
      editBtn: $('pipeline-edit-btn'), cloneBtn: $('pipeline-clone-btn'),
      deleteBtn: $('pipeline-delete-btn'),
      threshold: $('pipeline-threshold'), eyeReject: $('pipeline-eye-reject'),
      tokenCap: $('pipeline-token-cap'), gateTimeout: $('pipeline-gate-timeout'),
      startBtn: $('pipeline-start-btn'), browseBtn: $('pipeline-browse-btn'),
      statusBadge: $('pipeline-status-badge'),
      progressPanel: $('pipeline-progress-panel'),
      progressTitle: $('pipeline-progress-title'),
      phaseTree: $('pipeline-phase-tree'),
      progressBar: $('pipeline-progress-bar'),
      progressText: $('pipeline-progress-text'),
      budgetBar: $('pipeline-budget-bar'),
      pauseBtn: $('pipeline-pause-btn'), clearBtn: $('pipeline-clear-btn'),
      cancelBtn: $('pipeline-cancel-btn'),
      gateModal: $('pipeline-gate-modal'), gateTitle: $('gate-modal-title'),
      gateSummary: $('gate-modal-summary'), gatePayload: $('gate-modal-payload'),
      gateApprove: $('gate-approve-btn'), gateReject: $('gate-reject-btn'),
      reviewPanel: $('pipeline-review-panel'),
      summaryGrid: $('pipeline-summary-grid'),
      keeperPreview: $('pipeline-keeper-preview'),
      acceptBtn: $('pipeline-accept-btn'), rejectBtn: $('pipeline-reject-btn'),
      restoreBtn: $('pipeline-restore-btn'),
      logPanel: $('pipeline-log-panel'), logList: $('pipeline-log-list'),
      logCount: $('pipeline-log-count'),
      emptyState: $('pipeline-empty-state'), emptyTpls: $('pipeline-empty-tpls'),
      aiGenBtn: $('pipeline-ai-gen-btn'),
      genModal: $('pipeline-gen-modal'), genGoal: $('pipeline-gen-goal'),
      genBase: $('pipeline-gen-base'), genStatus: $('pipeline-gen-status'),
      genRunBtn: $('pipeline-gen-run-btn'), genCancelBtn: $('pipeline-gen-cancel-btn'),
    };
  }

  function init() {
    cacheDom(); bindEvents(); _loadConfig(); _loadTemplates();
    console.log('[pipeline] init done');
  }
  function bindEvents() {
    dom.browseBtn.addEventListener('click', async () => {
      try { const f = await window.api.selectFolder(); if (f) dom.folder.value = f; } catch (_) {}
    });
    dom.startBtn.addEventListener('click', _start);
    dom.pauseBtn.addEventListener('click', _pause);
    dom.clearBtn.addEventListener('click', _clearStale);
    dom.cancelBtn.addEventListener('click', _cancel);
    dom.gateApprove.addEventListener('click', () => _resolveGate('approve'));
    dom.gateReject.addEventListener('click', () => _resolveGate('reject'));
    if (dom.acceptBtn) dom.acceptBtn.addEventListener('click', () => _resolveGate('approve'));
    else console.error('[pipeline] acceptBtn not found!');
    if (dom.rejectBtn) dom.rejectBtn.addEventListener('click', () => _resolveGate('reject'));
    else console.error('[pipeline] rejectBtn not found!');
    if (dom.restoreBtn) dom.restoreBtn.addEventListener('click', _restore);

    // ── 模板编辑器入口 ──
    if (dom.editBtn) dom.editBtn.addEventListener('click', _editTemplate);
    if (dom.cloneBtn) dom.cloneBtn.addEventListener('click', _cloneTemplate);
    if (dom.deleteBtn) dom.deleteBtn.addEventListener('click', _deleteTemplate);
    if (dom.template) dom.template.addEventListener('change', _onTemplateChange);

    // ── AI 生成工作流 ──
    if (dom.aiGenBtn) dom.aiGenBtn.addEventListener('click', _openGenModal);
    if (dom.genRunBtn) dom.genRunBtn.addEventListener('click', _runGenerate);
    if (dom.genCancelBtn) dom.genCancelBtn.addEventListener('click', _closeGenModal);
    if (dom.genModal) dom.genModal.addEventListener('click', (e) => {
      if (e.target === dom.genModal && !dom.genRunBtn.disabled) _closeGenModal();
    });

    // 同步 WorkflowEditor 回调
    if (typeof WorkflowEditor !== 'undefined') {
      WorkflowEditor.setOnSave(async (savedId) => {
        await _loadTemplates();
        if (savedId && dom.template) dom.template.value = savedId;
        _onTemplateChange();
      });
    }
    console.log('[pipeline] bindEvents done');
  }

  function _loadConfig() {
    try {
      const c = JSON.parse(localStorage.getItem('pipeline_config') || '{}');
      if (c.folder) dom.folder.value = c.folder;
      if (c.goal) dom.goal.value = c.goal;
      if (c.template) dom.template.value = c.template;
      if (c.autonomy) dom.autonomy.value = c.autonomy;
    } catch (_) {}
  }
  function _saveConfig() {
    try { localStorage.setItem('pipeline_config', JSON.stringify({
      folder: dom.folder.value, goal: dom.goal.value,
      template: dom.template.value, autonomy: dom.autonomy.value })); } catch (_) {}
  }

  async function _start() {
    const folder = dom.folder.value.trim();
    if (!folder) { App.toast(t('plNeedFolder'), 'warning'); return; }
    const res = await window.api.sendCommand('pipeline_start', {
      work_folder: folder, template: dom.template.value,
      autonomy: dom.autonomy.value, goal: dom.goal.value.trim(),
      cloud_token: localStorage.getItem('studio_token') || '',
      pipeline_params: {
        threshold: dom.threshold.value ? parseFloat(dom.threshold.value) : null,
        eye_reject: parseFloat(dom.eyeReject.value) || 0.15,
        budget_token_cap: parseInt(dom.tokenCap.value) || 150000,
        gate_timeout: parseInt(dom.gateTimeout.value) || 0,
      },
    });
    if (res.error) {
      if (res.error === 'llm_not_configured')
        App.toast(t('plLLMRequired'), 'error');
      else App.toast(t('plStartFailed') + res.error + (res.detail ? ': ' + res.detail : ''), 'error');
      return;
    }
    _saveConfig();
    _runId = res.run_id; _active = true;
    _phases = []; _artifacts = {}; _logCount = 0;
    if (dom.logList) dom.logList.innerHTML = '';
    if (dom.logCount) dom.logCount.textContent = '0';
    _showProgress(true); _hideReview(); _hideGate();
    _setBadge(t('running'), 'badge-running');
    dom.startBtn.disabled = true;
    dom.clearBtn.style.display = 'none';
    dom.cancelBtn.style.display = '';
    dom.pauseBtn.textContent = t('plBtnPause');
    dom.pauseBtn.style.display = '';
  }

  async function _pause() {
    const label = dom.pauseBtn.textContent || '';
    const isResume = label.includes(t('plBtnContinue')) || label.includes(t('plBtnResume'));
    if (isResume) {
      // 续跑：进程内暂停 → pipeline_resume 解除；无进程内 run 但有持久化 state 时，
      // 后端 handle_pipeline_resume 自动落到 _resume_from_state 断点续跑
      const r = await window.api.sendCommand('pipeline_resume', { run_id: _runId, work_folder: dom.folder.value.trim(), cloud_token: localStorage.getItem('studio_token') || '' });
      if (r && r.error) { App.toast(t('plStartFailed') + r.error + (r.detail ? ': ' + r.detail : ''), 'error'); return; }
      _active = true; if (r && r.run_id) _runId = r.run_id;
      dom.pauseBtn.textContent = t('plBtnPause'); _setBadge(t('running'), 'badge-running');
      dom.startBtn.disabled = true; dom.clearBtn.style.display = 'none'; dom.cancelBtn.style.display = '';
      return;
    }
    if (!_active) return;
    await window.api.sendCommand('pipeline_pause', { run_id: _runId });
    dom.pauseBtn.textContent = t('plBtnContinue'); _setBadge(t('plPaused'), 'badge-warn');
  }
  async function _cancel() { if (!_active) return;
    await window.api.sendCommand('pipeline_cancel', { run_id: _runId, work_folder: dom.folder.value.trim() }); _reset(); }
  async function _clearStale() {
    await window.api.sendCommand('pipeline_cancel', { run_id: _runId, work_folder: dom.folder.value.trim() });
    _reset(); dom.clearBtn.style.display = 'none'; }
  async function _resolveGate(decision) {
    console.log('[pipeline] _resolveGate called, decision=', decision, '_pendingGateId=', _pendingGateId, 'modalGateId=', dom.gateModal?.dataset?.gateId);
    // Use _pendingGateId (survives _hideGate clearing the modal dataset)
    const gid = _pendingGateId || (dom.gateModal && dom.gateModal.dataset.gateId);
    if (!gid) {
      // No pending gate - pipeline may already be done
      console.log('[pipeline] no pending gate, ignoring click');
      if (dom.acceptBtn) dom.acceptBtn.textContent = t('plBtnDoneAccept');
      if (dom.rejectBtn) dom.rejectBtn.textContent = t('plBtnDoneReject');
      return;
    }
    _pendingGateId = null;
    try {
      console.log('[pipeline] sending gate_resolve', gid, decision);
      const r1 = await window.api.sendCommand('pipeline_gate_resolve', { gate_id: gid, decision });
      console.log('[pipeline] gate_resolve result:', r1);
      _hideGate();
      if (decision === 'approve') {
        // 门的解除靠 gate_resolve 写入的 decision(后端门走独立 Event)；
        // 补发 resume 仅兼容"门解决前用户按过暂停"的场景。
        // reject 不发 resume——避免把已终止的 run 从断点状态复活。
        console.log('[pipeline] sending pipeline_resume');
        const r2 = await window.api.sendCommand('pipeline_resume', { run_id: _runId, work_folder: dom.folder.value.trim(), cloud_token: localStorage.getItem('studio_token') || '' });
        console.log('[pipeline] resume result:', r2);
      }
      dom.pauseBtn.textContent = t('plBtnPause');
      // Visual feedback
      if (decision === 'approve' && dom.acceptBtn) dom.acceptBtn.textContent = t('plBtnConfirmed');
      if (decision === 'reject' && dom.rejectBtn) dom.rejectBtn.textContent = t('plBtnRejected');
    } catch (e) {
      console.error('[pipeline] _resolveGate error:', e);
      App.toast(t('opFailed') + ': ' + (e.message || e), 'error');
    }
  }

  async function _restore() {
    const folder = dom.folder.value.trim();
    if (!folder) { App.toast(t('plNeedFolder'), 'warning'); return; }
    if (!(await Modal.showConfirm(t('plRestoreTitle'), t('plRestoreConfirm')))) return;
    try {
      dom.restoreBtn.disabled = true;
      dom.restoreBtn.textContent = t('plRestoring');
      const r = await window.api.sendCommand('pipeline_restore', { work_folder: folder });
      console.log('[pipeline] restore result:', r);
      if (r && r.error) {
        App.toast(t('plRestoreFailed') + r.error, 'error');
      } else {
        App.toast(r.message || t('plRestoreDone'), 'success');
        _reset();
      }
    } catch (e) {
      console.error('[pipeline] restore error:', e);
      App.toast(t('plRestoreFailed') + (e.message || e), 'error');
    } finally {
      dom.restoreBtn.disabled = false;
      dom.restoreBtn.textContent = t('plRestore');
    }
  }

  // ── 事件处理 ──
  function handleEvent(eventName, data) {
    if (!_active && !eventName.startsWith('pipeline_')) return;
    switch (eventName) {
      case 'pipeline_started':
        _phases = []; dom.clearBtn.style.display = 'none';
        dom.cancelBtn.style.display = ''; dom.pauseBtn.style.display = '';
        dom.progressTitle.textContent = t('running') + ' — ' + (data.goal || '');
        break;
      case 'pipeline_phase_start':
        _phases.push({ id: data.id, title: data.title, status: 'running' });
        _renderPhases(); break;
      case 'pipeline_phase_done': {
        const p = _phases.find(p => p.id === data.id);
        if (p) p.status = 'done';
        _renderPhases(); break;
      }
      case 'pipeline_step_start':
        _resetProgress(data.phase || '', data.step || '');
        break;
      case 'pipeline_step':
        // 步骤内状态提示（后端 detail：'AI 评分中…' 等）：显示到当前步骤状态行
        if (data.detail) _resetProgress(data.phase || '', (data.step ? data.step + ' — ' : '') + data.detail);
        break;
      case 'pipeline_progress':
        // Accept both "current" (incremental) and "done" (final count) from Python
        const done = data.done != null ? data.done : data.current;
        if (done != null && data.total && data.total > 0) {
          const pct = Math.min(100, Math.round((done / data.total) * 100));
          dom.progressBar.style.width = pct + '%';
          dom.progressText.textContent = (data.phase || '') + ': ' + done + '/' + data.total;
        }
        break;
      case 'pipeline_gate':
        _state.status = 'gating';
        _showPendingGate(data);
        break;
      case 'pipeline_paused':
        _setBadge(t('plPaused'), 'badge-warn'); dom.pauseBtn.textContent = t('plBtnContinue'); break;
      case 'pipeline_resumed':
        _setBadge(t('running'), 'badge-running'); dom.pauseBtn.textContent = t('plBtnPause'); _hideGate(); break;
      case 'pipeline_step_skip': {
        const stepName = data.step || data.step_name || '';
        const reason = data.reason || data.detail || '';
        _appendLog('info', t('plLogSkip', { step: stepName }) + (reason ? t('plLogSkipReason', { reason }) : ''));
        break;
      }
      case 'pipeline_log':
        _appendLog(data.level || 'info', data.message || ''); break;
      case 'pipeline_done':
        _active = false; _state.status = 'done';
        _pendingGateId = null;  // gate already resolved
        _artifacts = data.artifacts || {};
        dom.progressBar.style.width = '100%';
        dom.progressText.textContent = data.summary || t('done');
        dom.progressTitle.textContent = t('done');
        _setBadge(t('done'), 'badge-success');
        dom.startBtn.disabled = false; dom.pauseBtn.textContent = t('plBtnPause');
        dom.clearBtn.style.display = 'none'; dom.cancelBtn.style.display = 'none';
        // Keep progress + log panels visible after completion
        _showProgress(true);
        _showReview(data.artifacts || {});
        _saveConfig(); break;
      case 'pipeline_error':
        _active = false; _state.status = 'error';
        dom.progressTitle.textContent = t('error');
        _setBadge(t('error'), 'badge-error');
        dom.startBtn.disabled = false;
        _appendLog('error', data.error || t('unknownError')); break;
      case 'pipeline_rejected':
        // 终审拒绝：后端已复原(移回/清标/删 state)。独立终态，不显示完成态
        _active = false; _state.status = 'rejected';
        _pendingGateId = null;
        _hideGate(); _hideReview();
        dom.progressTitle.textContent = t('plRejectedRestored');
        dom.progressText.textContent = data.summary || '';
        _setBadge(t('plRejectedRestored'), 'badge-warn');
        dom.startBtn.disabled = false; dom.pauseBtn.textContent = t('plBtnPause');
        dom.clearBtn.style.display = 'none'; dom.cancelBtn.style.display = 'none';
        _appendLog('warn', data.summary || t('plRejectedRestored'));
        break;
    }
  }

  function _resetProgress(phase, step) {
    dom.progressBar.style.width = '0%';
    dom.progressText.textContent = (phase || '') + ' › ' + (step || '');
  }

  function _setBadge(text, cls) {
    dom.statusBadge.textContent = text;
    dom.statusBadge.className = 'badge ' + cls;
  }

  function _formatPayload(pl) {
    const parts = [];
    if (pl.groups != null) parts.push(t('plGateGroups', { groups: pl.groups, count: pl.to_move || 0 }));
    if (pl.artifacts) {
      const a = pl.artifacts;
      parts.push(t('plScan') + ' ' + (a.scanned||0) + ' | ' + t('plScore') + ' ' + (a.scored||0) + ' | ' + t('plThreshold') + ' ' + (a.threshold_used||'—'));
      parts.push(t('plDedup') + ' ' + (a.dedup_to_move||0) + ' | ' + t('plReject') + ' ' + (a.reject||0) + ' | ' + t('plKeeper') + ' ' + (a.keep||0));
    }
    return parts.map(s => '<p style="font-size:13px;margin:4px 0">' + s + '</p>').join('');
  }

  // ── UI 渲染 ──
  function _showProgress(s) {
    dom.progressPanel.style.display = s ? '' : 'none';
    dom.logPanel.style.display = s ? '' : 'none';
    // 有运行内容时收起首屏空态，无内容时恢复
    if (dom.emptyState) dom.emptyState.style.display = s ? 'none' : '';
  }
  function _hideGate() { Modal.animatedHide(dom.gateModal); dom.gateModal.dataset.gateId = ''; }
  // 渲染挂起的门：phase 门(终审) → 审核面板(缩略图+通过/拒绝按钮)；
  // action/budget 门 → 文本弹窗。事件推送与 pollState 页面重进共用。
  function _showPendingGate(gate) {
    if (!gate || !gate.gate_id) return;
    _pendingGateId = gate.gate_id;
    dom.gateModal.dataset.gateId = gate.gate_id;
    const pl = gate.payload || {};
    if (gate.type === 'phase' && pl.artifacts) {
      _artifacts = pl.artifacts;
      _showReview(_artifacts);
      // Show accept/reject buttons for the user to approve the final result
      if (dom.acceptBtn) dom.acceptBtn.style.display = '';
      if (dom.rejectBtn) dom.rejectBtn.style.display = '';
      // Keep progress + log panels visible (don't hide them)
    } else {
      dom.gateTitle.textContent = gate.type === 'action' ? t('plGateConfirm') :
        gate.type === 'budget' ? t('plGateBudget') : t('plGateWait');
      dom.gateSummary.textContent = gate.summary || '';
      if (pl.artifacts) _artifacts = pl.artifacts;
      dom.gatePayload.innerHTML = _formatPayload(pl);
      Modal.animatedShow(dom.gateModal);
    }
  }
  function _hideReview() { dom.reviewPanel.style.display = 'none'; }
  function _showReview(a) {
    a = a || {}; dom.reviewPanel.style.display = '';
    // 确认/拒绝按钮默认隐藏——仅终审门挂起时由 _showPendingGate 显示，
    // 完成后(pipeline_done)留在面板上会走 no-gid 分支只改文案，误导用户
    if (dom.acceptBtn) dom.acceptBtn.style.display = 'none';
    if (dom.rejectBtn) dom.rejectBtn.style.display = 'none';
    dom.summaryGrid.innerHTML = [
      [t('plScan'), a.scanned||0], [t('plScore'), a.scored||0], [t('plThreshold'), a.threshold_used||'—'], [t('plDedup'), a.dedup_to_move||0],
      [t('plReject'), a.reject||0], [t('plBorderline'), a.borderline||0], [t('plUncertain'), a.uncertain||0], [t('plKeeper'), a.keep||0], [t('plAvgScore'), a.keep_avg_score||'—'],
    ].map(([k,v]) => '<div><div class="val">'+v+'</div><div class="lbl">'+k+'</div></div>').join('');
    const kp = a.keep_paths || [], rp = a.reject_paths || [], up = a.uncertain_paths || [];
    dom.keeperPreview.innerHTML =
      (kp.length ? '<div style="grid-column:1/-1;font-weight:600;margin-bottom:4px;font-size:13px">'+t('plKeeperCount', { count: kp.length })+'</div>' : '') +
      kp.slice(0,50).map(p => _thumb(p,'keep')).join('') +
      (up.length ? '<div style="grid-column:1/-1;font-weight:600;margin:8px 0 4px;font-size:13px;color:#f59e0b">'+t('plUncertainCount', { count: up.length })+'</div>' : '') +
      up.slice(0,30).map(p => _thumb(p,'uncertain')).join('') +
      (rp.length ? '<div style="grid-column:1/-1;font-weight:600;margin:8px 0 4px;font-size:13px;color:var(--text2)">'+t('plRejectCount', { count: rp.length })+'</div>' : '') +
      rp.slice(0,20).map(p => _thumb(p,'reject')).join('');
  }
  function _thumb(p, t) {
    const thumb = p.thumb || '', border = t==='keep' ? '2px solid var(--brand)' : t==='uncertain' ? '2px solid #f59e0b' : '1px solid var(--border)';
    const op = t==='reject' ? '0.5' : '1';
    const img = thumb
      ? '<img src="local-image://x/?p='+encodeURIComponent(thumb)+'" loading="lazy" style="width:100%;height:100%;object-fit:cover;border-radius:4px">'
      : '<div style="width:100%;height:100%;display:flex;align-items:center;justify-content:center;color:var(--text3);font-size:24px"><i class="ph ph-image"></i></div>';
    return '<div style="position:relative;width:140px;height:100px;background:var(--bg2);border-radius:6px;overflow:hidden;border:'+border+';opacity:'+op+'">'+img+
      '<div style="position:absolute;bottom:0;left:0;right:0;background:rgba(0,0,0,.6);padding:2px 6px;font-size:10px;color:#fff">'+ (p.score||0).toFixed(1)+(p.manual?' ★':'')+'</div></div>';
  }
  function _renderPhases() {
    dom.phaseTree.innerHTML = _phases.map(p =>
      '<span style="margin-right:6px;font-size:13px">'+(p.status==='done'?'✅':p.status==='running'?'🔄':'⏳')+' '+p.title+'</span>'
    ).join(' › ');
  }
  function _appendLog(level, msg) {
    const c = level==='error'?'var(--danger)':level==='warn'?'#f59e0b':'var(--text2)';
    dom.logList.innerHTML += '<div style="color:'+c+';padding:2px 0;border-bottom:1px solid var(--border);font-size:12px">['+new Date().toLocaleTimeString()+'] '+msg+'</div>';
    dom.logList.scrollTop = dom.logList.scrollHeight;
    _logCount++; if (dom.logCount) dom.logCount.textContent = _logCount;
  }
  function _reset() {
    _active = false; _runId = null; _state = { status: 'idle' };
    _pendingGateId = null;
    _showProgress(false); _hideGate(); _hideReview();
    dom.startBtn.disabled = false; _setBadge(t('ready'), 'badge-idle');
    dom.progressBar.style.width = '0%';
    dom.clearBtn.style.display = 'none'; dom.cancelBtn.style.display = ''; dom.pauseBtn.style.display = '';
  }

  function onPageEnter() { cacheDom(); pollState(); _loadTemplates(); }

  // ═══════════════════════════════════════════════════════════════════════
  // Template management
  // ═══════════════════════════════════════════════════════════════════════

  async function _loadTemplates() {
    try {
      const res = await window.api.sendCommand('list_templates', {});
      _templates = res.templates || [];
      _renderTemplateSelect();
    } catch (e) {
      console.error('[pipeline] 加载模板列表失败:', e);
      // Fallback: keep hardcoded option
      if (_templates.length === 0) {
        _templates = [{ id: 'cull_pipeline', title: t('plDefaultTemplate'), source: 'builtin' }];
        _renderTemplateSelect();
      }
    }
  }

  function _renderTemplateSelect() {
    if (!dom.template) return;
    const currentValue = dom.template.value;

    // 分组
    const builtins = _templates.filter(t => t.source === 'builtin');
    const customs = _templates.filter(t => t.source === 'custom');

    let html = '';
    if (builtins.length > 0) {
      html += '<optgroup label="' + t('plBuiltinTemplates') + '">';
      for (const t of builtins) {
        html += '<option value="' + _escAttr(t.id) + '">' + _escAttr(t.title || t.id) + '</option>';
      }
      html += '</optgroup>';
    }
    if (customs.length > 0) {
      html += '<optgroup label="' + t('plCustomTemplates') + '">';
      for (const t of customs) {
        html += '<option value="' + _escAttr(t.id) + '">' + _escAttr(t.title || t.id) + '</option>';
      }
      html += '</optgroup>';
    }
    // 兜底：如果没有任何模板（IPC 返回空），保留初始硬编码选项
    if (!html) {
      html = '<option value="cull_pipeline">' + t('plDefaultTemplate') + '</option>';
    }

    dom.template.innerHTML = html;

    // 恢复之前的选择
    if (currentValue) {
      const exists = _templates.some(t => t.id === currentValue);
      if (exists) dom.template.value = currentValue;
      else _loadConfig(); // 从 localStorage 恢复
    } else {
      _loadConfig();
    }

    _onTemplateChange();
    _renderEmptyTplButtons();
  }

  // 首屏空态的高频模板快捷按钮：点击等同在配置面板里选中该模板
  function _renderEmptyTplButtons() {
    if (!dom.emptyTpls) return;
    const tops = _templates.slice(0, 3);
    dom.emptyTpls.innerHTML = tops.map(tpl =>
      '<button class="btn btn-outline btn-sm" data-tpl="' + _escAttr(tpl.id) + '">' +
      '<i class="ph ph-lightning"></i> ' + _escAttr(tpl.title || tpl.id) + '</button>'
    ).join('');
    dom.emptyTpls.querySelectorAll('button[data-tpl]').forEach(btn => {
      btn.addEventListener('click', () => {
        if (dom.template) {
          dom.template.value = btn.dataset.tpl;
          _onTemplateChange();
        }
        if (dom.browseBtn) dom.browseBtn.focus();
      });
    });
  }

  function _onTemplateChange() {
    if (!dom.template) return;
    const selectedId = dom.template.value;
    const tpl = _templates.find(t => t.id === selectedId);
    const isCustom = tpl && tpl.source === 'custom';
    // 只有自定义模板可删除，内置模板不可
    if (dom.deleteBtn) dom.deleteBtn.style.display = isCustom ? '' : 'none';
  }

  // ═══════════════════════════════════════════════════════════════════════
  // AI 生成工作流
  // ═══════════════════════════════════════════════════════════════════════

  function _openGenModal() {
    if (!dom.genModal) return;
    // 预填目标描述（复用配置面板里的目标输入）
    if (dom.genGoal && dom.goal && dom.goal.value.trim() && !dom.genGoal.value.trim()) {
      dom.genGoal.value = dom.goal.value.trim();
    }
    // 填充基底模板下拉（复用模板清单）
    if (dom.genBase) {
      let html = '<option value="">' + t('plGenFromScratch') + '</option>';
      const builtinTag = t('plBuiltinTag');
      for (const t of _templates) {
        html += '<option value="' + _escAttr(t.id) + '">' + _escAttr(t.title || t.id) +
                (t.source === 'builtin' ? builtinTag : '') + '</option>';
      }
      dom.genBase.innerHTML = html;
      // 默认选中当前配置面板里的模板作为基底
      if (dom.template && dom.template.value) dom.genBase.value = dom.template.value;
    }
    if (dom.genStatus) { dom.genStatus.textContent = ''; dom.genStatus.style.color = 'var(--text2)'; }
    dom.genRunBtn.disabled = false;
    Modal.animatedShow(dom.genModal);
  }

  function _closeGenModal() {
    if (dom.genModal) Modal.animatedHide(dom.genModal);
  }

  async function _runGenerate() {
    const goal = (dom.genGoal.value || '').trim();
    if (!goal) {
      dom.genStatus.style.color = 'var(--danger)';
      dom.genStatus.textContent = t('plGenNeedGoal');
      return;
    }
    const base = dom.genBase.value || '';
    dom.genRunBtn.disabled = true;
    dom.genStatus.style.color = 'var(--text2)';
    dom.genStatus.textContent = t('plGenRunning');
    try {
      const resp = await window.api.sendCommand('generate_pipeline_template', {
        goal: goal, base_template: base || undefined,
      });
      if (!resp) throw new Error(t('plGenNoResponse'));
      if (resp.error === 'llm_not_configured') {
        dom.genStatus.style.color = 'var(--danger)';
        dom.genStatus.textContent = t('plGenLLMRequired');
        return;
      }
      if (resp.template) {
        // 成功：关闭弹窗，以草稿形式打开工作流编辑器
        _closeGenModal();
        if (typeof WorkflowEditor !== 'undefined') {
          WorkflowEditor.openEditor(resp.template, { draft: true });
        } else {
          App.toast(t('wfModuleNotLoaded'), 'error');
        }
        return;
      }
      // 失败：显示错误清单
      const errs = resp.errors && resp.errors.length ? resp.errors : [resp.error || t('plGenFailed')];
      dom.genStatus.style.color = 'var(--danger)';
      dom.genStatus.textContent = t('plGenFailedList', { errors: errs.slice(0, 5).join('\n') });
    } catch (e) {
      dom.genStatus.style.color = 'var(--danger)';
      dom.genStatus.textContent = t('plGenFailed') + ': ' + (e.message || e);
    } finally {
      dom.genRunBtn.disabled = false;
    }
  }

  function _editTemplate() {
    if (!dom.template) return;
    const templateId = dom.template.value;
    if (!templateId) return;
    const tpl = _templates.find(t => t.id === templateId);
    if (!tpl) return;

    if (typeof WorkflowEditor === 'undefined') {
      App.toast(t('wfModuleNotLoaded'), 'error');
      return;
    }

    if (tpl.source === 'builtin') {
      // 内置模板 → 克隆后编辑
      WorkflowEditor.cloneAndEdit(templateId);
    } else {
      // 自定义模板 → 直接编辑
      WorkflowEditor.openEditor(templateId);
    }
  }

  function _cloneTemplate() {
    if (!dom.template) return;
    const templateId = dom.template.value;
    if (!templateId) return;
    if (typeof WorkflowEditor === 'undefined') {
      App.toast(t('wfModuleNotLoaded'), 'error');
      return;
    }
    WorkflowEditor.cloneAndEdit(templateId);
  }

  function _deleteTemplate() {
    if (!dom.template) return;
    const templateId = dom.template.value;
    if (!templateId) return;
    const tpl = _templates.find(t => t.id === templateId);
    if (!tpl || tpl.source === 'builtin') {
      App.toast(t('wfBuiltinReadonly'), 'warning');
      return;
    }
    if (typeof WorkflowEditor === 'undefined') {
      App.toast(t('wfModuleNotLoaded'), 'error');
      return;
    }
    WorkflowEditor.deleteTemplate(templateId);
  }

  // 供 WorkflowEditor 查找模板
  function getTemplates() { return _templates; }

  function _escAttr(s) {
    if (!s) return '';
    return String(s).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }
  async function pollState() {
    const folder = dom.folder?.value?.trim(); if (!folder) return;
    try {
      const st = await window.api.sendCommand('pipeline_get_state', { work_folder: folder });
      if (!st || st.status === 'idle') return;
      _state = st; _artifacts = st.artifacts || {};
      if (st.run_id) _runId = st.run_id;
      // 活跃 run 挂起的门(对象)才可重渲染；磁盘 run_state.json 里的 pending_gate 只是 bool
      const gate = (st.pending_gate && st.pending_gate.gate_id) ? st.pending_gate : null;
      if (st.status === 'running') {
        _active = true; _showProgress(true);
        dom.startBtn.disabled = true; _setBadge(t('running'), 'badge-running');
        dom.pauseBtn.textContent = t('plBtnPause');
        if (st.phase_idx != null) dom.progressText.textContent = 'Phase ' + (st.phase_idx + 1);
        if (gate) _showPendingGate(gate);  // 门/终审挂起中：重渲染，而不是藏掉
        else { _hideReview(); _hideGate(); }
      } else if (st.status === 'paused') {
        _showProgress(true); _hideReview();
        _setBadge(t('plInterrupted'), 'badge-warn');
        dom.pauseBtn.textContent = t('plBtnResume'); dom.pauseBtn.style.display = '';
        dom.progressText.textContent = t('plLastInterruptedAt', { n: (st.phase_idx||0)+1 });
        if (gate) {
          // 预算门等人中(status=paused + pending gate)：重渲染门弹窗，保持取消可用
          _active = true; dom.startBtn.disabled = true;
          dom.clearBtn.style.display = 'none'; dom.cancelBtn.style.display = '';
          _showPendingGate(gate);
        } else {
          // 无进程内 run 的断点(或纯暂停)：续跑按钮走 pipeline_resume(后端自动选路径)
          _active = false; dom.startBtn.disabled = false;
          dom.clearBtn.style.display = ''; dom.cancelBtn.style.display = 'none';
        }
      } else if (st.status === 'done') {
        _active = false; dom.startBtn.disabled = false;
        _setBadge(t('plLastDone'), 'badge-success'); _showProgress(true); _showReview(_artifacts);
      } else if (st.status === 'error') {
        _active = false; dom.startBtn.disabled = false;
        _setBadge(t('plLastFailed'), 'badge-error'); _showProgress(true);
      } else if (st.status === 'rejected') {
        // 终审拒绝的终态：已复原，不显示完成态/审核面板
        _active = false; dom.startBtn.disabled = false;
        _setBadge(t('plRejectedRestored'), 'badge-warn'); _showProgress(true);
        _hideReview(); _hideGate();
        dom.clearBtn.style.display = 'none'; dom.cancelBtn.style.display = 'none';
      }
    } catch (_) {}
  }

  return { init, onPageEnter, handleEvent, pollState, getTemplates };
})();

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => PipelineUI.init());
else PipelineUI.init();
