/**
 * Dédalo Standalone — Lógica del cliente visual de grafos.
 */

'use strict';

const $ = (id) => document.getElementById(id);

const state = {
  graph: { name: 'nuevo-flujo', nodes: [], edges: [], version: 1 },
  meta: { name: 'nuevo-flujo', description: '', schedule: '', enabled: true },
  selectedNodeId: null,
  catalog: { node_types: {}, choices: { secrets: [] } },
  templates: [],
  flows: [],
  secrets: [],
  isDirty: false,
  activeTab: 'flows',
  // Transformación y navegación 2D del lienzo (Pan y Zoom)
  view: { k: 1, x: 0, y: 0 },
  isPanning: false,
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

async function init() {
  setupTheme();
  setupInspector();
  setupEvents();
  await loadCatalog();
  await loadTemplates();
  await loadFlows();
  await loadSecrets();
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
      btnToggle.setAttribute('title', 'Mostrar panel lateral (Ajustes)');
    }
    if (save) localStorage.setItem('dedalo_inspector_collapsed', 'true');
  } else {
    inspector.classList.remove('collapsed');
    if (btnExpand) btnExpand.hidden = true;
    if (btnToggle) {
      btnToggle.classList.add('active');
      btnToggle.setAttribute('title', 'Ocultar panel lateral (Ajustes)');
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

function markDirty() {
  state.isDirty = true;
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
  $('unsavedFlowName').textContent = state.graph.name || 'sin nombre';
  $('unsavedSheet').hidden = false;
}

function closeUnsavedModal() {
  $('unsavedSheet').hidden = true;
  unsavedPendingAction = null;
  const picker = $('templatePicker');
  if (picker) picker.value = '';
}

function onUnsavedDiscard() {
  state.isDirty = false;
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
  toast('Zoom restablecido (100%)');
}

function centerFlow() {
  if (!state.graph.nodes.length) {
    state.view.x = 0;
    state.view.y = 0;
    state.view.k = 1.0;
    updateTransform();
    toast('Lienzo centrado.');
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
  toast('Flujo centrado.');
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

  $('btnNew').onclick = () => withUnsavedCheck(() => resetGraph('nuevo-flujo'));
  $('btnSidebarNewFlow').onclick = () => withUnsavedCheck(() => resetGraph('nuevo-flujo'));
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
  if ($('btnDebugFlow')) $('btnDebugFlow').onclick = startDebugFlow;
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
      toast('Nodos resaltados en el lienzo según el resultado de esta ejecución.');
    }
  };
  if ($('btnHistClearHighlight')) $('btnHistClearHighlight').onclick = () => {
    clearRunHighlights();
    toast('Resaltado eliminado.');
  };

  $('unsavedClose').onclick = () => closeUnsavedModal();
  $('btnUnsavedCancel').onclick = () => closeUnsavedModal();
  $('btnUnsavedDiscard').onclick = onUnsavedDiscard;
  $('btnUnsavedSave').onclick = onUnsavedSave;

  $('btnSaveSecret').onclick = addSecret;
  $('btnSaveConfig').onclick = saveConfig;

  if ($('paletteSearch')) {
    $('paletteSearch').addEventListener('input', () => renderPalette());
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
}

// ---------------------------------------------------------------------------
// Carga de Datos desde la API
// ---------------------------------------------------------------------------

async function loadCatalog() {
  try {
    const res = await fetch('/api/catalog');
    state.catalog = await res.json();
    renderPalette();
  } catch (e) {
    toast('Error al cargar catálogo de nodos');
  }
}

async function loadTemplates() {
  try {
    const res = await fetch('/api/templates');
    state.templates = await res.json();
    const picker = $('templatePicker');
    picker.innerHTML = '<option value="">— Cargar plantilla —</option>';
    state.templates.forEach((t) => {
      const opt = document.createElement('option');
      opt.value = t.name;
      opt.textContent = t.title;
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
    toast('Error al cargar lista de flujos');
  }
}

async function loadSecrets() {
  try {
    const res = await fetch('/api/secrets');
    state.secrets = await res.json();
    renderSecretsList();
  } catch (e) {}
}

async function loadConfig() {
  try {
    const res = await fetch('/api/config');
    const cfg = await res.json();
    $('cfgLlmUrl').value = cfg.llm_url || '';
    $('cfgLlmModel').value = cfg.llm_model || '';
    if (cfg.has_llm_key) {
      $('cfgLlmKey').placeholder = '•••••••• (guardada)';
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
    if (res.ok) toast('Configuración guardada.');
  } catch (e) {
    toast('Error al guardar configuración');
  }
}

// ---------------------------------------------------------------------------
// Renderizado de la Barra Lateral: Flujos, Nodos, Secretos
// ---------------------------------------------------------------------------

function renderFlowsList() {
  const list = $('flowsList');
  list.innerHTML = '';
  if (!state.flows.length) {
    list.innerHTML = '<p class="hint">No hay flujos guardados todavía.</p>';
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
    const badgeText = isEnabled ? 'Activo' : 'Pausado';

    const schedText = fl.schedule ? `⏰ ${fl.schedule}` : 'Manual';
    const nextText = fl.next_run ? `<span class="hint-inline">(${fl.next_run.slice(11, 16)})</span>` : '';

    const info = document.createElement('div');
    info.className = 'flow-info';
    info.innerHTML = `
      <span class="flow-title">${escapeHtml(fl.name || fl.id)}</span>
      <div class="flow-meta-line">
        <span class="${badgeClass}" title="Alternar activo/pausado">${badgeText}</span>
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
    btnRunNow.title = 'Ejecutar ahora en background';
    btnRunNow.innerHTML = '<svg class="ic" width="12" height="12"><use href="#i-play"/></svg>';
    btnRunNow.onmousedown = (e) => e.stopPropagation();
    btnRunNow.onclick = async (e) => {
      e.stopPropagation();
      await runFlowNow(fl.id);
    };

    const btnSched = document.createElement('button');
    btnSched.className = 'btn sm icon-btn';
    btnSched.title = 'Programar periodicidad';
    btnSched.innerHTML = '<svg class="ic" width="12" height="12"><use href="#i-clock"/></svg>';
    btnSched.onmousedown = (e) => e.stopPropagation();
    btnSched.onclick = (e) => {
      e.stopPropagation();
      openScheduleModal(fl.id);
    };

    const btnDel = document.createElement('button');
    btnDel.className = 'btn sm icon-btn';
    btnDel.title = 'Eliminar flujo';
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
  currentSchedFlowId = flowId || state.graph.name || 'nuevo-flujo';
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

  $('schedStatusVal').textContent = fl.enabled !== false ? (fl.schedule ? 'Programado' : 'Manual') : 'Pausado';
  $('schedNextVal').textContent = fl.next_run || (fl.schedule ? 'Activo (esperando cálculo)' : 'Ninguna');
  const lastRun = fl.last_run ? `${new Date(fl.last_run.last_run * 1000).toLocaleTimeString()} (${fl.last_run.status})` : 'Nunca';
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
    toast(`Programación de «${currentSchedFlowId}» guardada.`);
    $('scheduleSheet').hidden = true;
    await loadFlows();
    if (state.graph.name === currentSchedFlowId) {
      state.meta.schedule = schedule;
      state.meta.enabled = enabled;
    }
  } catch (e) {
    toast('Error al guardar programación.');
  }
}

async function toggleFlow(flowId) {
  try {
    const res = await fetch(`/api/flow/toggle?id=${encodeURIComponent(flowId)}`, { method: 'POST' });
    if (res.ok) {
      await loadFlows();
      toast(`Estado de «${flowId}» actualizado.`);
    }
  } catch (e) {
    toast('Error al cambiar estado.');
  }
}

async function runFlowNow(flowId) {
  toast(`Ejecutando «${flowId}» en background...`);
  try {
    const res = await fetch(`/api/flow/run_now?id=${encodeURIComponent(flowId)}`, { method: 'POST' });
    const data = await res.json();
    if (res.ok) {
      toast(`«${flowId}» completado con éxito en ${data.duration}s.`);
      await loadFlows();
      if (flowId === state.graph.name && data.result) {
        highlightRunOnCanvas(data.result);
      }
      if (state.activeTab === 'history') {
        loadHistory();
      }
    } else {
      toast(`Error en «${flowId}»: ${data.detail || 'Fallo'}`);
    }
  } catch (e) {
    toast('Fallo de conexión al disparar flujo.');
  }
}

async function runScheduleModalNow() {
  if (!currentSchedFlowId) return;
  $('schedStatusVal').textContent = 'Ejecutando...';
  await runFlowNow(currentSchedFlowId);
  const fl = state.flows.find((f) => f.id === currentSchedFlowId);
  if (fl) {
    $('schedStatusVal').textContent = fl.last_run ? fl.last_run.status : 'Listo';
    $('schedLastVal').textContent = fl.last_run ? `${fl.last_run.duration}s` : '-';
  }
}

function renderPalette() {
  const p = $('palette');
  p.innerHTML = '';
  const categories = { input: 'Entrada', transform: 'Transformación', output: 'Salida' };
  const query = ($('paletteSearch') && $('paletteSearch').value || '').trim().toLowerCase();
  let totalMatched = 0;

  Object.entries(categories).forEach(([catKey, catTitle]) => {
    const block = document.createElement('div');
    block.className = `category-block cat-${catKey}`;
    block.innerHTML = `<h4>${catTitle}</h4>`;
    let catMatched = 0;

    Object.entries(state.catalog.node_types || {}).forEach(([typeKey, spec]) => {
      if (spec.category !== catKey) return;
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
      catMatched++;
      totalMatched++;

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
      block.appendChild(pill);
    });

    if (catMatched > 0 || !query) {
      p.appendChild(block);
    }
  });

  if (query && totalMatched === 0) {
    p.innerHTML = `<p class="hint" style="text-align: center; padding: 1.5rem 0.5rem;">No hay nodos que coincidan con «${escapeHtml(query)}»</p>`;
  }
}

function renderSecretsList() {
  const list = $('secretsList');
  list.innerHTML = '';
  if (!state.secrets.length) {
    list.innerHTML = '<p class="hint">No hay secretos registrados.</p>';
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
    toast('Nombre y valor requeridos.');
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
      toast(`Secreto «${name}» guardado.`);
      await loadSecrets();
      await loadCatalog();
      renderInspector();
    }
  } catch (e) {
    toast('Error al guardar secreto');
  }
}

async function removeSecret(name) {
  if (!confirm(`¿Eliminar secreto «${name}»?`)) return;
  try {
    await fetch(`/api/secrets?name=${encodeURIComponent(name)}`, { method: 'DELETE' });
    toast(`Secreto «${name}» eliminado.`);
    await loadSecrets();
    await loadCatalog();
    renderInspector();
  } catch (e) {
    toast('Error al eliminar secreto');
  }
}

// ---------------------------------------------------------------------------
// Historial de Ejecuciones y Resultados Paso a Paso
// ---------------------------------------------------------------------------

let currentRunDetail = null;

async function loadHistory() {
  const list = $('historyList');
  if (!list) return;
  list.innerHTML = '<p class="hint">Cargando historial...</p>';

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
    list.innerHTML = '<p class="hint" style="color:var(--port-err);">Error al cargar el historial.</p>';
  }
}

function renderHistory(runs) {
  const list = $('historyList');
  if (!list) return;
  list.innerHTML = '';

  if (!runs || runs.length === 0) {
    list.innerHTML = '<p class="hint">No hay ejecuciones registradas.</p>';
    return;
  }

  runs.forEach((r) => {
    const card = document.createElement('div');
    card.className = `history-card ${state.selectedRunId === r.id ? 'active' : ''}`;
    card.dataset.runId = r.id;

    const dt = new Date(r.started_at * 1000);
    const dateStr = dt.toLocaleDateString('es-ES', { day: '2-digit', month: '2-digit' }) + ' ' +
                    dt.toLocaleTimeString('es-ES', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    const durStr = r.duration != null ? `${r.duration}s` : '...';
    const trigStr = r.trigger_type === 'schedule' ? 'Cron' : r.trigger_type === 'preview' ? 'Prueba' : 'Manual';
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
          <span style="color: var(--primary);">${r.step_count || 0} pasos</span>
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
      toast('No se encontró el detalle de la ejecución');
      return;
    }
    const run = await res.json();
    currentRunDetail = run;

    $('histRunId').textContent = run.id;
    const dt = new Date(run.started_at * 1000);
    const dateStr = dt.toLocaleString('es-ES');
    const trigStr = run.trigger_type === 'schedule' ? 'Programado (Cron)'
                  : run.trigger_type === 'preview' ? 'Prueba en lienzo'
                  : 'Disparo manual';

    const metaBar = $('histMetaBar');
    const statusColor = run.status === 'ok' ? 'var(--port-ok)' : 'var(--port-err)';
    metaBar.innerHTML = `
      <div><b>Flujo:</b> <span>${escapeHtml(run.flow_name || run.flow_id)}</span></div>
      <div><b>Tipo:</b> <span>${trigStr}</span></div>
      <div><b>Fecha:</b> <span>${dateStr}</span></div>
      <div><b>Duración:</b> <span>${run.duration != null ? run.duration + 's' : '-'}</span></div>
      <div><b>Estado:</b> <span style="font-weight:700; color:${statusColor};">${run.status.toUpperCase()}</span></div>
    `;

    if (run.error) {
      metaBar.innerHTML += `<div style="width: 100%; color: var(--port-err); margin-top: 4px;"><b>Error:</b> ${escapeHtml(run.error)}</div>`;
    }

    const body = $('histStepsBody');
    body.innerHTML = '';

    if (!run.steps || run.steps.length === 0) {
      body.innerHTML = '<p class="hint">No hay pasos registrados para esta ejecución.</p>';
    } else {
      run.steps.forEach((s, idx) => {
        const stepCard = document.createElement('div');
        stepCard.className = 'step-card';
        const badgeClass = s.status === 'ok' ? 'ok' : s.status === 'skipped' ? 'skipped' : 'error';
        const statusLabel = s.status === 'ok' ? 'Éxito' : s.status === 'skipped' ? 'Omitido' : 'Error';

        let inputHtml = '';
        if (s.inputs != null) {
          inputHtml = `
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px; margin-top: 6px;">
              <span class="hint" style="font-size:0.7rem; text-transform:uppercase; font-weight:600;">Entrada Recibida:</span>
              <div style="display: flex; gap: 4px;">
                <button class="icon-btn view-full-in-btn" style="font-size:0.72rem; padding: 2px 6px;" title="Ver entrada completa">🔍 Ver completo</button>
                <button class="icon-btn copy-in-btn" style="font-size:0.72rem; padding: 2px 6px;" title="Copiar entrada">Copiar</button>
              </div>
            </div>
            <pre class="step-data" style="max-height: 100px;">${escapeHtml(s.inputs)}</pre>
          `;
        }

        let dataHtml = '';
        if (s.data != null) {
          dataHtml = `
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px;">
              <span class="hint" style="font-size:0.7rem; text-transform:uppercase; font-weight:600;">Resultado / Salida:</span>
              <div style="display: flex; gap: 4px;">
                <button class="icon-btn view-full-step-btn" style="font-size:0.72rem; padding: 2px 6px;" title="Ver datos completos sin truncar">🔍 Ver completo</button>
                <button class="icon-btn copy-step-btn" style="font-size:0.72rem; padding: 2px 6px;" title="Copiar datos">Copiar</button>
              </div>
            </div>
            <pre class="step-data">${escapeHtml(s.data)}</pre>
          `;
        }

        let errHtml = '';
        if (s.error) {
          errHtml = `<div class="step-error"><b>Error de nodo:</b> ${escapeHtml(s.error)}</div>`;
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
              <button class="btn sm secondary locate-node-btn" title="Localizar y centrar este nodo en el lienzo visual">
                <svg class="ic" width="12" height="12"><use href="#i-center"/></svg> Ver nodo
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
              `Ejecución ${run.id} — Salida de «${s.node_id}»`,
              `Estado: ${statusLabel} | Duración: ${s.duration || 0} ms`,
              s.data
            );
          };
        }

        const viewInBtn = stepCard.querySelector('.view-full-in-btn');
        if (viewInBtn) {
          viewInBtn.onclick = () => {
            openDataViewer(
              `Ejecución ${run.id} — Entrada de «${s.node_id}»`,
              `Paso #${idx + 1}`,
              s.inputs
            );
          };
        }

        const copyInBtn = stepCard.querySelector('.copy-in-btn');
        if (copyInBtn) {
          copyInBtn.onclick = () => {
            navigator.clipboard.writeText(s.inputs);
            toast('Entrada copiada al portapapeles.');
          };
        }

        const copyBtn = stepCard.querySelector('.copy-step-btn');
        if (copyBtn) {
          copyBtn.onclick = () => {
            navigator.clipboard.writeText(s.data);
            toast('Salida copiada al portapapeles.');
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
    toast('Error al obtener detalle de ejecución');
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
    toast(`Nodo «${nodeId}» seleccionado.`);
  }
}

async function purgeHistory() {
  if (!confirm('¿Deseas purgar del historial todas las ejecuciones de más de 30 días de antigüedad?')) {
    return;
  }
  try {
    const res = await fetch('/api/history/purge?days=30', { method: 'POST' });
    const data = await res.json();
    toast(`Historial purgado: ${data.deleted} ejecuciones eliminadas.`);
    await loadHistory();
  } catch (e) {
    toast('Error al purgar historial.');
  }
}

// ---------------------------------------------------------------------------
// Gestión de Flujos (Crear, Cargar, Guardar, Eliminar)
// ---------------------------------------------------------------------------

function resetGraph(name = 'nuevo-flujo') {
  state.graph = { name, nodes: [], edges: [], version: 1 };
  state.meta = { name, description: '', schedule: '', enabled: true };
  state.selectedNodeId = null;
  state.activeInspector = 'flow';
  state.runHighlights = {};
  state.isDirty = false;
  state.view = { k: 1, x: 0, y: 0 };
  $('flowName').value = name;
  render();
  if (state.activeTab === 'history') {
    loadHistory();
  }
  toast('Lienzo en blanco.');
}

async function openFlow(flowId) {
  try {
    const res = await fetch(`/api/flow?id=${encodeURIComponent(flowId)}`);
    if (!res.ok) throw new Error();
    const data = await res.json();
    state.graph = data.graph;
    state.meta = data.meta || { name: flowId };
    $('flowName').value = state.graph.name;
    state.selectedNodeId = null;
    state.activeInspector = 'flow';
    state.runHighlights = {};
    state.isDirty = false;
    render();
    renderFlowsList();
    centerFlow();
    if (state.activeTab === 'history') {
      loadHistory();
    }
    toast(`Flujo «${flowId}» cargado.`);
  } catch (e) {
    toast('Error al cargar flujo.');
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
    toast(`Plantilla «${templateName}» cargada.`);
  } catch (e) {
    toast('Error al cargar plantilla.');
  }
}

async function saveFlow() {
  const id = state.graph.name || 'flujo';
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
      alert('Error de validación:\n' + (Array.isArray(data.detail) ? data.detail.join('\n') : data.detail));
      return false;
    }
    state.isDirty = false;
    toast(`Flujo «${id}» guardado correctamente.`);
    await loadFlows();
    render();
    return true;
  } catch (e) {
    toast('Error al guardar el flujo.');
    return false;
  }
}

async function deleteFlow(flowId) {
  if (!confirm(`¿Eliminar definitivamente el flujo «${flowId}»?`)) return;
  try {
    const res = await fetch(`/api/flow?id=${encodeURIComponent(flowId)}`, { method: 'DELETE' });
    if (!res.ok) throw new Error();
    toast(`Flujo «${flowId}» eliminado.`);
    if (state.graph.name === flowId) {
      state.isDirty = false;
      state.activeInspector = null;
      resetGraph('nuevo-flujo');
    }
    await loadFlows();
  } catch (e) {
    toast('Error al eliminar flujo');
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
      alert('Error de compilación:\n' + (Array.isArray(data.detail) ? data.detail.join('\n') : data.detail));
      return;
    }
    $('sheetTitle').textContent = 'Código generado (Python plano)';
    $('sheetBody').textContent = data.code;
    $('sheetTrace').hidden = true;
    $('sheet').hidden = false;
  } catch (e) {
    toast('Fallo al compilar');
  }
}

async function runPreview() {
  toast('Ejecutando flujo...');
  try {
    const res = await fetch('/api/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ graph: state.graph }),
    });
    const data = await res.json();
    if (!res.ok) {
      alert('Error al ejecutar:\n' + (Array.isArray(data.detail) ? data.detail.join('\n') : data.detail));
      return;
    }
    state.lastRunResults = data.results || {};
    $('sheetTitle').textContent = 'Traza de ejecución en vivo';
    $('sheetBody').textContent = data.code;

    const traceDiv = $('sheetTrace');
    traceDiv.innerHTML = '<div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;"><strong>Resultados por nodo:</strong><span class="hint" style="font-size:0.72rem;">Haz clic en «Ver completo» para inspeccionar datos no truncados</span></div>';
    Object.entries(data.results || {}).forEach(([nid, r]) => {
      const cls = r.status === 'ok' ? 'trace-ok' : r.status === 'skipped' ? 'trace-skip' : 'trace-err';
      const detail = r.status === 'ok' ? JSON.stringify(r.data).slice(0, 100) : r.error || 'Omitido';
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
        <button class="btn sm secondary view-full-trace-btn" style="padding:2px 6px; font-size:0.7rem; margin-left:8px;">🔍 Ver completo</button>
      `;
      item.querySelector('.view-full-trace-btn').onclick = (e) => {
        e.stopPropagation();
        openDataViewer(
          `Nodo «${nid}» — Resultado`,
          `Estado: ${r.status} | Duración: ${r.duration_ms || 0} ms`,
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
    toast('Error al ejecutar flujo.');
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
    node.config[cf] = '# Procesa los datos de entrada y devuelve el resultado\nreturn entrada.text()';
  }

  state.graph.nodes.push(node);
  state.selectedNodeId = id;
  state.activeInspector = 'node';
  markDirty();
  render();
  toast(`Nodo «${spec.label}» añadido.`);
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
        <button class="node-btn-del" title="Eliminar nodo">
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
          <span>${codeLines ? `Editar (${codeLines} lín.)` : 'Escribir código'}</span>
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
    inPort.title = 'Entrada';
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
    okPort.title = 'Salida OK (éxito)';
    okPort.onmousedown = (e) => startWire(e, node.id, 'ok');
    el.appendChild(okPort);

    const errPort = document.createElement('div');
    errPort.className = 'port error out-error';
    errPort.dataset.role = 'out';
    errPort.dataset.port = 'error';
    errPort.title = 'Salida ERROR (fallo)';
    errPort.onmousedown = (e) => startWire(e, node.id, 'error');
    el.appendChild(errPort);

    // Etiquetas de puerto en borde derecho (ok / error)
    const tagOk = document.createElement('span');
    tagOk.className = 'port-tag ok';
    tagOk.textContent = 'ok';
    el.appendChild(tagOk);

    const tagErr = document.createElement('span');
    tagErr.className = 'port-tag error';
    tagErr.textContent = 'error';
    el.appendChild(tagErr);

    // Selección y Arrastre del nodo
    el.onmousedown = (e) => {
      if (e.target.classList.contains('port')) return;
      if (e.target.closest('.node-btn-del')) return;
      if (e.target.closest('.node-btn-edit-code')) return;
      if (e.button !== 0) return;
      e.stopPropagation();

      state.selectedNodeId = node.id;
      state.activeInspector = 'node';
      state.dragNode = node;
      const mouseWorld = screenToWorld(e.clientX, e.clientY);
      state.dragOffset = { x: mouseWorld.x - node.x, y: mouseWorld.y - node.y };

      // Actualizar selección visual sin destruir elementos del DOM
      document.querySelectorAll('.node').forEach((n) => n.classList.remove('selected'));
      el.classList.add('selected');
      document.querySelectorAll('.flow-card').forEach((c) => c.classList.remove('active'));
      renderInspector();
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

    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', d);
    path.setAttribute('class', `wire wire-${edge.port} ${edge.port === 'error' ? 'error' : 'ok'}`);
    path.style.pointerEvents = 'stroke';
    path.style.cursor = 'pointer';
    path.title = 'Haz clic derecho para desconectar';
    path.oncontextmenu = (e) => {
      e.preventDefault();
      removeEdge(edge.source, edge.target, edge.port);
    };
    svg.appendChild(path);
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
    toast(`Conectado por «${port}»`);
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
  if ($('dataViewerTitle')) $('dataViewerTitle').textContent = title || 'Inspección de Datos';
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
    text = '(Vacío / Sin datos)';
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
  if ($('dataViewerSize')) $('dataViewerSize').textContent = `${sizeStr} (${text.length} caracteres)`;

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
    toast('Contenido completo copiado al portapapeles.');
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
  pre.textContent = matched.join('\n') || `(No se encontraron líneas que contengan «${query}»)`;
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

function startDebugFlow() {
  if (!state.graph.nodes.length) {
    toast('El flujo no tiene nodos para depurar.');
    return;
  }
  const order = getTopologicalOrder();
  if (!order) {
    toast('El flujo contiene ciclos o conexiones inválidas.');
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
  updateDebugToolbar();
  highlightActiveDebugNode();
  toast('Modo depuración activo. Pulsa «Siguiente paso» para comenzar.');
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
    $('debugStepNodeName').textContent = `${node.label || node.id} (${node.type})`;
    $('btnDebugNext').disabled = false;
  } else {
    $('debugStepNum').textContent = total;
    $('debugStepNodeName').textContent = 'Completado';
    $('btnDebugNext').disabled = true;
  }
}

function highlightActiveDebugNode() {
  renderNodes();
  if (state.debug.active && state.debug.currentStepIndex < state.debug.stepsOrder.length) {
    const node = state.debug.stepsOrder[state.debug.currentStepIndex];
    selectNode(node);
  }
}

async function stepDebugNext() {
  if (!state.debug.active || state.debug.running) return;
  const idx = state.debug.currentStepIndex;
  if (idx >= state.debug.stepsOrder.length) {
    toast('Depuración completada.');
    return;
  }

  const node = state.debug.stepsOrder[idx];
  state.debug.running = true;
  $('btnDebugNext').disabled = true;
  toast(`Ejecutando paso ${idx + 1}: «${node.label || node.id}»...`);

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
      alert('Error en el paso:\n' + (data.detail || data.error || 'Error desconocido'));
      return;
    }

    state.debug.cachedResults = data.all_results || {};
    state.lastRunResults = data.all_results || {};

    const st = data.step.status;
    state.runHighlights[node.id] = st === 'ok' ? 'ok' : 'err';

    state.debug.currentStepIndex++;
    updateDebugToolbar();
    highlightActiveDebugNode();
    renderInspector();
  } catch (e) {
    state.debug.running = false;
    $('btnDebugNext').disabled = false;
    toast('Fallo al ejecutar paso de depuración.');
  }
}

async function runToNode(targetNodeId) {
  toast(`Ejecutando hasta nodo «${targetNodeId}»...`);
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
      alert('Error ejecutando hasta el nodo:\n' + (data.detail || data.error || 'Error'));
      return;
    }

    if (!state.debug.active) {
      const order = getTopologicalOrder();
      if (order) {
        state.debug.active = true;
        state.debug.stepsOrder = order;
        $('debugToolbar').hidden = false;
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
    toast(`Ejecutado hasta «${targetNodeId}» con éxito.`);
  } catch (e) {
    toast('Error en ejecución hasta el nodo.');
  }
}

async function continueDebugFlow() {
  if (!state.debug.active) return;
  while (state.debug.currentStepIndex < state.debug.stepsOrder.length) {
    await stepDebugNext();
    const lastNode = state.debug.stepsOrder[state.debug.currentStepIndex - 1];
    if (lastNode && state.debug.cachedResults[lastNode.id]?.status === 'error') {
      toast('Depuración detenida por error en el nodo.');
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
  toast('Depuración reiniciada.');
}

function stopDebugFlow() {
  state.debug.active = false;
  $('debugToolbar').hidden = true;
  renderNodes();
  toast('Depuración finalizada.');
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
  const badgeText = isOk ? 'OK' : execData.status === 'skipped' ? 'OMITIDO' : 'ERROR';
  const durationText = execData.duration_ms ? `${execData.duration_ms} ms` : '';

  box.innerHTML = `
    <div class="node-exec-head">
      <span class="node-exec-title">
        <svg class="ic" width="13" height="13"><use href="#i-play"/></svg>
        Datos de Ejecución
      </span>
      <div style="display: flex; gap: 6px; align-items: center;">
        <span class="hint" style="font-size: 0.72rem;">${durationText}</span>
        <span class="step-badge ${badgeClass}">${badgeText}</span>
      </div>
    </div>
    <div class="tab-pill-group" style="margin-bottom: 6px;">
      <button class="tab-pill active" id="pillOutput_${node.id}">Salida (Output)</button>
      <button class="tab-pill" id="pillInput_${node.id}">Entrada (Input)</button>
    </div>
    <pre class="node-exec-pre" id="nodeExecPre_${node.id}"></pre>
    <div class="node-exec-actions">
      <button class="btn sm secondary" id="btnViewFullData_${node.id}" title="Ver contenido completo no truncado en ventana ampliada">
        🔍 Ver completo (no truncado)
      </button>
      <button class="btn sm secondary" id="btnCopyExecData_${node.id}" title="Copiar al portapapeles">
        📋 Copiar
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
      displayText = activeTab === 'input' ? '(Sin entradas precedentes o nodo inicial)' : '(Salida vacía)';
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
      `Nodo «${node.label || node.id}» — ${activeTab === 'output' ? 'Salida' : 'Entrada'}`,
      `Tipo: ${node.type} | Estado: ${badgeText} ${durationText ? `| Duración: ${durationText}` : ''}`,
      rawData
    );
  };

  btnCopy.onclick = () => {
    const rawData = activeTab === 'output' ? (isOk ? execData.data : execData.error) : execData.inputs;
    const textToCopy = typeof rawData === 'object' ? JSON.stringify(rawData, null, 2) : String(rawData || '');
    navigator.clipboard.writeText(textToCopy);
    toast('Datos copiados al portapapeles.');
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
    sourceLabel = `Usando salida de <b>${escapeHtml(parentId)}</b> (${sizeKb} KB)`;
  } else {
    sourceLabel = 'Sin salida previa conectada. Puedes probar con muestra:';
  }

  box.innerHTML = `
    <div class="live-preview-head">
      <div class="live-preview-title">
        <span class="live-pulse-dot"></span>
        <span>Vista Previa en Vivo</span>
      </div>
      <span class="hint" id="livePreviewStatus_${node.id}" style="font-size: 0.72rem;">Listo</span>
    </div>
    <div class="live-preview-source" id="livePreviewSource_${node.id}">
      <span>${sourceLabel}</span>
    </div>
    <pre class="live-preview-pre" id="livePreviewOutput_${node.id}">(Calculando vista previa…)</pre>
    <a class="live-sample-toggle" id="toggleSampleInput_${node.id}">
      ${parentData === null ? '▼ Editar entrada de prueba' : '▶ Usar entrada de prueba personalizada'}
    </a>
    <div class="live-sample-area" id="sampleInputArea_${node.id}" ${parentData !== null ? 'hidden' : ''}>
      <textarea id="sampleInputText_${node.id}" rows="3" style="font-family: var(--mono); font-size: 0.72rem;" placeholder="Pega aquí HTML, JSON o texto de prueba...">${parentData ? (typeof parentData === 'string' ? parentData : JSON.stringify(parentData, null, 2)) : ''}</textarea>
    </div>
  `;

  const toggleBtn = box.querySelector(`#toggleSampleInput_${node.id}`);
  const area = box.querySelector(`#sampleInputArea_${node.id}`);
  toggleBtn.onclick = () => {
    area.hidden = !area.hidden;
    toggleBtn.textContent = area.hidden ? '▶ Usar entrada de prueba personalizada' : '▼ Ocultar entrada personalizada';
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
  if (statEl) statEl.textContent = 'Calculando…';

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
        count = `(${data.data.length} elementos)`;
      }
      if (statEl) statEl.innerHTML = `<span style="color:var(--port-ok)">OK ${count} · ${data.duration_ms} ms</span>`;

      let display = '';
      if (typeof data.data === 'object' && data.data !== null) {
        display = JSON.stringify(data.data, null, 2);
      } else {
        display = String(data.data !== null && data.data !== undefined ? data.data : '(Salida vacía)');
      }
      outEl.style.color = 'var(--text)';
      outEl.textContent = display;
    } else {
      if (statEl) statEl.innerHTML = `<span style="color:var(--port-err)">Error</span>`;
      outEl.style.color = 'var(--port-err)';
      outEl.textContent = data.error || 'Error desconocido';
    }
  } catch (e) {
    if (box) box.classList.remove('is-updating');
    if (statEl) statEl.textContent = 'Error de red';
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
      if (titleEl) titleEl.textContent = 'Ajustes del Nodo';
      renderNodeInspector(node, body);
      return;
    }
  }

  if (state.activeInspector === 'flow') {
    if (titleEl) titleEl.textContent = 'Ajustes del Flujo';
    renderFlowInspector(body);
    return;
  }

  // Estado vacío: clic fuera en el lienzo sin nodo ni flujo seleccionado
  if (titleEl) titleEl.textContent = 'Inspector';
  body.innerHTML = `
    <div style="display: flex; flex-direction: column; align-items: center; justify-content: center; height: 260px; text-align: center; color: var(--text-muted); gap: 12px; padding: 20px;">
      <svg class="ic" width="36" height="36" style="opacity: 0.35;"><use href="#i-flow"/></svg>
      <p style="font-size: 0.84rem; margin: 0; line-height: 1.5;">
        Haz clic en un <b>nodo</b> del lienzo o en un <b>flujo</b> de la barra lateral para ver y editar sus ajustes.
      </p>
    </div>
  `;
}

