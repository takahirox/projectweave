'use strict';
const view = document.getElementById('view');
const connection = document.getElementById('connection');
let state;
function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (className) node.className = className;
  return node;
}
function link(text, href, external = false) {
  const node = element('a', text);
  if (external) {
    try {
      const url = new URL(href);
      if (url.protocol !== 'https:' || url.hostname !== 'github.com') return element('span', text);
    } catch { return element('span', text); }
    node.rel = 'noreferrer';
  }
  node.href = href;
  return node;
}
function duration(seconds) {
  seconds = Math.floor(seconds);
  return `${Math.floor(seconds / 3600)}h ${Math.floor(seconds / 60) % 60}m ${seconds % 60}s`;
}
function badge(run) {
  const node = element('span');
  node.append(element('span', run.status, `badge ${run.status}`));
  if (run.long_running) node.append(element('span', 'Long running', 'badge'));
  return node;
}
function projects() {
  view.append(element('h1', 'Projects'));
  const cards = element('div', null, 'cards');
  for (const project of state.projects) {
    const card = element('article', null, 'card');
    const heading = element('h2');
    heading.append(link(project.name, `#project/${encodeURIComponent(project.name)}`));
    card.append(heading);
    const counts = element('div', null, 'counts');
    for (const [label, key] of [['Running', 'running'], ['Long running', 'long_running'], ['Failed', 'failed']]) {
      const count = element('div', label);
      count.prepend(element('strong', project[key]));
      counts.append(count);
    }
    card.append(counts);
    cards.append(card);
  }
  view.append(cards);
  if (!state.projects.length) view.append(element('p', 'No managed Projects in this workspace.'));
}
function projectDetail(name) {
  const project = state.projects.find(project => project.name === name);
  if (!project) { view.append(element('h1', 'Project not found')); return; }
  view.append(link('← All Projects', '#'), element('h1', name));
  if (project.url) view.append(link('Open GitHub Project', project.url, true));
  const runs = state.runs.filter(run => run.project === name);
  const wrap = element('div', null, 'table-wrap');
  const table = element('table');
  const header = element('tr');
  for (const text of ['Task', 'Repository', 'Status', 'Started', 'Elapsed', 'Run ID', 'Executor']) header.append(element('th', text));
  const head = element('thead'); head.append(header); table.append(head);
  const body = element('tbody');
  for (const run of runs.slice().reverse()) {
    const row = element('tr', null, run.long_running ? 'long' : '');
    const task = element('td');
    task.append(link(`#${run.task.number ?? '?'} ${run.task.title ?? ''}`, `#run/${run.id}`));
    const status = element('td'); status.append(badge(run));
    row.append(task, element('td', run.task.repository), status, element('td', run.started_at),
      element('td', duration(run.elapsed_seconds)), element('td', run.run_id || 'Not yet available'),
      element('td', run.executor_type || 'Preparing'));
    body.append(row);
  }
  table.append(body); wrap.append(table); view.append(wrap);
  if (!runs.length) view.append(element('p', 'No Tasks launched by this process.'));
}
const SVG = 'http://www.w3.org/2000/svg';
function svgElement(tag, attrs = {}, text) {
  const node = document.createElementNS(SVG, tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
  if (text !== undefined) node.textContent = text;
  return node;
}
function graphView(graph) {
  // Layer acyclic edges; place nodes in cycles in a final column.
  const rank = new Map(graph.nodes.map(node => [node.id, 0]));
  const incoming = new Map(graph.nodes.map(node => [node.id, 0]));
  const edges = graph.edges.filter(([a, b]) => incoming.has(a) && incoming.has(b));
  for (const [, b] of edges) incoming.set(b, incoming.get(b) + 1);
  const ready = graph.nodes.filter(node => incoming.get(node.id) === 0).map(node => node.id);
  const visited = new Set();
  while (ready.length) {
    const id = ready.shift(); visited.add(id);
    for (const [a, b] of edges.filter(([a]) => a === id)) {
      rank.set(b, Math.max(rank.get(b), rank.get(a) + 1));
      incoming.set(b, incoming.get(b) - 1);
      if (!incoming.get(b)) ready.push(b);
    }
  }
  let last = Math.max(0, ...rank.values());
  for (const node of graph.nodes) if (!visited.has(node.id)) rank.set(node.id, ++last);
  const rows = new Map(), positions = new Map();
  let maxRows = 1;
  for (const node of graph.nodes) {
    const column = rank.get(node.id), row = rows.get(column) || 0;
    rows.set(column, row + 1); maxRows = Math.max(maxRows, row + 1);
    positions.set(node.id, [30 + column * 230, 30 + row * 100]);
  }
  const svg = svgElement('svg', {class: 'graph', width: (last + 1) * 230 + 40, height: maxRows * 100 + 40, role: 'img', 'aria-label': 'GitWeave graph progress'});
  const defs = svgElement('defs');
  const marker = svgElement('marker', {id: `arrow-${graphSequence++}`, markerWidth: 8, markerHeight: 8, refX: 7, refY: 4, orient: 'auto'});
  marker.append(svgElement('path', {d: 'M0,0 L8,4 L0,8', fill: '#7f92ae'}));
  defs.append(marker); svg.append(defs);
  for (const [a, b] of edges) {
    const [x1, y1] = positions.get(a), [x2, y2] = positions.get(b);
    const d = x2 > x1 ? `M${x1 + 190},${y1 + 30} C${x1 + 210},${y1 + 30} ${x2 - 20},${y2 + 30} ${x2},${y2 + 30}`
      : `M${x1 + 95},${y1} C${x1 + 95},${y1 - 25} ${x2 + 95},${y2 - 25} ${x2 + 95},${y2}`;
    svg.append(svgElement('path', {d, class: 'edge', 'marker-end': `url(#${marker.id})`}));
  }
  for (const node of graph.nodes) {
    const [x, y] = positions.get(node.id);
    const group = svgElement('g', {class: node.status});
    group.append(svgElement('title', {}, `${node.id} (${node.kind}): ${node.status}`), svgElement('rect', {x, y, width: 190, height: 60, rx: 7}),
      svgElement('text', {x: x + 8, y: y + 23}, node.id.length > 24 ? node.id.slice(0, 23) + '…' : node.id),
      svgElement('text', {x: x + 8, y: y + 45}, node.status.replaceAll('_', ' ')));
    svg.append(group);
  }
  const wrap = element('div', null, 'graph-wrap'); wrap.append(svg); return wrap;
}
let graphSequence = 0;
function runDetail(id) {
  const run = state.runs.find(run => run.id === id);
  if (!run) { view.append(element('h1', 'Run not found')); return; }
  view.append(link(`← ${run.project}`, `#project/${encodeURIComponent(run.project)}`),
    element('h1', `#${run.task.number ?? '?'} ${run.task.title ?? ''}`), badge(run));
  if (run.task.url) view.append(link('Open GitHub Issue', run.task.url, true));
  const details = element('dl');
  for (const [label, value] of [['Repository', run.task.repository], ['Executor', run.executor_type || 'Preparing'],
    ['Started', run.started_at], ['Ended', run.ended_at || 'Still running'], ['Elapsed', duration(run.elapsed_seconds)],
    ['ProjectWeave Run ID', run.run_id || 'Not yet available']]) {
    details.append(element('dt', label), element('dd', value));
  }
  view.append(details);
  if (run.failure) view.append(element('h2', 'Failure'), element('pre', JSON.stringify(run.failure, null, 2)));
  for (const execution of run.executions) {
    if (execution.executor_type !== 'gitweave') continue;
    view.append(element('h2', `GitWeave · ${execution.id}`), element('p', `Run ID: ${execution.run_id || 'Not yet available'}`),
      element('p', `Active: ${execution.current_nodes.join(', ') || (run.status === 'running' ? 'Unknown without live events' : 'None')}; most recent: ${execution.recent_node || 'Unknown'}`));
    if (execution.graph) view.append(graphView(execution.graph), element('p', 'Blue: active · Green: completed · Red: failed · Gray: not yet observed. Conditional branches may never execute.', 'muted'));
    else view.append(element('p', `Graph unavailable: ${execution.graph_error || 'No graph data'}`));
  }
  view.append(element('h2', 'Recent execution logs'));
  const logs = element('pre', run.logs.length ? run.logs.map(log => `${log.at} [${log.type}] ${log.text}`).join('\n') : 'No output available yet.');
  logs.id = 'logs'; view.append(logs);
}
function render() {
  const previousLogs = document.getElementById('logs');
  const bottom = !previousLogs || previousLogs.scrollTop + previousLogs.clientHeight >= previousLogs.scrollHeight - 10;
  const scroll = previousLogs?.scrollTop || 0;
  view.replaceChildren();
  const [kind, ...parts] = location.hash.slice(1).split('/');
  let id;
  try { id = decodeURIComponent(parts.join('/')); } catch { id = ''; }
  if (kind === 'project') projectDetail(id);
  else if (kind === 'run') runDetail(id);
  else projects();
  const logs = document.getElementById('logs');
  if (logs) logs.scrollTop = bottom ? logs.scrollHeight : scroll;
}
async function refresh() {
  try {
    const response = await fetch('/api/state', {cache: 'no-store'});
    if (!response.ok) throw new Error('Dashboard unavailable');
    state = await response.json(); connection.textContent = ''; render();
  } catch { connection.textContent = 'Coordinator unavailable. Reconnecting…'; }
  setTimeout(refresh, 1000);
}
window.addEventListener('hashchange', () => { if (state) render(); });
refresh();
