/**
 * Dédalo Standalone — Lógica del cliente visual de grafos.
 */

'use strict';

const $ = (id) => document.getElementById(id);

// i18n: ayudantes dependientes del idioma (t/getLang vienen de i18n.js)
const dateLocale = () => (getLang() === 'es' ? 'es-ES' : 'en-US');
const defaultFlowName = () => t('flow.default_name');
const defaultScript = () => t('code.default_comment') + '\nreturn entrada.text()';

const state = {
  graph: { name: '', nodes: [], edges: [], version: 1 },
  meta: { name: '', description: '', schedule: '', enabled: true },
  selectedNodeId: null,
  catalog: { node_types: {}, choices: { secrets: [], llms: [] } },
  templates: [],
  flows: [],
  secrets: [],
  llms: [],
  isDirty: false,
  activeTab: 'flows',
  paletteFilter: 'all',
  paletteCollapsed: { input: false, transform: false, output: false },
  // Transformación y navegación 2D del lienzo (Pan y Zoom)
  view: { k: 1, x: 0, y: 0 },
  isPanning: false,
  isSpacePressed: false,
  panStart: { x: 0, y: 0 },
  panOrigin: { x: 0, y: 0 },
  // Interacción de arrastre y cableado
  dragNode: null,
  dragOffset: { x: 0, y: 0 },
  wiring: null, // { sourceNodeId, port, startX, startY }
  editingScriptNodeId: null,
  historyRuns: [],
  selectedRunId: null,
  runHighlights: {},
  activeInspector: null,
  debug: {
    active: false,
    currentStepIndex: 0,
    stepsOrder: [],
    cachedResults: {},
    running: false,
  },
  lastRunResults: {},
};

// ---------------------------------------------------------------------------
// Inicialización
// ---------------------------------------------------------------------------

function setupLanguageSelector() {
  const sel = $('cfgLang');
  if (!sel) return;
  sel.value = getLang();
  sel.onchange = () => {
    const code = sel.value;
    withUnsavedCheck(() => setLang(code));
  };
}

async function init() {
  await window.i18nReady;
  applyI18n();
  state.graph.name = defaultFlowName();
  state.meta.name = state.graph.name;
  setupLanguageSelector();
  setupTheme();
  setupInspector();
  setupEvents();
  await loadCatalog();
  await loadTemplates();
  await loadFlows();
  await loadSecrets();
  await loadLlms();
  await loadConfig();
  render();
}

function setupTheme() {
  const saved = localStorage.getItem('dedalo_theme') || 'dark';
  document.documentElement.setAttribute('data-theme', saved);
  $('btnTheme').onclick = () => {
    const curr = document.documentElement.getAttribute('data-theme') || 'dark';
    const next = curr === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    localStorage.setItem('dedalo_theme', next);
  };
}

function setInspectorCollapsed(collapsed, save = true) {
  const inspector = $('inspector');
  const btnExpand = $('btnExpandInspector');
  const btnToggle = $('btnToggleInspector');
  if (!inspector) return;

  if (collapsed) {
    inspector.classList.add('collapsed');
    if (btnExpand) btnExpand.hidden = false;
    if (btnToggle) {
      btnToggle.classList.remove('active');
      btnToggle.setAttribute('title', t('inspector.show_title'));
    }
    if (save) localStorage.setItem('dedalo_inspector_collapsed', 'true');
  } else {
    inspector.classList.remove('collapsed');
    if (btnExpand) btnExpand.hidden = true;
    if (btnToggle) {
      btnToggle.classList.add('active');
      btnToggle.setAttribute('title', t('inspector.hide_title'));
    }
    if (save) localStorage.setItem('dedalo_inspector_collapsed', 'false');
  }
}

function toggleInspector() {
  const inspector = $('inspector');
  if (!inspector) return;
  setInspectorCollapsed(!inspector.classList.contains('collapsed'));
}

function setupInspector() {
  const saved = localStorage.getItem('dedalo_inspector_collapsed') === 'true';
  setInspectorCollapsed(saved, false);

  const btnToggle = $('btnToggleInspector');
  if (btnToggle) btnToggle.onclick = toggleInspector;

  const btnCollapse = $('btnCollapseInspector');
  if (btnCollapse) btnCollapse.onclick = () => setInspectorCollapsed(true);

  const btnExpand = $('btnExpandInspector');
  if (btnExpand) btnExpand.onclick = () => setInspectorCollapsed(false);
}

function toast(msg) {
  const s = $('status');
  s.textContent = msg;
  s.classList.add('show');
  clearTimeout(s._timer);
  s._timer = setTimeout(() => s.classList.remove('show'), 2500);
}

function updateDirtyUI() {
  const badge = $('dirtyBadge');
  if (badge) {
    badge.classList.toggle('visible', !!state.isDirty);
  }
  const btnSave = $('btnSave');
  if (btnSave) {
    btnSave.classList.toggle('is-dirty', !!state.isDirty);
  }
  const name = state.graph?.name || defaultFlowName();
  document.title = (state.isDirty ? '● ' : '') + `${name} — Dédalo`;
}

function markDirty() {
  state.isDirty = true;
  updateDirtyUI();
}

function setClean() {
  state.isDirty = false;
  updateDirtyUI();
}

// ---------------------------------------------------------------------------
// Gestión de Cambios sin Guardar
// ---------------------------------------------------------------------------

let unsavedPendingAction = null;

function withUnsavedCheck(action) {
  if (!state.isDirty) {
    action();
    return;
  }
  unsavedPendingAction = action;
  $('unsavedMsg').innerHTML = t('confirm.unsaved_body', {
    name: `<b>${escapeHtml(state.graph.name || t('confirm.unnamed'))}</b>`,
  });
  $('unsavedSheet').hidden = false;
}

function closeUnsavedModal() {
  $('unsavedSheet').hidden = true;
  unsavedPendingAction = null;
  const picker = $('templatePicker');
  if (picker) picker.value = '';
  const langSel = $('cfgLang');
  if (langSel) langSel.value = getLang();
}

function onUnsavedDiscard() {
  setClean();
  $('unsavedSheet').hidden = true;
  const action = unsavedPendingAction;
  unsavedPendingAction = null;
  if (action) action();
}

async function onUnsavedSave() {
  const success = await saveFlow();
  if (success !== false) {
    $('unsavedSheet').hidden = true;
    const action = unsavedPendingAction;
    unsavedPendingAction = null;
    if (action) action();
  }
}

// ---------------------------------------------------------------------------
// Navegación 2D: Zoom, Pan y Centrado
// ---------------------------------------------------------------------------

function screenToWorld(clientX, clientY) {
  const rect = $('canvasWrap').getBoundingClientRect();
  return {
    x: (clientX - rect.left - state.view.x) / state.view.k,
    y: (clientY - rect.top - state.view.y) / state.view.k,
  };
}

function updateTransform() {
  const content = $('canvasContent');
  if (content) {
    content.style.transform = `translate(${state.view.x}px, ${state.view.y}px) scale(${state.view.k})`;
  }
  const wrap = $('canvasWrap');
  if (wrap) {
    wrap.style.backgroundPosition = `${state.view.x}px ${state.view.y}px`;
    wrap.style.backgroundSize = `${20 * state.view.k}px ${20 * state.view.k}px`;
  }
  const zoomLbl = $('zoomLevel');
  if (zoomLbl) {
    zoomLbl.textContent = `${Math.round(state.view.k * 100)}%`;
  }
}

function middle() {
  const wrap = $('canvasWrap');
  return [wrap.clientWidth / 2, wrap.clientHeight / 2];
}

function zoomAt(px, py, factor) {
  const minK = 0.2;
  const maxK = 2.5;
  const nextK = Math.min(maxK, Math.max(minK, state.view.k * factor));
  const realFactor = nextK / state.view.k;
  state.view.x = px - (px - state.view.x) * realFactor;
  state.view.y = py - (py - state.view.y) * realFactor;
  state.view.k = nextK;
  updateTransform();
}

function zoomIn() {
  zoomAt(...middle(), 1.25);
}

function zoomOut() {
  zoomAt(...middle(), 1 / 1.25);
}

function resetZoom() {
  const [cx, cy] = middle();
  const worldCenterX = (cx - state.view.x) / state.view.k;
  const worldCenterY = (cy - state.view.y) / state.view.k;
  state.view.k = 1.0;
  state.view.x = cx - worldCenterX * state.view.k;
  state.view.y = cy - worldCenterY * state.view.k;
  updateTransform();
  toast(t('toast.zoom_reset'));
}

function centerFlow() {
  if (!state.graph.nodes.length) {
    state.view.x = 0;
    state.view.y = 0;
    state.view.k = 1.0;
    updateTransform();
    toast(t('toast.canvas_centered'));
    return;
  }

  const wrap = $('canvasWrap');
  const boxW = wrap.clientWidth;
  const boxH = wrap.clientHeight;

  const xs = state.graph.nodes.map((n) => n.x);
  const ys = state.graph.nodes.map((n) => n.y);
  const minX = Math.min(...xs);
  const maxX = Math.max(...xs) + 200;
  const minY = Math.min(...ys);
  const maxY = Math.max(...ys) + 120;

  const bboxW = Math.max(200, maxX - minX);
  const bboxH = Math.max(120, maxY - minY);

  const pad = 60;
  const fitK = Math.min(1.5, Math.max(0.25, Math.min((boxW - pad * 2) / bboxW, (boxH - pad * 2) / bboxH)));

  state.view.k = fitK;
  const centerX = (minX + maxX) / 2;
  const centerY = (minY + maxY) / 2;
  state.view.x = boxW / 2 - centerX * state.view.k;
  state.view.y = boxH / 2 - centerY * state.view.k;
  updateTransform();
  toast(t('toast.flow_centered'));
}

// ---------------------------------------------------------------------------
// Navegación de Pestañas en Sidebar
// ---------------------------------------------------------------------------

function setupEvents() {
  document.querySelectorAll('.nav-tab').forEach((tab) => {
    tab.onclick = () => {
      document.querySelectorAll('.nav-tab').forEach((t) => t.classList.remove('active'));
      document.querySelectorAll('.tab-pane').forEach((p) => p.classList.remove('active'));
      tab.classList.add('active');
      const targetId = 'pane' + tab.dataset.tab.charAt(0).toUpperCase() + tab.dataset.tab.slice(1);
      $(targetId).classList.add('active');
      state.activeTab = tab.dataset.tab;
      if (tab.dataset.tab === 'history') {
        loadHistory();
      }
    };
  });

  $('flowName').oninput = (e) => {
    state.graph.name = e.target.value.trim().toLowerCase().replace(/[^a-z0-9_-]/g, '-');
    state.meta.name = state.graph.name;
    markDirty();
  };

  $('btnNew').onclick = () => withUnsavedCheck(() => resetGraph(defaultFlowName()));
  $('btnSidebarNewFlow').onclick = () => withUnsavedCheck(() => resetGraph(defaultFlowName()));
  if ($('btnImportZip')) $('btnImportZip').onclick = () => $('importZipInput').click();
  if ($('importZipInput')) $('importZipInput').onchange = (e) => {
    if (e.target.files && e.target.files.length) {
      handleImportZip(e.target.files[0]);
      e.target.value = '';
    }
  };
  if ($('btnExportAllZip')) $('btnExportAllZip').onclick = exportAllFlowsZip;
  $('btnSchedule').onclick = () => openScheduleModal(state.graph.name);
  $('btnCompile').onclick = compileGraph;
  $('btnRun').onclick = runPreview;
  if ($('btnDebugFlow')) $('btnDebugFlow').onclick = toggleDebugFlow;
  if ($('btnDebugReset')) $('btnDebugReset').onclick = resetDebugFlow;
  if ($('btnDebugNext')) $('btnDebugNext').onclick = stepDebugNext;
  if ($('btnDebugContinue')) $('btnDebugContinue').onclick = continueDebugFlow;
  if ($('btnDebugStop')) $('btnDebugStop').onclick = stopDebugFlow;

  if ($('btnCloseDataViewer')) $('btnCloseDataViewer').onclick = () => $('dataViewerModal').hidden = true;
  if ($('btnCloseDataViewerFoot')) $('btnCloseDataViewerFoot').onclick = () => $('dataViewerModal').hidden = true;
  if ($('btnCopyDataViewer')) $('btnCopyDataViewer').onclick = copyDataViewerContent;
  if ($('btnDataViewFormatted')) $('btnDataViewFormatted').onclick = () => switchDataViewerMode('formatted');
  if ($('btnDataViewRaw')) $('btnDataViewRaw').onclick = () => switchDataViewerMode('raw');
  if ($('dataViewerSearch')) $('dataViewerSearch').oninput = filterDataViewerContent;

  $('btnSave').onclick = saveFlow;
  $('templatePicker').onchange = (e) => {
    const val = e.target.value;
    if (val) {
      withUnsavedCheck(() => loadTemplate(val));
    }
  };

  $('sheetClose').onclick = () => $('sheet').hidden = true;
  if ($('codeClose')) $('codeClose').onclick = () => $('codeSheet').hidden = true;
  $('codeCancel').onclick = () => $('codeSheet').hidden = true;
  $('codeSave').onclick = saveScriptCode;

  $('schedClose').onclick = () => $('scheduleSheet').hidden = true;
  $('btnSchedSave').onclick = saveScheduleModal;
  $('btnSchedRunNow').onclick = runScheduleModalNow;
  $('schedPreset').onchange = onSchedPresetChange;

  if ($('btnRefreshHistory')) $('btnRefreshHistory').onclick = () => loadHistory();
  if ($('historyOnlyCurrent')) $('historyOnlyCurrent').onchange = () => loadHistory();
  if ($('btnHistoryPurge')) $('btnHistoryPurge').onclick = purgeHistory;
  if ($('histClose')) $('histClose').onclick = () => $('historySheet').hidden = true;
  if ($('btnHistCloseSheet')) $('btnHistCloseSheet').onclick = () => $('historySheet').hidden = true;
  if ($('btnHistHighlightCanvas')) $('btnHistHighlightCanvas').onclick = () => {
    if (currentRunDetail && currentRunDetail.steps) {
      highlightRunOnCanvas(currentRunDetail.steps);
      toast(t('toast.nodes_highlighted'));
    }
  };
  if ($('btnHistClearHighlight')) $('btnHistClearHighlight').onclick = () => {
    clearRunHighlights();
    toast(t('toast.highlight_cleared'));
  };

  $('unsavedClose').onclick = () => closeUnsavedModal();
  $('btnUnsavedCancel').onclick = () => closeUnsavedModal();
  $('btnUnsavedDiscard').onclick = onUnsavedDiscard;
  $('btnUnsavedSave').onclick = onUnsavedSave;

  $('btnSaveSecret').onclick = addSecret;
  if ($('btnSaveLlmProfile')) $('btnSaveLlmProfile').onclick = addLlmProfile;
  $('btnSaveConfig').onclick = saveConfig;

  if ($('paletteSearch')) {
    $('paletteSearch').addEventListener('input', () => renderPalette());
  }

  if ($('paletteChips')) {
    $('paletteChips').onclick = (e) => {
      const chipBtn = e.target.closest('.chip');
      if (!chipBtn) return;
      state.paletteFilter = chipBtn.dataset.chip || 'all';
      renderPalette();
    };
  }

  // Eventos globales de arrastre y clic en el canvas
  const canvas = $('canvasWrap');
  canvas.onmousedown = onCanvasMouseDown;
  window.addEventListener('mousemove', onCanvasMouseMove);
  window.addEventListener('mouseup', onCanvasMouseUp);

  // Arrastre de nodos desde la paleta hacia el lienzo (Drag & Drop)
  canvas.addEventListener('dragover', (e) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
    canvas.classList.add('drag-over');
  });
  canvas.addEventListener('dragleave', (e) => {
    if (e.relatedTarget && canvas.contains(e.relatedTarget)) return;
    canvas.classList.remove('drag-over');
  });
  canvas.addEventListener('drop', (e) => {
    e.preventDefault();
    canvas.classList.remove('drag-over');
    const typeKey = e.dataTransfer.getData('application/x-dedalo-node') || e.dataTransfer.getData('text/plain');
    if (!typeKey || !state.catalog.node_types || !state.catalog.node_types[typeKey]) return;
    const world = screenToWorld(e.clientX, e.clientY);
    addNode(typeKey, Math.round(world.x - 100), Math.round(world.y - 38));
  });

  // Controles de Zoom y Centrado
  if ($('btnZoomIn')) $('btnZoomIn').onclick = zoomIn;
  if ($('btnZoomOut')) $('btnZoomOut').onclick = zoomOut;
  if ($('btnZoomReset')) $('btnZoomReset').onclick = resetZoom;
  if ($('btnZoomCenter')) $('btnZoomCenter').onclick = centerFlow;

  canvas.addEventListener('wheel', (e) => {
    e.preventDefault();
    const rect = canvas.getBoundingClientRect();
    const px = e.clientX - rect.left;
    const py = e.clientY - rect.top;
    zoomAt(px, py, Math.exp(-e.deltaY * 0.0015));
  }, { passive: false });

  // Atajos de teclado globales y control de lienzo
  window.addEventListener('keydown', onKeyDown);
  window.addEventListener('keyup', onKeyUp);
  window.addEventListener('blur', () => {
    if (state.isSpacePressed) {
      state.isSpacePressed = false;
      document.body.classList.remove('space-active');
    }
  });
}