function renderNodeInspector(node, body) {
  const spec = state.catalog.node_types[node.type] || { required: [], optional: {}, help: {} };
  body.innerHTML = '';

  // ID y Etiqueta
  body.appendChild(createField('ID del nodo', node.id, (val) => {
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

  body.appendChild(createField('Etiqueta', node.label, (val) => {
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
  runToBtn.innerHTML = `<svg class="ic" width="13" height="13"><use href="#i-play"/></svg> <span>Ejecutar hasta este nodo</span>`;
  runToBtn.onclick = () => runToNode(node.id);
  body.appendChild(runToBtn);

  // Botón para editar código si es script
  const codeField = spec.code_field || (node.type === 'transform.script' ? 'code' : null);
  if (codeField) {
    const codeLines = (node.config[codeField] || '').split('\n').filter((l) => l.trim()).length;
    const btnBox = document.createElement('div');
    btnBox.className = 'field-group';
    btnBox.style.marginBottom = '14px';
    btnBox.innerHTML = '<label>Código Python *</label>';

    const btnCode = document.createElement('button');
    btnCode.className = 'btn primary';
    btnCode.style.display = 'flex';
    btnCode.style.alignItems = 'center';
    btnCode.style.justifyContent = 'center';
    btnCode.style.gap = '6px';
    btnCode.innerHTML = `<svg class="ic" width="14" height="14"><use href="#i-code"/></svg> <span>${codeLines ? `Editar código (${codeLines} líneas)` : 'Escribir código…'}</span>`;
    btnCode.onclick = () => openCodeEditor(node);
    btnBox.appendChild(btnCode);
    body.appendChild(btnBox);
  }

  // Ayuda interactiva para Webhook Entrante
  if (node.type === 'input.webhook') {
    const flowId = state.graph.name || 'mi-flujo';
    const hookUrl = `${window.location.origin}/api/webhook/${flowId}`;
    const box = document.createElement('div');
    box.className = 'field-group';
    box.style.background = 'var(--bg-subtle)';
    box.style.border = '1px solid var(--border)';
    box.style.borderRadius = '6px';
    box.style.padding = '8px 10px';
    box.style.marginBottom = '14px';
    box.innerHTML = `
      <label style="color: var(--primary); font-weight: 600;">URL del Webhook (POST / GET)</label>
      <div style="display: flex; gap: 6px; margin-top: 4px;">
        <input type="text" readonly value="${escapeHtml(hookUrl)}" style="font-family: var(--mono); font-size: 0.72rem; flex: 1; padding: 4px 6px;" />
        <button class="btn sm" id="btnCopyWebhook" title="Copiar URL">Copiar</button>
      </div>
      <span class="field-help" style="margin-top: 4px;">Envía datos JSON por POST a esta URL para disparar el flujo desde Home Assistant, cURL o alertas externas.</span>
    `;
    const btn = box.querySelector('#btnCopyWebhook');
    btn.onclick = () => {
      navigator.clipboard.writeText(hookUrl);
      toast('URL del webhook copiada');
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
      inputEl.innerHTML = '<option value="">— Sin secreto —</option>';
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
  btnDel.innerHTML = '<svg class="ic" width="14" height="14"><use href="#i-trash"/></svg> Eliminar nodo';
  btnDel.onclick = () => removeNode(node.id);
  body.appendChild(btnDel);
}

function renderFlowInspector(body) {
  body.innerHTML = '';

  // Nombre del flujo
  const nameGrp = document.createElement('div');
  nameGrp.className = 'field-group';
  nameGrp.innerHTML = '<label>Nombre del flujo *</label>';
  const nameInput = document.createElement('input');
  nameInput.value = state.graph.name || '';
  nameInput.placeholder = 'ej. mi-flujo';
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
  descGrp.innerHTML = '<label>Descripción</label>';
  const descInput = document.createElement('textarea');
  descInput.rows = 2;
  descInput.value = state.meta.description || '';
  descInput.placeholder = 'Notas o propósito de este flujo...';
  descInput.oninput = (e) => {
    state.meta.description = e.target.value;
    markDirty();
  };
  descGrp.appendChild(descInput);
  body.appendChild(descGrp);

  // Sección de Estado y Programación
  const secSched = document.createElement('div');
  secSched.className = 'inspector-section';
  secSched.textContent = 'Programación y Estado';
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
  enabledLbl.textContent = 'Flujo activo';
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
  presetGrp.innerHTML = '<label>Frecuencia de ejecución</label>';
  const presetSelect = document.createElement('select');
  const presets = [
    { value: '', label: 'Manual (sin programación)' },
    { value: '* * * * *', label: 'Cada minuto' },
    { value: '*/5 * * * *', label: 'Cada 5 minutos' },
    { value: '*/15 * * * *', label: 'Cada 15 minutos' },
    { value: '0 * * * *', label: 'Cada hora' },
    { value: '0 9 * * *', label: 'Diario (09:00)' },
    { value: 'custom', label: 'Personalizado (cron)' },
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
  cronGrp.innerHTML = '<label>Expresión Cron</label>';
  const cronInput = document.createElement('input');
  cronInput.value = currSched;
  cronInput.placeholder = 'ej. */5 * * * *';
  cronInput.style.display = (currSched || presetSelect.value === 'custom') ? 'block' : 'none';
  cronInput.oninput = (e) => {
    state.meta.schedule = e.target.value.trim();
    markDirty();
  };
  const cronHelp = document.createElement('span');
  cronHelp.className = 'field-help';
  cronHelp.textContent = 'Formato cron estándar de 5 campos (min hora día mes dds).';
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
  secStats.textContent = 'Estructura del Grafo';
  body.appendChild(secStats);

  const statsBox = document.createElement('div');
  statsBox.innerHTML = `
    <div class="flow-stats-row">
      <span>Nodos en lienzo:</span>
      <span class="flow-stats-val">${state.graph.nodes.length}</span>
    </div>
    <div class="flow-stats-row">
      <span>Conexiones (cables):</span>
      <span class="flow-stats-val">${state.graph.edges.length}</span>
    </div>
    <div class="flow-stats-row">
      <span>Estado cambios:</span>
      <span class="flow-stats-val" style="color: ${state.isDirty ? 'var(--port-err)' : 'var(--port-ok)'}">${state.isDirty ? 'Sin guardar' : 'Al día'}</span>
    </div>
  `;
  body.appendChild(statsBox);

  // Acciones rápidas
  const secActions = document.createElement('div');
  secActions.className = 'inspector-section';
  secActions.textContent = 'Acciones';
  body.appendChild(secActions);

  const actBox = document.createElement('div');
  actBox.style.display = 'flex';
  actBox.style.flexDirection = 'column';
  actBox.style.gap = '8px';
  actBox.style.marginTop = '8px';

  const btnSave = document.createElement('button');
  btnSave.className = 'btn primary sm';
  btnSave.innerHTML = '<svg class="ic" width="13" height="13"><use href="#i-save"/></svg> Guardar flujo';
  btnSave.onclick = saveFlow;

  const btnRun = document.createElement('button');
  btnRun.className = 'btn secondary sm';
  btnRun.innerHTML = '<svg class="ic" width="13" height="13"><use href="#i-play"/></svg> Probar ejecución';
  btnRun.onclick = runPreview;

  const btnAdvSched = document.createElement('button');
  btnAdvSched.className = 'btn secondary sm';
  btnAdvSched.innerHTML = '<svg class="ic" width="13" height="13"><use href="#i-clock"/></svg> Programación avanzada...';
  btnAdvSched.onclick = () => openScheduleModal(state.graph.name);

  const btnExportZip = document.createElement('button');
  btnExportZip.className = 'btn secondary sm';
  btnExportZip.innerHTML = '<svg class="ic" width="13" height="13"><use href="#i-download"/></svg> Exportar este flujo (.zip)';
  btnExportZip.onclick = () => exportFlowZip(state.graph.name);

  const btnDelFlow = document.createElement('button');
  btnDelFlow.className = 'btn secondary sm';
  btnDelFlow.style.color = 'var(--port-err)';
  btnDelFlow.style.borderColor = 'rgba(239, 68, 68, 0.35)';
  btnDelFlow.style.marginTop = '6px';
  btnDelFlow.innerHTML = '<svg class="ic" width="13" height="13"><use href="#i-trash"/></svg> Eliminar este flujo';
  btnDelFlow.onclick = () => deleteFlow(state.graph.name);

  actBox.appendChild(btnSave);
  actBox.appendChild(btnRun);
  actBox.appendChild(btnAdvSched);
  actBox.appendChild(btnExportZip);
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
    : '# Procesa los datos de entrada y devuelve el resultado\nreturn entrada.text()';

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
  head.textContent = 'Variables de entrada';
  inputsPanel.appendChild(head);

  const incoming = state.graph.edges.filter((e) => e.target === node.id);
  if (!incoming.length) {
    const hint = document.createElement('p');
    hint.className = 'hint';
    hint.textContent = 'Conecta otros nodos a este para acceder a sus datos.';
    inputsPanel.appendChild(hint);
  } else {
    incoming.forEach((edge) => {
      const srcNode = state.graph.nodes.find((n) => n.id === edge.source);
      const srcSpec = srcNode ? state.catalog.node_types[srcNode.type] : null;

      const card = document.createElement('div');
      card.className = 'wired';

      const kind = document.createElement('span');
      kind.className = 'kind';
      kind.textContent = `${srcSpec ? srcSpec.label : (srcNode ? srcNode.type : '?')} · puerto ${edge.port}`;

      const codeChip = document.createElement('code');
      codeChip.textContent = `entrada["${edge.source}"]`;
      codeChip.title = 'Haz clic para insertar en el código';
      codeChip.onclick = () => {
        if (codeMirrorInstance) {
          codeMirrorInstance.replaceSelection(`entrada["${edge.source}"]`);
          codeMirrorInstance.focus();
        }
      };

      const what = document.createElement('span');
      what.className = 'what';
      what.textContent = srcSpec && srcSpec.output ? `Devuelve: ${srcSpec.output}` : 'Salida del nodo';

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
    <strong style="color: var(--text); display: block; margin-bottom: 4px;">Atajos de entrada:</strong>
    <div>▸ <code title="Texto combinado">entrada.text()</code></div>
    <div>▸ <code title="Primer dato entrante">entrada.first</code></div>
    <div>▸ <code title="Diccionario seguro">entrada.get("id")</code></div>
    <div>▸ <code title="Lista de entradas">list(entrada)</code></div>
    <p style="margin-top: 6px; font-size: 0.7rem; color: var(--text-muted); line-height: 1.3;">
      Usa <code style="cursor: default;">return</code> para pasar el valor a los nodos siguientes.<br/>
      <span style="opacity: 0.8;">(Ctrl+Enter para aplicar)</span>
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
  toast(`Código de «${node.id}» actualizado.`);
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
    toast('Guarda el flujo antes de exportarlo', 'err');
    return;
  }
  const link = document.createElement('a');
  link.href = `/api/flow/export?id=${encodeURIComponent(fid)}`;
  link.download = `${fid}.zip`;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  toast(`Descargando ${fid}.zip`, 'ok');
}

function exportAllFlowsZip() {
  const link = document.createElement('a');
  link.href = '/api/flows/export-all';
  link.download = 'dedalo_flows_backup.zip';
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  toast('Descargando backup completo de flujos (.zip)', 'ok');
}

async function handleImportZip(file) {
  if (!file) return;
  const formData = new FormData();
  formData.append('file', file);
  toast('Importando flujos...', 'info');
  try {
    const res = await fetch('/api/flows/import', {
      method: 'POST',
      body: formData,
    });
    const data = await res.json();
    if (!res.ok || data.status === 'error') {
      toast(data.detail || data.error || 'Error al importar', 'err');
      return;
    }
    const count = data.imported_count || (data.imported ? data.imported.length : 0);
    toast(`¡Importados ${count} flujo(s) con éxito!`, 'ok');
    if (data.required_secrets && data.required_secrets.length > 0) {
      setTimeout(() => {
        toast(`Aviso: Flujos importados requieren secretos: ${data.required_secrets.join(', ')}`, 'info');
      }, 2500);
    }
    await loadFlows();
    if (count === 1 && data.imported && data.imported[0]) {
      loadFlow(data.imported[0].id);
    }
  } catch (err) {
    toast(`Error de conexión: ${err.message}`, 'err');
  }
}

window.onload = init;
