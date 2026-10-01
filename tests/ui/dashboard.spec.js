const {test, expect} = require('@playwright/test');

function fixture() {
  const started = '2026-10-01T08:00:00Z';
  const graph = {
    nodes: [
      {id: 'readiness', kind: 'command', status: 'completed'},
      {id: 'implement', kind: 'agent', status: 'active'},
      {id: 'validate', kind: 'command', status: 'not_executed'},
      {id: 'review', kind: 'agent', status: 'failed'},
      {id: 'publish', kind: 'command', status: 'unknown'},
    ],
    edges: [['readiness', 'implement'], ['implement', 'validate'], ['validate', 'review'], ['review', 'publish']],
  };
  const task = {number: 81, title: 'Redesign the workflow explorer', repository: 'team/projectweave',
    url: 'https://github.com/team/projectweave/issues/81'};
  const running = {
    id: 'run-live', project: 'Operations', task, status: 'running', long_running: true,
    started_at: started, ended_at: null, elapsed_seconds: 4200, run_id: 'pw-live',
    executor_type: 'gitweave', failure: null,
    executions: [{id: 'execute-1', executor_type: 'gitweave', run_id: 'gw-live', current_nodes: ['implement'], recent_node: 'implement', graph}],
    logs: [
      {at: started, type: 'node_started', node_id: 'implement', instance_id: 'implement-1', execution_id: 'execute-1', text: 'implement'},
      {at: started, type: 'agent_output', node_id: 'implement', execution_id: 'execute-1', text: 'Updating dashboard styles'},
      {at: started, type: 'stdout', execution_id: 'execute-1', text: 'Unattributed executor output'},
      {at: started, type: 'command_stderr', node_id: 'review', execution_id: 'execute-1', text: 'Review failed'},
    ],
  };
  const failed = {...structuredClone(running), id: 'run-failed', run_id: 'pw-failed', task: {...task, number: 82, title: 'Run command checks'},
    executor_type: 'command', status: 'failed', long_running: false, elapsed_seconds: 30, ended_at: started,
    failure: {message: 'Command exited with status 7'}, executions: [], logs: []};
  const complete = {...structuredClone(failed), id: 'run-completed', status: 'completed', failure: null};
  return {
    projects: [{name: 'Operations', url: 'https://github.com/orgs/team/projects/1', running: 1, long_running: 1, failed: 1},
      {name: 'Idle', url: null, running: 0, long_running: 0, failed: 0}],
    runs: [running, failed, complete], long_running_seconds: 3600,
  };
}
async function setup(page, snapshot, hash = '') {
  await page.route('**/api/state', route => route.fulfill({json: snapshot}));
  await page.goto(`/${hash}`);
  await expect(page.getByRole('status')).toHaveText('Live · 1s');
}
function inspector(page, execution = 'execute-1') { return page.getByRole('complementary', {name: `Selected node for ${execution}`}); }
function graphNode(page, name) { return page.getByRole('button', {name: new RegExp(`^${name} \\(`)}); }
async function noOverflow(page) {
  const width = await page.evaluate(() => ({page: document.documentElement.scrollWidth, viewport: window.innerWidth}));
  expect(width.page, `Page width ${width.page} exceeds viewport ${width.viewport}`).toBeLessThanOrEqual(width.viewport);
}

test('Projects overview surfaces active work and compact operational counts', async ({page}, testInfo) => {
  await setup(page, fixture());
  const operations = page.locator('.project-row').filter({has: page.getByRole('link', {name: 'Operations', exact: true})});
  await expect(operations).toContainText('1 Running');
  await expect(operations).toContainText('1 Long running');
  await expect(operations).toContainText('1 Failed');
  await expect(operations).toContainText('#81 Redesign the workflow explorer');
  await expect(operations).toContainText('Active · implement');
  await expect(operations).toContainText('1h 10m 0s');
  await expect(page.locator('.project-row').filter({has: page.getByRole('link', {name: 'Idle', exact: true})})).toContainText('No tasks launched');
  await noOverflow(page);
  await page.screenshot({path: testInfo.outputPath('projects.png'), fullPage: true});
  await page.getByRole('link', {name: 'Operations', exact: true}).click();
  await expect(page.getByRole('heading', {name: '#81 Redesign the workflow explorer'})).toBeVisible();
  await expect(page.getByRole('link', {name: 'Open GitHub Project ↗'})).toHaveAttribute('href', 'https://github.com/orgs/team/projects/1');
});

test('Selecting a run keeps the Project and task list available, including generic failures', async ({page}) => {
  await setup(page, fixture(), '#run/run-live');
  await expect(page.getByRole('heading', {name: 'Operations', exact: true})).toBeVisible();
  await expect(page.locator('.run-item')).toHaveCount(3);
  await expect(page.locator('.run-item').first()).toHaveAttribute('aria-current', 'true');
  await page.locator('.run-item[href="#run/run-failed"]').click();
  await expect(page.getByRole('heading', {name: 'Failure', exact: true})).toBeVisible();
  await expect(page.locator('.failure-detail')).toContainText('Command exited with status 7');
  await expect(page.locator('.run-facts')).toContainText('command');
  await expect(page.locator('.workflow')).toHaveCount(0);
  await expect(page.locator('.run-item')).toHaveCount(3);
  await noOverflow(page);
});