// ---------------------------------------------------------------------------
// Atajos de Teclado y Ergonomía
// ---------------------------------------------------------------------------

function isEditingText(target) {
  if (!target) return false;
  const tag = target.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return true;
  if (target.isContentEditable) return true;
  if (target.closest && (target.closest('.CodeMirror') || target.closest('.code-editor-wrap'))) return true;
  return false;
}

function onKeyDown(e) {
  const isCmdOrCtrl = e.metaKey || e.ctrlKey;
  const key = e.key;

  // 1. Atajo Guardar: Cmd+S / Ctrl+S (funciona siempre, incluso dentro de campos de formulario o editor)
  if (isCmdOrCtrl && key.toLowerCase() === 's') {
    e.preventDefault();
    e.stopPropagation();
    saveFlow();
    return;
  }

  // 2. Escape: cerrar modales, cancelar cableado o deseleccionar nodo
  if (key === 'Escape') {
    const unsaved = $('unsavedSheet');
    if (unsaved && !unsaved.hidden) { closeUnsavedModal(); return; }
    const dataViewer = $('dataViewerModal');
    if (dataViewer && !dataViewer.hidden) { dataViewer.hidden = true; return; }
    const hist = $('historySheet');
    if (hist && !hist.hidden) { hist.hidden = true; return; }
    const code = $('codeSheet');
    if (code && !code.hidden) { code.hidden = true; return; }
    const sched = $('scheduleSheet');
    if (sched && !sched.hidden) { sched.hidden = true; return; }
    const sheet = $('sheet');
    if (sheet && !sheet.hidden) { sheet.hidden = true; return; }

    // Cancelar cableado en curso
    if (state.wiring) {
      state.wiring = null;
      document.body.classList.remove('is-wiring');
      renderWires();
      return;
    }

    // Deseleccionar nodo activo
    if (state.selectedNodeId !== null) {
      state.selectedNodeId = null;
      state.activeInspector = 'flow';
      document.querySelectorAll('.node').forEach((n) => n.classList.remove('selected'));
      renderInspector();
      return;
    }
    return;
  }

  // Si el foco está dentro de un campo de texto o editor, no procesar atajos del lienzo
  if (isEditingText(e.target)) return;

  // Si hay algún diálogo o modal abierto, no procesar atajos del lienzo
  const openSheet = document.querySelector('.sheet:not([hidden])');
  if (openSheet) return;

  // 3. Suprimir / Backspace: Eliminar nodo seleccionado
  if ((key === 'Delete' || key === 'Backspace') && !isCmdOrCtrl) {
    if (state.selectedNodeId) {
      e.preventDefault();
      removeNode(state.selectedNodeId);
      return;
    }
  }

  // 4. Duplicar nodo: Cmd+D / Ctrl+D
  if (isCmdOrCtrl && key.toLowerCase() === 'd') {
    if (state.selectedNodeId) {
      e.preventDefault();
      duplicateNode(state.selectedNodeId);
      return;
    }
  }

  // 5. Barra espaciadora: Activar modo pan en el lienzo
  if (e.code === 'Space' && !e.repeat) {
    e.preventDefault();
    state.isSpacePressed = true;
    document.body.classList.add('space-active');
  }
}

function onKeyUp(e) {
  if (e.code === 'Space') {
    state.isSpacePressed = false;
    document.body.classList.remove('space-active');
  }
}

async function loadCatalog() {
  try {
    const res = await fetch('/api/catalog');
    state.catalog = await res.json();
    renderPalette();
  } catch (e) {
    toast(t('error.load_catalog'));
  }
}

async function loadTemplates() {
  try {
    const res = await fetch('/api/templates');
    state.templates = await res.json();
    const picker = $('templatePicker');
    picker.innerHTML = `<option value="">${t('toolbar.templates_load')}</option>`;
    state.templates.forEach((tpl) => {
      const opt = document.createElement('option');
      opt.value = tpl.name;
      opt.textContent = tpl.title;
      picker.appendChild(opt);
    });
  } catch (e) {}
}

async function loadFlows() {
  try {
    const res = await fetch('/api/flows');
    state.flows = await res.json();
    renderFlowsList();
  } catch (e) {
    toast(t('error.load_flows'));
  }
}

async function loadSecrets() {
  try {
    const res = await fetch('/api/secrets');
    state.secrets = await res.json();
    renderSecretsList();
    populateProfileSecretDropdown();
  } catch (e) {}
}

async function loadLlms() {
  try {
    const res = await fetch('/api/llms');
    state.llms = await res.json();
    renderLlmsList();
  } catch (e) {}
}

function populateProfileSecretDropdown() {
  const sel = $('newProfileSecret');
  if (!sel) return;
  const currentVal = sel.value;
  sel.innerHTML = `<option value="">${t('settings.no_secret_local')}</option>`;
  (state.secrets || []).forEach((s) => {
    const opt = document.createElement('option');
    opt.value = s;
    opt.textContent = s;
    if (s === currentVal) opt.selected = true;
    sel.appendChild(opt);
  });
}

async function loadConfig() {
  try {
    const res = await fetch('/api/config');
    const cfg = await res.json();
    $('cfgLlmUrl').value = cfg.llm_url || '';
    $('cfgLlmModel').value = cfg.llm_model || '';
    if (cfg.has_llm_key) {
      $('cfgLlmKey').placeholder = t('settings.key_saved');
    }
  } catch (e) {}
}

async function saveConfig() {
  const payload = {
    llm_url: $('cfgLlmUrl').value.trim(),
    llm_model: $('cfgLlmModel').value.trim(),
    llm_key: $('cfgLlmKey').value.trim(),
  };
  try {
    const res = await fetch('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (res.ok) toast(t('toast.config_saved'));
  } catch (e) {
    toast(t('error.save_config'));
  }
}

// ---------------------------------------------------------------------------
// Renderizado de la Barra Lateral: Flujos, Nodos, Secretos
// ---------------------------------------------------------------------------

function renderFlowsList() {
  const list = $('flowsList');
  list.innerHTML = '';
  if (!state.flows.length) {
    list.innerHTML = `<p class="hint">${t('flows.empty')}</p>`;
    return;
  }

  state.flows.forEach((fl) => {
    const card = document.createElement('div');
    card.className = 'flow-card' + (fl.id === state.graph.name ? ' active' : '');
    card.onclick = () => {
      document.querySelectorAll('.flow-card').forEach((c) => c.classList.remove('active'));
      card.classList.add('active');
      document.querySelectorAll('.node').forEach((n) => n.classList.remove('selected'));
      state.selectedNodeId = null;
      state.activeInspector = 'flow';
      if (fl.id === state.graph.name) {
        renderInspector();
        return;
      }
      withUnsavedCheck(() => openFlow(fl.id));
    };

    const isEnabled = fl.enabled !== false;
    const badgeClass = isEnabled ? 'badge-active' : 'badge-paused';
    const badgeText = isEnabled ? t('flows.active') : t('flows.paused');

    const schedText = fl.schedule ? `⏰ ${fl.schedule}` : t('flows.manual');
    const nextText = fl.next_run ? `<span class="hint-inline">(${fl.next_run.slice(11, 16)})</span>` : '';

    const info = document.createElement('div');
    info.className = 'flow-info';
    info.innerHTML = `
      <span class="flow-title">${escapeHtml(fl.name || fl.id)}</span>
      <div class="flow-meta-line">
        <span class="${badgeClass}" title="${t('flows.toggle_title')}">${badgeText}</span>
        <span class="flow-meta">${escapeHtml(schedText)} ${nextText}</span>
      </div>
    `;

    const badgeEl = info.querySelector('.' + badgeClass);
    badgeEl.onmousedown = (e) => e.stopPropagation();
    badgeEl.onclick = async (e) => {
      e.stopPropagation();
      await toggleFlow(fl.id);
    };

    const actions = document.createElement('div');
    actions.className = 'flow-actions';

    const btnRunNow = document.createElement('button');
    btnRunNow.className = 'btn sm icon-btn';
    btnRunNow.title = t('flows.run_now_title');
    btnRunNow.innerHTML = '<svg class="ic" width="12" height="12"><use href="#i-play"/></svg>';
    btnRunNow.onmousedown = (e) => e.stopPropagation();
    btnRunNow.onclick = async (e) => {
      e.stopPropagation();
      await runFlowNow(fl.id);
    };

    const btnSched = document.createElement('button');
    btnSched.className = 'btn sm icon-btn';
    btnSched.title = t('flows.schedule_title');
    btnSched.innerHTML = '<svg class="ic" width="12" height="12"><use href="#i-clock"/></svg>';
    btnSched.onmousedown = (e) => e.stopPropagation();
    btnSched.onclick = (e) => {
      e.stopPropagation();
      openScheduleModal(fl.id);
    };

    const btnDel = document.createElement('button');
    btnDel.className = 'btn sm icon-btn';
    btnDel.title = t('flows.delete_title');
    btnDel.innerHTML = '<svg class="ic" width="12" height="12"><use href="#i-trash"/></svg>';
    btnDel.onmousedown = (e) => e.stopPropagation();
    btnDel.onclick = (e) => {
      e.stopPropagation();
      e.preventDefault();
      deleteFlow(fl.id);
    };

    actions.appendChild(btnRunNow);
    actions.appendChild(btnSched);
    actions.appendChild(btnDel);
    card.appendChild(info);
    card.appendChild(actions);
    list.appendChild(card);
  });
}

let currentSchedFlowId = null;

async function openScheduleModal(flowId) {
  currentSchedFlowId = flowId || state.graph.name || defaultFlowName();
  $('schedFlowTitle').textContent = currentSchedFlowId;

  const fl = state.flows.find((f) => f.id === currentSchedFlowId) || state.meta || {};
  $('schedEnabled').checked = fl.enabled !== false;
  $('schedExpr').value = fl.schedule || '';

  const expr = fl.schedule || '';
  const presetSelect = $('schedPreset');
  let matched = false;
  for (let opt of presetSelect.options) {
    if (opt.value === expr || (expr === '' && opt.value === 'manual')) {
      presetSelect.value = opt.value;
      matched = true;
      break;
    }
  }
  if (!matched) presetSelect.value = expr ? 'custom' : 'manual';

  $('schedStatusVal').textContent = fl.enabled !== false ? (fl.schedule ? t('schedule.state_scheduled') : t('flows.manual')) : t('flows.paused');
  $('schedNextVal').textContent = fl.next_run || (fl.schedule ? t('schedule.next_pending') : t('schedule.next_none'));
  const lastRun = fl.last_run ? `${new Date(fl.last_run.last_run * 1000).toLocaleTimeString()} (${fl.last_run.status})` : t('schedule.never');
  $('schedLastVal').textContent = lastRun;

  $('scheduleSheet').hidden = false;
}

function onSchedPresetChange() {
  const val = $('schedPreset').value;
  if (val === 'manual') {
    $('schedExpr').value = '';
  } else if (val !== 'custom') {
    $('schedExpr').value = val;
  }
}

async function saveScheduleModal() {
  if (!currentSchedFlowId) return;
  const schedule = $('schedExpr').value.trim();
  const enabled = $('schedEnabled').checked;

  try {
    const res = await fetch('/api/flow/schedule', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: currentSchedFlowId, schedule, enabled }),
    });
    if (!res.ok) throw new Error();
    toast(t('toast.schedule_saved', { id: currentSchedFlowId }));
    $('scheduleSheet').hidden = true;
    await loadFlows();
    if (state.graph.name === currentSchedFlowId) {
      state.meta.schedule = schedule;
      state.meta.enabled = enabled;
    }
  } catch (e) {
    toast(t('error.save_schedule'));
  }
}

async function toggleFlow(flowId) {
  try {
    const res = await fetch(`/api/flow/toggle?id=${encodeURIComponent(flowId)}`, { method: 'POST' });
    if (res.ok) {
      await loadFlows();
      toast(t('toast.flow_state_updated', { id: flowId }));
    }
  } catch (e) {
    toast(t('error.toggle_flow'));
  }
}

async function runFlowNow(flowId) {
  toast(t('toast.flow_running', { id: flowId }));
  try {
    const res = await fetch(`/api/flow/run_now?id=${encodeURIComponent(flowId)}`, { method: 'POST' });
    const data = await res.json();
    if (res.ok) {
      toast(t('toast.flow_done', { id: flowId, duration: data.duration }));
      await loadFlows();
      if (flowId === state.graph.name && data.result) {
        highlightRunOnCanvas(data.result);
      }
      if (state.activeTab === 'history') {
        loadHistory();
      }
    } else {
      toast(t('toast.flow_failed', { id: flowId, detail: data.detail || t('error.failure') }));
    }
  } catch (e) {
    toast(t('error.trigger_connection'));
  }
}

async function runScheduleModalNow() {
  if (!currentSchedFlowId) return;
  $('schedStatusVal').textContent = t('schedule.running');
  await runFlowNow(currentSchedFlowId);
  const fl = state.flows.find((f) => f.id === currentSchedFlowId);
  if (fl) {
    $('schedStatusVal').textContent = fl.last_run ? fl.last_run.status : t('common.ready');
    $('schedLastVal').textContent = fl.last_run ? `${fl.last_run.duration}s` : '-';
  }
}

const NODE_CHIP_TAGS = {
  net: ['http', 'web', 'rss', 'imap', 'mqtt', 'ping', 'dns', 'ssl', 'webhook', 'weather', 'ntfy', 'telegram', 'discord', 'slack', 'email'],
  data: ['html', 'regex', 'template', 'array', 'filter', 'diff', 'dedup', 'summarize', 'clean_email', 'datetime', 'sqlite', 'file', 'json'],
  ai: ['coder', 'agent', 'summarize'],
  git: ['git'],
  sys: ['pi_health', 'disk_free', 'command', 'healthcheck', 'script', 'threshold', 'router'],
};

function nodeMatchesChip(typeKey, spec, chip) {
  if (!chip || chip === 'all') return true;
  const sub = typeKey.split('.')[1] || '';
  const hay = `${typeKey} ${spec.label || ''} ${spec.output || ''}`.toLowerCase();
  const tags = NODE_CHIP_TAGS[chip] || [];
  return tags.some((t) => hay.includes(t) || sub.includes(t));
}

