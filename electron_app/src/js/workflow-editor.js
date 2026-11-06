/**
 * workflow-editor.js — 自定义工作流模板编辑器
 *
 * 提供可视化拖拽编辑管线模板的功能：
 * - 新建/编辑/克隆/删除模板
 * - 添加/删除/排序阶段和步骤
 * - 实时验证步骤依赖关系
 * - HTML5 原生拖拽（零依赖）
 */
const WorkflowEditor = (() => {
  // ── State ──
  let _editingTemplate = null;     // 当前编辑的模板对象
  let _availableSteps = {};        // 从后端获取的步骤元数据
  let _errors = [];
  let _warnings = [];
  let _dirty = false;              // 是否有未保存的修改
  let _draft = false;              // 是否为 AI 生成草稿（顶部横幅，保存后消失）
  let _originalId = null;          // 打开时的原模板 ID（改 ID 保存时后端据此删旧文件）
  let _configTarget = null;        // 正在编辑 params/condition/gate 的步骤 {pi, si}
  let _onSaveCallback = null;      // 保存后的回调（刷新模板列表）

  const $ = (id) => document.getElementById(id);

  // ── Category display info ──
  function _catLabel(cat) {
    const map = { 'import': t('wfCatImport'), 'score': t('wfCatScore'), 'dedup': t('wfCatDedup'), 'cull': t('wfCatCull'), 'output': t('wfCatOutput'), 'deliver': t('wfCatDeliver') };
    return map[cat] || cat || '';
  }

  const CATEGORY_ICONS = {
    'import': '📦', 'score': '⭐', 'dedup': '🔗', 'cull': '🗑️', 'output': '✅', 'deliver': '🚚',
  };

  function init() {
    _bindEvents();
    console.log('[workflow-editor] init done');
  }

  function _bindEvents() {
    $('wf-cancel-btn').addEventListener('click', closeEditor);
    $('wf-save-btn').addEventListener('click', _save);
    $('wf-add-phase-btn').addEventListener('click', _addPhase);

    // 步骤参数弹窗
    $('wf-step-config-save').addEventListener('click', _saveStepConfig);
    $('wf-step-config-cancel').addEventListener('click', _closeStepConfig);
    $('wf-step-config-modal').addEventListener('click', (e) => {
      if (e.target === $('wf-step-config-modal')) _closeStepConfig();
    });

    // 关闭弹窗（点击背景）
    $('workflow-editor-modal').addEventListener('click', async (e) => {
      if (e.target === $('workflow-editor-modal')) {
        if (_dirty) {
          if (!(await Modal.showConfirm(t('wfUnsavedTitle'), t('wfUnsaved')))) return;
        }
        closeEditor();
      }
    });

    // 头部 标题/ID/描述 输入：标 _dirty + ID 实时校验（静态元素，只绑一次）
    bindHeaderEvents();
  }

  function setOnSave(fn) {
    _onSaveCallback = fn;
  }

  // ═══════════════════════════════════════════════════════════════════════
  // Step loading
  // ═══════════════════════════════════════════════════════════════════════

  async function _ensureSteps() {
    if (Object.keys(_availableSteps).length > 0) return true;
    try {
      const res = await window.api.sendCommand('get_available_steps', {});
      if (res.error) {
        console.error('[wf-editor] get_available_steps 失败:', res.error);
        App.toast(t('wfStepsFetchErr', { err: res.error }), 'error');
        return false;
      }
      _availableSteps = res.steps || {};
      if (Object.keys(_availableSteps).length === 0) {
        App.toast(t('wfStepsEmpty'), 'error');
        return false;
      }
      return true;
    } catch (e) {
      console.error('[wf-editor] 获取步骤列表失败:', e);
      App.toast(t('wfStepsLoadFail'), 'error');
      return false;
    }
  }

  // ═══════════════════════════════════════════════════════════════════════
  // Open / Close
  // ═══════════════════════════════════════════════════════════════════════

  async function openEditor(templateOrId, opts) {
    // 确保步骤列表已加载
    if (!(await _ensureSteps())) return;

    _draft = !!(opts && opts.draft);
    _originalId = null;  // 默认无原 ID（新建/克隆/草稿）；仅编辑已存在的自定义模板时设置

    // 解析模板
    if (templateOrId === null || templateOrId === undefined) {
      // 新建空白模板
      _editingTemplate = _blankTemplate();
    } else if (typeof templateOrId === 'string') {
      // 从列表获取模板数据
      const tpl = _findTemplateById(templateOrId);
      if (!tpl) {
        App.toast(t('wfTplNotExist', { id: templateOrId }), 'error');
        return;
      }
      if (tpl.source === 'builtin') {
        // 内置模板 → 克隆后编辑
        _editingTemplate = _cloneFromBuiltin(tpl);
      } else {
        _editingTemplate = JSON.parse(JSON.stringify(tpl));
        _originalId = tpl.id;
      }
    } else if (typeof templateOrId === 'object') {
      _editingTemplate = JSON.parse(JSON.stringify(templateOrId));
    }

    _errors = [];
    _warnings = [];
    _dirty = _draft;  // AI 草稿本身就是未保存的修改
    _render();
    _updateDraftBanner();
    Modal.animatedShow($('workflow-editor-modal'));
  }

  function _updateDraftBanner() {
    const b = $('wf-draft-banner');
    if (b) b.style.display = _draft ? '' : 'none';
  }

  function closeEditor() {
    Modal.animatedHide($('workflow-editor-modal'));
    _editingTemplate = null;
    _errors = [];
    _warnings = [];
    _dirty = false;
    _draft = false;
    _updateDraftBanner();
  }

  // ═══════════════════════════════════════════════════════════════════════
  // Template helpers
  // ═══════════════════════════════════════════════════════════════════════

  function _blankTemplate() {
    return {
      id: '',
      title: '',
      description: '',
      source: 'custom',
      version: 1,
      phases: [
        { id: 'phase1', title: t('wfPhaseDefault', { n: 1 }), steps: [] },
      ],
    };
  }

  function _cloneFromBuiltin(tpl) {
    return {
      id: _uniqueCustomId(tpl.id + '_custom'),
      title: tpl.title + t('wfSuffixCustom'),
      description: tpl.description || '',
      source: 'custom',
      base_template: tpl.id,
      version: 1,
      phases: JSON.parse(JSON.stringify(tpl.phases || [])),
    };
  }

  // 生成不与现有模板冲突的自定义 ID：xx_custom 已存在时追加序号（_custom2、_custom3…），
  // 避免克隆保存时静默覆盖旧自定义模板
  function _uniqueCustomId(baseId) {
    const existing = (typeof PipelineUI !== 'undefined' && PipelineUI.getTemplates)
      ? PipelineUI.getTemplates().map(t => t.id) : [];
    let id = baseId;
    for (let n = 2; existing.indexOf(id) !== -1; n++) id = baseId + n;
    return id;
  }

  // 从缓存的模板列表中查找
  function _findTemplateById(id) {
    // 尝试从 PipelineUI 的模板缓存中获取
    if (typeof PipelineUI !== 'undefined' && PipelineUI.getTemplates) {
      const templates = PipelineUI.getTemplates();
      return templates.find(t => t.id === id);
    }
    return null;
  }

  // ═══════════════════════════════════════════════════════════════════════
  // Render
  // ═══════════════════════════════════════════════════════════════════════

  function _render() {
    const tpl = _editingTemplate;
    if (!tpl) return;

    // 头部
    $('wf-editor-title').value = tpl.title || '';
    $('wf-editor-id').value = tpl.id || '';
    $('wf-editor-desc').value = tpl.description || '';

    // 阶段列表
    _renderPhases();
    _validate();
  }

  function _renderPhases() {
    const container = $('wf-phases-container');
    if (!container) return;

    const phases = _editingTemplate.phases || [];
    let html = '';

    phases.forEach((phase, pi) => {
      const steps = phase.steps || [];
      html += '<div class="wf-phase-card" data-phase-index="' + pi + '">';
      // Phase header
      html += '<div class="wf-phase-header">';
      html += '<input type="text" class="wf-phase-id-input" value="' + _esc(phase.id || '') + '" '
            + 'placeholder="' + t('wfPhaseId') + '" style="width:80px;font-size:12px" data-phase-id="true">';
      html += '<input type="text" class="wf-phase-title-input" value="' + _esc(phase.title || '') + '" '
            + 'placeholder="' + t('wfPhaseTitle') + '" style="flex:1;font-weight:600;font-size:13px" data-phase-title="true">';
      html += '<button class="btn-icon" title="' + t('wfMoveUp') + '" data-action="move-phase-up" data-pi="' + pi + '">'
            + '<i class="ph ph-arrow-up"></i></button>';
      html += '<button class="btn-icon" title="' + t('wfMoveDown') + '" data-action="move-phase-down" data-pi="' + pi + '">'
            + '<i class="ph ph-arrow-down"></i></button>';
      html += '<button class="btn-icon" title="' + t('wfDelPhase') + '" data-action="delete-phase" data-pi="' + pi + '" '
            + 'style="color:var(--danger)"><i class="ph ph-x"></i></button>';
      html += '</div>';

      // Step list
      html += '<div class="wf-steps-list">';
      steps.forEach((step, si) => {
        const meta = _availableSteps[step.name] || {};
        const invalid = _stepHasError(step.name, pi, si);
        const displayTitle = meta.title || step.name;
        html += '<div class="wf-step-row' + (invalid ? ' invalid' : '') + '" '
              + 'draggable="true" data-step-index="' + si + '" data-phase-index="' + pi + '">';
        html += '<span class="wf-drag-handle">⠿</span>';
        html += '<span class="wf-step-title">' + _esc(displayTitle) + '</span>';
        html += '<span class="wf-step-name">' + _esc(step.name) + '</span>';
        html += '<span class="wf-step-cat" style="margin-left:auto;font-size:11px;color:var(--text3)">'
              + _esc(_catLabel(meta.category)) + '</span>';
        const hasCfg = !!(step.params || step.condition || step.gate);
        html += '<button class="btn-icon" title="' + t('wfStepCfgTip') + '" data-action="config-step" data-pi="' + pi + '" data-si="' + si + '"'
              + (hasCfg ? ' style="color:var(--brand)"' : '') + '><i class="ph ph-gear"></i></button>';
        html += '<button class="btn-icon" title="' + t('wfMoveUp') + '" data-action="move-step-up" data-pi="' + pi + '" data-si="' + si + '">'
              + '<i class="ph ph-arrow-up"></i></button>';
        html += '<button class="btn-icon" title="' + t('wfMoveDown') + '" data-action="move-step-down" data-pi="' + pi + '" data-si="' + si + '">'
              + '<i class="ph ph-arrow-down"></i></button>';
        html += '<button class="btn-icon" title="' + t('wfRemove') + '" data-action="remove-step" data-pi="' + pi + '" data-si="' + si + '" '
              + 'style="color:var(--danger)"><i class="ph ph-x"></i></button>';
        html += '</div>';
      });

      if (steps.length === 0) {
        html += '<div class="wf-empty-hint" style="padding:8px;text-align:center;color:var(--text3);font-size:12px">'
              + t('wfEmptySteps') + '</div>';
      }

      html += '</div>';

      // Add step dropdown
      html += '<div style="margin-top:4px">';
      html += '<select class="wf-add-step-select" data-pi="' + pi + '" '
            + 'style="width:100%;font-size:12px;padding:4px">';
      html += '<option value="">+ ' + t('wfAddStep') + '...</option>';
      // Group by category
      const grouped = _groupStepsByCategory();
      for (const [cat, catInfo] of Object.entries(grouped)) {
        html += '<optgroup label="' + _esc((CATEGORY_ICONS[cat] || '') + ' ' + _catLabel(cat)) + '">';
        for (const step of catInfo.steps) {
          html += '<option value="' + _esc(step.name) + '">' + _esc(step.title) + ' (' + _esc(step.name) + ')' + '</option>';
        }
        html += '</optgroup>';
      }
      html += '</select>';
      html += '</div>';

      html += '</div>';
    });

    container.innerHTML = html;

    // 绑定事件
    _bindPhaseEvents();
    _bindDragEvents();
  }

  function _groupStepsByCategory() {
    const grouped = {};
    for (const [name, meta] of Object.entries(_availableSteps)) {
      const cat = meta.category || 'other';
      if (!grouped[cat]) grouped[cat] = { steps: [] };
      grouped[cat].steps.push({ name, title: meta.title, description: meta.description });
    }
    return grouped;
  }

  function _stepHasError(stepName, pi, si) {
    if (!_availableSteps[stepName]) return true; // unknown step
    const meta = _availableSteps[stepName];
    // Check requires_steps_before
    const allSteps = _allStepsFlat();
    const stepIndex = _globalStepIndex(pi, si);
    for (const req of (meta.requires_steps_before || [])) {
      if (allSteps.indexOf(req) === -1 || allSteps.indexOf(req) >= stepIndex) return true;
    }
    for (const req of (meta.requires_steps_after || [])) {
      if (allSteps.indexOf(req) !== -1 && allSteps.indexOf(req) <= stepIndex) return true;
    }
    return false;
  }

  function _allStepsFlat() {
    const names = [];
    for (const phase of (_editingTemplate.phases || [])) {
      for (const step of (phase.steps || [])) {
        names.push(step.name);
      }
    }
    return names;
  }

  function _globalStepIndex(pi, si) {
    let idx = 0;
    for (let i = 0; i < (_editingTemplate.phases || []).length; i++) {
      const steps = _editingTemplate.phases[i].steps || [];
      if (i === pi) return idx + si;
      idx += steps.length;
    }
    return idx;
  }

  // ═══════════════════════════════════════════════════════════════════════
  // Phase events
  // ═══════════════════════════════════════════════════════════════════════

  // 点击事件绑定在常驻容器上（事件委托），重复绑定会导致一次点击触发多次——只绑一次
  let _phaseEventsBound = false;
  function _bindPhaseEvents() {
    if (_phaseEventsBound) return;
    _phaseEventsBound = true;
    const container = $('wf-phases-container');
    if (!container) return;

    // 点击按钮（async：delete-phase 需等自定义确认框）
    container.addEventListener('click', async (e) => {
      const btn = e.target.closest('[data-action]');
      if (!btn) return;

      const action = btn.dataset.action;
      const pi = parseInt(btn.dataset.pi);
      const si = parseInt(btn.dataset.si);

      switch (action) {
        case 'move-phase-up':
          if (pi > 0) { _swapPhases(pi, pi - 1); _renderPhases(); _validate(); _markDirty(); }
          break;
        case 'move-phase-down':
          if (pi < _editingTemplate.phases.length - 1) { _swapPhases(pi, pi + 1); _renderPhases(); _validate(); _markDirty(); }
          break;
        case 'delete-phase':
          if (_editingTemplate.phases.length <= 1) { App.toast(t('wfKeepOnePhase'), 'warning'); return; }
          if (await Modal.showConfirm(t('wfDelPhase'), t('wfDelPhaseConfirm'))) {
            _editingTemplate.phases.splice(pi, 1);
            _renderPhases(); _validate(); _markDirty();
          }
          break;
        case 'move-step-up':
          if (si > 0) { _swapSteps(pi, si, si - 1); _renderPhases(); _validate(); _markDirty(); }
          break;
        case 'move-step-down':
          if (si < (_editingTemplate.phases[pi].steps || []).length - 1) { _swapSteps(pi, si, si + 1); _renderPhases(); _validate(); _markDirty(); }
          break;
        case 'remove-step':
          _editingTemplate.phases[pi].steps.splice(si, 1);
          _renderPhases(); _validate(); _markDirty();
          break;
        case 'config-step':
          _openStepConfig(pi, si);
          break;
      }
    });

    // 添加步骤下拉
    container.addEventListener('change', (e) => {
      const sel = e.target.closest('.wf-add-step-select');
      if (!sel) return;
      const pi = parseInt(sel.dataset.pi);
      const stepName = sel.value;
      if (!stepName) return;
      if (!_editingTemplate.phases[pi]) return;
      _editingTemplate.phases[pi].steps = _editingTemplate.phases[pi].steps || [];
      _editingTemplate.phases[pi].steps.push({ name: stepName });
      _renderPhases(); _validate(); _markDirty();
    });

    // 阶段标题/ID 输入
    container.addEventListener('input', (e) => {
      if (e.target.dataset.phaseId === 'true') {
        const phaseCard = e.target.closest('.wf-phase-card');
        if (!phaseCard) return;
        const pi = parseInt(phaseCard.dataset.phaseIndex);
        if (!isNaN(pi) && _editingTemplate.phases[pi]) {
          _editingTemplate.phases[pi].id = e.target.value;
          _validate(); _markDirty();
        }
      } else if (e.target.dataset.phaseTitle === 'true') {
        const phaseCard = e.target.closest('.wf-phase-card');
        if (!phaseCard) return;
        const pi = parseInt(phaseCard.dataset.phaseIndex);
        if (!isNaN(pi) && _editingTemplate.phases[pi]) {
          _editingTemplate.phases[pi].title = e.target.value;
          _markDirty();
        }
      }
    });
  }

  // ═══════════════════════════════════════════════════════════════════════
  // Drag-and-Drop
  // ═══════════════════════════════════════════════════════════════════════

  function _bindDragEvents() {
    const container = $('wf-phases-container');
    if (!container) return;

    // Step drag
    container.querySelectorAll('.wf-step-row').forEach(row => {
      row.addEventListener('dragstart', (e) => {
        e.dataTransfer.setData('text/plain', JSON.stringify({
          type: 'step',
          pi: parseInt(row.dataset.phaseIndex),
          si: parseInt(row.dataset.stepIndex),
        }));
        e.dataTransfer.effectAllowed = 'move';
        row.classList.add('dragging');
      });
      row.addEventListener('dragend', () => row.classList.remove('dragging'));
    });

    // Step drop targets (phase cards)
    container.querySelectorAll('.wf-steps-list').forEach(list => {
      list.addEventListener('dragover', (e) => {
        e.preventDefault();
        e.dataTransfer.dropEffect = 'move';
        list.classList.add('drag-over');
      });
      list.addEventListener('dragleave', () => list.classList.remove('drag-over'));
      list.addEventListener('drop', (e) => {
        e.preventDefault();
        list.classList.remove('drag-over');
        try {
          const data = JSON.parse(e.dataTransfer.getData('text/plain'));
          if (data.type !== 'step') return;
          const srcPi = data.pi;
          const srcSi = data.si;
          const phaseCard = list.closest('.wf-phase-card');
          if (!phaseCard) return;
          const dstPi = parseInt(phaseCard.dataset.phaseIndex);
          // Remove from source
          const srcSteps = _editingTemplate.phases[srcPi]?.steps;
          if (!srcSteps || srcSi >= srcSteps.length) return;
          const [moved] = srcSteps.splice(srcSi, 1);
          // Add to destination (at the end, or before the hovered element)
          const dstSteps = _editingTemplate.phases[dstPi]?.steps;
          if (!dstSteps) return;
          // Find insertion index based on mouse position
          const children = Array.from(list.children).filter(c => c.classList.contains('wf-step-row'));
          let insertIdx = dstSteps.length;
          for (let i = 0; i < children.length; i++) {
            const rect = children[i].getBoundingClientRect();
            if (e.clientY < rect.top + rect.height / 2) {
              insertIdx = i;
              break;
            }
          }
          dstSteps.splice(insertIdx, 0, moved);
          _renderPhases(); _validate(); _markDirty();
        } catch (_) {}
      });
    });

    // Phase card drag (for reordering phases)
    container.querySelectorAll('.wf-phase-card').forEach(card => {
      card.addEventListener('dragover', (e) => {
        e.preventDefault();
        e.dataTransfer.dropEffect = 'move';
        card.classList.add('drag-over');
      });
      card.addEventListener('dragleave', () => card.classList.remove('drag-over'));
      card.addEventListener('drop', (e) => {
        e.preventDefault();
        card.classList.remove('drag-over');
        try {
          const data = JSON.parse(e.dataTransfer.getData('text/plain'));
          if (data.type !== 'step') return; // only steps are draggable
          // Handled by the steps-list drop handler above
        } catch (_) {}
      });
    });
  }

  // ═══════════════════════════════════════════════════════════════════════
  // Mutations
  // ═══════════════════════════════════════════════════════════════════════

  function _addPhase() {
    const phases = _editingTemplate.phases || [];
    const idx = phases.length + 1;
    phases.push({ id: 'phase' + idx, title: t('wfPhaseDefault', { n: idx }), steps: [] });
    _renderPhases(); _validate(); _markDirty();
  }

  function _swapPhases(i, j) {
    const phases = _editingTemplate.phases;
    [phases[i], phases[j]] = [phases[j], phases[i]];
  }

  function _swapSteps(pi, si, sj) {
    const steps = _editingTemplate.phases[pi].steps;
    [steps[si], steps[sj]] = [steps[sj], steps[si]];
  }

  function _markDirty() {
    _dirty = true;
  }

  // ═══════════════════════════════════════════════════════════════════════
  // Step params/condition/gate 编辑（内存编辑 + 整体保存，与编辑器现有模式一致）
  // ═══════════════════════════════════════════════════════════════════════

  function _openStepConfig(pi, si) {
    const phase = (_editingTemplate.phases || [])[pi];
    const step = phase && (phase.steps || [])[si];
    if (!step) return;
    _configTarget = { pi, si };
    const meta = _availableSteps[step.name] || {};
    $('wf-step-config-title').textContent = t('wfStepCfgFor', { name: meta.title || step.name });
    $('wf-step-params').value = step.params ? JSON.stringify(step.params, null, 2) : '';
    $('wf-step-condition').value = step.condition ? JSON.stringify(step.condition, null, 2) : '';
    $('wf-step-gate').value = step.gate ? JSON.stringify(step.gate, null, 2) : '';
    $('wf-step-config-msg').textContent = '';
    Modal.animatedShow($('wf-step-config-modal'));
  }

  function _closeStepConfig() {
    Modal.animatedHide($('wf-step-config-modal'));
    _configTarget = null;
  }

  function _parseStepJson(elId, label) {
    const raw = $(elId).value.trim();
    if (!raw) return { value: null };
    try {
      const v = JSON.parse(raw);
      if (!v || typeof v !== 'object' || Array.isArray(v)) {
        return { error: t('wfJsonObjRequired', { label }) };
      }
      return { value: v };
    } catch (e) {
      return { error: t('wfJsonParseFail', { label, err: e.message }) };
    }
  }

  function _saveStepConfig() {
    if (!_configTarget || !_editingTemplate) return;
    const p = _parseStepJson('wf-step-params', 'params');
    const c = _parseStepJson('wf-step-condition', 'condition');
    const g = _parseStepJson('wf-step-gate', 'gate');
    const err = p.error || c.error || g.error;
    if (err) { $('wf-step-config-msg').textContent = err; return; }
    const step = _editingTemplate.phases[_configTarget.pi].steps[_configTarget.si];
    if (p.value) step.params = p.value; else delete step.params;
    if (c.value) step.condition = c.value; else delete step.condition;
    if (g.value) step.gate = g.value; else delete step.gate;
    _closeStepConfig();
    _renderPhases(); _validate(); _markDirty();
  }

  // ═══════════════════════════════════════════════════════════════════════
  // Header input events
  // ═══════════════════════════════════════════════════════════════════════

  // 头部输入是静态元素，由 _bindEvents() 在 init 时调用一次
  function bindHeaderEvents() {
    const titleInput = $('wf-editor-title');
    const idInput = $('wf-editor-id');
    const descInput = $('wf-editor-desc');

    if (titleInput) titleInput.addEventListener('input', () => {
      if (_editingTemplate) { _editingTemplate.title = titleInput.value; _markDirty(); }
    });
    if (idInput) idInput.addEventListener('input', () => {
      if (_editingTemplate) { _editingTemplate.id = idInput.value; _validate(); _markDirty(); }
    });
    if (descInput) descInput.addEventListener('input', () => {
      if (_editingTemplate) { _editingTemplate.description = descInput.value; _markDirty(); }
    });
  }

  // ═══════════════════════════════════════════════════════════════════════
  // Validation
  // ═══════════════════════════════════════════════════════════════════════

  function _validate() {
    const tpl = _editingTemplate;
    if (!tpl) return;

    const errors = [];
    const warnings = [];

    // V1: ID format
    const tid = tpl.id || '';
    if (!tid.match(/^[a-z0-9_]{2,64}$/)) {
      errors.push(t('wfIdInvalid'));
    }

    // V2: Phases
    const phases = tpl.phases || [];
    if (phases.length === 0) {
      errors.push(t('wfNeedPhase'));
    } else {
      phases.forEach((p, i) => {
        if (!(p.steps || []).length) {
          errors.push(t('wfPhaseEmptyMsg', { n: i + 1, title: p.title || t('wfUnnamed') }));
        }
      });
    }

    // V13: Phase ID unique
    const phaseIds = phases.map(p => p.id || '');
    if (new Set(phaseIds).size !== phaseIds.length) {
      errors.push(t('wfPhaseIdDup'));
    }

    // V3-V8: Step constraints
    const allSteps = _allStepsFlat();
    allSteps.forEach((name, idx) => {
      const meta = _availableSteps[name];
      if (!meta) {
        errors.push(t('wfUnknownStepMsg', { name }));
        return;
      }
      for (const req of (meta.requires_steps_before || [])) {
        if (allSteps.indexOf(req) === -1 || allSteps.indexOf(req) >= idx) {
          errors.push(t('wfMustAfter', { step: meta.title, req: _availableSteps[req]?.title || req }));
        }
      }
      for (const req of (meta.requires_steps_after || [])) {
        if (allSteps.indexOf(req) !== -1 && allSteps.indexOf(req) <= idx) {
          errors.push(t('wfMustBefore', { step: meta.title, req: _availableSteps[req]?.title || req }));
        }
      }
    });

    // V10: Artifact chain
    const produced = new Set();
    for (const name of allSteps) {
      const meta = _availableSteps[name];
      if (!meta) continue;
      for (const req of (meta.requires_artifacts || [])) {
        if (!produced.has(req)) {
          warnings.push(t('wfArtifactMissingMsg', { step: meta.title, artifact: req }));
        }
      }
      for (const art of (meta.produces_artifacts || [])) {
        produced.add(art);
      }
    }

    // V14-V16: step 的 params/condition/gate 结构（与后端 step_registry 校验一致）
    const COND_OPS = ['gt', 'ge', 'lt', 'le', 'eq', 'ne', 'exists', 'not_exists', 'contains'];
    const GATE_TYPES = ['action'];
    for (const phase of phases) {
      for (const step of (phase.steps || [])) {
        const meta = _availableSteps[step.name];
        if (!meta) continue;  // 未知步骤已在 V3 报错
        const label = meta.title || step.name;
        // V14: condition 结构 —— key 前缀 artifacts./params./goal + op 白名单
        if (step.condition != null) {
          const c = step.condition;
          const keyOk = typeof c.key === 'string' &&
            (c.key === 'goal' || c.key.startsWith('artifacts.') || c.key.startsWith('params.'));
          if (typeof c !== 'object' || Array.isArray(c) || !keyOk || COND_OPS.indexOf(c.op) === -1) {
            errors.push(t('wfCondInvalid', { label, ops: COND_OPS.join('/') }));
          } else if (c.op !== 'exists' && c.op !== 'not_exists' && !('value' in c)) {
            // 与后端 step_registry 一致：非 exists 类 op 必须带 value
            errors.push(t('wfCondNeedValue', { label, op: c.op }));
          }
        }
        // V15: gate 结构 —— type 白名单 + timeout 若提供须为正数
        if (step.gate != null) {
          const g = step.gate;
          const timeoutOk = g.timeout == null || (typeof g.timeout === 'number' && g.timeout > 0);
          if (typeof g !== 'object' || Array.isArray(g) || GATE_TYPES.indexOf(g.type) === -1 || !timeoutOk) {
            errors.push(t('wfGateInvalid', { label, types: GATE_TYPES.join('/') }));
          }
        }
        // V16: params 键须在该步骤 user_configurable_params 内（仅警告）
        if (step.params != null) {
          const p = step.params;
          if (typeof p !== 'object' || Array.isArray(p)) {
            errors.push(t('wfParamsObjInvalid', { label }));
          } else {
            const allowed = meta.user_configurable_params || [];
            for (const k of Object.keys(p)) {
              if (allowed.indexOf(k) === -1) {
                warnings.push(t('wfParamKeyNotAllowed', { label, key: k }));
              }
            }
          }
        }
      }
    }

    _errors = errors;
    _warnings = warnings;

    // 显示验证消息
    const msgDiv = $('wf-validation-msg');
    if (msgDiv) {
      let html = '';
      for (const e of errors) html += '<div class="wf-error">❌ ' + _esc(e) + '</div>';
      for (const w of warnings) html += '<div class="wf-warn">⚠️ ' + _esc(w) + '</div>';
      if (errors.length === 0 && warnings.length === 0) {
        html = '<div style="color:var(--green)">✓ ' + t('wfValid') + '</div>';
      }
      msgDiv.innerHTML = html;
    }

    // 禁用/启用保存按钮
    const saveBtn = $('wf-save-btn');
    if (saveBtn) {
      saveBtn.disabled = errors.length > 0;
    }
  }

  // ═══════════════════════════════════════════════════════════════════════
  // Save / Clone / Delete
  // ═══════════════════════════════════════════════════════════════════════

  async function _save() {
    if (_errors.length > 0) {
      App.toast(t('wfFixErrors') + '：' + _errors.join('；'), 'error');
      return;
    }

    const tpl = _editingTemplate;
    if (!tpl) return;

    // 同步头部输入
    tpl.title = $('wf-editor-title').value.trim() || tpl.title;
    tpl.id = $('wf-editor-id').value.trim() || tpl.id;
    tpl.description = $('wf-editor-desc').value.trim() || tpl.description;

    if (!tpl.title) { App.toast(t('wfNeedTitle'), 'warning'); return; }
    if (!tpl.id) { App.toast(t('wfNeedId'), 'warning'); return; }

    const saveBtn = $('wf-save-btn');
    saveBtn.disabled = true;
    saveBtn.textContent = t('wfSaving');

    try {
      const res = await window.api.sendCommand('save_template', { template: tpl, original_id: _originalId });
      if (res.error) {
        if (res.errors && res.errors.length) {
          App.toast(t('wfValidateFail', { errs: res.errors.join('；') }), 'error');
        } else {
          App.toast(t('wfSaveFail') + ': ' + res.error, 'error');
        }
        return;
      }
      if (res.warnings && res.warnings.length) {
        console.warn('[wf-editor] 保存成功但有警告:', res.warnings);
      }
      _dirty = false;
      closeEditor();
      if (_onSaveCallback) _onSaveCallback(tpl.id);
    } catch (e) {
      App.toast(t('wfSaveFail') + ': ' + (e.message || e), 'error');
    } finally {
      // 成功/失败都恢复按钮，避免下次打开仍卡「保存中…」
      saveBtn.disabled = false;
      saveBtn.innerHTML = '<i class="ph ph-check"></i> ' + t('wfSave');
    }
  }

  async function cloneAndEdit(templateId) {
    const tpl = _findTemplateById(templateId);
    if (!tpl) {
      App.toast(t('wfTemplateNotFound') + ': ' + templateId, 'error');
      return;
    }
    const newId = tpl.id + '_v' + ((tpl.version || 1) + 1);
    const newTitle = (tpl.title || templateId) + t('wfSuffixCopy');

    try {
      const res = await window.api.sendCommand('clone_template', {
        source_template_id: templateId,
        new_id: newId,
        new_title: newTitle,
      });
      if (res.error) {
        // 如果克隆失败（如 ID 冲突），直接在编辑器打开
        if (tpl.source === 'builtin') {
          _editingTemplate = _cloneFromBuiltin(tpl);
        } else {
          _editingTemplate = JSON.parse(JSON.stringify(tpl));
          _editingTemplate.id = (await Modal.showInput(t('wfCloneTpl'), t('wfCloneNewId'))) || newId;
          _editingTemplate.title = (await Modal.showInput(t('wfCloneTpl'), t('wfCloneNewName'))) || newTitle;
          _editingTemplate.source = 'custom';
        }
        _errors = []; _warnings = []; _dirty = false;
        _originalId = null;  // 克隆兜底打开的是未保存的新模板，无原 ID
        // 确保步骤列表已加载再渲染
        if (!(await _ensureSteps())) return;
        _render();
        Modal.animatedShow($('workflow-editor-modal'));
        return;
      }
      // 克隆成功，打开新模板编辑
      if (_onSaveCallback) await _onSaveCallback(newId);
      openEditor(newId);
    } catch (e) {
      App.toast(t('wfCloneFail2') + ': ' + (e.message || e), 'error');
    }
  }

  async function deleteTemplate(templateId) {
    if (!(await Modal.showConfirm(t('wfDelTplTitle'), t('wfDelTplConfirm', { id: templateId })))) return;

    try {
      const res = await window.api.sendCommand('delete_template', { template_id: templateId });
      if (res.error) {
        App.toast(t('wfDeleteFail') + ': ' + res.error, 'error');
        return;
      }
      if (_onSaveCallback) await _onSaveCallback(null);
    } catch (e) {
      App.toast(t('wfDeleteFail') + ': ' + (e.message || e), 'error');
    }
  }

  // ═══════════════════════════════════════════════════════════════════════
  // Utils
  // ═══════════════════════════════════════════════════════════════════════

  function _esc(s) {
    if (!s) return '';
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  // ── Public API ──
  return {
    init,
    openEditor,
    closeEditor,
    cloneAndEdit,
    deleteTemplate,
    bindHeaderEvents,
    setOnSave,
  };
})();

// Auto-init on DOM ready (before PipelineUI so it can reference WorkflowEditor)
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => WorkflowEditor.init());
} else {
  WorkflowEditor.init();
}