test('Graph nodes are selectable by keyboard, with scoped output and persistent focus on refresh', async ({page}, testInfo) => {
  const snapshot = fixture();
  await setup(page, snapshot, '#run/run-live');
  await expect(inspector(page)).toContainText('implement');
  await expect(inspector(page)).toContainText('Updating dashboard styles');
  await expect(inspector(page)).not.toContainText('Unattributed executor output');
  await page.screenshot({path: testInfo.outputPath('workflow.png'), fullPage: true});
  for (const status of ['completed', 'active', 'not_executed', 'failed', 'unknown']) {
    await expect(page.locator(`.graph-node.${status}`)).toHaveCount(1);
  }
  await graphNode(page, 'review').focus();
  await graphNode(page, 'review').press('Enter');
  await expect(inspector(page)).toContainText('Review failed');
  snapshot.runs[0].elapsed_seconds++;
  await expect(page.locator('.run-facts')).toContainText('1h 10m 1s');
  await expect(graphNode(page, 'review')).toBeFocused();
  await expect(graphNode(page, 'review')).toHaveAttribute('aria-pressed', 'true');
  await inspector(page).getByRole('button', {name: 'Follow current node'}).click();
  snapshot.runs[0].executions[0].current_nodes = ['validate'];
  snapshot.runs[0].executions[0].graph.nodes[1].status = 'completed';
  snapshot.runs[0].executions[0].graph.nodes[2].status = 'active';
  await expect(inspector(page).getByRole('heading', {name: 'validate', exact: true})).toBeVisible();
  await expect(inspector(page)).toContainText('No attributed output');
});

test('Following progress reveals distant nodes without scrolling the page, and keyboard regions retain focus', async ({page}) => {
  const snapshot = fixture();
  const execution = snapshot.runs[0].executions[0];
  execution.current_nodes = ['publish'];
  await setup(page, snapshot, '#run/run-live');
  const graph = page.locator('.graph-wrap');
  const visible = () => graph.evaluate(wrap => {
    const node = wrap.querySelector('.graph-node.selected').getBBox();
    return node.x >= wrap.scrollLeft && node.x + node.width <= wrap.scrollLeft + wrap.clientWidth;
  });
  await expect.poll(visible).toBe(true);
  await graph.focus();
  snapshot.runs[0].elapsed_seconds++;
  await expect(page.locator('.run-facts')).toContainText('1h 10m 1s');
  await expect(graph).toBeFocused();
  const logs = page.getByRole('region', {name: 'Recent execution logs'});
  await logs.focus();
  snapshot.runs[0].elapsed_seconds++;
  await expect(page.locator('.run-facts')).toContainText('1h 10m 2s');
  await expect(logs).toBeFocused();
  await page.locator('.skip-link').focus();
  await page.locator('.skip-link').press('Enter');
  await expect(page.locator('#view')).toBeFocused();
  expect(new URL(page.url()).hash).toBe('#run/run-live');
  await noOverflow(page);
});

test('Trace filters lifecycle events, keeps older output accessible, and follows recent output', async ({page}) => {
  const snapshot = fixture();
  snapshot.runs[0].logs.push(...Array.from({length: 90}, (_, i) => ({at: snapshot.runs[0].started_at, type: 'stdout', text: `line ${i}`})));
  await setup(page, snapshot, '#run/run-live');
  const logs = page.getByRole('region', {name: 'Recent execution logs'});
  await expect.poll(() => logs.evaluate(node => node.scrollTop + node.clientHeight >= node.scrollHeight - 10)).toBe(true);
  await logs.evaluate(node => { node.scrollTop = 0; });
  snapshot.runs[0].logs.push({at: snapshot.runs[0].started_at, type: 'stdout', text: 'New output while reading older entries'});
  await expect(logs).toContainText('New output while reading older entries');
  expect(await logs.evaluate(node => node.scrollTop)).toBe(0);
  await page.getByRole('button', {name: 'Jump to latest'}).click();
  await expect.poll(() => logs.evaluate(node => node.scrollTop + node.clientHeight >= node.scrollHeight - 10)).toBe(true);
  snapshot.runs[0].logs.push({type: 'stdout', text: 'Newest output'});
  await expect(logs).toContainText('Newest output');
  await expect.poll(() => logs.evaluate(node => node.scrollTop + node.clientHeight >= node.scrollHeight - 10)).toBe(true);
  const filter = page.getByRole('combobox', {name: 'Filter execution trace'});
  await filter.selectOption('lifecycle');
  await expect(logs.locator('.log-row')).toHaveCount(1);
  await expect(logs.locator('.lifecycle')).toContainText('node started');
  await filter.selectOption('output');
  await expect(logs.locator('.lifecycle')).toHaveCount(0);
  await expect(logs).toContainText('Unattributed executor output');
  await expect(logs).toContainText('command stderr');
  await noOverflow(page);
});

