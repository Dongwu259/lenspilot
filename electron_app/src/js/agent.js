/**
 * agent.js — AI 助手 Agent 模块前端
 *
 * 对话式 Agent + 定时任务 + 快捷启动。所有入口都汇入 agent_run 命令。
 * 后端 agent_module.py 运行 OpenAI 兼容 function-calling 循环，进程内调用
 * photo_scorer 的工具，沙箱限制在工作文件夹内。
 *
 * 事件: agent_thinking / agent_tool_call / agent_tool_progress /
 *       agent_tool_result / agent_permission_request / agent_permission_resolved /
 *       agent_iteration / agent_done / agent_error / agent_log
 */

const Agent = {
  workFolder: '',
  sessionId: null,
  compactContext: '',
  _multimodalOn: false,
  running: false,
  _thinkEl: null,           // current streaming assistant text bubble
  _thinkBuf: '',
  _thinkRAF: null,
  _toolCards: {},           // call_id -> card element
  _pendingApprovals: {},    // request_id -> card element
  tasks: [],
  _timers: {},              // task.id -> setTimeout handle
  log: [],
  transcript: [],           // persisted conversation records
  _saveTimer: null,
  prompts: [],              // prompt library cache
  taskState: {},            // taskId -> {last_run_ts, history:[{ts,status,...}]}
  _firedSessions: {},       // session_id -> {taskId, retried, prompt} (headless correlation)

  // ── Lifecycle ──
  init() {
    if (this._initialized) return;
    this._initialized = true;
    this._bindChat();
    this._bindSettings();
    this._bindTasks();
    this._bindDashboard();
    this._bindKBPrompts();
    this._fillTimeSelects();
    this.loadTasks();
    this.armAll();
    this.refreshSettings();
    this.refreshApiKeyGate();
    this._loadHistory();
    this._renderTranscript();
    this._refreshPrompts();
    this._loadTaskState();
    // Auto-save on close / navigate away.
    window.addEventListener('beforeunload', () => { this._saveHistoryNow(); this._saveTaskState(); });
  },

  onPageEnter() {
    this.refreshSettings();
    this.renderTasks();
    // scroll messages to bottom
    const m = document.getElementById('agent-messages');
    if (m) m.scrollTop = m.scrollHeight;
  },

  _uid() {
    return 'a' + Math.random().toString(36).slice(2, 10) + Date.now().toString(36).slice(-4);
  },

  _esc(s) {
    return UI._escapeHtml(s);
  },

  // ════════════════════════════════════════════════════════════════
  // Chat UI
  // ════════════════════════════════════════════════════════════════
  _bindChat() {
    const $ = id => document.getElementById(id);
    $('btn-agent-folder').addEventListener('click', async () => {
      const f = await window.api.selectFolder();
      if (f) this._setFolder(f);
    });
    // Single dual-purpose button: send when idle, stop when running.
    $('btn-agent-send').addEventListener('click', () => {
      if (this.running) this.cancel(); else this.send();
    });
    $('agent-prompt').addEventListener('keydown', e => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); this.send(); }
    });
    // Auto-grow the textarea up to a max height.
    $('agent-prompt').addEventListener('input', e => {
      e.target.style.height = 'auto';
      e.target.style.height = Math.min(200, e.target.scrollHeight) + 'px';
    });
    // Panel toggles
    $('btn-agent-settings').addEventListener('click', () => this._openSettings());
    $('agent-settings-close').addEventListener('click', () => this._closeSettings());
    $('btn-agent-task').addEventListener('click', () => this._openTaskPanel());
    $('agent-task-close').addEventListener('click', () => this._closeTaskPanel());
    $('btn-agent-usage').addEventListener('click', () => this._openUsage());
    $('agent-usage-close').addEventListener('click', () => this._closeUsage());
    $('btn-agent-usage-clear').addEventListener('click', () => this._clearUsage());
    $('btn-agent-export').addEventListener('click', () => this.exportHistory());
    $('btn-agent-import').addEventListener('click', () => this.importHistory());
    $('btn-agent-clear').addEventListener('click', () => this.clearHistory());
    // 顶栏调试数据(迭代/token)默认隐藏，ⓘ 按钮切换显隐
    $('btn-agent-budget').addEventListener('click', () => {
      $('agent-budget').classList.toggle('budget-hidden');
    });
    // Destructive-mode select in the settings panel syncs the hidden field.
    $('agent-autoapprove-select').addEventListener('change', e => {
      $('agent-autoapprove').value = e.target.value;
    });
  },

  _setFolder(f) {
    this.workFolder = f;
    const hidden = document.getElementById('agent-folder');
    const label = document.getElementById('agent-folder-label');
    if (hidden) hidden.value = f;
    if (label) {
      const short = f.length > 36 ? '…' + f.slice(-34) : f;
      label.textContent = short;
      label.title = f;
    }
  },

  // 面板退场动画：先加 .slide-out 滑出，200ms 后再隐藏；重开时取消未完成的关闭
  _showPanel(id) {
    const el = document.getElementById(id);
    if (!el) return;
    if (el._slideTimer) { clearTimeout(el._slideTimer); el._slideTimer = null; }
    el.classList.remove('slide-out');
    el.classList.remove('hidden');
  },
  _hidePanel(id) {
    const el = document.getElementById(id);
    if (!el || el.classList.contains('hidden') || el._slideTimer) return;
    el.classList.add('slide-out');
    el._slideTimer = setTimeout(() => {
      el._slideTimer = null;
      el.classList.add('hidden');
      el.classList.remove('slide-out');
    }, 200);
  },

  _openTaskPanel() { this._showPanel('agent-task-panel'); this.renderTasks(); },
  _closeTaskPanel() { this._hidePanel('agent-task-panel'); },
  _openSettings() { this._showPanel('agent-settings-panel'); this.refreshSettings(); this._refreshKB(); this._refreshPrompts(); },
  _closeSettings() { this._hidePanel('agent-settings-panel'); },
  _openUsage() { this._showPanel('agent-usage-panel'); this.refreshUsage(); },
  _closeUsage() { this._hidePanel('agent-usage-panel'); },

  async refreshUsage() {
    try {
      const r = await window.api.sendCommand('agent_get_usage', {});
      this._renderUsage(r || {});
    } catch (e) { /* backend not ready */ }
  },

  async _clearUsage() {
    if (!(await Modal.showConfirm(t('agentClearUsageTitle'), t('agentClearUsageMsg')))) return;
    try {
      await window.api.sendCommand('agent_clear_usage', {});
      this.refreshUsage();
      App.toast(t('agentCleared'), 'success');
    } catch (e) { App.toast(t('agentClearFail', { msg: e.message }), 'error'); }
  },

  _renderUsage(data) {
    const t = (data.total) || { prompt: 0, completion: 0, cached: 0, total: 0, runs: 0 };
    const stats = document.getElementById('agent-usage-stats');
    if (stats) stats.innerHTML = `
      <div class="agent-usage-stat"><div class="v">${this._fmtTokens(t.total)}</div><div class="l">${I18N.t('agentUsageTotal')}</div></div>
      <div class="agent-usage-stat"><div class="v">${this._fmtTokens(t.prompt)}</div><div class="l">${I18N.t('agentUsageInput')}</div></div>
      <div class="agent-usage-stat"><div class="v">${this._fmtTokens(t.completion)}</div><div class="l">${I18N.t('agentUsageOutput')}</div></div>
      <div class="agent-usage-stat"><div class="v">${this._fmtTokens(t.cached)}</div><div class="l">${I18N.t('agentUsageCached')}</div></div>`;
    this._renderUsageChart(data.history || []);
    const hist = document.getElementById('agent-usage-history');
    if (hist) {
      const rows = (data.history || []).slice().reverse();
      if (!rows.length) {
        hist.innerHTML = `<div style="color:var(--text3);padding:12px;text-align:center">${I18N.t('noRecords')}</div>`;
      } else {
        hist.innerHTML = rows.map(r => {
          const d = new Date((r.ts || 0) * 1000).toLocaleString('zh-CN', { month:'short', day:'numeric', hour:'2-digit', minute:'2-digit' });
          const cache = r.cached_tokens ? ` <span style="color:var(--green)">${I18N.t('agentCacheShort', { n: this._fmtTokens(r.cached_tokens) })}</span>` : '';
          return `<div class="agent-usage-row">
            <span class="t">${d}</span>
            <span class="n" title="${this._esc(r.prompt || '')}">${this._esc((r.task_name || r.prompt || '').slice(0, 40))} · ${I18N.t('agentIterRound', { n: r.iterations || 0 })}</span>
            <span class="tk">${this._fmtTokens(r.total_tokens)}${cache}</span>
          </div>`;
        }).join('');
      }
    }
  },

  _renderUsageChart(history) {
    const box = document.getElementById('agent-usage-chart');
    if (!box) return;
    const W = 600, H = 140, P = 28;
    const recent = history.slice(-30);
    if (recent.length < 2) {
      box.innerHTML = `<div style="color:var(--text3);font-size:12px;text-align:center;padding:40px 0">${t('agentUsageChartEmpty')}</div>`;
      return;
    }
    const max = Math.max(...recent.map(r => r.total_tokens || 0), 1);
    const bw = (W - P * 2) / recent.length;
    const bars = recent.map((r, i) => {
      const h = Math.max(2, ((r.total_tokens || 0) / max) * (H - P * 2));
      const x = P + i * bw;
      const y = H - P - h;
      const cached = r.cached_tokens || 0;
      const ch = Math.max(0, (cached / max) * (H - P * 2));
      const title = `${this._fmtTokens(r.total_tokens)} · ${t('agentCacheLabel', { n: this._fmtTokens(cached) })}`;
      // cached portion (green) stacked on bottom, rest brand
      return `<title>${title}</title>` +
        `<rect x="${x + 1}" y="${H - P - ch}" width="${Math.max(1, bw - 2)}" height="${ch}" fill="var(--green)" opacity="0.6"/>` +
        `<rect x="${x + 1}" y="${y}" width="${Math.max(1, bw - 2)}" height="${h - ch}" fill="var(--brand)"/>`;
    }).join('');
    // baseline + max label
    box.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
      <line x1="${P}" y1="${H - P}" x2="${W - P}" y2="${H - P}" stroke="var(--border)"/>
      <text x="${W - P}" y="${H - P - 4}" text-anchor="end" font-size="10" fill="var(--text3)">${this._fmtTokens(max)}</text>
      ${bars}
    </svg>
    <div style="font-size:11px;color:var(--text3);margin-top:6px"><span style="display:inline-block;width:10px;height:10px;background:var(--brand);border-radius:2px;vertical-align:middle"></span> ${t('agentLegendCost')} &nbsp; <span style="display:inline-block;width:10px;height:10px;background:var(--green);border-radius:2px;vertical-align:middle"></span> ${t('agentLegendCache', { count: recent.length })}</div>`;
  },

  async send() {
    if (this.running) { this.cancel(); return; }
    const folder = document.getElementById('agent-folder').value.trim() || this.workFolder;
    let prompt = document.getElementById('agent-prompt').value.trim();
    const autoApprove = document.getElementById('agent-autoapprove').value === 'true';
    if (!prompt) { App.toast(t('agentNeedInput'), 'warning'); return; }

    // ── Slash commands (handled locally, never sent to backend) ──
    if (prompt.startsWith('/')) {
      this._handleSlashCommand(prompt.trim());
      document.getElementById('agent-prompt').value = '';
      return;
    }

    // folder may be empty — the model can set it via set_work_folder if the
    // user gave a path in the prompt.

    this.workFolder = folder;

    // Prepend compact context if available (consumed once, then cleared)
    if (this.compactContext) {
      prompt = `[对话上下文]\n${this.compactContext}\n\n[新指令]\n${prompt}`;
      this.compactContext = '';
      // Update badge
      const badge = document.getElementById('agent-context-badge');
      if (badge) badge.classList.add('hidden');
    }

    this._addUserCard(prompt);
    document.getElementById('agent-prompt').value = '';
    document.getElementById('agent-prompt').style.height = 'auto';
    this._setRunning(true);
    this._thinkEl = null;
    this._thinkBuf = '';
    if (this._thinkRAF) { cancelAnimationFrame(this._thinkRAF); this._thinkRAF = null; }
    this._toolCards = {};
    this._pendingApprovals = {};

    try {
      // Set sessionId up-front so early streaming events (which can arrive
      // before the agent_run promise resolves) are routed to the chat.
      const sid = this._uid();
      this.sessionId = sid;
      const r = await window.api.sendCommand('agent_run', {
        session_id: sid,
        work_folder: folder,
        prompt,
        auto_approve_destructive: autoApprove,
        headless: false,
        model: localStorage.getItem('agent_model') || undefined,
        cloud_token: localStorage.getItem('studio_token') || '',
      });
      if (r && r.ok) {
        this.sessionId = r.session_id || sid;
        // Crash recovery: previous session was interrupted
        if (r.recovery) {
          const rec = r.recovery;
          this._addCard('system', t('agentRecovery', { iter: rec.iteration, msgs: rec.message_count, undos: rec.undo_stack_size }));
        }
      } else {
        this._setRunning(false);
        this.sessionId = null;
        const err = (r && r.error) || t('agentStartFail');
        if (err === 'agent_busy') App.toast(t('agentBusy'), 'warning');
        else App.toast(err, 'error');
        this._addCard('error', `❌ ${err}`);
      }
    } catch (e) {
      this._setRunning(false);
      this.sessionId = null;
      App.toast(t('agentStartFailMsg', { msg: e.message }), 'error');
    }
  },

  async cancel() {
    if (!this.sessionId) return;
    try { await window.api.sendCommand('agent_cancel', { session_id: this.sessionId }); }
    catch (e) { /* ignore */ }
    App.toast(t('agentStopping'), 'info');
  },

  quickLaunch({ folder, selectedPaths }) {
    if (!folder) { App.toast(t('agentNoFolder'), 'warning'); return; }
    this._setFolder(folder);
    const def = selectedPaths && selectedPaths.length
      ? t('agentQuickPromptSel', { count: selectedPaths.length })
      : t('agentQuickPrompt');
    const inp = document.getElementById('agent-prompt');
    inp.value = def;
    App.setPage('agent');
    setTimeout(() => { inp.focus(); inp.style.height = 'auto'; inp.style.height = Math.min(200, inp.scrollHeight) + 'px'; }, 200);
    App.toast(t('agentQuickFilled'), 'info');
  },

  // ── Dashboard hero chat ──
  _bindDashboard() {
    const $ = id => document.getElementById(id);
    const ta = $('dash-prompt');
    if (!ta) return;
    ta.addEventListener('keydown', e => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); this.dashboardSend(); }
    });
    ta.addEventListener('input', e => {
      e.target.style.height = 'auto';
      e.target.style.height = Math.min(200, e.target.scrollHeight) + 'px';
    });
    $('btn-dash-send').addEventListener('click', () => this.dashboardSend());
    $('btn-dash-goto-settings').addEventListener('click', () => this.openSettings());
    // 官方 API 已停用: 历史按钮不再存在, 保留防护避免空引用
    const dashOfficial = $('btn-dash-use-official');
    if (dashOfficial) dashOfficial.addEventListener('click', async () => {
      await this._fetchOfficialKey();
      this.refreshApiKeyGate();
    });
  },

  dashboardSend() {
    if (!this.apiKeySet) { this.openSettings(); return; }
    const ta = document.getElementById('dash-prompt');
    const prompt = (ta && ta.value.trim()) || '';
    if (!prompt) return;
    // Carry the prompt to the AI助手 page and let the user pick a folder there.
    const inp = document.getElementById('agent-prompt');
    if (inp) {
      inp.value = prompt;
      inp.style.height = 'auto';
      inp.style.height = Math.min(200, inp.scrollHeight) + 'px';
    }
    ta.value = '';
    ta.style.height = 'auto';
    App.setPage('agent');
    setTimeout(() => { if (inp) inp.focus(); }, 220);
    if (!this.workFolder) App.toast(t('agentDashFolderHint'), 'info');
  },

  async refreshApiKeyGate() {
    let set = false;
    try {
      const r = await window.api.sendCommand('agent_get_config', {});
      if (r && r.agent) {
        if (r.agent.api_key_set) {
          set = true;  // personal key configured
        } else if (r.agent.use_local && r.agent.local_model_present) {
          set = true;  // 本地模型已就绪（官方 key 已停用）
        } else if (r.agent.official_key_set) {
          set = true;  // (legacy: 恒为 false)
        }
      }
    } catch (e) { /* backend not ready */ }
    this.apiKeySet = set;
    const warn = document.getElementById('dash-api-warn');
    const ta = document.getElementById('dash-prompt');
    if (warn) warn.classList.toggle('hidden', set);
    if (ta) {
      ta.disabled = !set;
      ta.placeholder = set
        ? t('dashHeroPlaceholder')
        : t('agentDashNoKey');
    }
    return set;
  },

  // Jump interface: switch to AI助手 page and open its settings (gear) panel.
  openSettings() {
    App.setPage('agent');
    // setPage→onPageEnter runs synchronously; open the gear panel after.
    setTimeout(() => this._openSettings(), 60);
  },

  _setRunning(on) {
    this.running = on;
    const btn = document.getElementById('btn-agent-send');
    const badge = document.getElementById('agent-status-badge');
    if (btn) {
      btn.classList.toggle('stop', on);
      btn.innerHTML = on ? '<i class="ph ph-stop"></i>' : '<i class="ph ph-paper-plane-tilt"></i>';
      btn.title = on ? t('stop') : t('agentSend');
    }
    if (badge) badge.textContent = on ? t('running') : t('ready');
  },

  _addUserCard(text) {
    this._addCard('user', `<div class="agent-bubble user">${this._esc(text)}</div>`);
    this._tPush({ type: 'user', text });
  },

  _addCard(kind, html) {
    const wrap = document.getElementById('agent-messages');
    // Remove the empty-state placeholder on first message.
    const empty = wrap.querySelector('.agent-empty');
    if (empty) empty.remove();
    const el = document.createElement('div');
    el.className = `agent-msg agent-${kind}`;
    el.innerHTML = html;
    wrap.appendChild(el);
    wrap.scrollTop = wrap.scrollHeight;
    return el;
  },

  // Finalize the streaming assistant bubble: render any buffered text, record
  // it to the transcript, and detach. Called before a tool/done/error event.
  _finalizeThinkBubble() {
    if (this._thinkRAF) { cancelAnimationFrame(this._thinkRAF); this._thinkRAF = null; }
    if (this._thinkEl && this._thinkBuf) this._thinkEl.innerHTML = this._md(this._thinkBuf);
    if (this._thinkBuf && this._thinkBuf.trim()) this._tPush({ type: 'assistant', text: this._thinkBuf });
    this._thinkBuf = '';
    this._thinkEl = null;
  },

  // ── Conversation history (transcript) persistence ──
  _historyKey() { return 'agent_history_' + (App.user?.id || 'default'); },
  _loadHistory() {
    try { this.transcript = JSON.parse(localStorage.getItem(this._historyKey()) || '[]') || []; }
    catch (e) { this.transcript = []; }
  },
  _saveHistoryNow() {
    try { localStorage.setItem(this._historyKey(), JSON.stringify(this.transcript)); }
    catch (e) { /* quota — ignore */ }
  },
  _saveHistoryDebounced() {
    if (this._saveTimer) clearTimeout(this._saveTimer);
    this._saveTimer = setTimeout(() => { this._saveTimer = null; this._saveHistoryNow(); }, 400);
  },
  _tPush(rec) {
    this.transcript.push(rec);
    this._saveHistoryDebounced();
  },

  _renderTranscript() {
    const wrap = document.getElementById('agent-messages');
    if (!wrap) return;
    wrap.innerHTML = '';
    if (!this.transcript.length) {
      wrap.innerHTML = `<div class="agent-empty">
        <i class="ph ph-sparkle" style="font-size:40px;color:var(--text3)"></i>
        <div style="margin-top:12px;font-size:15px;color:var(--text2)">${t('agentDesc')}</div>
        <div style="margin-top:6px;font-size:12px;color:var(--text3);max-width:420px;text-align:center">${t('agentEmptyHint')}</div>
      </div>`;
      return;
    }
    this.transcript.forEach(rec => this._appendTranscriptCard(rec, wrap));
    wrap.scrollTop = wrap.scrollHeight;
  },

  _appendTranscriptCard(rec, wrap) {
    const el = document.createElement('div');
    el.className = `agent-msg agent-${rec.type}`;
    if (rec.type === 'user') {
      el.innerHTML = `<div class="agent-bubble user">${this._esc(rec.text)}</div>`;
    } else if (rec.type === 'assistant') {
      el.innerHTML = `<div class="agent-bubble assistant agent-md">${this._md(rec.text || '')}</div>`;
    } else if (rec.type === 'tool') {
      el.innerHTML = this._toolCardHTML(rec);
    } else if (rec.type === 'perm') {
      const approved = rec.approved;
      const state = approved === true ? t('agentPermStateApproved') : approved === false ? t('agentPermStateDenied') : t('agentWaitingConfirm');
      el.innerHTML = `<div class="agent-perm-card">
        <div class="agent-perm-head"><i class="ph ph-warning"></i> ${t('agentPermNeedConfirm', { tool: this._esc(rec.tool) })}</div>
        <div class="agent-perm-summary">${this._esc(rec.summary || '')}</div>
        <div class="agent-perm-buttons"><span style="font-size:12px;color:var(--text3)">${state}</span></div>
      </div>`;
    } else if (rec.type === 'done') {
      el.innerHTML = `<div class="agent-bubble done agent-md"><b>${t('agentDoneLabel')}</b>${this._md(rec.summary || '')}<div class="agent-stats">${this._esc(rec.stats || '')}</div></div>`;
    } else if (rec.type === 'ask') {
      el.innerHTML = `<div class="agent-perm-card" style="border-color:var(--brand)">
        <div class="agent-perm-head"><i class="ph ph-question"></i> ${t('agentAskTitle')}</div>
        <div class="agent-perm-summary">${this._esc(rec.question || '')}</div>
        <div class="agent-perm-buttons"><span style="font-size:12px;color:var(--text3)">${rec.answer ? t('agentAnswered', { answer: this._esc(rec.answer) }) : t('agentUnanswered')}</span></div>
      </div>`;
    } else if (rec.type === 'error') {
      el.innerHTML = `<div class="agent-bubble error">❌ ${this._esc(rec.error || t('unknownError'))} <span style="font-size:11px;color:var(--text3)">(${this._esc(rec.phase || '')})</span></div>`;
    }
    wrap.appendChild(el);
    // wire tool-card collapse
    if (rec.type === 'tool') {
      const head = el.querySelector('.agent-tool-head');
      if (head) head.addEventListener('click', () => {
        const det = el.querySelector('.agent-tool-detail');
        const caret = el.querySelector('.agent-tool-caret');
        const open = !det.classList.contains('hidden');
        det.classList.toggle('hidden', open);
        if (caret) caret.className = 'ph agent-tool-caret ' + (open ? 'ph-caret-right' : 'ph-caret-down');
      });
    }
  },

  _toolCardHTML(rec) {
    const status = rec.ok === null ? t('agentStatusRunning') : (rec.ok ? t('agentDoneLabel') : t('agentFailLabel'));
    const resultHTML = rec.result != null
      ? `<div class="agent-tool-result"><div class="agent-tool-section">${t('agentToolOutput')} <pre>${this._esc(this._fmtResult(rec.result))}</pre></div></div>`
      : '';
    return `<div class="agent-tool-card">
      <div class="agent-tool-head"><i class="ph ph-caret-right agent-tool-caret"></i> <i class="ph ph-wrench"></i> <b>${this._esc(rec.tool)}</b>
        <span class="agent-tool-status">${status}</span></div>
      <div class="agent-tool-detail hidden">
        <div class="agent-tool-section">${t('agentToolInput')} <pre>${this._esc(this._fmtArgs(rec.args))}</pre></div>
        ${resultHTML}
      </div>
    </div>`;
  },

  async clearHistory() {
    if (this.running) { App.toast(t('agentStopFirst'), 'warning'); return; }
    if (!this.transcript.length) { App.toast(t('agentNoHistory'), 'info'); return; }
    if (!(await Modal.showConfirm(t('agentClearTitle'), t('agentClearConfirm')))) return;
    this.transcript = [];
    this._saveHistoryNow();
    this._renderTranscript();
    App.toast(t('agentClearedHistory'), 'success');
  },

  exportHistory() {
    if (!this.transcript.length) { App.toast(t('agentNoExport'), 'info'); return; }
    const payload = { app: 'AutoPhotoStudio', version: 1, exported: new Date().toISOString(), transcript: this.transcript };
    window.api.exportHistory(JSON.stringify(payload, null, 2), 'agent-history.json').then(r => {
      if (r && r.ok) App.toast(t('agentExportedTo', { path: r.path }), 'success');
      else if (r && r.error) App.toast(t('agentExportFail', { msg: r.error }), 'error');
    }).catch(e => App.toast(t('agentExportFail', { msg: e.message }), 'error'));
  },

  async importHistory() {
    if (this.running) { App.toast(I18N.t('agentStopFirst'), 'warning'); return; }
    try {
      const r = await window.api.importHistory();
      if (!r || !r.ok) { if (r && r.error) App.toast(I18N.t('agentImportFail', { msg: r.error }), 'error'); return; }
      let data;
      try { data = JSON.parse(r.content); }
      catch (e) { App.toast(I18N.t('agentInvalidJson'), 'error'); return; }
      const t = Array.isArray(data) ? data : data.transcript;
      if (!Array.isArray(t)) { App.toast(I18N.t('agentNoRecordsInFile'), 'error'); return; }
      if (this.transcript.length && !(await Modal.showConfirm(I18N.t('agentImportTitle'), I18N.t('agentImportReplace')))) return;
      this.transcript = t;
      this._saveHistoryNow();
      this._renderTranscript();
      App.toast(I18N.t('agentImported', { count: t.length }), 'success');
    } catch (e) { App.toast(I18N.t('agentImportFail', { msg: e.message }), 'error'); }
  },

  // ══════════════════════════════════════════════════════════════════════
  // Slash Commands
  // ══════════════════════════════════════════════════════════════════════

  _showSystemMsg(text) {
    this._addCard('system', `<div class="agent-bubble system" style="font-family:'SF Mono','Menlo','Consolas',monospace;font-size:12px;white-space:pre-wrap">${this._esc(text)}</div>`);
    const el = document.getElementById('agent-messages');
    if (el) el.scrollTop = el.scrollHeight;
  },

  _handleSlashCommand(raw) {
    const parts = raw.slice(1).trim().split(/\s+/);
    const cmd = (parts[0] || '').toLowerCase();
    const args = parts.slice(1);
    if (!cmd) { this._showSystemMsg(t('agentSlashHelpHint')); return; }
    const entry = this.commands[cmd];
    if (!entry) { this._showSystemMsg(t('agentUnknownCmd', { cmd })); return; }
    try {
      const result = this[entry.fn](args);
      if (result instanceof Promise) {
        result.then(r => { if (r !== undefined && r !== null) this._showSystemMsg(String(r)); })
              .catch(e => this._showSystemMsg(t('agentCmdError', { msg: e.message })));
      } else if (result !== undefined && result !== null) {
        this._showSystemMsg(String(result));
      }
    } catch (e) {
      this._showSystemMsg(t('agentCmdError', { msg: e.message }));
    }
  },

  // ── Individual command handlers ──

  _cmdHelp() {
    const lines = [t('agentCmdListTitle'), ''];
    for (const [name, entry] of Object.entries(this.commands)) {
      lines.push(`  /${name.padEnd(10)} ${t(entry.desc)}`);
    }
    lines.push('', t('agentCmdLocalNote'));
    return lines.join('\n');
  },

  _cmdClear() { this.clearHistory(); return null; },

  async _cmdCompact() {
    if (!this.transcript.length) return t('agentCompactNone');
    if (this.running) return t('agentCompactRunning');

    this._showSystemMsg(t('agentCompacting'));
    try {
      const r = await window.api.sendCommand('agent_compact', {
        transcript: this.transcript,
      });
      if (r && r.ok && r.summary) {
        this.compactContext = r.summary;
        // Show badge in the input area
        const badge = document.getElementById('agent-context-badge');
        if (badge) {
          badge.textContent = t('agentCtxLoadedChars', { count: r.summary.length });
          badge.classList.remove('hidden');
        }
        return t('agentCompactDone', { count: r.summary.length, summary: r.summary });
      }
      return t('agentCompactFail', { msg: (r && r.error) || t('unknownError') });
    } catch (e) {
      return t('agentCompactFail', { msg: e.message });
    }
  },

  _cmdContext() {
    const counts = { user: 0, assistant: 0, tool: 0, done: 0, perm: 0, error: 0, ask: 0 };
    let totalChars = 0;
    for (const r of this.transcript) {
      counts[r.type] = (counts[r.type] || 0) + 1;
      totalChars += JSON.stringify(r).length;
    }
    const estTokens = Math.round(totalChars / 2);  // rough estimate
    const histKey = this._historyKey();
    const raw = localStorage.getItem(histKey) || '';
    const storageKB = (new Blob([raw]).size / 1024).toFixed(1);
    const lines = [
      t('agentCtxStatsTitle'),
      '',
      t('agentCtxTotal', { count: this.transcript.length }),
      t('agentCtxUser', { count: counts.user }),
      t('agentCtxAssistant', { count: counts.assistant }),
      t('agentCtxTool', { count: counts.tool }),
      t('agentCtxDone', { count: counts.done }),
      '',
      t('agentCtxChars', { count: totalChars.toLocaleString() }),
      t('agentCtxTokens', { count: estTokens.toLocaleString() }),
      `localStorage: ${storageKB} KB`,
      t('agentCtxSid', { id: this.sessionId || t('agentCtxNone') }),
    ];
    if (this.running) lines.push('', t('agentCtxRunningWarn'));
    return lines.join('\n');
  },

  _cmdRetry() {
    if (this.running) return t('agentRetryRunning');
    const lastUser = [...this.transcript].reverse().find(r => r.type === 'user');
    if (!lastUser) return t('agentRetryNone');
    // Remove the last user message and everything after it from the transcript
    const idx = this.transcript.indexOf(lastUser);
    if (idx >= 0) this.transcript = this.transcript.slice(0, idx);
    this._saveHistoryNow();
    this._renderTranscript();
    document.getElementById('agent-prompt').value = lastUser.text;
    this.send();
    return null;
  },

  _cmdModel(args) {
    const key = 'agent_model';
    if (args.length > 0) {
      const name = args.join(' ');
      localStorage.setItem(key, name);
      return t('agentModelSwitched', { name });
    }
    const current = localStorage.getItem(key);
    if (current) return t('agentModelCurrent', { name: current });
    return t('agentModelUnset');
  },

  _cmdExport() {
    if (!this.transcript.length) return t('agentCmdNoExport');
    // Build markdown
    const lines = [`# LensPilot Agent 对话导出`, `> 导出时间: ${new Date().toISOString()}`, ''];
    for (const r of this.transcript) {
      if (r.type === 'user') {
        lines.push(`## 用户\n\n${r.text}\n`);
      } else if (r.type === 'assistant') {
        lines.push(`## 助手\n\n${r.text}\n`);
      } else if (r.type === 'tool') {
        const ok = r.ok ? '✓' : '✗';
        lines.push(`### 工具: ${r.tool} ${ok}\n\`\`\`json\n${JSON.stringify(r.args || {}, null, 2)}\n\`\`\`\n`);
      } else if (r.type === 'done') {
        lines.push(`---\n> 完成: ${r.summary || ''}\n`);
        if (r.stats) lines.push(`> ${JSON.stringify(r.stats)}\n`);
      }
    }
    const md = lines.join('\n');
    const blob = new Blob([md], { type: 'text/markdown' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = `agent-export-${new Date().toISOString().slice(0,10)}.md`;
    a.click();
    URL.revokeObjectURL(url);
    return t('agentExportedMd');
  },

  async _cmdStats() {
    try {
      const token = localStorage.getItem('studio_token') || '';
      if (token) {
        const r = await window.api.sendCommand('agent_get_quota', { cloud_token: token });
        if (r && !r.error) {
          return [
            t('agentStatsTitle'),
            '',
            t('agentStatsRemaining', { count: (r.remaining || 0).toLocaleString() }),
            t('agentStatsUsed', { used: (r.tokens_used || 0).toLocaleString(), total: (r.quota_total || 0).toLocaleString() }),
            t('agentStatsPct', { pct: r.quota_total > 0 ? ((r.tokens_used / r.quota_total) * 100).toFixed(1) : 0 }),
            '',
            t('agentStatsCloud'),
          ].join('\n');
        }
      }
    } catch (_) { /* fall through */ }
    return t('agentStatsFail');
  },

  _cmdFolder(args) {
    if (args.length > 0) {
      const path = args.join(' ');
      document.getElementById('agent-folder').value = path;
      this.workFolder = path;
      return t('agentFolderSet', { path });
    }
    const current = document.getElementById('agent-folder').value.trim() || this.workFolder || '';
    return current ? t('agentFolderCurrent', { path: current }) : t('agentFolderUnset');
  },

  async _cmdToken() {
    try {
      const r = await window.api.sendCommand('agent_get_usage', {});
      if (r && r.runs) {
        const runs = r.runs.slice(-10);
        const lines = [t('agentTokenDetailTitle'), ''];
        for (const run of runs.reverse()) {
          const date = (run.timestamp || '').slice(0, 10);
          const total = (run.tokens_total || 0).toLocaleString();
          lines.push(`  ${date}  ${total.padStart(10)} tokens  | ${(run.prompt || '').slice(0, 60)}`);
        }
        return lines.join('\n');
      }
    } catch (_) {}
    return t('agentTokenDetailFail');
  },

  // Command registry
  commands: {
    help:    { desc: 'agentCmdHelpDesc', fn: '_cmdHelp' },
    clear:   { desc: 'agentCmdClearDesc', fn: '_cmdClear' },
    compact: { desc: 'agentCmdCompactDesc', fn: '_cmdCompact' },
    context: { desc: 'agentCmdContextDesc', fn: '_cmdContext' },
    retry:   { desc: 'agentCmdRetryDesc', fn: '_cmdRetry' },
    model:   { desc: 'agentCmdModelDesc', fn: '_cmdModel' },
    export:  { desc: 'agentCmdExportDesc', fn: '_cmdExport' },
    stats:   { desc: 'agentCmdStatsDesc', fn: '_cmdStats' },
    folder:  { desc: 'agentCmdFolderDesc', fn: '_cmdFolder' },
    token:   { desc: 'agentCmdTokenDesc', fn: '_cmdToken' },
  },

  // ── Event routing (called from app.js setupBackendEvents) ──
  handleEvent(name, data) {
    if (!data) data = {};
    // Only the interactive session (this.sessionId) renders in the chat.
    // Other sessions (headless scheduled runs) are logged, not rendered.
    const interactive = !data.session_id || data.session_id === this.sessionId;
    switch (name) {
      case 'agent_thinking':
        if (!interactive) return;
        if (!this._thinkEl) {
          const el = this._addCard('assistant', `<div class="agent-bubble assistant agent-md"></div>`);
          this._thinkEl = el.querySelector('.agent-bubble');
          this._thinkBuf = '';
          this._thinkRAF = null;
        }
        this._thinkBuf += (data.text || '');
        this._scheduleThinkRender();
        break;

      case 'agent_tool_call':
        if (!interactive) { this.addLog(`🔧 ${data.tool} ${this._fmtArgs(data.args)}`, 'info'); return; }
        this._finalizeThinkBubble();
        this._toolCards[data.call_id] = this._renderToolCard(data);
        this._tPush({ type: 'tool', call_id: data.call_id, tool: data.tool, args: data.args, result: null, ok: null });
        break;

      case 'agent_tool_progress':
        if (!interactive) return;
        this._updateToolProgress(data.call_id, data);
        break;

      case 'agent_tool_result': {
        if (!interactive) {
          this.addLog(`${data.ok ? '✅' : '⚠️'} ${data.tool}: ${this._fmtResult(data.result)}`, data.ok ? 'success' : 'warn');
          return;
        }
        this._finishToolCard(data);
        break;
      }

      case 'agent_permission_request': {
        if (!interactive) { this.addLog(t('agentLogUnattendedDenied', { tool: data.tool }), 'warn'); return; }
        this._renderPermissionCard(data);
        break;
      }

      case 'agent_permission_resolved':
        if (interactive) this._resolvePermissionCard(data);
        break;

      case 'agent_iteration':
        if (interactive) this._updateBudget(data);
        break;

      case 'agent_work_folder_changed':
        if (interactive && data.work_folder) this._setFolder(data.work_folder);
        break;

      case 'agent_ask_user':
        if (interactive) this._renderQuestionCard(data);
        break;

      case 'agent_watch_progress': {
        // update count in the matching task card
        const card = document.querySelector(`#agent-task-list [data-task-id="${data.id}"] [data-watch-count]`);
        if (card) card.textContent = String(data.count || 0);
        break;
      }
      case 'agent_watch_fired':
        this.addLog(t('agentLogWatchFired', { count: data.count, folder: data.folder }), 'info');
        break;
      case 'agent_watch_started':
        this.addLog(t('agentLogWatchStarted', { folder: data.folder, threshold: data.threshold }), 'info');
        break;
      case 'agent_watch_stopped':
        break;

      case 'agent_done':
        this._onDone(data);
        break;

      case 'agent_error':
        this._onError(data);
        break;

      case 'agent_log':
        // log events from a headless session still get shown; interactive too.
        this.addLog(data.message || '', data.level || 'info');
        break;

      case 'agent_local_download':
        this._onLocalDownloadProgress(data);
        break;
    }
  },

  // ── 本地模型下载 ──
  async downloadLocalModel() {
    const btn = document.getElementById('btn-agent-local-download');
    const prog = document.getElementById('agent-local-download-progress');
    if (btn) btn.disabled = true;
    if (prog) prog.textContent = '正在下载本地模型（GGUF + 视觉模型，~1.5GB）…';
    try {
      const r = await window.api.sendCommand('agent_download_local_model', {});
      if (r && r.error) {
        App.toast(r.error, 'error');
      } else if (r && r.already_present) {
        App.toast('本地模型已就绪', 'success');
      } else {
        App.toast('本地模型下载完成', 'success');
      }
      this.refreshSettings();
    } catch (e) {
      App.toast(t('agentSaveFailedMsg', { msg: e.message }), 'error');
    } finally {
      if (btn) btn.disabled = false;
    }
  },

  _onLocalDownloadProgress(data) {
    const prog = document.getElementById('agent-local-download-progress');
    if (!prog) return;
    if (data && data.done) {
      prog.textContent = '下载完成';
      return;
    }
    const mb = data && data.total ? Math.floor(data.current / 1048576) : '?';
    const mbT = data && data.total ? Math.floor(data.total / 1048576) : '?';
    prog.textContent = `${data && data.file ? data.file : ''} ${mb} / ${mbT} MB`;
  },

  _scroll() {
    const m = document.getElementById('agent-messages');
    if (m) m.scrollTop = m.scrollHeight;
  },

  _fmtArgs(args) {
    try { return JSON.stringify(args, null, 2); } catch (e) { return String(args); }
  },
  _fmtResult(r) {
    try {
      const s = typeof r === 'string' ? r : JSON.stringify(r, null, 2);
      return s.length > 2000 ? s.slice(0, 2000) + '\n' + t('agentTruncated') : s;
    } catch (e) { return String(r); }
  },

  // ── Streaming markdown render (throttled) ──
  _scheduleThinkRender() {
    if (this._thinkRAF) return;
    this._thinkRAF = requestAnimationFrame(() => {
      this._thinkRAF = null;
      if (this._thinkEl) this._thinkEl.innerHTML = this._md(this._thinkBuf || '');
      this._scroll();
    });
  },

  // ── Compact markdown → HTML renderer ──
  // Handles: code blocks ```, inline code `, bold **, italic *, headings #,
  // tables | a | b |, unordered lists -/*, ordered lists, blockquote >,
  // horizontal rule ---, links [t](u), line breaks. Escapes HTML first.
  _md(text) {
    if (!text) return '';
    const blocks = [];
    let buf = String(text);
    // 1. fenced code blocks → placeholders
    buf = buf.replace(/```(\w*)\n?([\s\S]*?)```/g, (_, lang, code) => {
      blocks.push(`<pre class="md-code"><code>${this._esc(code.replace(/\n$/, ''))}</code></pre>`);
      return ` BLOCK${blocks.length - 1} `;
    });
    // 2. split into lines for block-level processing
    const lines = buf.split('\n');
    const out = [];
    let i = 0;
    const inline = (s) => {
      // restore block placeholders inside inline is not expected; protect inline code first
      const codes = [];
      s = s.replace(/`([^`]+)`/g, (_, c) => { codes.push(`<code class="md-icode">${this._esc(c)}</code>`); return `IC${codes.length - 1}`; });
      s = this._esc(s);
      s = s.replace(/IC(\d+)/g, (_, n) => codes[+n]);
      // bold then italic
      s = s.replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>');
      s = s.replace(/(^|[^*])\*([^*]+)\*/g, '$1<i>$2</i>');
      s = s.replace(/__([^_]+)__/g, '<b>$1</b>');
      // links [text](url)
      s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
      return s;
    };
    while (i < lines.length) {
      let line = lines[i];
      // block placeholder line
      const pm = line.match(/^ BLOCK(\d+) $/);
      if (pm) { out.push(blocks[+pm[1]]); i++; continue; }
      // table: current line has |, next line is a separator |---|---|
      if (/\|/.test(line) && i + 1 < lines.length && /^\s*\|?[\s:|-]*-[\s:|-]*\|/.test(lines[i + 1])) {
        const header = line.split('|').map(c => c.trim()).filter((c, idx, arr) => !(idx === 0 && c === '') && !(idx === arr.length - 1 && c === ''));
        i += 2; // skip separator
        const rows = [];
        while (i < lines.length && /\|/.test(lines[i]) && lines[i].trim() !== '') {
          const cells = lines[i].split('|').map(c => c.trim());
          // drop leading/trailing empty from leading/trailing |
          if (cells[0] === '') cells.shift();
          if (cells[cells.length - 1] === '') cells.pop();
          rows.push(cells);
          i++;
        }
        let tbl = '<div class="md-table-wrap"><table class="md-table"><thead><tr>';
        header.forEach(h => tbl += `<th>${inline(h)}</th>`);
        tbl += '</tr></thead><tbody>';
        rows.forEach(r => { tbl += '<tr>'; for (let c = 0; c < header.length; c++) tbl += `<td>${inline(r[c] || '')}</td>`; tbl += '</tr>'; });
        tbl += '</tbody></table></div>';
        out.push(tbl);
        continue;
      }
      // heading
      const hm = line.match(/^(#{1,4})\s+(.*)$/);
      if (hm) { out.push(`<h${hm[1].length} class="md-h">${inline(hm[2])}</h${hm[1].length}>`); i++; continue; }
      // horizontal rule
      if (/^\s*([-*_])\1\1[-*_\s]*$/.test(line) && line.trim()) { out.push('<hr class="md-hr">'); i++; continue; }
      // blockquote
      if (/^\s*>\s?/.test(line)) {
        const qs = [];
        while (i < lines.length && /^\s*>\s?/.test(lines[i])) { qs.push(lines[i].replace(/^\s*>\s?/, '')); i++; }
        out.push(`<blockquote class="md-quote">${inline(qs.join(' '))}</blockquote>`);
        continue;
      }
      // unordered list
      if (/^\s*[-*+]\s+/.test(line)) {
        const items = [];
        while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i])) { items.push(lines[i].replace(/^\s*[-*+]\s+/, '')); i++; }
        out.push('<ul class="md-ul">' + items.map(it => `<li>${inline(it)}</li>`).join('') + '</ul>');
        continue;
      }
      // ordered list
      if (/^\s*\d+\.\s+/.test(line)) {
        const items = [];
        while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) { items.push(lines[i].replace(/^\s*\d+\.\s+/, '')); i++; }
        out.push('<ol class="md-ol">' + items.map(it => `<li>${inline(it)}</li>`).join('') + '</ol>');
        continue;
      }
      // blank line
      if (line.trim() === '') { out.push(''); i++; continue; }
      // paragraph (collect consecutive non-blank, non-special lines)
      const para = [];
      while (i < lines.length && lines[i].trim() !== '' &&
             !/^(#{1,4})\s/.test(lines[i]) && !/^\s*[-*+]\s/.test(lines[i]) &&
             !/^\s*\d+\.\s/.test(lines[i]) && !/^\s*>\s?/.test(lines[i]) &&
             !/^ BLOCK/.test(lines[i]) && !/\|/.test(lines[i] + (lines[i + 1] || ''))) {
        para.push(lines[i]); i++;
      }
      if (para.length) { out.push(`<p class="md-p">${inline(para.join(' '))}</p>`); }
    }
    return out.join('\n');
  },

  _renderToolCard(data) {
    const el = this._addCard('tool', `
      <div class="agent-tool-card">
        <div class="agent-tool-head"><i class="ph ph-caret-right agent-tool-caret"></i> <i class="ph ph-wrench"></i> <b>${this._esc(data.tool)}</b>
          <span class="agent-tool-status">${t('agentStatusRunning')}</span></div>
        <div class="agent-tool-detail hidden">
          <div class="agent-tool-section">${t('agentToolInput')} <pre>${this._esc(this._fmtArgs(data.args))}</pre></div>
          <div class="agent-tool-progress hidden"><div class="progress-bar" style="height:6px"><div class="progress-fill" style="width:0%"></div></div><span class="agent-tool-progtext"></span></div>
          <div class="agent-tool-result hidden"></div>
        </div>
      </div>`);
    const head = el.querySelector('.agent-tool-head');
    head.style.cursor = 'pointer';
    head.addEventListener('click', () => {
      const det = el.querySelector('.agent-tool-detail');
      const caret = el.querySelector('.agent-tool-caret');
      const open = !det.classList.contains('hidden');
      det.classList.toggle('hidden', open);
      caret.className = 'ph agent-tool-caret ' + (open ? 'ph-caret-right' : 'ph-caret-down');
    });
    return el;
  },

  _updateToolProgress(callId, data) {
    const card = this._toolCards[callId];
    if (!card) return;
    // Show progress in the collapsed status pill so the user sees activity
    // without expanding the card.
    const status = card.querySelector('.agent-tool-status');
    if (status) status.textContent = `${data.current || 0}/${data.total || 0}`;
    const box = card.querySelector('.agent-tool-progress');
    if (box) {
      box.classList.remove('hidden');
      const pct = data.total ? Math.round((data.current / data.total) * 100) : 0;
      box.querySelector('.progress-fill').style.width = pct + '%';
      box.querySelector('.agent-tool-progtext').textContent =
        `${data.current || 0}/${data.total || 0} ${data.last_file || ''}`;
    }
    this._scroll();
  },

  _finishToolCard(data) {
    const card = this._toolCards[data.call_id];
    if (card) {
      const status = card.querySelector('.agent-tool-status');
      if (status) status.textContent = data.ok ? t('agentDoneLabel') : t('agentFailLabel');
      const box = card.querySelector('.agent-tool-result');
      if (box) {
        box.classList.remove('hidden');
        box.innerHTML = `<div class="agent-tool-section">${t('agentToolOutput')} <pre>${this._esc(this._fmtResult(data.result))}</pre></div>`;
      }
      delete this._toolCards[data.call_id];
    }
    // sync transcript record
    const rec = this.transcript.find(r => r.type === 'tool' && r.call_id === data.call_id);
    if (rec) { rec.result = data.result; rec.ok = data.ok; this._saveHistoryDebounced(); }
    this._scroll();
  },

  _renderPermissionCard(data) {
    const el = this._addCard('perm', `
      <div class="agent-perm-card">
        <div class="agent-perm-head"><i class="ph ph-warning"></i> ${t('agentPermNeedConfirm', { tool: this._esc(data.tool) })}</div>
        <div class="agent-perm-summary">${this._esc(data.summary || this._fmtArgs(data.args))}</div>
        <div class="agent-perm-buttons">
          <button class="btn btn-danger btn-sm" data-act="approve">${t('agentPermApprove')}</button>
          <button class="btn btn-outline btn-sm" data-act="deny">${t('agentPermReject')}</button>
        </div>
      </div>`);
    this._pendingApprovals[data.request_id] = el;
    el.querySelectorAll('button').forEach(b => b.addEventListener('click', async () => {
      const approved = b.dataset.act === 'approve';
      try {
        await window.api.sendCommand('agent_approve', { request_id: data.request_id, approved });
      } catch (e) { /* ignore */ }
    }));
    this._tPush({ type: 'perm', request_id: data.request_id, tool: data.tool, summary: data.summary || this._fmtArgs(data.args), approved: null });
    this._scroll();
  },

  _resolvePermissionCard(data) {
    const el = this._pendingApprovals[data.request_id];
    if (el) {
      const btns = el.querySelector('.agent-perm-buttons');
      if (btns) btns.innerHTML = `<span style="font-size:12px;color:var(--text3)">${data.approved ? t('agentPermStateApproved') : t('agentPermStateDenied')}</span>`;
      delete this._pendingApprovals[data.request_id];
    }
    const rec = this.transcript.find(r => r.type === 'perm' && r.request_id === data.request_id);
    if (rec) { rec.approved = !!data.approved; this._saveHistoryDebounced(); }
  },

  _renderQuestionCard(data) {
    const el = this._addCard('ask', `
      <div class="agent-perm-card" style="border-color:var(--brand)">
        <div class="agent-perm-head"><i class="ph ph-question"></i> ${t('agentAskTitle')}</div>
        <div class="agent-perm-summary">${this._esc(data.question || '')}</div>
        <div class="agent-perm-buttons">
          <input type="text" class="agent-ask-input" placeholder="${t('agentAskPlaceholder2')}" style="flex:1;padding:6px 10px;border:1px solid var(--border);border-radius:8px;background:var(--bg);color:var(--text);font-size:13px">
          <button class="btn btn-primary btn-sm" data-act="reply">${t('agentReply')}</button>
        </div>
      </div>`);
    const input = el.querySelector('.agent-ask-input');
    const send = async () => {
      const answer = (input.value || '').trim();
      if (!answer) return;
      try { await window.api.sendCommand('agent_reply', { request_id: data.request_id, answer }); }
      catch (e) { /* ignore */ }
      const btns = el.querySelector('.agent-perm-buttons');
      if (btns) btns.innerHTML = `<span style="font-size:12px;color:var(--text3)">${t('agentAnswered', { answer: this._esc(answer) })}</span>`;
    };
    el.querySelector('[data-act="reply"]').addEventListener('click', send);
    input.addEventListener('keydown', e => { if (e.key === 'Enter') send(); });
    this._tPush({ type: 'ask', question: data.question || '', answer: null });
    this._scroll();
  },

  _updateBudget(data) {
    const el = document.getElementById('agent-budget');
    if (!el) return;
    const t = data.tokens_used || 0;
    const p = data.prompt_tokens || 0, c = data.completion_tokens || 0, ch = data.cached_tokens || 0;
    el.innerHTML = `<span>${I18N.t('agentBudgetIter', { n: data.iteration || 0 })}</span> · ${I18N.t('agentBudgetIn', { n: p })} · ${I18N.t('agentBudgetOut', { n: c })}` +
      (ch ? ` · ${I18N.t('agentBudgetCache', { n: ch })}` : '') + ` · ${I18N.t('agentBudgetTotal', { n: t })}`;
  },

  _onDone(data) {
    const mine = this.sessionId && data.session_id === this.sessionId;
    if (!mine) {
      // a headless/scheduled run finished — record history + log + notify
      const fired = this._firedSessions[data.session_id];
      if (fired) {
        this._recordTaskRun(fired.taskId, { ts: Date.now(), status: 'done', summary: data.summary || '', tokens: data.tokens_used || 0, iterations: data.iterations || 0 });
        delete this._firedSessions[data.session_id];
      } else {
        // agent_watcher 进程内直触发的会话不在 _firedSessions 里——从 session_id
        // (watch_<taskId>_<ts>) 解析 taskId 记账，否则任务卡 lastRun 永远空白
        const watchTaskId = this._watchSessionTaskId(data.session_id);
        if (watchTaskId) {
          this._recordTaskRun(watchTaskId, { ts: Date.now(), status: 'done', summary: data.summary || '', tokens: data.tokens_used || 0, iterations: data.iterations || 0 });
        }
      }
      this.addLog(t('agentLogTaskDone', { summary: data.summary || '' }), 'success');
      if (typeof App !== 'undefined' && App.notify) App.notify(t('agentNotifyTaskDone'), data.summary || '');
      return;
    }
    this._setRunning(false);
    this._finalizeThinkBubble();
    const stats = t('agentStatsRounds', { n: data.iterations || 0 }) + ` · ${t('agentBudgetIn', { n: data.prompt_tokens || 0 })}` + ` · ${t('agentBudgetOut', { n: data.completion_tokens || 0 })}` +
      (data.cached_tokens ? ` · ${t('agentCacheLabel', { n: data.cached_tokens })}` : '') + ` · ${((data.duration_ms || 0) / 1000).toFixed(1)}s`;
    this._addCard('done', `<div class="agent-bubble done agent-md"><b>${t('agentDoneLabel')}</b>${this._md(data.summary || '')}<div class="agent-stats">${stats}</div></div>`);
    this._tPush({ type: 'done', summary: data.summary || '', stats });
    if (typeof App !== 'undefined' && App.notify) App.notify(t('agentNotifyDone'), data.summary || '');
    // refresh the usage panel if open so totals update live
    if (!document.getElementById('agent-usage-panel').classList.contains('hidden')) this.refreshUsage();
  },

  _onError(data) {
    const mine = this.sessionId && data.session_id === this.sessionId;
    if (!mine) {
      const fired = this._firedSessions[data.session_id];
      if (fired) {
        this._recordTaskRun(fired.taskId, { ts: Date.now(), status: 'error', error: data.error || '' });
        // Retry once after 60s (reuses the original effective prompt).
        if (!fired.retried) {
          const task = this.tasks.find(t => t.id === fired.taskId);
          if (task) {
            this.addLog(t('agentLogRetryOnce', { name: task.name }), 'warn');
            const prompt = fired.prompt;
            setTimeout(() => this.fire(task, { prompt, isRetry: true }), 60000);
          }
        }
        delete this._firedSessions[data.session_id];
      } else {
        // watch 会话同上——记账但不重试（prompt 由后端 watcher 构造，前端没有）
        const watchTaskId = this._watchSessionTaskId(data.session_id);
        if (watchTaskId) {
          this._recordTaskRun(watchTaskId, { ts: Date.now(), status: 'error', error: data.error || '' });
        }
      }
      this.addLog(t('agentLogTaskFail', { error: data.error || '' }), 'error');
      if (typeof App !== 'undefined' && App.notify) App.notify(t('agentNotifyTaskFail'), data.error || '');
      return;
    }
    this._setRunning(false);
    this._finalizeThinkBubble();
    this._addCard('error', `<div class="agent-bubble error">❌ ${this._esc(data.error || t('unknownError'))} <span style="font-size:11px;color:var(--text3)">(${data.phase || ''})</span></div>`);
    this._tPush({ type: 'error', error: data.error || '', phase: data.phase || '' });
    App.toast(data.error || t('agentErrGeneric'), 'error');
  },

  // agent_watcher 后端直触发的会话 id 形如 watch_<taskId>_<ts>；解析出 taskId（无则 null）
  _watchSessionTaskId(sid) {
    if (typeof sid !== 'string' || !sid.startsWith('watch_')) return null;
    const taskId = sid.slice(6, sid.lastIndexOf('_'));
    return this.tasks.some(t => t.id === taskId) ? taskId : null;
  },

  // ════════════════════════════════════════════════════════════════
  // Log panel
  // ════════════════════════════════════════════════════════════════
  addLog(message, level = 'info') {
    const ts = new Date().toLocaleTimeString('zh-CN', { hour12: false });
    this.log.push({ ts, message, level });
    if (this.log.length > 500) this.log.shift();
    const el = document.getElementById('agent-log');
    if (!el) return;
    const line = document.createElement('div');
    line.className = `log-line log-${level}`;
    line.textContent = `[${ts}] ${message}`;
    el.appendChild(line);
    el.scrollTop = el.scrollHeight;
  },

  // ════════════════════════════════════════════════════════════════
  // Settings (LLM config in backend app_config.json)
  // ════════════════════════════════════════════════════════════════
  _bindSettings() {
    const $ = id => document.getElementById(id);
    $('btn-agent-test').addEventListener('click', () => this.testLlm());
    $('btn-agent-save-cfg').addEventListener('click', () => this.saveConfig());
    $('btn-agent-key-toggle').addEventListener('click', () => this._toggleKeyVisible());
    // 官方 API 已停用: 历史按钮不再存在, 保留防护避免空引用
    const officialBtn = $('btn-agent-use-official');
    if (officialBtn) officialBtn.addEventListener('click', () => this._fetchOfficialKey());

    // MCP
    $('btn-mcp-add').addEventListener('click', () => this._mcpShowForm());
    $('btn-mcp-test').addEventListener('click', () => this._mcpTest());
    $('btn-mcp-save').addEventListener('click', () => this._mcpSave());
    $('btn-mcp-cancel').addEventListener('click', () => this._mcpCancel());

    // Skills
    $('btn-skill-add').addEventListener('click', () => this._skillShowForm());
    $('btn-skill-test').addEventListener('click', () => this._skillTest());
    $('btn-skill-save').addEventListener('click', () => this._skillSave());
    $('btn-skill-cancel').addEventListener('click', () => this._skillCancel());

    // 设置面板打开时，点击面板外部区域（或再次点设置按钮）→ 自动收起
    document.addEventListener('click', (e) => {
      const panel = document.getElementById('agent-settings-panel');
      if (!panel || panel.classList.contains('hidden')) return;
      const gear = document.getElementById('btn-agent-settings');
      if (!panel.contains(e.target) && !(gear && gear.contains(e.target))) {
        this._closeSettings();
      }
    });
  },

  _updateMultimodalToggle() {
    const btn = document.getElementById('btn-agent-multimodal-toggle');
    const sel = document.getElementById('agent-multimodal');
    if (!btn) return;
    const on = sel ? sel.value === 'on' : btn.classList.contains('on');
    if (on) {
      btn.innerHTML = '<i class="ph ph-eye"></i> ' + t('agentMultimodal');
      btn.style.color = 'var(--brand)'; btn.style.borderColor = 'var(--brand)';
      btn.title = t('agentMmOnTitle');
    } else {
      btn.innerHTML = '<i class="ph ph-eye-slash"></i> ' + t('agentMultimodal');
      btn.style.color = ''; btn.style.borderColor = '';
      btn.title = t('agentMmOffTitle');
    }
    btn.classList.toggle('on', on);
    this._multimodalOn = on;
  },

  toggleMultimodal() {
    const sel = document.getElementById('agent-multimodal');
    const btn = document.getElementById('btn-agent-multimodal-toggle');
    if (!sel) return;
    sel.value = sel.value === 'on' ? 'off' : 'on';
    this._updateMultimodalToggle();
    window.api.sendCommand('agent_save_config', {
      agent: { multimodal: sel.value }
    }).catch(function(){});
  },

  // ── MCP UI ──

  _mcpEditId: null,
  async _refreshMCP() {
    try {
      const r = await window.api.sendCommand('mcp_list_servers', {});
      const list = document.getElementById('mcp-server-list');
      if (!r || !r.available) {
        list.innerHTML = '<span style="color:var(--text3)">' + t('agentMcpSdkMissing') + '</span>';
        return;
      }
      const servers = r.connected || [];
      if (!servers.length) { list.innerHTML = '<span style="color:var(--text3)">' + t('agentMcpEmpty') + '</span>'; return; }
      list.innerHTML = servers.map(s =>
        `<div style="display:flex;align-items:center;gap:8px;padding:4px 0">
          <span style="color:var(--brand)">●</span> ${s}
          <button class="btn btn-ghost btn-sm" onclick="Agent._mcpEdit('${s.replace(/'/g, "\\'")}')" style="margin-left:auto"><i class="ph ph-pencil"></i></button>
          <button class="btn btn-ghost btn-sm" onclick="Agent._mcpRemove('${s.replace(/'/g, "\\'")}')" style="color:var(--danger)"><i class="ph ph-trash"></i></button>
        </div>`
      ).join('');
    } catch (_) {}
  },
  _mcpShowForm(name) {
    this._mcpEditId = (name && typeof name === 'string') ? name : null;
    document.getElementById('mcp-edit-form').classList.remove('hidden');
    document.getElementById('mcp-edit-name').value = this._mcpEditId || '';
  },
  _mcpEdit(name) { this._mcpShowForm(name); },
  async _mcpRemove(name) {
    if (!(await Modal.showConfirm(t('agentMcpDelTitle'), t('agentMcpDelMsg', { name })))) return;
    await window.api.sendCommand('mcp_remove_server', { name });
    App.toast(t('agentDeleted'), 'success'); this._refreshMCP();
  },
  async _mcpTest() {
    const cfg = { type: document.getElementById('mcp-edit-type').value, command: document.getElementById('mcp-edit-command').value, args: (document.getElementById('mcp-edit-args').value || '').split(/\s+/).filter(Boolean) };
    const envText = document.getElementById('mcp-edit-env').value || '';
    if (envText.trim()) { cfg.env = {}; envText.split('\n').forEach(l => { const [k,...v] = l.split('='); if (k) cfg.env[k.trim()] = v.join('=').trim(); }); }
    App.toast(t('agentTesting'), 'info');
    const r = await window.api.sendCommand('mcp_test_connection', { config: cfg });
    App.toast(r.ok ? t('agentMcpConnOk', { count: r.tools.length, tools: r.tools.join(', ') }) : t('agentMcpConnFail', { msg: r.message }), r.ok ? 'success' : 'error');
  },
  async _mcpSave() {
    const name = document.getElementById('mcp-edit-name').value.trim();
    if (!name) { App.toast(t('agentNameRequired'), 'warning'); return; }
    const cfg = { type: document.getElementById('mcp-edit-type').value, command: document.getElementById('mcp-edit-command').value, args: (document.getElementById('mcp-edit-args').value || '').split(/\s+/).filter(Boolean) };
    const envText = document.getElementById('mcp-edit-env').value || '';
    if (envText.trim()) { cfg.env = {}; envText.split('\n').forEach(l => { const [k,...v] = l.split('='); if (k) cfg.env[k.trim()] = v.join('=').trim(); }); }
    await window.api.sendCommand('mcp_add_server', { name, config: cfg });
    document.getElementById('mcp-edit-form').classList.add('hidden'); App.toast(t('agentSaved'), 'success'); this._refreshMCP();
  },
  _mcpCancel() { document.getElementById('mcp-edit-form').classList.add('hidden'); },

  // ── Skills UI ──

  _skillEditName: null,
  async _refreshSkills() {
    try {
      const r = await window.api.sendCommand('skill_list', {});
      const list = document.getElementById('skill-list');
      const skills = r.skills || [];
      if (!skills.length) { list.innerHTML = '<span style="color:var(--text3)">' + t('agentSkillEmpty') + '</span>'; return; }
      list.innerHTML = skills.map(s =>
        `<div style="display:flex;align-items:center;gap:8px;padding:4px 0">
          <span><strong>${s.name}</strong> — ${s.description || t('agentSkillNoDesc')}${s.trigger ? t('agentSkillTrigger', { trigger: s.trigger }) : ''}</span>
          <button class="btn btn-ghost btn-sm" onclick="Agent._skillEdit('${s.name.replace(/'/g, "\\'")}')" style="margin-left:auto"><i class="ph ph-pencil"></i></button>
          <button class="btn btn-ghost btn-sm" onclick="Agent._skillDelete('${s.name.replace(/'/g, "\\'")}')" style="color:var(--danger)"><i class="ph ph-trash"></i></button>
        </div>`
      ).join('');
    } catch (_) {}
  },
  _skillShowForm(name) {
    this._skillEditName = (name && typeof name === 'string') ? name : '';
    document.getElementById('skill-edit-form').classList.remove('hidden');
    document.getElementById('skill-edit-name').value = this._skillEditName;
    if (this._skillEditName) {
      // Load existing skill data... (simplified — rely on saved state)
    }
  },
  _skillEdit(name) { this._skillShowForm(name); },
  async _skillDelete(name) {
    if (!(await Modal.showConfirm(t('agentSkillDelTitle'), t('agentSkillDelMsg', { name })))) return;
    await window.api.sendCommand('skill_delete', { name });
    App.toast(t('agentDeleted'), 'success'); this._refreshSkills();
  },
  async _skillTest() {
    const name = document.getElementById('skill-edit-name').value.trim();
    const trigger = document.getElementById('skill-edit-trigger').value.trim();
    if (!trigger) { App.toast(t('agentSkillNeedRegex'), 'warning'); return; }
    try { new RegExp(trigger); } catch (e) { App.toast(t('agentSkillBadRegex', { msg: e.message }), 'error'); return; }
    const testText = await Modal.showInput(t('agentSkillTestTitle'), t('agentSkillTestMsg'));
    if (testText) {
      const re = new RegExp(trigger, 'i');
      App.toast(re.test(testText) ? t('agentSkillMatchOk') : t('agentSkillMatchNo'), re.test(testText) ? 'success' : 'warning');
    }
  },
  async _skillSave() {
    const name = document.getElementById('skill-edit-name').value.trim();
    if (!name) { App.toast(t('agentNameRequired'), 'warning'); return; }
    await window.api.sendCommand('skill_save', {
      name,
      description: document.getElementById('skill-edit-desc').value.trim(),
      trigger: document.getElementById('skill-edit-trigger').value.trim(),
      body: document.getElementById('skill-edit-body').value,
    });
    document.getElementById('skill-edit-form').classList.add('hidden');
    App.toast(t('agentSaved'), 'success'); this._refreshSkills();
  },
  _skillCancel() { document.getElementById('skill-edit-form').classList.add('hidden'); },

  _toggleKeyVisible() {
    const inp = document.getElementById('agent-api-key');
    const btn = document.getElementById('btn-agent-key-toggle');
    if (!inp) return;
    if (inp.type === 'password') {
      inp.type = 'text';
      if (btn) btn.innerHTML = '<i class="ph ph-eye-slash"></i>';
    } else {
      inp.type = 'password';
      if (btn) btn.innerHTML = '<i class="ph ph-eye"></i>';
    }
  },

  async _fetchOfficialKey() {
    // 官方 API 已停用: 后端统一返回禁用提示, 仅提示用户改走本地/个人 Key
    const btn = document.getElementById('btn-agent-use-official');
    if (btn) { btn.disabled = true; }
    try {
      const r = await window.api.sendCommand('agent_fetch_official_key', {});
      App.toast((r && r.error) || '官方 API 已停用', 'warn');
    } catch (e) {
      App.toast(t('agentFetchFail', { msg: e.message }), 'error');
    }
    if (btn) { btn.disabled = false; }
  },

  _fmtTokens(n) {
    if (!n || n <= 0) return '0';
    if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
    if (n >= 1000) return (n / 1000).toFixed(0) + 'k';
    return String(n);
  },

  async _refreshQuota() {
    const token = localStorage.getItem('studio_token') || '';
    if (!token) return;
    try {
      const r = await window.api.sendCommand('agent_get_quota', { cloud_token: token });
      if (r && r.quota_total > 0) {
        const remaining = Math.max(0, r.remaining || 0);
        const total = r.quota_total || 0;
        const pct = total > 0 ? Math.round(remaining / total * 100) : 0;
        const row = document.getElementById('agent-quota-row');
        if (row) row.style.display = '';
        const text = document.getElementById('agent-quota-text');
        if (text) {
          text.innerHTML = t('agentQuotaText', { used: this._fmtTokens(r.tokens_used), total: this._fmtTokens(total), color: pct < 10 ? 'var(--red)' : 'var(--green)', pct });
        }
      }
    } catch (e) { /* ignore */ }
  },

  async refreshSettings() {
    this._refreshMCP();
    this._refreshSkills();
    this._updateMultimodalToggle();
    let a = {};
    try {
      const r = await window.api.sendCommand('agent_get_config', {});
      a = (r && r.agent) || {};
    } catch (e) { /* backend not ready yet */ }
    const $ = id => document.getElementById(id);
    if ($('agent-base-url')) $('agent-base-url').value = a.base_url || '';
    if ($('agent-model')) $('agent-model').value = a.model || '';
    if ($('agent-reasoning')) $('agent-reasoning').value = a.reasoning_effort || 'off';
    if ($('agent-multimodal')) $('agent-multimodal').value = a.multimodal || 'off';
    this._multimodalOn = (a.multimodal === 'on');
    if ($('agent-temperature')) { $('agent-temperature').value = a.temperature || 0.3; $('agent-temperature-label').textContent = a.temperature || 0.3; }
    if ($('agent-preferences')) $('agent-preferences').value = a.preferences || '';
    const mmBtn = $('btn-agent-multimodal-toggle');
    if (mmBtn) {
      if (this._multimodalOn) mmBtn.classList.add('on'); else mmBtn.classList.remove('on');
      this._updateMultimodalToggle();
    }
    if ($('agent-key-desc')) $('agent-key-desc').textContent = a.api_key_set ? t('agentKeySetKeep') : t('agentKeyNotSet');
    // Local model status (built-in llama-server + Qwen3-VL 2B)
    const localStatus = $('agent-local-status');
    if (localStatus) {
      if (a.use_local) {
        if (a.local_model_present) {
          localStatus.innerHTML = a.local_running
            ? '<span style="color:var(--green)">● 运行中</span> · Qwen3-VL 2B（本地推理，零成本）'
            : '<span style="color:var(--green)">✓ 模型已就绪</span> · 首次使用时自动启动（本地推理，零成本）';
        } else {
          localStatus.innerHTML = '<span style="color:var(--orange)">⚠ 模型未下载</span> · 首次使用将自动下载 ~1.9GB（断点续传）'
            + (a.local_reason ? '（' + this._esc(a.local_reason) + '）' : '');
        }
      } else {
        localStatus.innerHTML = '本地模型已停用，将使用下方个人云端 Key';
      }
    }
    if ($('agent-use-local')) $('agent-use-local').value = a.use_local ? 'true' : 'false';
    // 未下载时显示「下载本地模型」按钮（已下载/已停用时隐藏）
    const dlWrap = $('agent-local-download-wrap');
    if (dlWrap) dlWrap.classList.toggle('hidden', !(a.use_local && !a.local_model_present));
    // Official key state (persisted across restarts)
    const offStatus = $('agent-official-status');
    const offDesc = $('agent-official-desc');
    const keyInp = $('agent-api-key');
    if (a.official_key_set) {
      if (offDesc) offDesc.textContent = t('agentUsingSharedKeyPersist');
      if (offStatus) { offStatus.textContent = t('agentModelLabel', { model: a.official_model || '—' }); offStatus.classList.remove('hidden'); }
      if (keyInp) { keyInp.disabled = true; keyInp.placeholder = t('agentUsingOfficial'); }
    }
    const defAA = String(a.default_auto_approve === true);
    if ($('agent-autoapprove-select')) $('agent-autoapprove-select').value = defAA;
    if ($('agent-autoapprove')) $('agent-autoapprove').value = defAA;
    // Topbar model badge — 始终显示当前生效的模型名（本地优先）
    const mb = $('agent-model-badge');
    if (mb) {
      const re = a.reasoning_effort && a.reasoning_effort !== 'off' ? a.reasoning_effort : '';
      let model = '';
      let engine = '';
      if (a.use_local) {
        // 本地模型已启用：模型名即 Qwen3-VL（下载中/已就绪都在这里标识）
        model = 'Qwen3-VL 2B';
        engine = a.local_model_present ? t('agentEngineLocal') : t('agentEngineDownload');
      } else if (a.model) {
        model = a.model;
        engine = t('agentEngineCloud');
      } else if (a.official_model) {
        model = a.official_model;
        engine = t('agentEngineOfficial');
      }
      if (model || re) {
        mb.classList.remove('hidden');
        const dlCls = (!a.local_model_present && a.use_local) ? ' dl' : '';
        mb.innerHTML = `${this._esc(model)}${re ? `<span class="re">${t('agentThinkingBadge', { effort: this._esc(re) })}</span>` : ''}${engine ? `<span class="engine${dlCls}">${this._esc(engine)}</span>` : ''}`;
      } else {
        mb.classList.add('hidden');
      }
    }
    // Refresh official key quota if we have a token
    this._refreshQuota();
  },

  async saveConfig() {
    if (this._savingConfig) return;
    this._savingConfig = true;
    try {
      const $ = id => document.getElementById(id);
      const aa = $('agent-autoapprove-select') ? $('agent-autoapprove-select').value === 'true' : false;
      const agent = {
        base_url: $('agent-base-url').value.trim(),
        model: $('agent-model').value.trim(),
        use_local: $('agent-use-local') ? $('agent-use-local').value === 'true' : true,
        default_auto_approve: aa,
        reasoning_effort: $('agent-reasoning') ? $('agent-reasoning').value : 'off',
        multimodal: $('agent-multimodal') ? $('agent-multimodal').value : 'off',
        temperature: $('agent-temperature') ? parseFloat($('agent-temperature').value) : 0.3,
        preferences: $('agent-preferences') ? $('agent-preferences').value.trim() : '',
      };
      const key = $('agent-api-key').value.trim();
      if (key) agent.api_key = key;
      await window.api.sendCommand('agent_save_config', { agent });
      App.toast(t('agentConfigSaved'), 'success');
      if ($('agent-autoapprove')) $('agent-autoapprove').value = String(aa);
      if ($('agent-key-desc')) $('agent-key-desc').textContent = t('agentKeySetKeep');
      this.refreshSettings();
    } catch (e) {
      App.toast(t('agentSaveFailedMsg', { msg: e.message }), 'error');
    } finally {
      this._savingConfig = false;
    }
  },

  async testLlm() {
    const $ = id => document.getElementById(id);
    const res = $('agent-test-result');
    res.textContent = t('agentTesting');
    try {
      const r = await window.api.sendCommand('agent_test_llm', {
        base_url: $('agent-base-url').value.trim(),
        api_key: $('agent-api-key').value.trim(),
        model: $('agent-model').value.trim(),
      });
      if (r && r.ok) {
        res.innerHTML = `<span style="color:var(--green)">${t('agentTestOk')}</span> · ${t('agentTestReply', { reply: this._esc((r.reply || '').slice(0, 40)) })}`;
      } else {
        res.innerHTML = `<span style="color:var(--red)">${t('agentTestFail', { msg: this._esc(r && r.error ? r.error : t('agentUnknown')) })}</span>`;
      }
    } catch (e) {
      res.innerHTML = `<span style="color:var(--red)">✗ ${this._esc(e.message)}</span>`;
    }
  },

  // ════════════════════════════════════════════════════════════════
  // P3: Knowledge base + prompt library
  // ════════════════════════════════════════════════════════════════
  _bindKBPrompts() {
    const $ = id => document.getElementById(id);
    // KB
    $('btn-agent-kb-add').addEventListener('click', () => this._kbEdit(null));
    $('btn-agent-kb-save').addEventListener('click', () => this._kbSave());
    $('btn-agent-kb-cancel').addEventListener('click', () => this._kbCloseEditor());
    // Prompts
    $('btn-agent-prompt-add').addEventListener('click', () => this._promptEdit(null));
    $('btn-agent-prompt-save').addEventListener('click', () => this._promptSave());
    $('btn-agent-prompt-cancel').addEventListener('click', () => this._promptCloseEditor());
    $('agent-prompt-select').addEventListener('change', e => {
      const p = this.prompts.find(x => x.name === e.target.value);
      if (p) {
        const inp = document.getElementById('agent-prompt');
        inp.value = p.text;
        inp.style.height = 'auto';
        inp.style.height = Math.min(200, inp.scrollHeight) + 'px';
        inp.focus();
      }
      e.target.value = '';
    });
  },

  async _refreshKB() {
    let docs = [];
    try { const r = await window.api.sendCommand('agent_kb_list', {}); docs = (r && r.documents) || []; }
    catch (e) { return; }
    const list = document.getElementById('agent-kb-list');
    if (!list) return;
    if (!docs.length) { list.innerHTML = `<div style="color:var(--text3);font-size:12px">${t('agentNoDocs')}</div>`; return; }
    list.innerHTML = '';
    docs.forEach(d => {
      const el = document.createElement('div');
      el.style.cssText = 'display:flex;align-items:center;gap:8px;padding:6px 8px;border:1px solid var(--border);border-radius:8px';
      el.innerHTML = `<i class="ph ph-file-text" style="color:var(--text3)"></i><span style="flex:1;font-size:13px" title="${this._esc(d.preview || '')}">${this._esc(d.name)}</span>
        <button class="btn btn-outline btn-sm" data-a="edit"><i class="ph ph-pencil"></i></button>
        <button class="btn btn-outline btn-sm" data-a="del"><i class="ph ph-trash"></i></button>`;
      el.querySelector('[data-a="edit"]').addEventListener('click', () => this._kbEdit(d.name));
      el.querySelector('[data-a="del"]').addEventListener('click', () => this._kbDelete(d.name));
      list.appendChild(el);
    });
  },
  async _kbEdit(name) {
    const $ = id => document.getElementById(id);
    $('agent-kb-editor').classList.remove('hidden');
    if (name) {
      try { const r = await window.api.sendCommand('agent_kb_get', { name }); $('agent-kb-name').value = r.name; $('agent-kb-content').value = r.content || ''; }
      catch (e) { App.toast(t('agentReadFail'), 'error'); }
    } else { $('agent-kb-name').value = ''; $('agent-kb-content').value = ''; }
  },
  _kbCloseEditor() { document.getElementById('agent-kb-editor').classList.add('hidden'); },
  async _kbSave() {
    const $ = id => document.getElementById(id);
    const name = $('agent-kb-name').value.trim();
    const content = $('agent-kb-content').value;
    if (!name) { App.toast(t('agentDocNameRequired'), 'warning'); return; }
    try { await window.api.sendCommand('agent_kb_save', { name, content }); App.toast(t('agentSaved'), 'success'); this._kbCloseEditor(); this._refreshKB(); }
    catch (e) { App.toast(t('agentSaveFailedMsg', { msg: e.message }), 'error'); }
  },
  async _kbDelete(name) {
    if (!(await Modal.showConfirm(t('agentDelDocTitle'), t('agentDelDocMsg', { name })))) return;
    try { await window.api.sendCommand('agent_kb_delete', { name }); this._refreshKB(); }
    catch (e) { App.toast(t('agentDeleteFail'), 'error'); }
  },

  async _refreshPrompts() {
    try { const r = await window.api.sendCommand('agent_get_prompts', {}); this.prompts = (r && r.prompts) || []; }
    catch (e) { this.prompts = []; }
    // render list
    const list = document.getElementById('agent-prompt-list');
    if (list) {
      if (!this.prompts.length) { list.innerHTML = `<div style="color:var(--text3);font-size:12px">${t('agentNoPrompts')}</div>`; }
      else {
        list.innerHTML = '';
        this.prompts.forEach((p, i) => {
          const el = document.createElement('div');
          el.style.cssText = 'display:flex;align-items:center;gap:8px;padding:6px 8px;border:1px solid var(--border);border-radius:8px';
          el.innerHTML = `<i class="ph ph-quote" style="color:var(--text3)"></i><span style="flex:1;font-size:13px">${this._esc(p.name)}</span>
            <button class="btn btn-outline btn-sm" data-a="edit"><i class="ph ph-pencil"></i></button>
            <button class="btn btn-outline btn-sm" data-a="del"><i class="ph ph-trash"></i></button>`;
          el.querySelector('[data-a="edit"]').addEventListener('click', () => this._promptEdit(i));
          el.querySelector('[data-a="del"]').addEventListener('click', () => this._promptDelete(i));
          list.appendChild(el);
        });
      }
    }
    // populate dock select
    const sel = document.getElementById('agent-prompt-select');
    if (sel) {
      const cur = sel.value;
      sel.innerHTML = '<option value="">' + t('agentPromptEllipsis') + '</option>' + this.prompts.map(p => `<option value="${this._esc(p.name)}">${this._esc(p.name)}</option>`).join('');
      sel.value = '';
    }
  },
  _promptEdit(idx) {
    const $ = id => document.getElementById(id);
    $('agent-prompt-editor').classList.remove('hidden');
    const p = (idx != null && this.prompts[idx]) || {};
    $('agent-prompt-name').value = p.name || '';
    $('agent-prompt-text').value = p.text || '';
    this._promptEditIdx = idx;
  },
  _promptCloseEditor() { document.getElementById('agent-prompt-editor').classList.add('hidden'); this._promptEditIdx = null; },
  async _promptSave() {
    const $ = id => document.getElementById(id);
    const name = $('agent-prompt-name').value.trim();
    const text = $('agent-prompt-text').value;
    if (!name) { App.toast(t('agentNameRequired'), 'warning'); return; }
    const list = this.prompts.slice();
    const idx = this._promptEditIdx;
    if (idx != null && list[idx]) list[idx] = { name, text };
    else list.push({ name, text });
    try { await window.api.sendCommand('agent_save_prompts', { prompts: list }); this.prompts = list; this._promptCloseEditor(); this._refreshPrompts(); App.toast(t('agentSaved'), 'success'); }
    catch (e) { App.toast(t('agentSaveFailedMsg', { msg: e.message }), 'error'); }
  },
  async _promptDelete(idx) {
    const p = this.prompts[idx];
    if (!p || !(await Modal.showConfirm(t('agentDelPromptTitle'), t('agentDelPromptMsg', { name: p.name })))) return;
    const list = this.prompts.slice(); list.splice(idx, 1);
    try { await window.api.sendCommand('agent_save_prompts', { prompts: list }); this.prompts = list; this._refreshPrompts(); }
    catch (e) { App.toast(t('agentDeleteFail'), 'error'); }
  },

  // ════════════════════════════════════════════════════════════════
  // Scheduled tasks (localStorage, mirroring automation.js)
  // ════════════════════════════════════════════════════════════════
  _tasksKey() { return 'agent_tasks_' + (App.user?.id || 'default'); },
  _logKey() { return 'agent_run_log_' + (App.user?.id || 'default'); },

  loadTasks() {
    try { this.tasks = JSON.parse(localStorage.getItem(this._tasksKey()) || '[]'); }
    catch (e) { this.tasks = []; }
  },
  saveTasks() {
    localStorage.setItem(this._tasksKey(), JSON.stringify(this.tasks));
  },

  _bindTasks() {
    const $ = id => document.getElementById(id);
    $('btn-agent-task-add').addEventListener('click', () => this._openTaskForm(null));
    $('btn-agent-task-cancel').addEventListener('click', () => this._closeTaskForm());
    $('btn-agent-task-save').addEventListener('click', () => this._saveTaskForm());
    $('btn-agent-task-folder').addEventListener('click', async () => {
      const f = await window.api.selectFolder();
      if (f) $('agent-task-folder').value = f;
    });
    $('agent-task-freq').addEventListener('change', () => this._syncTaskFormFields());
    $('agent-task-type').addEventListener('change', () => this._syncTaskFormFields());
  },

  _fillTimeSelects() {
    const h = document.getElementById('agent-task-hour');
    const m = document.getElementById('agent-task-minute');
    if (h && !h.options.length) {
      for (let i = 0; i < 24; i++) {
        const o = document.createElement('option'); o.value = i; o.textContent = String(i).padStart(2, '0'); h.appendChild(o);
      }
    }
    if (m && !m.options.length) {
      [0, 10, 20, 30, 40, 50].forEach(v => {
        const o = document.createElement('option'); o.value = v; o.textContent = String(v).padStart(2, '0'); m.appendChild(o);
      });
    }
  },

  _syncTaskFormFields() {
    const $ = id => document.getElementById(id);
    const type = $('agent-task-type').value;
    const isWatch = type === 'watch';
    $('agent-task-schedule-fields').classList.toggle('hidden', isWatch);
    $('agent-task-watch-fields').classList.toggle('hidden', !isWatch);
    $('agent-task-onlynew-row').classList.toggle('hidden', isWatch);
    if (!isWatch) {
      const freq = $('agent-task-freq').value;
      const isInterval = freq === 'interval';
      $('agent-task-interval-row').classList.toggle('hidden', !isInterval);
      $('agent-task-time-row').classList.toggle('hidden', isInterval || freq === 'hourly');
      $('agent-task-day-row').classList.toggle('hidden', isInterval || freq !== 'weekly');
    }
  },

  _openTaskForm(task) {
    const $ = id => document.getElementById(id);
    $('agent-task-form').classList.remove('hidden');
    $('agent-task-id').value = task ? task.id : '';
    $('agent-task-name').value = task ? task.name : '';
    $('agent-task-enabled').value = String(task ? task.enabled !== false : true);
    const type = (task && task.type) || 'schedule';
    $('agent-task-type').value = type;
    const sch = (task && task.schedule) || { type: 'custom', custom: { freq: 'daily', hour: 9, minute: 0, dayOfWeek: '1', multiTimes: [] } };
    const c = sch.custom || {};
    const freq = sch.type === 'hourly' ? 'hourly' : (c.freq || 'daily');
    $('agent-task-freq').value = freq;
    $('agent-task-day').value = c.dayOfWeek || '1';
    $('agent-task-hour').value = String(c.hour != null ? c.hour : 9);
    $('agent-task-minute').value = String(c.minute != null ? c.minute : 0);
    $('agent-task-interval').value = (task && task.interval_minutes) || 30;
    $('agent-task-threshold').value = (task && task.threshold) || 10;
    $('agent-task-quiet').value = (task && task.quiet_seconds) || 8;
    $('agent-task-folder').value = task ? (task.work_folder || '') : '';
    $('agent-task-prompt').value = task ? (task.prompt || '') : '';
    $('agent-task-autoapprove').value = String(task ? task.auto_approve_destructive === true : true);
    $('agent-task-onlynew').value = String(task ? task.only_new_files === true : false);
    $('agent-task-model').value = (task && task.model) || '';
    $('agent-task-reasoning').value = (task && task.reasoning_effort) || '';
    this._syncTaskFormFields();
  },

  _closeTaskForm() {
    document.getElementById('agent-task-form').classList.add('hidden');
  },

  _saveTaskForm() {
    const $ = id => document.getElementById(id);
    const id = $('agent-task-id').value || this._uid();
    const type = $('agent-task-type').value;
    const freq = $('agent-task-freq').value;
    const schedule = freq === 'hourly'
      ? { type: 'hourly', custom: {} }
      : { type: 'custom', custom: {
          freq,
          dayOfWeek: $('agent-task-day').value,
          hour: parseInt($('agent-task-hour').value) || 9,
          minute: parseInt($('agent-task-minute').value) || 0,
          multiTimes: [],
        } };
    const task = {
      id,
      type,
      name: $('agent-task-name').value.trim() || t('agentUnnamedTask'),
      enabled: $('agent-task-enabled').value === 'true',
      schedule,
      work_folder: $('agent-task-folder').value.trim(),
      prompt: $('agent-task-prompt').value.trim(),
      auto_approve_destructive: $('agent-task-autoapprove').value === 'true',
      only_new_files: $('agent-task-onlynew').value === 'true',
      model: $('agent-task-model').value.trim() || null,
      reasoning_effort: $('agent-task-reasoning').value || null,
    };
    if (type === 'watch') {
      task.threshold = parseInt($('agent-task-threshold').value) || 10;
      task.quiet_seconds = parseInt($('agent-task-quiet').value) || 8;
    }
    if (freq === 'interval') {
      task.interval_minutes = parseInt($('agent-task-interval').value) || 30;
    }
    if (!task.work_folder) { App.toast(t('agentNeedFolder'), 'warning'); return; }
    if (!task.prompt) { App.toast(t('agentNeedPrompt'), 'warning'); return; }

    const idx = this.tasks.findIndex(t => t.id === id);
    if (idx >= 0) this.tasks[idx] = task; else this.tasks.push(task);
    this.saveTasks();
    this._disarm(id);
    this._closeTaskForm();
    this.renderTasks();
    this.armAll();
    App.toast(t('agentTaskSaved'), 'success');
  },

  _editTask(id) { const t = this.tasks.find(x => x.id === id); if (t) this._openTaskForm(t); },

  async _deleteTask(id) {
    if (!(await Modal.showConfirm(t('agentDelTaskTitle'), t('agentDelTaskMsg')))) return;
    this._disarm(id);
    this.tasks = this.tasks.filter(t => t.id !== id);
    this.saveTasks();
    this.renderTasks();
  },

  _toggleTask(id) {
    const t = this.tasks.find(x => x.id === id);
    if (!t) return;
    t.enabled = !t.enabled;
    this.saveTasks();
    this._disarm(id);
    this.armAll();
    this.renderTasks();
  },

  renderTasks() {
    const list = document.getElementById('agent-task-list');
    if (!list) return;
    if (!this.tasks.length) {
      list.innerHTML = `<div style="color:var(--text3);font-size:13px;padding:8px">${t('agentNoTasksHint')}</div>`;
      return;
    }
    list.innerHTML = '';
    this.tasks.forEach(t => {
      const el = document.createElement('div');
      el.style.cssText = 'border:1px solid var(--border);border-radius:8px;padding:10px;display:flex;flex-direction:column;gap:4px';
      el.dataset.taskId = t.id;
      const isWatch = t.type === 'watch';
      const next = isWatch ? null : this._calcNextRun(t);
      const lastRun = this._lastRunText(t.id);
      const metaLine = isWatch
        ? I18N.t('agentWatchMeta', { threshold: t.threshold, quiet: t.quiet_seconds }) + `<span data-watch-count>0</span>/${t.threshold}${lastRun ? ' · ' + lastRun : ''}`
        : `${this._fmtSchedule(t)}${t.only_new_files ? ' · ' + I18N.t('agentNewFilesOnly') : ''} · ${next ? I18N.t('agentNextRun', { time: next.toLocaleString('zh-CN', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) }) : I18N.t('schedNoSchedule')}${lastRun ? ' · ' + lastRun : ''}`;
      el.innerHTML = `
        <div style="display:flex;align-items:center;gap:8px">
          <input type="checkbox" ${t.enabled ? 'checked' : ''} data-act="toggle">
          <b style="flex:1">${this._esc(t.name)}</b>
          <span class="badge" style="background:${t.enabled ? 'var(--brand)' : 'var(--bg3)'};color:${t.enabled ? '#fff' : 'var(--text3)'}">${isWatch ? I18N.t('agentListen') : I18N.t('agentScheduled2')} · ${t.enabled ? I18N.t('agentEnabled') : I18N.t('agentDisabled')}</span>
          <button class="btn btn-outline btn-sm" data-act="run"><i class="ph ph-play"></i></button>
          <button class="btn btn-outline btn-sm" data-act="edit"><i class="ph ph-pencil"></i></button>
          <button class="btn btn-outline btn-sm" data-act="del"><i class="ph ph-trash"></i></button>
        </div>
        <div style="font-size:12px;color:var(--text2)">📁 ${this._esc(t.work_folder)}</div>
        <div style="font-size:12px;color:var(--text2)">💬 ${this._esc(t.prompt)}</div>
        <div style="font-size:11px;color:var(--text3)">${metaLine}</div>`;
      el.querySelector('[data-act="toggle"]').addEventListener('change', () => this._toggleTask(t.id));
      el.querySelector('[data-act="run"]').addEventListener('click', () => this._runTaskNow(t));
      el.querySelector('[data-act="edit"]').addEventListener('click', () => this._editTask(t.id));
      el.querySelector('[data-act="del"]').addEventListener('click', () => this._deleteTask(t.id));
      list.appendChild(el);
    });
  },

  // ── Per-task run state (last_run_ts + history) ──
  _taskStateKey() { return 'agent_task_state_' + (App.user?.id || 'default'); },
  _loadTaskState() {
    try { this.taskState = JSON.parse(localStorage.getItem(this._taskStateKey()) || '{}') || {}; }
    catch (e) { this.taskState = {}; }
  },
  _saveTaskState() { try { localStorage.setItem(this._taskStateKey(), JSON.stringify(this.taskState)); } catch (e) { /* quota */ } },
  _setTaskState(taskId, patch) {
    const s = this.taskState[taskId] || {};
    Object.assign(s, patch);
    this.taskState[taskId] = s;
    this._saveTaskState();
  },
  _recordTaskRun(taskId, rec) {
    const s = this.taskState[taskId] || { history: [] };
    s.history = s.history || [];
    s.history.push(rec);
    if (s.history.length > 10) s.history = s.history.slice(-10);
    // last_run_ts 是 only_new_files 的新文件水位线——只有成功才推进，
    // 失败保持原值，避免本次的新文件下次永久漏处理
    if (rec.status === 'done') s.last_run_ts = rec.ts / 1000;
    s.last_status = rec.status;
    this.taskState[taskId] = s;
    this._saveTaskState();
    this.renderTasks();
  },
  _lastRunText(taskId) {
    const s = this.taskState[taskId];
    if (!s || !s.history || !s.history.length) return '';
    const last = s.history[s.history.length - 1];
    const t = new Date(last.ts).toLocaleString('zh-CN', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
    if (last.status === 'done') return I18N.t('agentLastRunOk', { time: t, tokens: last.tokens || 0 });
    return I18N.t('agentLastRunFail', { time: t });
  },

  _fmtSchedule(task) {
    return Scheduler.fmtSchedule(task && task.schedule, task && task.interval_minutes);
  },

  // ── Scheduling (adapted from automation.js _calcNextRun; takes a task) ──
  _calcNextRun(task) {
    return Scheduler.calcNextRun(task && task.schedule, task && task.interval_minutes);
  },

  _disarm(id) {
    if (this._timers[id]) { clearTimeout(this._timers[id]); delete this._timers[id]; }
    const t = this.tasks.find(x => x.id === id);
    if (t && t.type === 'watch') {
      // stop backend watcher (best-effort)
      try { window.api.sendCommand('agent_watch_stop', { id }); } catch (e) { /* ignore */ }
    }
  },

  armAll() {
    this.tasks.forEach(t => {
      if (!t.enabled) return;
      this._disarm(t.id);
      if (t.type === 'watch') {
        // start a backend hot-folder watcher
        try {
          window.api.sendCommand('agent_watch_start', {
            id: t.id,
            name: t.name,
            watch_folder: t.work_folder,
            threshold: t.threshold || 10,
            quiet_seconds: t.quiet_seconds || 8,
            prompt: t.prompt,
            auto_approve_destructive: t.auto_approve_destructive !== false ? true : true, // force auto for unattended
            model: t.model || null,
            cloud_token: localStorage.getItem('studio_token') || '',
          });
        } catch (e) { /* ignore */ }
        return;
      }
      const next = this._calcNextRun(t);
      if (!next) return;
      // Scheduler.calcNextRun always returns a future time — no catch-up branch needed
      const delay = next - Date.now();
      this._timers[t.id] = setTimeout(() => { this.fire(t); this.armAll(); }, delay);
    });
  },

  // LLM key 是否已配置（refreshApiKeyGate 同款判断）；查询失败时放行，
  // 维持原流程由 agent_run 的错误路径处理
  async _hasLlmKey() {
    try {
      const r = await window.api.sendCommand('agent_get_config', {});
      return !!(r && r.agent && (r.agent.api_key_set
        || (r.agent.use_local && r.agent.local_model_present)
        || r.agent.official_key_set));
    } catch (e) { return true; }
  },

  async fire(task, opts = {}) {
    // LLM key 预检：无 key 时点火只会立即报错 + 60s 重试（每周期两条失败
    // 通知）——缺失则静默跳过，每次触发最多提示一条
    if (!(await this._hasLlmKey())) {
      this.addLog(t('agentLogNoKey', { name: task.name }), 'warn');
      App.toast(t('agentLogNoKey', { name: task.name }), 'warning');
      return;
    }
    const sid = opts.session_id || this._uid();
    // Prepare prompt: optionally restrict to new files since last run.
    let prompt = opts.prompt || null;
    if (!prompt && task.type === 'schedule' && task.only_new_files) {
      const st = this.taskState[task.id] || {};
      let files = [];
      try {
        const r = await window.api.sendCommand('agent_list_new_files', { folder: task.work_folder, since_ts: st.last_run_ts || 0 });
        files = (r && r.files) || [];
      } catch (e) { /* backend not ready */ }
      if (!files.length) {
        this.addLog(t('agentLogNoNewFiles', { name: task.name }), 'info');
        return;
      }
      prompt = task.prompt + `\n\n[本次仅处理新增的 ${files.length} 张照片：]\n` + files.slice(0, 30).join('\n') + (files.length > 30 ? '\n…' : '');
      // last_run_ts 不在此预推进——由 _recordTaskRun 在执行成功后推进，
      // 失败则保持原值，下次调度重试同一批新文件
    }
    if (!prompt) prompt = task.prompt;
    this._firedSessions[sid] = { taskId: task.id, retried: !!opts.isRetry, prompt };
    this.addLog(t('agentLogTaskFired', { name: task.name }) + (opts.isRetry ? t('agentRetryMark') : ''), 'info');
    // Unattended: force auto-approve so destructive ops don't block forever.
    const autoApprove = true;
    if (task.auto_approve_destructive !== true) {
      this.addLog(t('agentLogForceApprove'), 'warn');
    }
    try {
      const r = await window.api.sendCommand('agent_run', {
        session_id: sid,
        work_folder: task.work_folder,
        prompt,
        auto_approve_destructive: autoApprove,
        model: task.model || null,
        reasoning_effort: task.reasoning_effort || null,
        headless: true,
        task_name: task.name || '',
        cloud_token: localStorage.getItem('studio_token') || '',
      });
      if (!r || !r.ok) {
        this.addLog(t('agentLogStartFail', { error: (r && r.error) || t('agentUnknown') }), 'error');
        if (typeof App !== 'undefined' && App.notify) App.notify(t('agentNotifyTaskFail'), task.name + ': ' + ((r && r.error) || ''));
        delete this._firedSessions[sid];
      }
    } catch (e) {
      this.addLog(t('agentLogStartError', { msg: e.message }), 'error');
      delete this._firedSessions[sid];
    }
  },

  _runTaskNow(task) {
    if (this.running) { App.toast(t('agentRunningWait'), 'warning'); return; }
    // run as interactive so the user sees it in the chat
    this.workFolder = task.work_folder;
    document.getElementById('agent-folder').value = task.work_folder;
    document.getElementById('agent-prompt').value = task.prompt;
    document.getElementById('agent-autoapprove').value = String(task.auto_approve_destructive === true);
    this.send();
  },
};

document.addEventListener('DOMContentLoaded', () => Agent.init());
