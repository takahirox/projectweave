'use strict';
const view = document.getElementById('view');
const connection = document.getElementById('connection');
let state;
let displayedRun = null;
let traceFilter = 'all';
const selectedNodes = new Map();
const lifecycleTypes = new Set(['node_started', 'node_completed', 'node_failed']);
const statusNames = {running: 'Running', active: 'Active', completed: 'Completed', failed: 'Failed',
  not_executed: 'Pending', unknown: 'Unknown'};

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
function button(text, action, className) {
  const node = element('button', text, className);
  node.type = 'button';
  node.addEventListener('click', action);
  return node;
}
function duration(seconds) {
  seconds = Math.max(0, Math.floor(seconds || 0));
  return `${Math.floor(seconds / 3600)}h ${Math.floor(seconds / 60) % 60}m ${seconds % 60}s`;
}
function timestamp(value, short = false) {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return short ? date.toLocaleTimeString() : date.toLocaleString();
}
function statusBadge(status, text = statusNames[status] || status) {
  return element('span', text, `badge ${status}`);
}
function badge(run) {
  const node = element('span', null, 'badges');
  node.append(statusBadge(run.status));
  if (run.long_running) node.append(statusBadge('long', 'Long running'));
  return node;
}
function counts(project) {
  const node = element('div', null, 'counts');
  for (const [label, key, style] of [['Running', 'running', 'running'], ['Long running', 'long_running', 'long'], ['Failed', 'failed', 'failed']]) {
    node.append(statusBadge(project[key] ? style : 'muted', `${project[key]} ${label}`));
  }
  return node;
}
function taskTitle(run) { return `#${run.task.number ?? '?'} ${run.task.title || 'Untitled task'}`; }
function executionPosition(run) {
  const execution = run.executions.at(-1);
  if (!execution) return 'Preparing execution';
  if (execution.current_nodes.length) return `Active · ${execution.current_nodes.join(', ')}`;
  if (execution.recent_node) return `Latest · ${execution.recent_node}`;
  return execution.executor_type === 'gitweave' ? 'Awaiting observed progress' : `${execution.executor_type} executor`;
}
function rankedRuns(runs) {
  // Keep live work first, with long-running work ahead of other running Tasks.
  const priority = run => run.long_running ? 0 : run.status === 'running' ? 1 : run.status === 'failed' ? 2 : 3;
  return runs.slice().reverse().sort((a, b) => priority(a) - priority(b));
}
function pageHeader(title, description, right) {
  const header = element('div', null, 'page-header');
  const identity = element('div');
  identity.append(element('h1', title));
  if (description) identity.append(element('p', description, 'muted'));
  header.append(identity);
  if (right) header.append(right);
  return header;
}
function projects() {
  view.append(pageHeader('Projects', 'An overview of work in this coordinator.', element('span', `${state.projects.length} managed`, 'muted')));
  const list = element('section', null, 'project-list');
  list.setAttribute('aria-label', 'Project operations overview');
  const labels = element('div', null, 'project-row project-columns');
  for (const label of ['Project', 'Execution status', 'Relevant task / run', 'Elapsed']) labels.append(element('span', label));
  list.append(labels);
  for (const project of state.projects) {
    const row = element('article', null, 'project-row');
    const identity = element('div', null, 'project-identity');
    identity.append(link(project.name, `#project/${encodeURIComponent(project.name)}`));
    const members = state.runs.filter(run => run.project === project.name);
    identity.append(element('small', `${members.length} ${members.length === 1 ? 'run' : 'runs'} this session`, 'muted'));
    const relevant = rankedRuns(members)[0];
    const task = element('div', null, 'project-task');
    if (relevant) {
      task.append(link(taskTitle(relevant), `#run/${encodeURIComponent(relevant.id)}`),
        element('small', `${relevant.task.repository || 'Repository unavailable'} · ${executionPosition(relevant)}`, 'muted'));
    } else task.append(element('span', 'No tasks launched', 'muted'));
    row.append(identity, counts(project), task, element('span', relevant ? duration(relevant.elapsed_seconds) : '—', 'elapsed'));
    list.append(row);
  }
  if (!state.projects.length) list.append(element('p', 'No managed Projects in this workspace.', 'empty'));
  view.append(list);
}
function projectDetail(name, runId) {
  const project = state.projects.find(project => project.name === name);
  if (!project) { view.append(element('h1', 'Project not found'), link('All Projects', '#')); return; }
  const breadcrumb = element('nav', null, 'breadcrumb');
  breadcrumb.setAttribute('aria-label', 'Breadcrumb');
  breadcrumb.append(link('Projects', '#'), element('span', '/'), element('span', name));
  view.append(breadcrumb);
  const identity = element('div', null, 'project-links');
  identity.append(counts(project));
  if (project.url) identity.append(link('Open GitHub Project ↗', project.url, true));
  view.append(pageHeader(name, 'Tasks and execution progress', identity));
  const runs = rankedRuns(state.runs.filter(run => run.project === name));
  const selected = runs.find(run => run.id === runId) || runs[0];
  const explorer = element('div', null, 'explorer');
  const sidebar = element('aside', null, 'run-list');
  sidebar.setAttribute('aria-label', 'Task runs');
  sidebar.setAttribute('data-scroll', `runs:${name}`);
  const heading = element('div', null, 'section-heading');
  heading.append(element('h2', 'Task runs'), element('span', runs.length, 'muted'));
  sidebar.append(heading);
  for (const run of runs) {
    const item = link('', `#run/${encodeURIComponent(run.id)}`);
    item.className = `run-item${run.id === selected?.id ? ' selected' : ''}`;
    item.dataset.focus = `run:${run.id}`;
    if (run.id === selected?.id) item.setAttribute('aria-current', 'true');
    item.append(badge(run), element('strong', taskTitle(run)), element('small', run.task.repository || 'Repository unavailable', 'muted'));
    const meta = element('div', null, 'run-meta');
    meta.append(element('span', run.executor_type || 'Preparing'), element('span', duration(run.elapsed_seconds), 'elapsed'));
    const started = element('small', `Started ${timestamp(run.started_at)}`, 'muted');
    item.append(meta, started, element('small', executionPosition(run), 'position'));
    sidebar.append(item);
  }
  if (!runs.length) sidebar.append(element('p', 'No Tasks launched by this process.', 'empty'));
  const detail = element('section', null, 'run-detail');
  detail.setAttribute('aria-label', 'Selected run detail');
  if (selected) {
    if (displayedRun !== selected.id) { traceFilter = 'all'; displayedRun = selected.id; }
    runDetail(selected, detail);
  } else detail.append(element('div', 'Task execution details will appear here when work starts.', 'empty'));
  explorer.append(sidebar, detail); view.append(explorer);
}
function logRow(log) {
  const row = element('div', null, `log-row ${lifecycleTypes.has(log.type) ? 'lifecycle' : 'output'}${log.type.includes('stderr') || log.type === 'node_failed' ? ' error-output' : ''}`);
  const time = element('time', timestamp(log.at, true));
  if (log.at) { time.dateTime = log.at; time.title = timestamp(log.at); }
  const source = element('span', null, 'log-source');
  source.append(element('span', log.type.replaceAll('_', ' '), 'log-stream'));
  source.append(element('span', log.node_id || 'run', 'log-node'));
  source.title = [log.execution_id, log.node_id, log.instance_id].filter(Boolean).join(' · ') || 'Run output; node attribution unavailable';
  row.append(time, source, element('code', log.text));
  return row;
}
function workflow(run, execution) {
  const key = `${run.id}:${execution.id}`;
  const panel = element('section', null, 'workflow');
  const heading = element('div', null, 'section-heading');
  heading.append(element('h3', 'GitWeave workflow'), element('span', execution.id, 'mono muted'));
  panel.append(heading);
  const context = element('div', null, 'workflow-context');
  context.append(element('span', `Active · ${execution.current_nodes.join(', ') || (run.status === 'running' ? 'Unknown without live events' : 'None')}`),
    element('span', `Latest · ${execution.recent_node || 'Not observed'}`), element('span', `Elapsed · ${duration(run.elapsed_seconds)}`));
  if (execution.graph_path) context.append(element('span', `Graph · ${execution.graph_path}`, 'mono'));
  panel.append(context);
  if (execution.graph?.nodes.length) {
    const nodes = execution.graph.nodes;
    const requested = selectedNodes.get(key);
    const selected = nodes.find(node => node.id === requested) || nodes.find(node => execution.current_nodes.includes(node.id)) ||
      nodes.find(node => node.id === execution.recent_node) || nodes[0];
    const layout = element('div', null, 'workflow-layout');
    const visualization = element('div', null, 'workflow-visualization');
    visualization.append(WorkflowGraph.create(execution.graph, selected.id, id => { selectedNodes.set(key, id); render(); }, key, statusNames));
    const legend = element('div', null, 'graph-legend');
    for (const status of ['completed', 'active', 'not_executed', 'failed', 'unknown']) legend.append(statusBadge(status));
    visualization.append(legend, element('p', 'Pending means not yet observed; conditional branches may never execute.', 'graph-note muted'));
    const inspector = element('aside', null, 'node-detail');
    inspector.setAttribute('aria-label', `Selected node for ${execution.id}`);
    inspector.append(element('h4', selected.id), statusBadge(selected.status), element('p', selected.kind || 'Node', 'muted'));
    const follow = button('Follow current node', () => { selectedNodes.delete(key); render(); }, 'text-button');
    follow.dataset.focus = `follow:${key}`;
    inspector.append(follow, element('h4', 'Recent node output'));
    const logs = run.logs.filter(log => log.execution_id === execution.id && log.node_id === selected.id).slice(-6);
    const output = element('div', null, 'node-logs');
    output.setAttribute('data-scroll', `node-logs:${key}:${selected.id}`);
    output.dataset.follow = 'true';
    for (const log of logs) output.append(logRow(log));
    if (!logs.length) output.append(element('p', 'No attributed output for this node yet. Unattributed output remains in the execution trace.', 'muted'));
    inspector.append(output);
    layout.append(visualization, inspector); panel.append(layout);
  } else panel.append(element('p', `Graph unavailable: ${execution.graph_error || 'No graph nodes available'}`, 'empty'));
  panel.append(element('p', `GitWeave Run ID · ${execution.run_id || 'Not yet available'}`, 'execution-id mono muted'));
  return panel;
}
function executionTrace(run) {
  const panel = element('section', null, 'trace');
  const heading = element('div', null, 'section-heading');
  heading.append(element('h3', 'Execution trace'));
  const controls = element('div', null, 'trace-controls');
  const filter = element('select');
  filter.setAttribute('aria-label', 'Filter execution trace');
  filter.dataset.focus = 'trace-filter';
  for (const [value, text] of [['all', 'All output'], ['lifecycle', 'Lifecycle events'], ['output', 'Agent / subprocess output']]) {
    const option = element('option', text); option.value = value; filter.append(option);
  }
  filter.value = traceFilter;
  filter.addEventListener('change', () => { traceFilter = filter.value; render(); });
  const latest = button('Jump to latest', () => { const logs = document.getElementById('logs'); logs.scrollTop = logs.scrollHeight; }, 'text-button');
  latest.dataset.focus = 'latest';
  controls.append(filter, latest); heading.append(controls); panel.append(heading);
  const logs = element('div', null, 'trace-output'); logs.id = 'logs';
  logs.setAttribute('data-scroll', `trace:${run.id}:${traceFilter}`);
  logs.dataset.focus = 'trace';
  logs.dataset.follow = 'true';
  logs.tabIndex = 0;
  logs.setAttribute('role', 'region'); logs.setAttribute('aria-label', 'Recent execution logs');
  const filtered = run.logs.filter(log => traceFilter === 'all' || lifecycleTypes.has(log.type) === (traceFilter === 'lifecycle'));
  for (const log of filtered) logs.append(logRow(log));
  if (!filtered.length) logs.append(element('p', run.logs.length ? 'No entries match this filter.' : 'No output available yet.', 'empty'));
  panel.append(logs, element('p', 'Recent output is bounded. Scroll up to pause following; jump to latest to resume.', 'trace-note muted'));
  return panel;
}
function runDetail(run, container) {
  const header = element('div', null, 'run-heading');
  header.append(element('p', 'SELECTED RUN', 'eyebrow'), element('h2', taskTitle(run)), badge(run));
  if (run.task.url) header.append(link('Open GitHub Issue ↗', run.task.url, true));
  container.append(header);
  const details = element('dl', null, 'run-facts');
  for (const [label, value] of [['Repository', run.task.repository || 'Unavailable'], ['Executor', run.executor_type || 'Preparing'],
    ['Started', timestamp(run.started_at)], ['Elapsed', duration(run.elapsed_seconds)],
    ['Ended', run.ended_at ? timestamp(run.ended_at) : 'Still running'], ['Position', executionPosition(run)],
    ['ProjectWeave Run ID', run.run_id || 'Not yet available']]) {
    const fact = element('div', null, label === 'ProjectWeave Run ID' ? 'run-identity' : '');
    fact.append(element('dt', label), element('dd', value)); details.append(fact);
  }
  container.append(details);
  for (const execution of run.executions) if (execution.executor_type === 'gitweave') container.append(workflow(run, execution));
  if (run.failure) {
    const failure = element('section', null, 'failure-detail');
    failure.append(element('h3', 'Failure'), element('pre', JSON.stringify(run.failure, null, 2))); container.append(failure);
  }
  container.append(executionTrace(run));
}
function render() {
  // Keep pointer capture intact while dragging; the next poll applies live state.
  if (WorkflowGraph.interacting) return;
  // Polling must preserve list/log scroll, trace follow state, and keyboard focus.
  const scroll = new Map(Array.from(view.querySelectorAll('[data-scroll]'), node => [node.dataset.scroll,
    {top: node.scrollTop, left: node.scrollLeft,
      bottom: node.scrollTop + node.clientHeight >= node.scrollHeight - 10}]));
  const focusKey = document.activeElement?.dataset.focus;
  view.replaceChildren();
  const [kind, ...parts] = location.hash.slice(1).split('/');
  let id;
  try { id = decodeURIComponent(parts.join('/')); } catch { id = ''; }
  if (kind === 'project') projectDetail(id);
  else if (kind === 'run') {
    const run = state.runs.find(run => run.id === id);
    if (run) projectDetail(run.project, id);
    else view.append(element('h1', 'Run not found'), link('All Projects', '#'));
  } else projects();
  if (focusKey) Array.from(view.querySelectorAll('[data-focus]')).find(node => node.dataset.focus === focusKey)?.focus({preventScroll: true});
  for (const node of view.querySelectorAll('[data-scroll]')) {
    const previous = scroll.get(node.dataset.scroll);
    node.scrollTop = node.dataset.follow && (!previous || previous.bottom) ? node.scrollHeight : previous?.top || 0;
    node.scrollLeft = previous?.left || 0;
  }
  for (const canvas of view.querySelectorAll('.graph-wrap')) canvas.initializeGraph();
}
async function refresh() {
  try {
    const response = await fetch('/api/state', {cache: 'no-store'});
    if (!response.ok) throw new Error('Dashboard unavailable');
    state = await response.json();
    connection.textContent = 'Live · 1s'; connection.className = 'connection live'; render();
  } catch { connection.textContent = 'Coordinator unavailable. Reconnecting…'; connection.className = 'connection disconnected'; }
  setTimeout(refresh, 1000);
}
window.addEventListener('hashchange', () => { if (state) render(); });
document.querySelector('.skip-link').addEventListener('click', event => {
  event.preventDefault(); view.focus();
});
refresh();