test('Graph scroll survives polling and repeated executors keep node output separate', async ({page}) => {
  const snapshot = fixture();
  const run = snapshot.runs[0];
  run.executions.push({...structuredClone(run.executions[0]), id: 'execute-2', run_id: 'gw-second'});
  run.logs.push({at: run.started_at, type: 'agent_output', node_id: 'implement', execution_id: 'execute-2', text: 'Second invocation only'});
  await setup(page, snapshot, '#run/run-live');
  await expect(inspector(page, 'execute-1')).not.toContainText('Second invocation only');
  await expect(inspector(page, 'execute-2')).toContainText('Second invocation only');
  await expect(inspector(page, 'execute-2')).not.toContainText('Updating dashboard styles');
  const graph = page.locator('.graph-wrap').first();
  await graph.evaluate(node => { node.scrollLeft = 120; });
  const left = await graph.evaluate(node => node.scrollLeft);
  expect(left).toBeGreaterThan(0);
  run.elapsed_seconds = 4300;
  await expect(page.locator('.run-facts')).toContainText('1h 11m 40s');
  expect(await graph.evaluate(node => node.scrollLeft)).toBe(left);
});

test('Empty, missing graph and unknown progress states remain useful', async ({page}) => {
  const snapshot = fixture();
  snapshot.runs[0].executions[0].graph = null;
  snapshot.runs[0].executions[0].graph_error = 'Graph file missing';
  snapshot.runs[0].executions[0].current_nodes = [];
  snapshot.runs[0].executions[0].recent_node = null;
  await setup(page, snapshot, '#run/run-live');
  await expect(page.locator('.workflow')).toContainText('Graph unavailable: Graph file missing');
  await expect(page.locator('.workflow')).toContainText('Unknown without live events');
  await page.goto('/#project/Idle');
  await expect(page.getByText('No Tasks launched by this process.')).toBeVisible();
  await page.goto('/#run/missing');
  await expect(page.getByRole('heading', {name: 'Run not found'})).toBeVisible();
  await page.goto('/#project/%E0%A4%A');
  await expect(page.getByRole('heading', {name: 'Project not found'})).toBeVisible();
});

test('Untrusted content stays text and external URLs are constrained to GitHub HTTPS', async ({page}) => {
  const snapshot = fixture();
  snapshot.runs[0].task.title = '<img src=x onerror="window.injected=true">';
  snapshot.runs[0].task.url = 'javascript:window.injected=true';
  snapshot.projects[0].url = 'https://example.com/';
  snapshot.runs[0].logs[0].text = '<script>window.injected=true</script>';
  await setup(page, snapshot, '#run/run-live');
  await expect(page.locator('.run-heading h2')).toContainText('<img src=x');
  await expect(page.locator('#logs')).toContainText('<script>window.injected=true</script>');
  await expect(page.getByRole('link', {name: 'Open GitHub Issue ↗'})).toHaveCount(0);
  await expect(page.getByRole('link', {name: 'Open GitHub Project ↗'})).toHaveCount(0);
  expect(await page.evaluate(() => window.injected)).toBeUndefined();
});

test('Disconnects show a status and recover while keeping the selected run', async ({page}) => {
  const snapshot = fixture();
  let online = true;
  await page.route('**/api/state', route => online ? route.fulfill({json: snapshot}) : route.fulfill({status: 503}));
  await page.goto('/#run/run-live');
  await expect(page.getByRole('status')).toHaveText('Live · 1s');
  online = false;
  await expect(page.getByRole('status')).toContainText('Reconnecting');
  await expect(page.locator('.run-heading')).toContainText('Redesign the workflow explorer');
  online = true;
  await expect(page.getByRole('status')).toHaveText('Live · 1s');
  await expect(page.locator('.run-item[aria-current]')).toHaveAttribute('href', '#run/run-live');
});

test('Light and dark layouts avoid page overflow with long identifiers at small widths', async ({page}, testInfo) => {
  const snapshot = fixture();
  const longName = 'Project/'.repeat(18);
  snapshot.projects[0].name = longName;
  snapshot.runs.forEach(run => { run.project = longName; });
  snapshot.runs[0].task.title = 'LongIssueTitle'.repeat(20);
  snapshot.runs[0].run_id = 'long-run-id'.repeat(20);
  await setup(page, snapshot, '#run/run-live');
  await noOverflow(page);
  await page.screenshot({path: testInfo.outputPath('workflow-light.png'), fullPage: true});
  await page.emulateMedia({colorScheme: 'dark'});
  await noOverflow(page);
  expect(await page.locator('body').evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--bg').trim())).toBe('#14171d');
  await page.setViewportSize({width: 320, height: 740});
  await noOverflow(page);
  await page.goto('/');
  await expect(page.getByRole('heading', {name: 'Projects', exact: true})).toBeVisible();
  await noOverflow(page);
});