function renderPalette() {
  const p = $('palette');
  p.innerHTML = '';
  const categories = { input: t('palette.cat_input'), transform: t('palette.cat_transform'), output: t('palette.cat_output') };
  const query = ($('paletteSearch') && $('paletteSearch').value || '').trim().toLowerCase();
  const activeChip = state.paletteFilter || 'all';
  let totalMatched = 0;

  // Sincronizar estado visual de los chips
  document.querySelectorAll('#paletteChips .chip').forEach((c) => {
    c.classList.toggle('active', c.dataset.chip === activeChip);
  });

  Object.entries(categories).forEach(([catKey, catTitle]) => {
    const matchingSpecs = [];

    Object.entries(state.catalog.node_types || {}).forEach(([typeKey, spec]) => {
      if (spec.category !== catKey) return;
      if (!nodeMatchesChip(typeKey, spec, activeChip)) return;
      if (query) {
        const sub = typeKey.split('.')[1] || '';
        const label = (spec.label || '').toLowerCase();
        const out = (spec.output || '').toLowerCase();
        const matches = label.includes(query) ||
                        typeKey.toLowerCase().includes(query) ||
                        sub.toLowerCase().includes(query) ||
                        out.includes(query);
        if (!matches) return;
      }
      matchingSpecs.push({ typeKey, spec });
    });

    totalMatched += matchingSpecs.length;

    if (matchingSpecs.length === 0 && (query || activeChip !== 'all')) {
      return;
    }

    const isCollapsed = !query && activeChip === 'all' && !!state.paletteCollapsed[catKey];

    const block = document.createElement('div');
    block.className = `category-block cat-${catKey}${isCollapsed ? ' collapsed' : ''}`;

    const head = document.createElement('div');
    head.className = 'category-head';
    head.title = isCollapsed ? t('palette.expand') : t('palette.collapse');
    head.innerHTML = `
      <div class="category-head-title">
        <svg class="category-chevron ic" width="12" height="12"><use href="#i-chevron-down"/></svg>
        <span>${catTitle}</span>
      </div>
      <span class="category-badge-count">${matchingSpecs.length}</span>
    `;
    head.onclick = () => {
      state.paletteCollapsed[catKey] = !state.paletteCollapsed[catKey];
      renderPalette();
    };
    block.appendChild(head);

    const content = document.createElement('div');
    content.className = 'category-content';

    matchingSpecs.forEach(({ typeKey, spec }) => {
      const pill = document.createElement('div');
      pill.className = `node-pill cat-${catKey}`;
      pill.draggable = true;
      pill.innerHTML = `<span>${escapeHtml(spec.label)}</span><span class="badge">${typeKey.split('.')[1]}</span>`;
      pill.onclick = () => addNode(typeKey);
      pill.ondragstart = (e) => {
        e.dataTransfer.setData('text/plain', typeKey);
        e.dataTransfer.setData('application/x-dedalo-node', typeKey);
        e.dataTransfer.effectAllowed = 'copy';
        pill.classList.add('dragging-pill');
      };
      pill.ondragend = () => {
        pill.classList.remove('dragging-pill');
      };
      content.appendChild(pill);
    });

    block.appendChild(content);
    p.appendChild(block);
  });

  const countHint = $('nodesCountHint');
  if (countHint) {
    countHint.textContent = t(totalMatched === 1 ? 'palette.count_one' : 'palette.count_many', { n: totalMatched });
  }

  if (totalMatched === 0) {
    p.innerHTML = `<p class="hint" style="text-align: center; padding: 1.5rem 0.5rem;">${t('palette.no_match', { query: escapeHtml(query || activeChip) })}</p>`;
  }
}

function renderSecretsList() {
  const list = $('secretsList');
  list.innerHTML = '';
  if (!state.secrets.length) {
    list.innerHTML = `<p class="hint">${t('secrets.empty')}</p>`;
    return;
  }
  state.secrets.forEach((name) => {
    const item = document.createElement('div');
    item.className = 'secret-item';
    item.innerHTML = `<span>🔑 ${escapeHtml(name)}</span>`;
    const btnDel = document.createElement('button');
    btnDel.className = 'btn sm icon-btn';
    btnDel.innerHTML = '<svg class="ic" width="12" height="12"><use href="#i-trash"/></svg>';
    btnDel.onclick = () => removeSecret(name);
    item.appendChild(btnDel);
    list.appendChild(item);
  });
}

async function addSecret() {
  const name = $('newSecretName').value.trim().toLowerCase().replace(/[^a-z0-9_-]/g, '-');
  const value = $('newSecretValue').value.trim();
  if (!name || !value) {
    toast(t('toast.name_value_required'));
    return;
  }
  try {
    const res = await fetch('/api/secrets', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, value }),
    });
    if (res.ok) {
      $('newSecretName').value = '';
      $('newSecretValue').value = '';
      toast(t('toast.secret_saved', { name }));
      await loadSecrets();
      await loadCatalog();
      renderInspector();
    }
  } catch (e) {
    toast(t('error.save_secret'));
  }
}

async function removeSecret(name) {
  if (!confirm(t('confirm.delete_secret', { name }))) return;
  try {
    await fetch(`/api/secrets?name=${encodeURIComponent(name)}`, { method: 'DELETE' });
    toast(t('toast.secret_deleted', { name }));
    await loadSecrets();
    await loadCatalog();
    renderInspector();
  } catch (e) {
    toast(t('error.delete_secret'));
  }
}

// ---------------------------------------------------------------------------
// Gestión de Endpoints LLM
// ---------------------------------------------------------------------------

function renderLlmsList() {
  const list = $('llmsList');
  if (!list) return;
  list.innerHTML = '';
  if (!state.llms || !state.llms.length) {
    list.innerHTML = `<p class="hint">${t('settings.no_endpoints')}</p>`;
    return;
  }
  state.llms.forEach((item) => {
    const el = document.createElement('div');
    el.className = 'llm-item';

    const info = document.createElement('div');
    info.className = 'llm-item-info';

    const title = document.createElement('div');
    title.className = 'llm-item-title';
    title.innerHTML = `<span>${escapeHtml(item.name)}</span><span class="llm-item-badge">${escapeHtml(item.provider || 'custom')}</span>`;

    const details = document.createElement('div');
    details.className = 'llm-item-details';
    const secLabel = item.secret ? ` [key: ${item.secret}]` : '';
    const ctxLabel = item.max_context ? ` [ctx: ${item.max_context}]` : '';
    const tempLabel = item.temperature !== undefined ? ` [T: ${item.temperature}]` : '';
    details.textContent = `${item.model || t('settings.model_fallback')} @ ${item.url || ''}${secLabel}${ctxLabel}${tempLabel}`;

    info.appendChild(title);
    info.appendChild(details);

    const btnDel = document.createElement('button');
    btnDel.className = 'btn sm icon-btn';
    btnDel.title = t('settings.delete_endpoint_title');
    btnDel.innerHTML = '<svg class="ic" width="12" height="12"><use href="#i-trash"/></svg>';
    btnDel.onclick = () => removeLlmProfile(item.name);

    el.appendChild(info);
    el.appendChild(btnDel);
    list.appendChild(el);
  });
}

async function addLlmProfile() {
  const nameInput = $('newProfileName');
  if (!nameInput) return;
  const name = nameInput.value.trim().toLowerCase().replace(/[^a-z0-9_-]/g, '-');
  const provider = $('newProfileProvider').value.trim();
  const model = $('newProfileModel').value.trim() || 'llama3';
  const url = $('newProfileUrl').value.trim() || 'http://localhost:11434/v1';
  const secret = $('newProfileSecret').value.trim();
  const maxContext = parseInt($('newProfileContext').value.trim(), 10) || 0;
  const rawTemp = $('newProfileTemp').value.trim();
  const temperature = rawTemp !== '' ? parseFloat(rawTemp) : 0.7;

  if (!name) {
    toast(t('toast.endpoint_name_required'));
    return;
  }

  const payload = {
    name,
    provider,
    url,
    model,
    secret,
    max_context: maxContext,
    temperature,
  };

  try {
    const res = await fetch('/api/llms', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (res.ok) {
      $('newProfileName').value = '';
      $('newProfileModel').value = '';
      $('newProfileUrl').value = '';
      $('newProfileContext').value = '';
      $('newProfileTemp').value = '';
      toast(t('toast.endpoint_saved', { name }));
      await loadLlms();
      await loadCatalog();
      renderInspector();
    } else {
      const err = await res.json();
      toast(err.detail || t('error.save_endpoint'));
    }
  } catch (e) {
    toast(t('error.save_endpoint'));
  }
}

async function removeLlmProfile(name) {
  if (!confirm(t('confirm.delete_endpoint', { name }))) return;
  try {
    const res = await fetch(`/api/llms/${encodeURIComponent(name)}`, { method: 'DELETE' });
    if (res.ok) {
      toast(t('toast.endpoint_deleted', { name }));
      await loadLlms();
      await loadCatalog();
      renderInspector();
    }
  } catch (e) {
    toast(t('error.delete_endpoint'));
  }
}

// ---------------------------------------------------------------------------
// Historial de Ejecuciones y Resultados Paso a Paso
// ---------------------------------------------------------------------------

let currentRunDetail = null;

async function loadHistory() {
  const list = $('historyList');
  if (!list) return;
  list.innerHTML = `<p class="hint">${t('history.loading')}</p>`;

  const onlyCurrent = $('historyOnlyCurrent') ? $('historyOnlyCurrent').checked : true;
  const flowId = onlyCurrent && state.graph && state.graph.name ? state.graph.name : '';

  try {
    const url = flowId ? `/api/history?flow_id=${encodeURIComponent(flowId)}&limit=50` : '/api/history?limit=50';
    const res = await fetch(url);
    if (!res.ok) throw new Error();
    const runs = await res.json();
    state.historyRuns = runs;
    renderHistory(runs);
  } catch (e) {
    list.innerHTML = `<p class="hint" style="color:var(--port-err);">${t('error.load_history')}</p>`;
  }
}

function renderHistory(runs) {
  const list = $('historyList');
  if (!list) return;
  list.innerHTML = '';

  if (!runs || runs.length === 0) {
    list.innerHTML = `<p class="hint">${t('history.empty')}</p>`;
    return;
  }

  runs.forEach((r) => {
    const card = document.createElement('div');
    card.className = `history-card ${state.selectedRunId === r.id ? 'active' : ''}`;
    card.dataset.runId = r.id;

    const dt = new Date(r.started_at * 1000);
    const dateStr = dt.toLocaleDateString(dateLocale(), { day: '2-digit', month: '2-digit' }) + ' ' +
                    dt.toLocaleTimeString(dateLocale(), { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    const durStr = r.duration != null ? `${r.duration}s` : '...';
    const trigStr = r.trigger_type === 'schedule' ? t('history.trigger_cron') : r.trigger_type === 'preview' ? t('history.trigger_test') : t('history.trigger_manual');
    const statusBadge = r.status === 'ok' ? '<span class="hist-badge ok">OK</span>'
                      : r.status === 'running' ? '<span class="hist-badge running">RUN</span>'
                      : '<span class="hist-badge error">ERR</span>';

    card.innerHTML = `
      <div class="hist-header">
        <div class="hist-title" title="${escapeHtml(r.flow_name || r.flow_id)}">
          <span class="hist-trigger">${trigStr}</span>
          <span>${escapeHtml(r.flow_name || r.flow_id)}</span>
        </div>
        ${statusBadge}
      </div>
      <div class="hist-meta">
        <span>${dateStr}</span>
        <div style="display: flex; gap: 8px;">
          <span>${durStr}</span>
          <span style="color: var(--primary);">${t('history.steps_count', { n: r.step_count || 0 })}</span>
        </div>
      </div>
    `;

    card.onclick = () => {
      state.selectedRunId = r.id;
      document.querySelectorAll('.history-card').forEach((c) => c.classList.remove('active'));
      card.classList.add('active');
      openRunDetail(r.id);
    };

    list.appendChild(card);
  });
}

async function openRunDetail(runId) {
  try {
    const res = await fetch(`/api/history/run?id=${encodeURIComponent(runId)}`);
    if (!res.ok) {
      toast(t('toast.run_not_found'));
      return;
    }
    const run = await res.json();
    currentRunDetail = run;

    $('histRunId').textContent = run.id;
    const dt = new Date(run.started_at * 1000);
    const dateStr = dt.toLocaleString(dateLocale());
    const trigStr = run.trigger_type === 'schedule' ? t('history.trigger_long_cron')
                  : run.trigger_type === 'preview' ? t('history.trigger_long_test')
                  : t('history.trigger_long_manual');

    const metaBar = $('histMetaBar');
    const statusColor = run.status === 'ok' ? 'var(--port-ok)' : 'var(--port-err)';
    metaBar.innerHTML = `
      <div><b>${t('history.meta_flow')}</b> <span>${escapeHtml(run.flow_name || run.flow_id)}</span></div>
      <div><b>${t('history.meta_type')}</b> <span>${trigStr}</span></div>
      <div><b>${t('history.meta_date')}</b> <span>${dateStr}</span></div>
      <div><b>${t('history.meta_duration')}</b> <span>${run.duration != null ? run.duration + 's' : '-'}</span></div>
      <div><b>${t('history.meta_status')}</b> <span style="font-weight:700; color:${statusColor};">${run.status.toUpperCase()}</span></div>
    `;

    if (run.error) {
      metaBar.innerHTML += `<div style="width: 100%; color: var(--port-err); margin-top: 4px;"><b>${t('history.meta_error')}</b> ${escapeHtml(run.error)}</div>`;
    }

    const body = $('histStepsBody');
    body.innerHTML = '';

    if (!run.steps || run.steps.length === 0) {
      body.innerHTML = `<p class="hint">${t('history.no_steps')}</p>`;
    } else {
      run.steps.forEach((s, idx) => {
        const stepCard = document.createElement('div');
        stepCard.className = 'step-card';
        const badgeClass = s.status === 'ok' ? 'ok' : s.status === 'skipped' ? 'skipped' : 'error';
        const statusLabel = s.status === 'ok' ? t('history.status_ok') : s.status === 'skipped' ? t('history.status_skipped') : t('history.status_error');

        let inputHtml = '';
        if (s.inputs != null) {
          inputHtml = `
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px; margin-top: 6px;">
              <span class="hint" style="font-size:0.7rem; text-transform:uppercase; font-weight:600;">${t('history.input_received')}</span>
              <div style="display: flex; gap: 4px;">
                <button class="icon-btn view-full-in-btn" style="font-size:0.72rem; padding: 2px 6px;" title="${t('history.view_input_title')}">🔍 ${t('common.view_full')}</button>
                <button class="icon-btn copy-in-btn" style="font-size:0.72rem; padding: 2px 6px;" title="${t('history.copy_input_title')}">${t('common.copy')}</button>
              </div>
            </div>
            <pre class="step-data" style="max-height: 100px;">${escapeHtml(s.inputs)}</pre>
          `;
        }

        let dataHtml = '';
        if (s.data != null) {
          dataHtml = `
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px;">
              <span class="hint" style="font-size:0.7rem; text-transform:uppercase; font-weight:600;">${t('history.output_label')}</span>
              <div style="display: flex; gap: 4px;">
                <button class="icon-btn view-full-step-btn" style="font-size:0.72rem; padding: 2px 6px;" title="${t('history.view_output_title')}">🔍 ${t('common.view_full')}</button>
                <button class="icon-btn copy-step-btn" style="font-size:0.72rem; padding: 2px 6px;" title="${t('history.copy_data_title')}">${t('common.copy')}</button>
              </div>
            </div>
            <pre class="step-data">${escapeHtml(s.data)}</pre>
          `;
        }

        let errHtml = '';
        if (s.error) {
          errHtml = `<div class="step-error"><b>${t('history.node_error')}</b> ${escapeHtml(s.error)}</div>`;
        }

        const durLabel = s.duration ? `<span class="hint" style="font-size:0.7rem;">${s.duration} ms</span>` : '';

        stepCard.innerHTML = `
          <div class="step-head">
            <div class="step-title">
              <span style="color: var(--text-muted); font-size: 0.72rem; font-family: var(--mono);">#${idx + 1}</span>
              <span>${escapeHtml(s.node_id)}</span>
            </div>
            <div style="display: flex; align-items: center; gap: 8px;">
              ${durLabel}
              <span class="step-badge ${badgeClass}">${statusLabel}</span>
              <button class="btn sm secondary locate-node-btn" title="${t('history.locate_title')}">
                <svg class="ic" width="12" height="12"><use href="#i-center"/></svg> ${t('history.view_node')}
              </button>
            </div>
          </div>
          <div class="step-body">
            ${inputHtml}
            ${dataHtml}
            ${errHtml}
          </div>
        `;

        const viewStepBtn = stepCard.querySelector('.view-full-step-btn');
        if (viewStepBtn) {
          viewStepBtn.onclick = () => {
            openDataViewer(
              t('viewer.run_output', { run: run.id, node: s.node_id }),
              t('viewer.status_line', { status: statusLabel, ms: s.duration || 0 }),
              s.data
            );
          };
        }

        const viewInBtn = stepCard.querySelector('.view-full-in-btn');
        if (viewInBtn) {
          viewInBtn.onclick = () => {
            openDataViewer(
              t('viewer.run_input', { run: run.id, node: s.node_id }),
              t('viewer.step_n', { n: idx + 1 }),
              s.inputs
            );
          };
        }

        const copyInBtn = stepCard.querySelector('.copy-in-btn');
        if (copyInBtn) {
          copyInBtn.onclick = () => {
            navigator.clipboard.writeText(s.inputs);
            toast(t('toast.input_copied'));
          };
        }

        const copyBtn = stepCard.querySelector('.copy-step-btn');
        if (copyBtn) {
          copyBtn.onclick = () => {
            navigator.clipboard.writeText(s.data);
            toast(t('toast.output_copied'));
          };
        }

        const locateBtn = stepCard.querySelector('.locate-node-btn');
        if (locateBtn) {
          locateBtn.onclick = () => {
            $('historySheet').hidden = true;
            highlightNodeOnCanvas(s.node_id);
          };
        }

        body.appendChild(stepCard);
      });
    }

    // Resaltar automáticamente los nodos en el canvas
    highlightRunOnCanvas(run.steps);

    $('historySheet').hidden = false;
  } catch (e) {
    toast(t('error.load_run'));
  }
}

function clearRunHighlights() {
  state.runHighlights = {};
  renderNodes();
}

function highlightRunOnCanvas(steps) {
  state.runHighlights = {};
  if (Array.isArray(steps)) {
    steps.forEach((s) => {
      const k = s.status === 'ok' ? 'ok' : s.status === 'skipped' ? 'skip' : 'err';
      state.runHighlights[s.node_id] = k;
    });
  } else if (steps && typeof steps === 'object') {
    Object.entries(steps).forEach(([nid, r]) => {
      const st = (r && r.status) || 'ok';
      const k = st === 'ok' ? 'ok' : st === 'skipped' ? 'skip' : 'err';
      state.runHighlights[nid] = k;
    });
  }
  renderNodes();
}

function selectNode(node) {
  if (!node) return;
  state.selectedNodeId = node.id;
  state.activeInspector = 'node';
  document.querySelectorAll('.node').forEach((n) => {
    if (n.dataset.id === node.id) {
      n.classList.add('selected');
    } else {
      n.classList.remove('selected');
    }
  });
  document.querySelectorAll('.flow-card').forEach((c) => c.classList.remove('active'));
  renderInspector();
}

function highlightNodeOnCanvas(nodeId) {
  const node = state.graph.nodes.find((n) => n.id === nodeId);
  if (node) {
    selectNode(node);
    const canvas = $('canvasWrap');
    const cw = canvas.clientWidth;
    const ch = canvas.clientHeight;
    state.view.x = cw / 2 - (node.x + 90) * state.view.k;
    state.view.y = ch / 2 - (node.y + 40) * state.view.k;
    updateTransform();
    toast(t('toast.node_selected', { id: nodeId }));
  }
}

async function purgeHistory() {
  if (!confirm(t('confirm.purge_history'))) {
    return;
  }
  try {
    const res = await fetch('/api/history/purge?days=30', { method: 'POST' });
    const data = await res.json();
    toast(t('toast.history_purged', { n: data.deleted }));
    await loadHistory();
  } catch (e) {
    toast(t('error.purge_history'));
  }
}

// ---------------------------------------------------------------------------
// Gestión de Flujos (Crear, Cargar, Guardar, Eliminar)
// ---------------------------------------------------------------------------

function resetGraph(name = defaultFlowName()) {
  if (state.debug && state.debug.active) stopDebugFlow();
  state.graph = { name, nodes: [], edges: [], version: 1 };
  state.meta = { name, description: '', schedule: '', enabled: true };
  state.selectedNodeId = null;
  state.activeInspector = 'flow';
  state.runHighlights = {};
  setClean();
  state.view = { k: 1, x: 0, y: 0 };
  $('flowName').value = name;
  render();
  if (state.activeTab === 'history') {
    loadHistory();
  }
  toast(t('toast.blank_canvas'));
}

async function openFlow(flowId) {
  try {
    const res = await fetch(`/api/flow?id=${encodeURIComponent(flowId)}`);
    if (!res.ok) throw new Error();
    const data = await res.json();
    if (state.debug && state.debug.active) stopDebugFlow();
    state.graph = data.graph;
    state.meta = data.meta || { name: flowId };
    $('flowName').value = state.graph.name;
    state.selectedNodeId = null;
    state.activeInspector = 'flow';
    state.runHighlights = {};
    setClean();
    render();
    renderFlowsList();
    centerFlow();
    if (state.activeTab === 'history') {
      loadHistory();
    }
    toast(t('toast.flow_loaded', { id: flowId }));
  } catch (e) {
    toast(t('error.load_flow'));
  }
}

async function loadTemplate(templateName) {
  try {
    const res = await fetch(`/api/template?name=${encodeURIComponent(templateName)}`);
    if (!res.ok) throw new Error();
    const graph = await res.json();
    state.graph = graph;
    state.meta = { name: graph.name };
    $('flowName').value = graph.name;
    state.selectedNodeId = null;
    state.activeInspector = 'flow';
    state.runHighlights = {};
    markDirty();
    render();
    centerFlow();
    if (state.activeTab === 'history') {
      loadHistory();
    }
    toast(t('toast.template_loaded', { name: templateName }));
  } catch (e) {
    toast(t('error.load_template'));
  }
}

async function saveFlow() {
  const id = state.graph.name || t('flow.fallback_id');
  state.meta.name = id;
  const payload = {
    id,
    graph: state.graph,
    meta: state.meta,
    force: true,
  };
  try {
    const res = await fetch('/api/flow', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) {
      alert(t('error.validation') + '\n' + (Array.isArray(data.detail) ? data.detail.join('\n') : data.detail));
      return false;
    }
    setClean();
    toast(t('toast.flow_saved', { id }));
    await loadFlows();
    render();
    return true;
  } catch (e) {
    toast(t('error.save_flow'));
    return false;
  }
}

async function deleteFlow(flowId) {
  if (!confirm(t('confirm.delete_flow', { id: flowId }))) return;
  try {
    const res = await fetch(`/api/flow?id=${encodeURIComponent(flowId)}`, { method: 'DELETE' });
    if (!res.ok) throw new Error();
    toast(t('toast.flow_deleted', { id: flowId }));
    if (state.graph.name === flowId) {
      setClean();
      state.activeInspector = null;
      resetGraph(defaultFlowName());
    }
    await loadFlows();
  } catch (e) {
    toast(t('error.delete_flow'));
  }
}

async function compileGraph() {
  try {
    const res = await fetch('/api/compile', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ graph: state.graph }),
    });
    const data = await res.json();
    if (!res.ok) {
      alert(t('error.compile_title') + '\n' + (Array.isArray(data.detail) ? data.detail.join('\n') : data.detail));
      return;
    }
    $('sheetTitle').textContent = t('sheet.code_title');
    $('sheetBody').textContent = data.code;
    $('sheetTrace').hidden = true;
    $('sheet').hidden = false;
  } catch (e) {
    toast(t('error.compile_failed'));
  }
}

