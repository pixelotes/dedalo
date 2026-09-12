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
  // Interacción de arrastre y cableado
  dragNode: null,
  dragOffset: { x: 0, y: 0 },
  wiring: null, // { sourceNodeId, port, startX, startY }
  editingScriptNodeId: null,
};

// ---------------------------------------------------------------------------
// Inicialización
// ---------------------------------------------------------------------------

async function init() {
  setupTheme();
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
    };
  });

  $('flowName').oninput = (e) => {
    state.graph.name = e.target.value.trim().toLowerCase().replace(/[^a-z0-9_-]/g, '-');
    state.meta.name = state.graph.name;
    markDirty();
  };

  $('btnNew').onclick = () => withUnsavedCheck(() => resetGraph('nuevo-flujo'));
  $('btnSidebarNewFlow').onclick = () => withUnsavedCheck(() => resetGraph('nuevo-flujo'));
  $('btnSchedule').onclick = () => openScheduleModal(state.graph.name);
  $('btnCompile').onclick = compileGraph;
  $('btnRun').onclick = runPreview;
  $('btnSave').onclick = saveFlow;
  $('templatePicker').onchange = (e) => {
    const val = e.target.value;
    if (val) {
      withUnsavedCheck(() => loadTemplate(val));
    }
  };

  $('sheetClose').onclick = () => $('sheet').hidden = true;
  $('codeCancel').onclick = () => $('codeSheet').hidden = true;
  $('codeSave').onclick = saveScriptCode;

  $('schedClose').onclick = () => $('scheduleSheet').hidden = true;
  $('btnSchedSave').onclick = saveScheduleModal;
  $('btnSchedRunNow').onclick = runScheduleModalNow;
  $('schedPreset').onchange = onSchedPresetChange;

  $('unsavedClose').onclick = () => closeUnsavedModal();
  $('btnUnsavedCancel').onclick = () => closeUnsavedModal();
  $('btnUnsavedDiscard').onclick = onUnsavedDiscard;
  $('btnUnsavedSave').onclick = onUnsavedSave;

  $('btnSaveSecret').onclick = addSecret;
  $('btnSaveConfig').onclick = saveConfig;

  // Eventos globales de arrastre y clic en el canvas
  const canvas = $('canvasWrap');
  canvas.onmousedown = onCanvasMouseDown;
  canvas.onmousemove = onCanvasMouseMove;
  canvas.onmouseup = onCanvasMouseUp;
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
      if (fl.id === state.graph.name) {
        if (state.selectedNodeId !== null) {
          state.selectedNodeId = null;
          render();
        }
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
    btnRunNow.onclick = async (e) => {
      e.stopPropagation();
      await runFlowNow(fl.id);
    };

    const btnSched = document.createElement('button');
    btnSched.className = 'btn sm icon-btn';
    btnSched.title = 'Programar periodicidad';
    btnSched.innerHTML = '<svg class="ic" width="12" height="12"><use href="#i-clock"/></svg>';
    btnSched.onclick = (e) => {
      e.stopPropagation();
      openScheduleModal(fl.id);
    };

    const btnDel = document.createElement('button');
    btnDel.className = 'btn sm icon-btn';
    btnDel.title = 'Eliminar flujo';
    btnDel.innerHTML = '<svg class="ic" width="12" height="12"><use href="#i-trash"/></svg>';
    btnDel.onclick = (e) => {
      e.stopPropagation();
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

  Object.entries(categories).forEach(([catKey, catTitle]) => {
    const block = document.createElement('div');
    block.className = 'category-block';
    block.innerHTML = `<h4>${catTitle}</h4>`;

    Object.entries(state.catalog.node_types || {}).forEach(([typeKey, spec]) => {
      if (spec.category !== catKey) return;
      const pill = document.createElement('div');
      pill.className = 'node-pill';
      pill.innerHTML = `<span>${escapeHtml(spec.label)}</span><span class="badge">${typeKey.split('.')[1]}</span>`;
      pill.onclick = () => addNode(typeKey);
      block.appendChild(pill);
    });
    p.appendChild(block);
  });
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
// Gestión de Flujos (Crear, Cargar, Guardar, Eliminar)
// ---------------------------------------------------------------------------

function resetGraph(name = 'nuevo-flujo') {
  state.graph = { name, nodes: [], edges: [], version: 1 };
  state.meta = { name, description: '', schedule: '', enabled: true };
  state.selectedNodeId = null;
  $('flowName').value = name;
  render();
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
    state.isDirty = false;
    render();
    renderFlowsList();
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
    markDirty();
    render();
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
    await fetch(`/api/flow?id=${encodeURIComponent(flowId)}`, { method: 'DELETE' });
    toast(`Flujo «${flowId}» eliminado.`);
    if (state.graph.name === flowId) resetGraph();
    await loadFlows();
  } catch (e) {
    toast('Error al eliminar');
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
    $('sheetTitle').textContent = 'Traza de ejecución en vivo';
    $('sheetBody').textContent = data.code;

    const traceDiv = $('sheetTrace');
    traceDiv.innerHTML = '<strong>Resultados por nodo:</strong><br/>';
    Object.entries(data.results || {}).forEach(([nid, r]) => {
      const cls = r.status === 'ok' ? 'trace-ok' : r.status === 'skipped' ? 'trace-skip' : 'trace-err';
      const detail = r.status === 'ok' ? JSON.stringify(r.data).slice(0, 120) : r.error || 'Omitido';
      traceDiv.innerHTML += `<div class="trace-item ${cls}">▸ <b>${nid}</b> [${r.status}]: ${escapeHtml(detail)}</div>`;
    });
    traceDiv.hidden = false;
    $('sheet').hidden = false;
  } catch (e) {
    toast('Error al ejecutar flujo.');
  }
}

// ---------------------------------------------------------------------------
// Manipulación de Nodos y Cables
// ---------------------------------------------------------------------------

function addNode(typeKey) {
  const spec = state.catalog.node_types[typeKey];
  if (!spec) return;

  const count = state.graph.nodes.filter((n) => n.type === typeKey).length + 1;
  const baseName = typeKey.split('.')[1];
  const id = `${baseName}_${count}`;

  const node = {
    id,
    type: typeKey,
    label: spec.label,
    config: { ...spec.optional },
    x: 100 + (state.graph.nodes.length % 5) * 40,
    y: 100 + (state.graph.nodes.length % 5) * 40,
  };

  state.graph.nodes.push(node);
  state.selectedNodeId = id;
  markDirty();
  render();
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

function render() {
  $('flowName').value = state.graph.name || '';
  $('emptyHint').style.display = state.graph.nodes.length ? 'none' : 'block';

  renderWires();
  renderNodes();
  renderInspector();
}

function renderNodes() {
  const container = $('nodes');
  container.innerHTML = '';

  state.graph.nodes.forEach((node) => {
    const el = document.createElement('div');
    el.className = 'node' + (node.id === state.selectedNodeId ? ' selected' : '');
    el.style.left = `${node.x}px`;
    el.style.top = `${node.y}px`;

    // Cabecera
    const head = document.createElement('div');
    head.className = 'node-header';
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
    body.textContent = node.id;
    el.appendChild(body);

    // Puertos
    const ports = document.createElement('div');
    ports.className = 'node-ports';

    // Entrada (puerto 'in')
    const inPort = document.createElement('div');
    inPort.className = 'port in';
    inPort.title = 'Entrada';
    inPort.onmouseup = (e) => {
      e.stopPropagation();
      finishWire(node.id);
    };
    ports.appendChild(inPort);

    // Salidas (ok y error)
    const outGroup = document.createElement('div');
    outGroup.className = 'port-group';

    const okPort = document.createElement('div');
    okPort.className = 'port ok';
    okPort.title = 'Salida OK (éxito)';
    okPort.onmousedown = (e) => startWire(e, node.id, 'ok');

    const errPort = document.createElement('div');
    errPort.className = 'port error';
    errPort.title = 'Salida ERROR (fallo)';
    errPort.onmousedown = (e) => startWire(e, node.id, 'error');

    outGroup.appendChild(okPort);
    outGroup.appendChild(errPort);
    ports.appendChild(outGroup);
    el.appendChild(ports);

    // Selección y Arrastre del nodo
    el.onmousedown = (e) => {
      if (e.target.classList.contains('port')) return;
      if (e.target.closest('.node-btn-del')) return;
      state.selectedNodeId = node.id;
      state.dragNode = node;
      const rect = el.getBoundingClientRect();
      state.dragOffset = { x: e.clientX - rect.left, y: e.clientY - rect.top };
      render();
    };

    container.appendChild(el);
  });
}

function renderWires() {
  const svg = $('wires');
  svg.innerHTML = '';

  state.graph.edges.forEach((edge) => {
    const sNode = state.graph.nodes.find((n) => n.id === edge.source);
    const tNode = state.graph.nodes.find((n) => n.id === edge.target);
    if (!sNode || !tNode) return;

    const pOffset = edge.port === 'ok' ? 145 : 175;
    const x1 = sNode.x + pOffset;
    const y1 = sNode.y + 70;
    const x2 = tNode.x + 20;
    const y2 = tNode.y + 70;

    const dx = Math.max(40, Math.abs(x2 - x1) * 0.5);
    const d = `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`;

    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', d);
    path.setAttribute('class', `wire wire-${edge.port}`);
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
    const { startX, startY, currX, currY, port } = state.wiring;
    const dx = Math.max(40, Math.abs(currX - startX) * 0.5);
    const d = `M ${startX} ${startY} C ${startX + dx} ${startY}, ${currX - dx} ${currY}, ${currX} ${currY}`;
    const p = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    p.setAttribute('d', d);
    p.setAttribute('class', `wire wire-draft wire-${port}`);
    svg.appendChild(p);
  }
}

// ---------------------------------------------------------------------------
// Arrastre e Interacción en Canvas
// ---------------------------------------------------------------------------

function onCanvasMouseDown(e) {
  if (e.target.closest('.node') || e.target.closest('.port')) {
    return;
  }
  if (state.selectedNodeId !== null) {
    state.selectedNodeId = null;
    render();
  }
}

function onCanvasMouseMove(e) {
  const canvasRect = $('canvasWrap').getBoundingClientRect();
  const mouseX = e.clientX - canvasRect.left;
  const mouseY = e.clientY - canvasRect.top;

  if (state.dragNode) {
    state.dragNode.x = Math.max(10, mouseX - state.dragOffset.x);
    state.dragNode.y = Math.max(10, mouseY - state.dragOffset.y);
    markDirty();
    renderWires();
    const nodeEl = document.querySelector(`.node.selected`);
    if (nodeEl) {
      nodeEl.style.left = `${state.dragNode.x}px`;
      nodeEl.style.top = `${state.dragNode.y}px`;
    }
  } else if (state.wiring) {
    state.wiring.currX = mouseX;
    state.wiring.currY = mouseY;
    renderWires();
  }
}

function onCanvasMouseUp() {
  if (state.dragNode) state.dragNode = null;
  if (state.wiring) {
    state.wiring = null;
    renderWires();
  }
}

function startWire(e, sourceNodeId, port) {
  e.stopPropagation();
  const sNode = state.graph.nodes.find((n) => n.id === sourceNodeId);
  if (!sNode) return;
  const pOffset = port === 'ok' ? 145 : 175;
  state.wiring = {
    sourceNodeId,
    port,
    startX: sNode.x + pOffset,
    startY: sNode.y + 70,
    currX: sNode.x + pOffset,
    currY: sNode.y + 70,
  };
}

function finishWire(targetNodeId) {
  if (!state.wiring) return;
  const { sourceNodeId, port } = state.wiring;
  state.wiring = null;

  if (sourceNodeId === targetNodeId) return; // No auto-bucles
  const exists = state.graph.edges.some(
    (e) => e.source === sourceNodeId && e.target === targetNodeId && e.port === port
  );
  if (!exists) {
    state.graph.edges.push({ source: sourceNodeId, target: targetNodeId, port });
    markDirty();
    render();
  }
}

// ---------------------------------------------------------------------------
// Inspector de Propiedades del Nodo Seleccionado
// ---------------------------------------------------------------------------

function renderInspector() {
  const titleEl = $('inspectorTitle');
  const body = $('inspectorBody');
  if (!body) return;

  const node = state.graph.nodes.find((n) => n.id === state.selectedNodeId);
  if (node) {
    if (titleEl) titleEl.textContent = 'Ajustes del Nodo';
    renderNodeInspector(node, body);
  } else {
    if (titleEl) titleEl.textContent = 'Ajustes del Flujo';
    renderFlowInspector(body);
  }
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

  // Botón para editar código si es script
  if (spec.code_field) {
    const btnCode = document.createElement('button');
    btnCode.className = 'btn primary';
    btnCode.style.marginBottom = '14px';
    btnCode.textContent = 'Editar código Python...';
    btnCode.onclick = () => openCodeEditor(node);
    body.appendChild(btnCode);
  }

  // Campos de configuración
  const allFields = { ...spec.optional, ...node.config };
  spec.required.forEach((rf) => {
    if (!(rf in allFields)) allFields[rf] = '';
  });

  Object.entries(allFields).forEach(([key, defaultVal]) => {
    if (key === 'fan_in' || key === spec.code_field) return;

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
      };
    } else if (typeof defaultVal === 'boolean') {
      inputEl = document.createElement('input');
      inputEl.type = 'checkbox';
      inputEl.checked = Boolean(currentVal);
      inputEl.onchange = (e) => {
        node.config[key] = e.target.checked;
        markDirty();
      };
    } else {
      inputEl = document.createElement('textarea');
      inputEl.rows = typeof defaultVal === 'string' && defaultVal.length > 40 ? 3 : 1;
      inputEl.value = currentVal;
      inputEl.oninput = (e) => {
        node.config[key] = e.target.value;
        markDirty();
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

  actBox.appendChild(btnSave);
  actBox.appendChild(btnRun);
  actBox.appendChild(btnAdvSched);
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
// Editor de Scripts Python para nodos `transform.script`
// ---------------------------------------------------------------------------

function openCodeEditor(node) {
  state.editingScriptNodeId = node.id;
  $('codeNodeId').textContent = node.id;
  $('codeTextarea').value = node.config.code || 'return entrada.text()';

  // Mostrar lista de nodos entrantes disponibles
  const inputsPanel = $('codeInputs');
  const incoming = state.graph.edges.filter((e) => e.target === node.id);
  inputsPanel.innerHTML = '<strong>Entradas conectadas:</strong><br/>';
  if (!incoming.length) {
    inputsPanel.innerHTML += '<p class="hint">No hay nodos conectados a este script.</p>';
  } else {
    incoming.forEach((e) => {
      inputsPanel.innerHTML += `<div><code>entrada["${escapeHtml(e.source)}"]</code></div>`;
    });
  }
  $('codeSheet').hidden = false;
}

function saveScriptCode() {
  const node = state.graph.nodes.find((n) => n.id === state.editingScriptNodeId);
  if (node) {
    node.config.code = $('codeTextarea').value;
    markDirty();
    toast('Código del nodo actualizado.');
  }
  $('codeSheet').hidden = true;
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

window.onload = init;