async function runPreview() {
  toast(t('toast.running_flow'));
  try {
    const res = await fetch('/api/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ graph: state.graph }),
    });
    const data = await res.json();
    if (!res.ok) {
      alert(t('error.run_title') + '\n' + (Array.isArray(data.detail) ? data.detail.join('\n') : data.detail));
      return;
    }
    state.lastRunResults = data.results || {};
    $('sheetTitle').textContent = t('sheet.trace_title');
    $('sheetBody').textContent = data.code;

    const traceDiv = $('sheetTrace');
    traceDiv.innerHTML = `<div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;"><strong>${t('sheet.trace_results')}</strong><span class="hint" style="font-size:0.72rem;">${t('sheet.trace_hint')}</span></div>`;
    Object.entries(data.results || {}).forEach(([nid, r]) => {
      const cls = r.status === 'ok' ? 'trace-ok' : r.status === 'skipped' ? 'trace-skip' : 'trace-err';
      const detail = r.status === 'ok' ? JSON.stringify(r.data).slice(0, 100) : r.error || t('common.skipped');
      const dur = r.duration_ms ? `[${r.duration_ms} ms]` : '';
      const item = document.createElement('div');
      item.className = `trace-item ${cls}`;
      item.style.display = 'flex';
      item.style.justifyContent = 'space-between';
      item.style.alignItems = 'center';
      item.innerHTML = `
        <div style="flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">
          ▸ <b>${escapeHtml(nid)}</b> [${r.status}] ${dur}: ${escapeHtml(detail)}
        </div>
        <button class="btn sm secondary view-full-trace-btn" style="padding:2px 6px; font-size:0.7rem; margin-left:8px;">🔍 ${t('common.view_full')}</button>
      `;
      item.querySelector('.view-full-trace-btn').onclick = (e) => {
        e.stopPropagation();
        openDataViewer(
          t('viewer.node_result', { name: nid }),
          t('viewer.status_line', { status: r.status, ms: r.duration_ms || 0 }),
          r.status === 'ok' ? r.data : r.error
        );
      };
      traceDiv.appendChild(item);
    });
    traceDiv.hidden = false;
    $('sheet').hidden = false;

    highlightRunOnCanvas(data.results || {});
    if (state.selectedNodeId) {
      renderInspector();
    }
    if (state.activeTab === 'history') {
      loadHistory();
    }
  } catch (e) {
    toast(t('error.run_flow'));
  }
}

// ---------------------------------------------------------------------------
// Manipulación de Nodos y Cables
// ---------------------------------------------------------------------------

function addNode(typeKey, x, y) {
  const spec = state.catalog.node_types[typeKey];
  if (!spec) return;

  const count = state.graph.nodes.filter((n) => n.type === typeKey).length + 1;
  const baseName = typeKey.split('.')[1];
  const id = `${baseName}_${count}`;

  let posX = x;
  let posY = y;
  if (posX === undefined || posY === undefined) {
    const wrap = $('canvasWrap');
    if (wrap) {
      const rect = wrap.getBoundingClientRect();
      const centerWorld = screenToWorld(rect.left + rect.width / 2, rect.top + rect.height / 2);
      const offset = (state.graph.nodes.length % 5) * 35;
      posX = Math.round(centerWorld.x - 100 + offset);
      posY = Math.round(centerWorld.y - 40 + offset);
    } else {
      posX = 100 + (state.graph.nodes.length % 5) * 40;
      posY = 100 + (state.graph.nodes.length % 5) * 40;
    }
  }

  const node = {
    id,
    type: typeKey,
    label: spec.label,
    config: { ...spec.optional },
    x: posX,
    y: posY,
  };

  if (typeKey === 'transform.script' || spec.code_field) {
    const cf = spec.code_field || 'code';
    node.config[cf] = defaultScript();
  }

  state.graph.nodes.push(node);
  state.selectedNodeId = id;
  state.activeInspector = 'node';
  markDirty();
  render();
  toast(t('toast.node_added', { label: spec.label }));
}

function removeNode(nodeId) {
  state.graph.nodes = state.graph.nodes.filter((n) => n.id !== nodeId);
  state.graph.edges = state.graph.edges.filter((e) => e.source !== nodeId && e.target !== nodeId);
  if (state.selectedNodeId === nodeId) state.selectedNodeId = null;
  markDirty();
  render();
}

function removeEdge(source, target, port) {
  state.graph.edges = state.graph.edges.filter(
    (e) => !(e.source === source && e.target === target && e.port === port)
  );
  markDirty();
  render();
}

function duplicateNode(nodeId) {
  const orig = state.graph.nodes.find((n) => n.id === nodeId);
  if (!orig) return;
  const baseId = orig.id.replace(/_\d+$/, '');
  let n = 2;
  let newId = `${baseId}_${n}`;
  while (state.graph.nodes.some((x) => x.id === newId)) {
    n++;
    newId = `${baseId}_${n}`;
  }
  const newNode = JSON.parse(JSON.stringify(orig));
  newNode.id = newId;
  newNode.x = (newNode.x || 0) + 40;
  newNode.y = (newNode.y || 0) + 40;
  state.graph.nodes.push(newNode);
  state.selectedNodeId = newId;
  state.activeInspector = 'node';
  markDirty();
  render();
  toast(t('toast.node_duplicated', { id: newId }));
}

// ---------------------------------------------------------------------------
// Renderizado del Lienzo y Cables SVG
// ---------------------------------------------------------------------------

const elementFor = new Map();

function portCentre(id, which) {
  const node = state.graph.nodes.find((n) => n.id === id);
  if (!node) return null;
  const el = elementFor.get(id) || document.querySelector(`.node[data-id="${id}"]`);
  const w = el ? el.offsetWidth : 200;
  const h = el ? el.offsetHeight : 70;
  const x = which === 'in' ? node.x : node.x + w;
  let y;
  if (which === 'in') y = node.y + h * 0.5;
  else if (which === 'ok') y = node.y + h * 0.33;
  else y = node.y + h * 0.67;
  return { x, y };
}

function curve(from, to) {
  const dx = Math.max(38, Math.abs(to.x - from.x) * 0.45);
  return `M ${from.x} ${from.y} C ${from.x + dx} ${from.y}, ${to.x - dx} ${to.y}, ${to.x} ${to.y}`;
}

function render() {
  $('flowName').value = state.graph.name || '';
  $('emptyHint').style.display = state.graph.nodes.length ? 'none' : 'block';

  renderNodes();
  renderWires();
  renderInspector();
  updateTransform();
  updateDirtyUI();
}

function renderNodes() {
  const container = $('nodes');
  container.innerHTML = '';
  elementFor.clear();

  state.graph.nodes.forEach((node) => {
    const el = document.createElement('div');
    let cls = 'node' + (node.id === state.selectedNodeId ? ' selected' : '');
    if (state.runHighlights && state.runHighlights[node.id]) {
      cls += ` node-run-${state.runHighlights[node.id]}`;
    }
    if (state.debug && state.debug.active && state.debug.stepsOrder && state.debug.stepsOrder[state.debug.currentStepIndex]?.id === node.id) {
      cls += ' node-debug-active';
    }
    el.className = cls;
    el.dataset.id = node.id;
    el.style.left = `${node.x}px`;
    el.style.top = `${node.y}px`;

    // Cabecera
    const cat = node.type.split('.')[0] || 'input';
    const head = document.createElement('div');
    head.className = `node-header cat-${cat}`;
    head.innerHTML = `
      <span class="node-title-wrap" title="${escapeHtml(node.label || node.id)}">${escapeHtml(node.label || node.id)}</span>
      <div class="node-header-right">
        <span class="node-type-tag">${node.type.split('.')[1]}</span>
        <button class="node-btn-del" title="${t('node.delete')}">
          <svg class="ic" width="12" height="12"><use href="#i-trash"/></svg>
        </button>
      </div>
    `;
    const btnDel = head.querySelector('.node-btn-del');
    btnDel.onmousedown = (e) => e.stopPropagation();
    btnDel.onclick = (e) => {
      e.stopPropagation();
      removeNode(node.id);
    };
    el.appendChild(head);

    // Cuerpo
    const body = document.createElement('div');
    body.className = 'node-body';
    const spec = state.catalog.node_types[node.type];
    const codeField = spec ? (spec.code_field || (node.type === 'transform.script' ? 'code' : null)) : null;

    if (codeField) {
      const codeLines = (node.config[codeField] || '').split('\n').filter((l) => l.trim()).length;
      body.innerHTML = `
        <div style="margin-bottom: 6px; font-weight: 500;">${escapeHtml(node.id)}</div>
        <button class="btn sm secondary node-btn-edit-code" style="width: 100%; font-size: 0.72rem; padding: 3px 6px; display: flex; align-items: center; justify-content: center; gap: 4px;">
          <svg class="ic" width="12" height="12"><use href="#i-code"/></svg>
          <span>${codeLines ? t('node.edit_lines', { n: codeLines }) : t('node.write_code')}</span>
        </button>
      `;
      const btnEdit = body.querySelector('.node-btn-edit-code');
      btnEdit.onmousedown = (e) => e.stopPropagation();
      btnEdit.onclick = (e) => {
        e.stopPropagation();
        openCodeEditor(node);
      };
    } else {
      body.textContent = node.id;
    }
    el.appendChild(body);

    // Puertos en los bordes laterales (estilo tomos)
    // Entrada: borde izquierdo
    const inPort = document.createElement('div');
    inPort.className = 'port in';
    inPort.dataset.role = 'in';
    inPort.title = t('node.port_in');
    inPort.onmouseup = (e) => {
      if (state.wiring && state.wiring.sourceNodeId !== node.id) {
        e.stopPropagation();
        finishWire(node.id);
      }
    };
    el.appendChild(inPort);

    // Salidas OK y ERROR: borde derecho
    const okPort = document.createElement('div');
    okPort.className = 'port ok out-ok';
    okPort.dataset.role = 'out';
    okPort.dataset.port = 'ok';
    okPort.title = t('node.port_ok');
    okPort.onmousedown = (e) => startWire(e, node.id, 'ok');
    el.appendChild(okPort);

    const errPort = document.createElement('div');
    errPort.className = 'port error out-error';
    errPort.dataset.role = 'out';
    errPort.dataset.port = 'error';
    errPort.title = t('node.port_error');
    errPort.onmousedown = (e) => startWire(e, node.id, 'error');
    el.appendChild(errPort);

    // Selección y Arrastre del nodo
    el.onmousedown = (e) => {
      if (state.isSpacePressed) return;
      if (e.target.classList.contains('port')) return;
      if (e.target.closest('.node-btn-del')) return;
      if (e.target.closest('.node-btn-edit-code')) return;
      if (e.button !== 0) return;
      e.stopPropagation();

      selectNode(node);
      state.dragNode = node;
      const mouseWorld = screenToWorld(e.clientX, e.clientY);
      state.dragOffset = { x: mouseWorld.x - node.x, y: mouseWorld.y - node.y };
    };

    // Soltar cable en cualquier parte del nodo para conectarlo
    el.onmouseup = (e) => {
      if (state.wiring && state.wiring.sourceNodeId !== node.id) {
        e.stopPropagation();
        finishWire(node.id);
      }
    };

    el.ondblclick = (e) => {
      e.stopPropagation();
      setInspectorCollapsed(false);
    };

    container.appendChild(el);
    elementFor.set(node.id, el);
  });
}

function renderWires() {
  const svg = $('wires');
  svg.innerHTML = '';

  state.graph.edges.forEach((edge) => {
    const from = portCentre(edge.source, edge.port === 'error' ? 'error' : 'ok');
    const to = portCentre(edge.target, 'in');
    if (!from || !to) return;

    const d = curve(from, to);

    // Grupo contenedor del cable interactivo
    const group = document.createElementNS('http://www.w3.org/2000/svg', 'g');
    group.setAttribute('class', `wire-group wire-${edge.port} ${edge.port === 'error' ? 'error' : 'ok'}`);

    // Hitbox transparente ancha (16px) para facilitar detección del cursor
    const hitbox = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    hitbox.setAttribute('d', d);
    hitbox.setAttribute('class', 'wire-hitbox');

    // Trazado visual del cable
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', d);
    path.setAttribute('class', `wire wire-${edge.port} ${edge.port === 'error' ? 'error' : 'ok'}`);

    // Botón flotante para desconectar en el punto medio de la curva
    const midX = Math.round((from.x + to.x) / 2);
    const midY = Math.round((from.y + to.y) / 2);

    const delWrap = document.createElementNS('http://www.w3.org/2000/svg', 'g');
    delWrap.setAttribute('class', 'wire-del-wrap');
    delWrap.setAttribute('transform', `translate(${midX}, ${midY})`);
    delWrap.innerHTML = `
      <g class="wire-del-btn">
        <circle r="10" class="wire-del-circle"></circle>
        <path d="M -3.5 -3.5 L 3.5 3.5 M 3.5 -3.5 L -3.5 3.5" class="wire-del-cross"></path>
        <title>${t('canvas.wire_disconnect')}</title>
      </g>
    `;

    // Tooltip nativo
    const title = document.createElementNS('http://www.w3.org/2000/svg', 'title');
    title.textContent = t('canvas.wire_title', { port: edge.port.toUpperCase(), source: edge.source, target: edge.target });
    group.appendChild(title);

    group.appendChild(hitbox);
    group.appendChild(path);
    group.appendChild(delWrap);

    // Clic derecho para desconectar en cualquier punto
    group.oncontextmenu = (e) => {
      e.preventDefault();
      e.stopPropagation();
      removeEdge(edge.source, edge.target, edge.port);
    };

    // Clic izquierdo sobre el botón × de desconexión
    delWrap.onclick = (e) => {
      e.preventDefault();
      e.stopPropagation();
      removeEdge(edge.source, edge.target, edge.port);
    };

    svg.appendChild(group);
  });

  // Cable temporal durante arrastre
  if (state.wiring) {
    const from = { x: state.wiring.startX, y: state.wiring.startY };
    const to = { x: state.wiring.currX, y: state.wiring.currY };
    const d = curve(from, to);
    const p = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    p.setAttribute('d', d);
    p.setAttribute('class', `wire wire-draft wire-${state.wiring.port} dragging`);
    svg.appendChild(p);
  }
}

// ---------------------------------------------------------------------------
// Arrastre e Interacción en Canvas
// ---------------------------------------------------------------------------

function onCanvasMouseDown(e) {
  // Paneo con barra espaciadora en cualquier punto del lienzo
  if (state.isSpacePressed) {
    if (e.button === 0 || e.button === 1) {
      state.isPanning = true;
      state.panStart = { x: e.clientX, y: e.clientY };
      state.panOrigin = { x: state.view.x, y: state.view.y };
      $('canvasWrap').classList.add('panning');
      document.body.classList.add('panning');
      e.preventDefault();
      return;
    }
  }

  if (e.target.closest('.node') || e.target.closest('.port') || e.target.closest('.zoombar')) {
    return;
  }

  // Deseleccionar nodo y flujo si se hace clic en el fondo vacío del lienzo
  if (state.selectedNodeId !== null || state.activeInspector !== null) {
    state.selectedNodeId = null;
    state.activeInspector = null;
    document.querySelectorAll('.node').forEach((n) => n.classList.remove('selected'));
    document.querySelectorAll('.flow-card').forEach((c) => c.classList.remove('active'));
    renderInspector();
  }

  // Iniciar paneo arrastrando el fondo del lienzo (botón izquierdo o central)
  if (e.button === 0 || e.button === 1) {
    state.isPanning = true;
    state.panStart = { x: e.clientX, y: e.clientY };
    state.panOrigin = { x: state.view.x, y: state.view.y };
    $('canvasWrap').classList.add('panning');
  }
}

function onCanvasMouseMove(e) {
  if (state.isPanning) {
    const dx = e.clientX - state.panStart.x;
    const dy = e.clientY - state.panStart.y;
    state.view.x = state.panOrigin.x + dx;
    state.view.y = state.panOrigin.y + dy;
    updateTransform();
    return;
  }

  const mouseWorld = screenToWorld(e.clientX, e.clientY);

  if (state.dragNode) {
    state.dragNode.x = Math.round(mouseWorld.x - state.dragOffset.x);
    state.dragNode.y = Math.round(mouseWorld.y - state.dragOffset.y);
    markDirty();
    renderWires();
    const nodeEl = elementFor.get(state.dragNode.id) || document.querySelector(`.node[data-id="${state.dragNode.id}"]`);
    if (nodeEl) {
      nodeEl.style.left = `${state.dragNode.x}px`;
      nodeEl.style.top = `${state.dragNode.y}px`;
    }
  } else if (state.wiring) {
    state.wiring.currX = mouseWorld.x;
    state.wiring.currY = mouseWorld.y;
    renderWires();
  }
}

function onCanvasMouseUp(e) {
  if (state.isPanning) {
    state.isPanning = false;
    $('canvasWrap').classList.remove('panning');
    document.body.classList.remove('panning');
  }
  if (state.dragNode) state.dragNode = null;
  if (state.wiring) {
    const { sourceNodeId, port } = state.wiring;
    state.wiring = null;
    document.body.classList.remove('is-wiring');

    if (e) {
      const dropped = document.elementFromPoint(e.clientX, e.clientY);
      const targetEl = dropped && dropped.closest('.node');
      if (targetEl) {
        const targetId = targetEl.dataset.id;
        if (targetId && targetId !== sourceNodeId) {
          finishWireAction(sourceNodeId, targetId, port);
          return;
        }
      }
    }
    renderWires();
  }
}

function finishWireAction(sourceId, targetId, port) {
  if (sourceId === targetId) return;
  const exists = state.graph.edges.some(
    (edge) => edge.source === sourceId && edge.target === targetId && edge.port === port
  );
  if (!exists) {
    state.graph.edges.push({ source: sourceId, target: targetId, port });
    markDirty();
    render();
    toast(t('toast.connected', { port }));
  } else {
    renderWires();
  }
}

function startWire(e, sourceNodeId, port) {
  e.stopPropagation();
  if (e.button !== 0) return;
  const sNode = state.graph.nodes.find((n) => n.id === sourceNodeId);
  if (!sNode) return;
  const from = portCentre(sourceNodeId, port);
  if (!from) return;
  state.wiring = {
    sourceNodeId,
    port,
    startX: from.x,
    startY: from.y,
    currX: from.x,
    currY: from.y,
  };
  document.body.classList.add('is-wiring');
  renderWires();
}

function finishWire(targetNodeId) {
  if (!state.wiring) return;
  const { sourceNodeId, port } = state.wiring;
  state.wiring = null;
  document.body.classList.remove('is-wiring');
  finishWireAction(sourceNodeId, targetNodeId, port);
}

// ---------------------------------------------------------------------------
// Visor de Datos Completos e Inspección (No truncado)
// ---------------------------------------------------------------------------

let dataViewerCurrentData = null;
let dataViewerMode = 'formatted'; // 'formatted' | 'raw'

function openDataViewer(title, subtitle, data) {
  dataViewerCurrentData = data;
  dataViewerMode = 'formatted';
  if ($('dataViewerTitle')) $('dataViewerTitle').textContent = title || t('viewer.default_title');
  if ($('dataViewerSubtitle')) $('dataViewerSubtitle').textContent = subtitle || '';
  if ($('dataViewerSearch')) $('dataViewerSearch').value = '';

  if ($('btnDataViewFormatted')) $('btnDataViewFormatted').classList.add('active');
  if ($('btnDataViewRaw')) $('btnDataViewRaw').classList.remove('active');

  renderDataViewerContent();
  if ($('dataViewerModal')) $('dataViewerModal').hidden = false;
}

function renderDataViewerContent() {
  const pre = $('dataViewerContent');
  if (!pre) return;
  const raw = dataViewerCurrentData;
  let text = '';

  if (raw === null || raw === undefined) {
    text = t('viewer.empty');
  } else if (typeof raw === 'string') {
    text = raw;
    if (dataViewerMode === 'formatted') {
      try {
        const parsed = JSON.parse(raw);
        text = JSON.stringify(parsed, null, 2);
      } catch (e) {
        text = raw;
      }
    }
  } else {
    text = dataViewerMode === 'formatted' ? JSON.stringify(raw, null, 2) : JSON.stringify(raw);
  }

  const bytes = new Blob([text]).size;
  const sizeStr = bytes > 1024 * 1024 ? `${(bytes / (1024 * 1024)).toFixed(2)} MB` : `${(bytes / 1024).toFixed(2)} KB`;
  if ($('dataViewerSize')) $('dataViewerSize').textContent = t('viewer.size', { size: sizeStr, chars: text.length });

  pre.textContent = text;
}

function switchDataViewerMode(mode) {
  dataViewerMode = mode;
  if (mode === 'formatted') {
    if ($('btnDataViewFormatted')) $('btnDataViewFormatted').classList.add('active');
    if ($('btnDataViewRaw')) $('btnDataViewRaw').classList.remove('active');
  } else {
    if ($('btnDataViewFormatted')) $('btnDataViewFormatted').classList.remove('active');
    if ($('btnDataViewRaw')) $('btnDataViewRaw').classList.add('active');
  }
  renderDataViewerContent();
}

function copyDataViewerContent() {
  const pre = $('dataViewerContent');
  if (pre && pre.textContent) {
    navigator.clipboard.writeText(pre.textContent);
    toast(t('toast.content_copied'));
  }
}

function filterDataViewerContent(e) {
  const query = e.target.value.toLowerCase();
  const pre = $('dataViewerContent');
  if (!pre) return;
  if (!query) {
    renderDataViewerContent();
    return;
  }
  const text = pre.textContent;
  const lines = text.split('\n');
  const matched = lines.filter((l) => l.toLowerCase().includes(query));
  pre.textContent = matched.join('\n') || t('viewer.no_lines', { query });
}

// ---------------------------------------------------------------------------
// Controlador de Depuración Paso a Paso
// ---------------------------------------------------------------------------

function getTopologicalOrder() {
  const remaining = new Map(state.graph.nodes.map((n) => [n.id, n]));
  const indegree = new Map(state.graph.nodes.map((n) => [n.id, 0]));
  for (const e of state.graph.edges) {
    if (indegree.has(e.target)) {
      indegree.set(e.target, indegree.get(e.target) + 1);
    }
  }
  const ordered = [];
  while (remaining.size > 0) {
    const ready = Array.from(remaining.values()).find((n) => indegree.get(n.id) === 0);
    if (!ready) return null; // Ciclo detectado
    ordered.push(ready);
    remaining.delete(ready.id);
    for (const e of state.graph.edges) {
      if (e.source === ready.id && indegree.has(e.target)) {
        indegree.set(e.target, indegree.get(e.target) - 1);
      }
    }
  }
  return ordered;
}

function toggleDebugFlow() {
  if (state.debug && state.debug.active) {
    stopDebugFlow();
  } else {
    startDebugFlow();
  }
}

function startDebugFlow() {
  if (!state.graph.nodes.length) {
    toast(t('toast.debug_no_nodes'));
    return;
  }
  const order = getTopologicalOrder();
  if (!order) {
    toast(t('toast.debug_cycles'));
    return;
  }

  state.debug = {
    active: true,
    currentStepIndex: 0,
    stepsOrder: order,
    cachedResults: {},
    running: false,
  };

  $('debugToolbar').hidden = false;
  if ($('btnDebugFlow')) $('btnDebugFlow').classList.add('active');
  updateDebugToolbar();
  highlightActiveDebugNode();
  toast(t('toast.debug_active'));
}

function updateDebugToolbar() {
  const tb = $('debugToolbar');
  if (!tb || !state.debug.active) return;

  const total = state.debug.stepsOrder.length;
  const idx = state.debug.currentStepIndex;

  $('debugStepTotal').textContent = total;
  if (idx < total) {
    const node = state.debug.stepsOrder[idx];
    $('debugStepNum').textContent = idx + 1;
    const fullName = `${node.label || node.id} (${node.type})`;
    $('debugStepNodeName').textContent = fullName;
    $('debugStepNodeName').title = fullName;
    $('btnDebugNext').disabled = false;
  } else {
    $('debugStepNum').textContent = total;
    $('debugStepNodeName').textContent = t('debug.completed');
    $('debugStepNodeName').title = t('debug.flow_completed');
    $('btnDebugNext').disabled = true;
  }
}

function highlightActiveDebugNode() {
  renderNodes();
}

async function stepDebugNext() {
  if (!state.debug.active || state.debug.running) return;
  const idx = state.debug.currentStepIndex;
  if (idx >= state.debug.stepsOrder.length) {
    toast(t('toast.debug_done'));
    return;
  }

  const node = state.debug.stepsOrder[idx];
  state.debug.running = true;
  $('btnDebugNext').disabled = true;
  toast(t('toast.debug_step_running', { n: idx + 1, name: node.label || node.id }));

  try {
    const res = await fetch('/api/flow/step', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        graph: state.graph,
        target_node_id: node.id,
        mode: 'step',
        cached_results: state.debug.cachedResults,
      }),
    });
    const data = await res.json();
    state.debug.running = false;

    if (!res.ok || data.status !== 'ok') {
      alert(t('error.step_title') + '\n' + (data.detail || data.error || t('error.unknown')));
      return;
    }

    state.debug.cachedResults = data.all_results || {};
    state.lastRunResults = data.all_results || {};

    const st = data.step.status;
    state.runHighlights[node.id] = st === 'ok' ? 'ok' : 'err';

    state.debug.currentStepIndex++;
    updateDebugToolbar();
    renderNodes();
    selectNode(node);
  } catch (e) {
    state.debug.running = false;
    $('btnDebugNext').disabled = false;
    toast(t('toast.debug_step_failed'));
  }
}

async function runToNode(targetNodeId) {
  toast(t('toast.run_to_node', { id: targetNodeId }));
  try {
    const res = await fetch('/api/flow/step', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        graph: state.graph,
        target_node_id: targetNodeId,
        mode: 'run_to',
        cached_results: (state.debug && state.debug.cachedResults) || {},
      }),
    });
    const data = await res.json();
    if (!res.ok || data.status !== 'ok') {
      alert(t('error.run_to_title') + '\n' + (data.detail || data.error || t('error.generic')));
      return;
    }

    if (!state.debug.active) {
      const order = getTopologicalOrder();
      if (order) {
        state.debug.active = true;
        state.debug.stepsOrder = order;
        $('debugToolbar').hidden = false;
        if ($('btnDebugFlow')) $('btnDebugFlow').classList.add('active');
      }
    }

    state.debug.cachedResults = data.all_results || {};
    state.lastRunResults = data.all_results || {};

    Object.entries(data.all_results || {}).forEach(([nid, r]) => {
      state.runHighlights[nid] = r.status === 'ok' ? 'ok' : 'err';
    });

    const targetIdx = state.debug.stepsOrder.findIndex((n) => n.id === targetNodeId);
    if (targetIdx !== -1) {
      state.debug.currentStepIndex = targetIdx + 1;
    }

    updateDebugToolbar();
    highlightActiveDebugNode();

    const targetNode = state.graph.nodes.find((n) => n.id === targetNodeId);
    if (targetNode) {
      selectNode(targetNode);
    }
    toast(t('toast.run_to_done', { id: targetNodeId }));
  } catch (e) {
    toast(t('error.run_to_failed'));
  }
}

async function continueDebugFlow() {
  if (!state.debug.active) return;
  while (state.debug.currentStepIndex < state.debug.stepsOrder.length) {
    await stepDebugNext();
    const lastNode = state.debug.stepsOrder[state.debug.currentStepIndex - 1];
    if (lastNode && state.debug.cachedResults[lastNode.id]?.status === 'error') {
      toast(t('toast.debug_stopped_error'));
      break;
    }
  }
}

function resetDebugFlow() {
  if (!state.debug.active) return;
  state.debug.currentStepIndex = 0;
  state.debug.cachedResults = {};
  state.runHighlights = {};
  updateDebugToolbar();
  highlightActiveDebugNode();
  renderInspector();
  toast(t('toast.debug_reset'));
}

function stopDebugFlow() {
  state.debug.active = false;
  $('debugToolbar').hidden = true;
  if ($('btnDebugFlow')) $('btnDebugFlow').classList.remove('active');
  renderNodes();
  toast(t('toast.debug_ended'));
}

// ---------------------------------------------------------------------------
// Sección de Ejecución & Debug e Inspector de Transformación en Vivo
// ---------------------------------------------------------------------------

function renderNodeExecutionSection(node, container) {
  const execData =
    (state.debug && state.debug.cachedResults && state.debug.cachedResults[node.id]) ||
    (state.lastRunResults && state.lastRunResults[node.id]);
  if (!execData) return;

  const box = document.createElement('div');
  box.className = 'node-exec-box';

  const isOk = execData.status === 'ok';
  const badgeClass = isOk ? 'step-ok' : execData.status === 'skipped' ? 'step-skip' : 'step-err';
  const badgeText = isOk ? 'OK' : execData.status === 'skipped' ? t('node.badge_skipped') : 'ERROR';
  const durationText = execData.duration_ms ? `${execData.duration_ms} ms` : '';

  box.innerHTML = `
    <div class="node-exec-head">
      <span class="node-exec-title">
        <svg class="ic" width="13" height="13"><use href="#i-play"/></svg>
        ${t('node.exec_title')}
      </span>
      <div style="display: flex; gap: 6px; align-items: center;">
        <span class="hint" style="font-size: 0.72rem;">${durationText}</span>
        <span class="step-badge ${badgeClass}">${badgeText}</span>
      </div>
    </div>
    <div class="tab-pill-group" style="margin-bottom: 6px;">
      <button class="tab-pill active" id="pillOutput_${node.id}">${t('node.output_tab')}</button>
      <button class="tab-pill" id="pillInput_${node.id}">${t('node.input_tab')}</button>
    </div>
    <pre class="node-exec-pre" id="nodeExecPre_${node.id}"></pre>
    <div class="node-exec-actions">
      <button class="btn sm secondary" id="btnViewFullData_${node.id}" title="${t('node.view_full_title')}">
        🔍 ${t('node.view_full_btn')}
      </button>
      <button class="btn sm secondary" id="btnCopyExecData_${node.id}" title="${t('node.copy_title')}">
        📋 ${t('common.copy')}
      </button>
    </div>
  `;

  let activeTab = 'output';
  const pre = box.querySelector(`#nodeExecPre_${node.id}`);
  const pillOut = box.querySelector(`#pillOutput_${node.id}`);
  const pillIn = box.querySelector(`#pillInput_${node.id}`);
  const btnFull = box.querySelector(`#btnViewFullData_${node.id}`);
  const btnCopy = box.querySelector(`#btnCopyExecData_${node.id}`);

  function updateExecDisplay() {
    const rawData = activeTab === 'output' ? (isOk ? execData.data : execData.error) : execData.inputs;
    let displayText = '';
    if (rawData === null || rawData === undefined) {
      displayText = activeTab === 'input' ? t('node.no_input') : t('node.empty_output');
    } else if (typeof rawData === 'object') {
      displayText = JSON.stringify(rawData, null, 2);
    } else {
      displayText = String(rawData);
    }
    pre.textContent = displayText;
  }

  pillOut.onclick = () => {
    activeTab = 'output';
    pillOut.classList.add('active');
    pillIn.classList.remove('active');
    updateExecDisplay();
  };

  pillIn.onclick = () => {
    activeTab = 'input';
    pillIn.classList.add('active');
    pillOut.classList.remove('active');
    updateExecDisplay();
  };

  btnFull.onclick = () => {
    const rawData = activeTab === 'output' ? (isOk ? execData.data : execData.error) : execData.inputs;
    openDataViewer(
      t(activeTab === 'output' ? 'viewer.node_output' : 'viewer.node_input', { name: node.label || node.id }),
      t('viewer.node_meta', {
        type: node.type,
        status: badgeText,
        duration: durationText ? t('viewer.duration_part', { duration: durationText }) : '',
      }),
      rawData
    );
  };

  btnCopy.onclick = () => {
    const rawData = activeTab === 'output' ? (isOk ? execData.data : execData.error) : execData.inputs;
    const textToCopy = typeof rawData === 'object' ? JSON.stringify(rawData, null, 2) : String(rawData || '');
    navigator.clipboard.writeText(textToCopy);
    toast(t('toast.data_copied'));
  };

  updateExecDisplay();
  container.appendChild(box);
}

let livePreviewDebounceTimer = null;

function renderLivePreviewSection(node, spec, container) {
  const box = document.createElement('div');
  box.className = 'live-preview-box';
  box.id = `livePreview_${node.id}`;

  const incoming = state.graph.edges.filter((e) => e.target === node.id);
  let parentData = null;
  let parentId = null;
  for (const edge of incoming) {
    const cached =
      (state.debug && state.debug.cachedResults && state.debug.cachedResults[edge.source]) ||
      (state.lastRunResults && state.lastRunResults[edge.source]);
    if (cached && cached.data !== undefined && cached.data !== null) {
      parentData = cached.data;
      parentId = edge.source;
      break;
    }
  }

  let sourceLabel = '';
  if (parentData !== null) {
    const size = typeof parentData === 'string' ? parentData.length : JSON.stringify(parentData).length;
    const sizeKb = (size / 1024).toFixed(1);
    sourceLabel = t('preview.using_output', { id: escapeHtml(parentId), kb: sizeKb });
  } else {
    sourceLabel = t('preview.no_input');
  }

  box.innerHTML = `
    <div class="live-preview-head">
      <div class="live-preview-title">
        <span class="live-pulse-dot"></span>
        <span>${t('preview.title')}</span>
      </div>
      <span class="hint" id="livePreviewStatus_${node.id}" style="font-size: 0.72rem;">${t('common.ready')}</span>
    </div>
    <div class="live-preview-source" id="livePreviewSource_${node.id}">
      <span>${sourceLabel}</span>
    </div>
    <pre class="live-preview-pre" id="livePreviewOutput_${node.id}">${t('preview.calculating_output')}</pre>
    <a class="live-sample-toggle" id="toggleSampleInput_${node.id}">
      ${parentData === null ? t('preview.edit_sample') : t('preview.use_custom')}
    </a>
    <div class="live-sample-area" id="sampleInputArea_${node.id}" ${parentData !== null ? 'hidden' : ''}>
      <textarea id="sampleInputText_${node.id}" rows="3" style="font-family: var(--mono); font-size: 0.72rem;" placeholder="${t('preview.sample_placeholder')}">${parentData ? (typeof parentData === 'string' ? parentData : JSON.stringify(parentData, null, 2)) : ''}</textarea>
    </div>
  `;

  const toggleBtn = box.querySelector(`#toggleSampleInput_${node.id}`);
  const area = box.querySelector(`#sampleInputArea_${node.id}`);
  toggleBtn.onclick = () => {
    area.hidden = !area.hidden;
    toggleBtn.textContent = area.hidden ? t('preview.use_custom') : t('preview.hide_custom');
  };

  const sampleText = box.querySelector(`#sampleInputText_${node.id}`);
  sampleText.oninput = () => {
    triggerLivePreview(node);
  };

  container.appendChild(box);
  triggerLivePreview(node);
}

function triggerLivePreview(node) {
  clearTimeout(livePreviewDebounceTimer);
  livePreviewDebounceTimer = setTimeout(() => {
    runLivePreview(node);
  }, 250);
}

async function runLivePreview(node) {
  const box = $(`livePreview_${node.id}`);
  const outEl = $(`livePreviewOutput_${node.id}`);
  const statEl = $(`livePreviewStatus_${node.id}`);
  if (!outEl) return;

  if (box) box.classList.add('is-updating');
  if (statEl) statEl.textContent = t('preview.calculating');

  let inputData = null;
  const sampleArea = $(`sampleInputArea_${node.id}`);
  const sampleText = $(`sampleInputText_${node.id}`);
  if (sampleArea && !sampleArea.hidden && sampleText && sampleText.value.trim()) {
    inputData = sampleText.value.trim();
  } else {
    const incoming = state.graph.edges.filter((e) => e.target === node.id);
    for (const edge of incoming) {
      const cached =
        (state.debug && state.debug.cachedResults && state.debug.cachedResults[edge.source]) ||
        (state.lastRunResults && state.lastRunResults[edge.source]);
      if (cached && cached.data !== undefined && cached.data !== null) {
        inputData = cached.data;
        break;
      }
    }
  }

  try {
    const res = await fetch('/api/node/preview', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        type: node.type,
        config: node.config,
        input_data: inputData,
        code: node.config.code || '',
        flow_id: state.graph.name || 'preview',
      }),
    });
    const data = await res.json();
    if (box) box.classList.remove('is-updating');

    if (data.status === 'ok') {
      let count = '';
      if (Array.isArray(data.data)) {
        count = t('preview.items', { n: data.data.length });
      }
      if (statEl) statEl.innerHTML = `<span style="color:var(--port-ok)">OK ${count} · ${data.duration_ms} ms</span>`;

      let display = '';
      if (typeof data.data === 'object' && data.data !== null) {
        display = JSON.stringify(data.data, null, 2);
      } else {
        display = String(data.data !== null && data.data !== undefined ? data.data : t('node.empty_output'));
      }
      outEl.style.color = 'var(--text)';
      outEl.textContent = display;
    } else {
      if (statEl) statEl.innerHTML = `<span style="color:var(--port-err)">${t('error.generic')}</span>`;
      outEl.style.color = 'var(--port-err)';
      outEl.textContent = data.error || t('error.unknown');
    }
  } catch (e) {
    if (box) box.classList.remove('is-updating');
    if (statEl) statEl.textContent = t('error.network');
    outEl.textContent = String(e);
  }
}

// ---------------------------------------------------------------------------
// Inspector de Propiedades del Nodo Seleccionado
// ---------------------------------------------------------------------------

function renderInspector() {
  const titleEl = $('inspectorTitle');
  const body = $('inspectorBody');
  if (!body) return;

  if (state.selectedNodeId) {
    const node = state.graph.nodes.find((n) => n.id === state.selectedNodeId);
    if (node) {
      if (titleEl) titleEl.textContent = t('inspector.node_title');
      renderNodeInspector(node, body);
      return;
    }
  }

  if (state.activeInspector === 'flow') {
    if (titleEl) titleEl.textContent = t('inspector.flow_title');
    renderFlowInspector(body);
    return;
  }

  // Estado vacío: clic fuera en el lienzo sin nodo ni flujo seleccionado
  if (titleEl) titleEl.textContent = t('inspector.title');
  body.innerHTML = `
    <div style="display: flex; flex-direction: column; align-items: center; justify-content: center; height: 260px; text-align: center; color: var(--text-muted); gap: 12px; padding: 20px;">
      <svg class="ic" width="36" height="36" style="opacity: 0.35;"><use href="#i-flow"/></svg>
      <p style="font-size: 0.84rem; margin: 0; line-height: 1.5;">
        ${t('inspector.empty_html')}
      </p>
    </div>
  `;
}

// ---------------------------------------------------------------------------
// Panel de Herramientas y Permisos para transform.coder
// ---------------------------------------------------------------------------

// category / name / desc son claves del catálogo i18n (se traducen al renderizar)
const CODER_TOOLS_SPEC = [
  {
    category: 'coder.cat_read',
    icon: '📂',
    tools: [
      { id: 'read', name: 'coder.tool_read_name', desc: 'coder.tool_read_desc' },
      { id: 'grep', name: 'coder.tool_grep_name', desc: 'coder.tool_grep_desc' },
      { id: 'find', name: 'coder.tool_find_name', desc: 'coder.tool_find_desc' },
      { id: 'ls', name: 'coder.tool_ls_name', desc: 'coder.tool_ls_desc' },
    ],
  },
  {
    category: 'coder.cat_write',
    icon: '✏️',
    tools: [
      { id: 'edit', name: 'coder.tool_edit_name', desc: 'coder.tool_edit_desc' },
      { id: 'write', name: 'coder.tool_write_name', desc: 'coder.tool_write_desc' },
    ],
  },
  {
    category: 'coder.cat_bash',
    icon: '💻',
    tools: [
      { id: 'bash', name: 'coder.tool_bash_name', desc: 'coder.tool_bash_desc' },
    ],
  },
  {
    category: 'coder.cat_net',
    icon: '🌐',
    tools: [
      { id: 'web_search', name: 'coder.tool_web_search_name', desc: 'coder.tool_web_search_desc' },
    ],
  },
];

const CODER_PRESETS = {
  coding: ['read', 'bash', 'edit', 'write'],
  read_only: ['read', 'grep', 'find', 'ls'],
  all: ['read', 'bash', 'edit', 'write', 'grep', 'find', 'ls', 'web_search'],
};

function setsEqual(setA, arrayB) {
  if (setA.size !== arrayB.length) return false;
  for (const item of arrayB) {
    if (!setA.has(item)) return false;
  }
  return true;
}

function createCoderToolsSection(node) {
  const container = document.createElement('div');
  container.className = 'field-group';
  container.style.marginBottom = '14px';

  const activeTools = new Set();
  const currentPreset = (node.config.tools_preset || 'coding').toLowerCase().trim();

  if (currentPreset === 'all') {
    CODER_PRESETS.all.forEach((t) => activeTools.add(t));
  } else if (currentPreset === 'read_only') {
    CODER_PRESETS.read_only.forEach((t) => activeTools.add(t));
  } else if (currentPreset === 'custom') {
    const raw = typeof node.config.custom_tools === 'string'
      ? node.config.custom_tools
      : (Array.isArray(node.config.custom_tools) ? node.config.custom_tools.join(',') : '');
    raw.split(',').map((s) => s.trim().toLowerCase()).filter(Boolean).forEach((t) => activeTools.add(t));
  } else {
    CODER_PRESETS.coding.forEach((t) => activeTools.add(t));
  }

  const lbl = document.createElement('label');
  lbl.innerHTML = `<span>🛠️ ${t('coder.tools_title')}</span>`;
  container.appendChild(lbl);

  const presetsBox = document.createElement('div');
  presetsBox.style.display = 'flex';
  presetsBox.style.gap = '4px';
  presetsBox.style.flexWrap = 'wrap';
  presetsBox.style.margin = '4px 0 8px 0';

  const presetOptions = [
    { id: 'coding', label: t('coder.preset_coding') },
    { id: 'read_only', label: t('coder.preset_read_only') },
    { id: 'all', label: t('coder.preset_all') },
    { id: 'custom', label: t('coder.preset_custom') },
  ];

  const presetButtons = {};

  function updatePresetButtons() {
    let matchedPreset = 'custom';
    if (setsEqual(activeTools, CODER_PRESETS.coding)) matchedPreset = 'coding';
    else if (setsEqual(activeTools, CODER_PRESETS.read_only)) matchedPreset = 'read_only';
    else if (setsEqual(activeTools, CODER_PRESETS.all)) matchedPreset = 'all';

    presetOptions.forEach((opt) => {
      const btn = presetButtons[opt.id];
      if (!btn) return;
      const isActive = opt.id === matchedPreset;
      btn.style.background = isActive ? 'var(--primary)' : 'var(--bg-subtle)';
      btn.style.color = isActive ? '#fff' : 'var(--text-muted)';
      btn.style.borderColor = isActive ? 'var(--primary)' : 'var(--border)';
      btn.style.fontWeight = isActive ? '600' : '400';
    });

    node.config.tools_preset = matchedPreset;
    if (matchedPreset === 'custom') {
      node.config.custom_tools = Array.from(activeTools).join(',');
    } else {
      node.config.custom_tools = '';
    }
  }

  const toolsBox = document.createElement('div');
  toolsBox.style.background = 'var(--bg-subtle)';
  toolsBox.style.border = '1px solid var(--border)';
  toolsBox.style.borderRadius = '6px';
  toolsBox.style.padding = '8px 10px';

  presetOptions.forEach((opt) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.textContent = opt.label;
    btn.style.fontSize = '0.72rem';
    btn.style.padding = '3px 8px';
    btn.style.borderRadius = '12px';
    btn.style.border = '1px solid var(--border)';
    btn.style.cursor = 'pointer';
    btn.style.transition = 'all 0.15s ease';

    btn.onclick = () => {
      if (opt.id === 'coding') {
        activeTools.clear();
        CODER_PRESETS.coding.forEach((t) => activeTools.add(t));
      } else if (opt.id === 'read_only') {
        activeTools.clear();
        CODER_PRESETS.read_only.forEach((t) => activeTools.add(t));
      } else if (opt.id === 'all') {
        activeTools.clear();
        CODER_PRESETS.all.forEach((t) => activeTools.add(t));
      }
      toolsBox.querySelectorAll('input[type="checkbox"][data-tool]').forEach((cb) => {
        cb.checked = activeTools.has(cb.dataset.tool);
      });
      updatePresetButtons();
      markDirty();
      if (node.type.startsWith('transform.')) triggerLivePreview(node);
    };

    presetButtons[opt.id] = btn;
    presetsBox.appendChild(btn);
  });

  container.appendChild(presetsBox);

  CODER_TOOLS_SPEC.forEach((cat, catIdx) => {
    const catHead = document.createElement('div');
    catHead.style.fontSize = '0.72rem';
    catHead.style.fontWeight = '600';
    catHead.style.textTransform = 'uppercase';
    catHead.style.letterSpacing = '0.04em';
    catHead.style.color = 'var(--text-muted)';
    catHead.style.margin = catIdx === 0 ? '2px 0 4px' : '10px 0 4px';
    catHead.style.display = 'flex';
    catHead.style.alignItems = 'center';
    catHead.style.justifyContent = 'space-between';

    const catTitle = document.createElement('span');
    catTitle.textContent = `${cat.icon} ${t(cat.category)}`;
    catHead.appendChild(catTitle);

    const catToggle = document.createElement('button');
    catToggle.type = 'button';
    catToggle.textContent = t('coder.toggle');
    catToggle.style.background = 'none';
    catToggle.style.border = 'none';
    catToggle.style.color = 'var(--primary)';
    catToggle.style.fontSize = '0.68rem';
    catToggle.style.cursor = 'pointer';
    catToggle.style.padding = '0';
    catToggle.onclick = () => {
      const allInCatChecked = cat.tools.every((t) => activeTools.has(t.id));
      cat.tools.forEach((t) => {
        if (allInCatChecked) activeTools.delete(t.id);
        else activeTools.add(t.id);
      });
      toolsBox.querySelectorAll('input[type="checkbox"][data-tool]').forEach((cb) => {
        if (cat.tools.some((t) => t.id === cb.dataset.tool)) {
          cb.checked = activeTools.has(cb.dataset.tool);
        }
      });
      updatePresetButtons();
      markDirty();
      if (node.type.startsWith('transform.')) triggerLivePreview(node);
    };
    catHead.appendChild(catToggle);
    toolsBox.appendChild(catHead);

    cat.tools.forEach((tool) => {
      const row = document.createElement('label');
      row.style.display = 'flex';
      row.style.alignItems = 'flex-start';
      row.style.gap = '8px';
      row.style.padding = '3px 0';
      row.style.fontSize = '0.78rem';
      row.style.cursor = 'pointer';

      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.dataset.tool = tool.id;
      cb.checked = activeTools.has(tool.id);
      cb.style.marginTop = '2px';
      cb.onchange = (e) => {
        if (e.target.checked) activeTools.add(tool.id);
        else activeTools.delete(tool.id);
        updatePresetButtons();
        markDirty();
        if (node.type.startsWith('transform.')) triggerLivePreview(node);
      };

      const info = document.createElement('div');
      info.innerHTML = `
        <div style="font-weight: 500; line-height: 1.2;">${escapeHtml(t(tool.name))}</div>
        <div style="font-size: 0.70rem; color: var(--text-muted); line-height: 1.2;">${escapeHtml(t(tool.desc))}</div>
      `;

      row.appendChild(cb);
      row.appendChild(info);
      toolsBox.appendChild(row);
    });
  });

  container.appendChild(toolsBox);
  updatePresetButtons();
  return container;
}

function renderNodeInspector(node, body) {
  const spec = state.catalog.node_types[node.type] || { required: [], optional: {}, help: {} };
  body.innerHTML = '';

  // ID y Etiqueta
  body.appendChild(createField(t('inspector.node_id'), node.id, (val) => {
    const clean = val.replace(/[^a-zA-Z0-9_]/g, '');
    if (clean && clean !== node.id) {
      state.graph.edges.forEach((e) => {
        if (e.source === node.id) e.source = clean;
        if (e.target === node.id) e.target = clean;
      });
      node.id = clean;
      state.selectedNodeId = clean;
      markDirty();
      render();
    }
  }));

  body.appendChild(createField(t('inspector.label'), node.label, (val) => {
    node.label = val;
    markDirty();
    renderNodes();
  }));

  // Botón para ejecutar la cadena hasta este nodo
  const runToBtn = document.createElement('button');
  runToBtn.className = 'btn sm secondary';
  runToBtn.style.marginBottom = '14px';
  runToBtn.style.width = '100%';
  runToBtn.style.display = 'flex';
  runToBtn.style.alignItems = 'center';
  runToBtn.style.justifyContent = 'center';
  runToBtn.style.gap = '6px';
  runToBtn.innerHTML = `<svg class="ic" width="13" height="13"><use href="#i-play"/></svg> <span>${t('inspector.run_to_node')}</span>`;
  runToBtn.onclick = () => runToNode(node.id);
  body.appendChild(runToBtn);

  // Botón para editar código si es script
  const codeField = spec.code_field || (node.type === 'transform.script' ? 'code' : null);
  if (codeField) {
    const codeLines = (node.config[codeField] || '').split('\n').filter((l) => l.trim()).length;
    const btnBox = document.createElement('div');
    btnBox.className = 'field-group';
    btnBox.style.marginBottom = '14px';
    btnBox.innerHTML = `<label>${t('inspector.python_code')}</label>`;

    const btnCode = document.createElement('button');
    btnCode.className = 'btn primary';
    btnCode.style.display = 'flex';
    btnCode.style.alignItems = 'center';
    btnCode.style.justifyContent = 'center';
    btnCode.style.gap = '6px';
    btnCode.innerHTML = `<svg class="ic" width="14" height="14"><use href="#i-code"/></svg> <span>${codeLines ? t('inspector.edit_code_lines', { n: codeLines }) : t('inspector.write_code')}</span>`;
    btnCode.onclick = () => openCodeEditor(node);
    btnBox.appendChild(btnCode);
    body.appendChild(btnBox);
  }

  // Ayuda interactiva para Webhook Entrante
  if (node.type === 'input.webhook') {
    const flowId = state.graph.name || t('flow.webhook_example');
    const hookUrl = `${window.location.origin}/api/webhook/${flowId}`;
    const box = document.createElement('div');
    box.className = 'field-group';
    box.style.background = 'var(--bg-subtle)';
    box.style.border = '1px solid var(--border)';
    box.style.borderRadius = '6px';
    box.style.padding = '8px 10px';
    box.style.marginBottom = '14px';
    box.innerHTML = `
      <label style="color: var(--primary); font-weight: 600;">${t('inspector.webhook_url')}</label>
      <div style="display: flex; gap: 6px; margin-top: 4px;">
        <input type="text" readonly value="${escapeHtml(hookUrl)}" style="font-family: var(--mono); font-size: 0.72rem; flex: 1; padding: 4px 6px;" />
        <button class="btn sm" id="btnCopyWebhook" title="${t('inspector.copy_url_title')}">${t('common.copy')}</button>
      </div>
      <span class="field-help" style="margin-top: 4px;">${t('inspector.webhook_help')}</span>
    `;
    const btn = box.querySelector('#btnCopyWebhook');
    btn.onclick = () => {
      navigator.clipboard.writeText(hookUrl);
      toast(t('toast.webhook_copied'));
    };
    body.appendChild(box);
  }

  // Campos de configuración
  const allFields = { ...spec.optional, ...node.config };
  spec.required.forEach((rf) => {
    if (!(rf in allFields)) allFields[rf] = '';
  });

  Object.entries(allFields).forEach(([key, defaultVal]) => {
    if (key === 'fan_in' || key === codeField || (codeField && key === 'code')) return;

    if (node.type === 'transform.coder' && (key === 'tools_preset' || key === 'custom_tools')) {
      if (key === 'tools_preset') {
        body.appendChild(createCoderToolsSection(node));
      }
      return;
    }

    // Comprobar show_when
    if (spec.show_when && spec.show_when[key]) {
      let visible = false;
      Object.entries(spec.show_when[key]).forEach(([reqField, validValues]) => {
        if (validValues.includes(node.config[reqField])) visible = true;
      });
      if (!visible) return;
    }

    const currentVal = node.config[key] !== undefined ? node.config[key] : defaultVal;
    let inputEl;

    // Desplegable de opciones (choices o choices_from)
    const choices = spec.choices && spec.choices[key];
    const choicesFrom = spec.choices_from && spec.choices_from[key];

    if (choices) {
      inputEl = document.createElement('select');
      choices.forEach((c) => {
        const opt = document.createElement('option');
        if (typeof c === 'object') {
          opt.value = c.value;
          opt.textContent = c.label;
        } else {
          opt.value = c;
          opt.textContent = c;
        }
        opt.selected = String(opt.value) === String(currentVal);
        inputEl.appendChild(opt);
      });
      inputEl.onchange = (e) => {
        node.config[key] = e.target.value;
        markDirty();
        renderInspector();
      };
    } else if (choicesFrom === 'secrets') {
      inputEl = document.createElement('select');
      inputEl.innerHTML = `<option value="">${t('inspector.no_secret')}</option>`;
      state.secrets.forEach((s) => {
        const opt = document.createElement('option');
        opt.value = s;
        opt.textContent = s;
        opt.selected = s === currentVal;
        inputEl.appendChild(opt);
      });
      inputEl.onchange = (e) => {
        node.config[key] = e.target.value;
        markDirty();
        if (node.type.startsWith('transform.')) triggerLivePreview(node);
      };
    } else if (choicesFrom === 'llms') {
      inputEl = document.createElement('select');
      inputEl.innerHTML = `<option value="">${t('inspector.llm_default')}</option>`;
      const catalogLlms = (state.catalog && state.catalog.choices && state.catalog.choices.llms) || [];
      if (catalogLlms.length > 0) {
        catalogLlms.forEach((m) => {
          const opt = document.createElement('option');
          opt.value = m.value;
          opt.textContent = m.label || m.value;
          opt.selected = String(opt.value) === String(currentVal);
          inputEl.appendChild(opt);
        });
      } else {
        (state.llms || []).forEach((m) => {
          const opt = document.createElement('option');
          const val = typeof m === 'string' ? m : m.name;
          const label = typeof m === 'string' ? m : `${m.name} (${m.model || ''})`;
          opt.value = val;
          opt.textContent = label;
          opt.selected = String(opt.value) === String(currentVal);
          inputEl.appendChild(opt);
        });
      }
      inputEl.onchange = (e) => {
        node.config[key] = e.target.value;
        markDirty();
        if (node.type.startsWith('transform.')) triggerLivePreview(node);
      };
    } else if (typeof defaultVal === 'boolean') {
      inputEl = document.createElement('input');
      inputEl.type = 'checkbox';
      inputEl.checked = Boolean(currentVal);
      inputEl.onchange = (e) => {
        node.config[key] = e.target.checked;
        markDirty();
        if (node.type.startsWith('transform.')) triggerLivePreview(node);
      };
    } else {
      const multilineKeys = ['template', 'query', 'cmd', 'prompt', 'system_prompt', 'body', 'headers', 'message'];
      const isMultiline = multilineKeys.includes(key) || (typeof defaultVal === 'string' && (defaultVal.length > 40 || String(currentVal).includes('\n')));
      inputEl = document.createElement('textarea');
      inputEl.rows = ['template', 'query', 'prompt', 'system_prompt', 'body'].includes(key) ? 4 : (isMultiline ? 2 : 1);
      if (['template', 'query', 'cmd', 'body', 'headers'].includes(key)) {
        inputEl.style.fontFamily = 'var(--mono)';
        inputEl.style.fontSize = '0.8rem';
      }
      inputEl.value = currentVal;
      inputEl.oninput = (e) => {
        node.config[key] = e.target.value;
        markDirty();
        if (node.type.startsWith('transform.')) triggerLivePreview(node);
      };
    }

    const grp = document.createElement('div');
    grp.className = 'field-group';
    const lbl = document.createElement('label');
    lbl.textContent = key + (spec.required.includes(key) ? ' *' : '');
    grp.appendChild(lbl);
    grp.appendChild(inputEl);

    if (spec.help && spec.help[key]) {
      const h = document.createElement('span');
      h.className = 'field-help';
      h.textContent = spec.help[key];
      grp.appendChild(h);
    }
    body.appendChild(grp);
  });

  // Si es un nodo de transformación, añadir el panel de Vista Previa en Vivo
  if (node.type.startsWith('transform.')) {
    renderLivePreviewSection(node, spec, body);
  }

  // Sección de datos de ejecución (Entrada y Salida completas sin truncar)
  renderNodeExecutionSection(node, body);

  // Botón para eliminar nodo
  const btnDel = document.createElement('button');
  btnDel.className = 'btn sm';
  btnDel.style.marginTop = '16px';
  btnDel.style.color = 'var(--port-err)';
  btnDel.innerHTML = `<svg class="ic" width="14" height="14"><use href="#i-trash"/></svg> ${t('node.delete')}`;
  btnDel.onclick = () => removeNode(node.id);
  body.appendChild(btnDel);
}

function renderFlowInspector(body) {
  body.innerHTML = '';

  // Nombre del flujo
  const nameGrp = document.createElement('div');
  nameGrp.className = 'field-group';
  nameGrp.innerHTML = `<label>${t('inspector.flow_name')}</label>`;
  const nameInput = document.createElement('input');
  nameInput.value = state.graph.name || '';
  nameInput.placeholder = t('inspector.flow_name_placeholder');
  nameInput.oninput = (e) => {
    const slug = e.target.value.trim().toLowerCase().replace(/[^a-z0-9_-]/g, '-');
    state.graph.name = slug;
    state.meta.name = slug;
    $('flowName').value = slug;
    markDirty();
  };
  nameGrp.appendChild(nameInput);
  body.appendChild(nameGrp);

  // Descripción
  const descGrp = document.createElement('div');
  descGrp.className = 'field-group';
  descGrp.innerHTML = `<label>${t('inspector.description')}</label>`;
  const descInput = document.createElement('textarea');
  descInput.rows = 2;
  descInput.value = state.meta.description || '';
  descInput.placeholder = t('inspector.description_placeholder');
  descInput.oninput = (e) => {
    state.meta.description = e.target.value;
    markDirty();
  };
  descGrp.appendChild(descInput);
  body.appendChild(descGrp);

  // Sección de Estado y Programación
  const secSched = document.createElement('div');
  secSched.className = 'inspector-section';
  secSched.textContent = t('inspector.sched_section');
  body.appendChild(secSched);

  // Checkbox: Flujo activo
  const enabledGrp = document.createElement('div');
  enabledGrp.className = 'field-group';
  enabledGrp.style.flexDirection = 'row';
  enabledGrp.style.alignItems = 'center';
  enabledGrp.style.justifyContent = 'space-between';
  const enabledLbl = document.createElement('label');
  enabledLbl.style.margin = '0';
  enabledLbl.style.cursor = 'pointer';
  enabledLbl.textContent = t('inspector.flow_active');
  const enabledInput = document.createElement('input');
  enabledInput.type = 'checkbox';
  enabledInput.checked = state.meta.enabled !== false;
  enabledInput.onchange = (e) => {
    state.meta.enabled = e.target.checked;
    markDirty();
    renderFlowsList();
  };
  enabledLbl.onclick = () => enabledInput.click();
  enabledGrp.appendChild(enabledLbl);
  enabledGrp.appendChild(enabledInput);
  body.appendChild(enabledGrp);

  // Presets de periodicidad
  const presetGrp = document.createElement('div');
  presetGrp.className = 'field-group';
  presetGrp.innerHTML = `<label>${t('inspector.frequency')}</label>`;
  const presetSelect = document.createElement('select');
  const presets = [
    { value: '', label: t('inspector.freq_manual') },
    { value: '* * * * *', label: t('inspector.freq_1m') },
    { value: '*/5 * * * *', label: t('inspector.freq_5m') },
    { value: '*/15 * * * *', label: t('inspector.freq_15m') },
    { value: '0 * * * *', label: t('inspector.freq_1h') },
    { value: '0 9 * * *', label: t('inspector.freq_daily') },
    { value: 'custom', label: t('inspector.freq_custom') },
  ];
  const currSched = state.meta.schedule || '';
  let matchedPreset = false;
  presets.forEach((p) => {
    const opt = document.createElement('option');
    opt.value = p.value;
    opt.textContent = p.label;
    if (currSched === p.value) {
      opt.selected = true;
      matchedPreset = true;
    }
    presetSelect.appendChild(opt);
  });
  if (!matchedPreset && currSched) {
    presetSelect.value = 'custom';
  }
  presetGrp.appendChild(presetSelect);
  body.appendChild(presetGrp);

  // Input de expresión Cron
  const cronGrp = document.createElement('div');
  cronGrp.className = 'field-group';
  cronGrp.innerHTML = `<label>${t('inspector.cron_expr')}</label>`;
  const cronInput = document.createElement('input');
  cronInput.value = currSched;
  cronInput.placeholder = t('inspector.cron_placeholder');
  cronInput.style.display = (currSched || presetSelect.value === 'custom') ? 'block' : 'none';
  cronInput.oninput = (e) => {
    state.meta.schedule = e.target.value.trim();
    markDirty();
  };
  const cronHelp = document.createElement('span');
  cronHelp.className = 'field-help';
  cronHelp.textContent = t('inspector.cron_help');
  cronHelp.style.display = cronInput.style.display;
  cronGrp.appendChild(cronInput);
  cronGrp.appendChild(cronHelp);
  body.appendChild(cronGrp);

  presetSelect.onchange = (e) => {
    if (e.target.value === 'custom') {
      cronInput.style.display = 'block';
      cronHelp.style.display = 'block';
    } else {
      state.meta.schedule = e.target.value;
      cronInput.value = e.target.value;
      cronInput.style.display = e.target.value ? 'block' : 'none';
      cronHelp.style.display = e.target.value ? 'block' : 'none';
      markDirty();
    }
  };

  // Resumen del flujo
  const secStats = document.createElement('div');
  secStats.className = 'inspector-section';
  secStats.textContent = t('inspector.graph_section');
  body.appendChild(secStats);

  const statsBox = document.createElement('div');
  statsBox.innerHTML = `
    <div class="flow-stats-row">
      <span>${t('inspector.stat_nodes')}</span>
      <span class="flow-stats-val">${state.graph.nodes.length}</span>
    </div>
    <div class="flow-stats-row">
      <span>${t('inspector.stat_edges')}</span>
      <span class="flow-stats-val">${state.graph.edges.length}</span>
    </div>
    <div class="flow-stats-row">
      <span>${t('inspector.stat_state')}</span>
      <span class="flow-stats-val" style="color: ${state.isDirty ? 'var(--port-err)' : 'var(--port-ok)'}">${state.isDirty ? t('toolbar.unsaved') : t('inspector.up_to_date')}</span>
    </div>
  `;
  body.appendChild(statsBox);

  // Acciones rápidas
  const secActions = document.createElement('div');
  secActions.className = 'inspector-section';
  secActions.textContent = t('inspector.actions');
  body.appendChild(secActions);

  const actBox = document.createElement('div');
  actBox.style.display = 'flex';
  actBox.style.flexDirection = 'column';
  actBox.style.gap = '8px';
  actBox.style.marginTop = '8px';

  const btnSave = document.createElement('button');
  btnSave.className = 'btn primary sm';
  btnSave.innerHTML = `<svg class="ic" width="13" height="13"><use href="#i-save"/></svg> ${t('inspector.save_flow')}`;
  btnSave.onclick = saveFlow;

  const btnRun = document.createElement('button');
  btnRun.className = 'btn secondary sm';
  btnRun.innerHTML = `<svg class="ic" width="13" height="13"><use href="#i-play"/></svg> ${t('inspector.test_run')}`;
  btnRun.onclick = runPreview;

  const btnAdvSched = document.createElement('button');
  btnAdvSched.className = 'btn secondary sm';
  btnAdvSched.innerHTML = `<svg class="ic" width="13" height="13"><use href="#i-clock"/></svg> ${t('inspector.adv_schedule')}`;
  btnAdvSched.onclick = () => openScheduleModal(state.graph.name);

  const btnExportZip = document.createElement('button');
  btnExportZip.className = 'btn secondary sm';
  btnExportZip.innerHTML = `<svg class="ic" width="13" height="13"><use href="#i-download"/></svg> ${t('inspector.export_zip')}`;
  btnExportZip.onclick = () => exportFlowZip(state.graph.name);

  const btnExportPy = document.createElement('button');
  btnExportPy.className = 'btn secondary sm';
  btnExportPy.innerHTML = `<svg class="ic" width="13" height="13"><use href="#i-download"/></svg> ${t('inspector.export_py')}`;
  btnExportPy.onclick = () => exportFlowPy(state.graph.name);

  const btnDelFlow = document.createElement('button');
  btnDelFlow.className = 'btn secondary sm';
  btnDelFlow.style.color = 'var(--port-err)';
  btnDelFlow.style.borderColor = 'rgba(239, 68, 68, 0.35)';
  btnDelFlow.style.marginTop = '6px';
  btnDelFlow.innerHTML = `<svg class="ic" width="13" height="13"><use href="#i-trash"/></svg> ${t('inspector.delete_flow')}`;
  btnDelFlow.onclick = () => deleteFlow(state.graph.name);

  actBox.appendChild(btnSave);
  actBox.appendChild(btnRun);
  actBox.appendChild(btnAdvSched);
  actBox.appendChild(btnExportZip);
  actBox.appendChild(btnExportPy);
  actBox.appendChild(btnDelFlow);
  body.appendChild(actBox);
}

function createField(label, value, onChange) {
  const grp = document.createElement('div');
  grp.className = 'field-group';
  const lbl = document.createElement('label');
  lbl.textContent = label;
  const input = document.createElement('input');
  input.value = value || '';
  input.oninput = (e) => onChange(e.target.value);
  grp.appendChild(lbl);
  grp.appendChild(input);
  return grp;
}

// ---------------------------------------------------------------------------
// Editor de Scripts Python para nodos `transform.script` con CodeMirror
// ---------------------------------------------------------------------------

let codeMirrorInstance = null;

function openCodeEditor(node) {
  state.editingScriptNodeId = node.id;
  const spec = state.catalog.node_types[node.type];
  const codeField = (spec && spec.code_field) || 'code';

  $('codeNodeId').textContent = node.id;
  $('codeSheet').hidden = false;

  const currentCode = node.config[codeField] !== undefined
    ? node.config[codeField]
    : defaultScript();

  // Inicializar CodeMirror si no existe aún
  if (!codeMirrorInstance) {
    const editorDiv = $('codeEditor');
    editorDiv.innerHTML = '';
    if (typeof CodeMirror !== 'undefined') {
      codeMirrorInstance = CodeMirror(editorDiv, {
        value: currentCode,
        mode: 'python',
        theme: 'dracula',
        lineNumbers: true,
        indentUnit: 4,
        tabSize: 4,
        indentWithTabs: false,
        lineWrapping: true,
        autofocus: true,
        extraKeys: {
          'Tab': (cm) => cm.execCommand('insertSoftTab'),
          'Cmd-Enter': () => saveScriptCode(),
          'Ctrl-Enter': () => saveScriptCode(),
        },
      });
    }
  } else {
    codeMirrorInstance.setValue(currentCode);
  }

  // Refrescar CodeMirror tras mostrar el modal para calcular las dimensiones correctamente
  if (codeMirrorInstance) {
    setTimeout(() => {
      codeMirrorInstance.refresh();
      codeMirrorInstance.focus();
    }, 20);
  }

  renderCodeEditorInputs(node);
}

function renderCodeEditorInputs(node) {
  const inputsPanel = $('codeInputs');
  inputsPanel.innerHTML = '';

  const head = document.createElement('h3');
  head.textContent = t('code.input_vars');
  inputsPanel.appendChild(head);

  const incoming = state.graph.edges.filter((e) => e.target === node.id);
  if (!incoming.length) {
    const hint = document.createElement('p');
    hint.className = 'hint';
    hint.textContent = t('code.connect_hint');
    inputsPanel.appendChild(hint);
  } else {
    incoming.forEach((edge) => {
      const srcNode = state.graph.nodes.find((n) => n.id === edge.source);
      const srcSpec = srcNode ? state.catalog.node_types[srcNode.type] : null;

      const card = document.createElement('div');
      card.className = 'wired';

      const kind = document.createElement('span');
      kind.className = 'kind';
      kind.textContent = t('code.port_kind', { label: srcSpec ? srcSpec.label : (srcNode ? srcNode.type : '?'), port: edge.port });

      const codeChip = document.createElement('code');
      codeChip.textContent = `entrada["${edge.source}"]`;
      codeChip.title = t('code.insert_title');
      codeChip.onclick = () => {
        if (codeMirrorInstance) {
          codeMirrorInstance.replaceSelection(`entrada["${edge.source}"]`);
          codeMirrorInstance.focus();
        }
      };

      const what = document.createElement('span');
      what.className = 'what';
      what.textContent = srcSpec && srcSpec.output ? t('code.returns', { output: srcSpec.output }) : t('code.node_output');

      card.appendChild(kind);
      card.appendChild(codeChip);
      card.appendChild(what);
      inputsPanel.appendChild(card);
    });
  }

  // Cheatsheet de métodos útiles de entrada
  const cheatsheet = document.createElement('div');
  cheatsheet.className = 'code-cheatsheet';
  cheatsheet.innerHTML = `
    <strong style="color: var(--text); display: block; margin-bottom: 4px;">${t('code.shortcuts')}</strong>
    <div>▸ <code title="${t('code.tip_text')}">entrada.text()</code></div>
    <div>▸ <code title="${t('code.tip_first')}">entrada.first</code></div>
    <div>▸ <code title="${t('code.tip_dict')}">entrada.get("id")</code></div>
    <div>▸ <code title="${t('code.tip_list')}">list(entrada)</code></div>
    <p style="margin-top: 6px; font-size: 0.7rem; color: var(--text-muted); line-height: 1.3;">
      ${t('code.return_hint', { code: '<code style="cursor: default;">return</code>' })}<br/>
      <span style="opacity: 0.8;">${t('code.apply_hint')}</span>
    </p>
  `;
  cheatsheet.querySelectorAll('code').forEach((el) => {
    if (el.title) {
      el.onclick = () => {
        if (codeMirrorInstance) {
          codeMirrorInstance.replaceSelection(el.textContent);
          codeMirrorInstance.focus();
        }
      };
    }
  });
  inputsPanel.appendChild(cheatsheet);
}

function saveScriptCode() {
  const node = state.graph.nodes.find((n) => n.id === state.editingScriptNodeId);
  if (!node) {
    $('codeSheet').hidden = true;
    return;
  }

  const spec = state.catalog.node_types[node.type];
  const codeField = (spec && spec.code_field) || 'code';
  const val = codeMirrorInstance ? codeMirrorInstance.getValue() : '';

  node.config[codeField] = val;
  markDirty();
  $('codeSheet').hidden = true;
  renderNodes();
  renderInspector();
  toast(t('toast.code_updated', { id: node.id }));
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function escapeHtml(str) {
  return String(str || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function exportFlowZip(flowId) {
  const fid = (flowId || state.graph.name || '').trim();
  if (!fid) {
    toast(t('toast.save_first'), 'err');
    return;
  }
  const link = document.createElement('a');
  link.href = `/api/flow/export?id=${encodeURIComponent(fid)}`;
  link.download = `${fid}.zip`;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  toast(t('toast.downloading', { file: `${fid}.zip` }), 'ok');
}

function exportFlowPy(flowId) {
  const fid = (flowId || state.graph.name || '').trim();
  if (!fid) {
    toast(t('toast.save_first'), 'err');
    return;
  }
  const link = document.createElement('a');
  link.href = `/api/flow/export-py?id=${encodeURIComponent(fid)}`;
  link.download = `${fid}.py`;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  toast(t('toast.downloading', { file: `${fid}.py` }), 'ok');
}

function exportAllFlowsZip() {
  const link = document.createElement('a');
  link.href = '/api/flows/export-all';
  link.download = 'dedalo_flows_backup.zip';
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  toast(t('toast.downloading_backup'), 'ok');
}

async function handleImportZip(file) {
  if (!file) return;
  const formData = new FormData();
  formData.append('file', file);
  toast(t('toast.importing'), 'info');
  try {
    const res = await fetch('/api/flows/import', {
      method: 'POST',
      body: formData,
    });
    const data = await res.json();
    if (!res.ok || data.status === 'error') {
      toast(data.detail || data.error || t('error.import'), 'err');
      return;
    }
    const count = data.imported_count || (data.imported ? data.imported.length : 0);
    toast(t('toast.imported', { n: count }), 'ok');
    if (data.required_secrets && data.required_secrets.length > 0) {
      setTimeout(() => {
        toast(t('toast.import_secrets', { names: data.required_secrets.join(', ') }), 'info');
      }, 2500);
    }
    await loadFlows();
    if (count === 1 && data.imported && data.imported[0]) {
      loadFlow(data.imported[0].id);
    }
  } catch (err) {
    toast(t('error.connection', { message: err.message }), 'err');
  }
}

window.onload = init;
