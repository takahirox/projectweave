const {test} = require('node:test');
const assert = require('node:assert/strict');
const {layout} = require('../projectweave/web/graph.js');

function graph(ids, edges) { return {nodes: ids.map(id => ({id, status: 'not_executed'})), edges}; }
function noOverlap(positions) {
  const nodes = [...positions.values()];
  nodes.forEach((a, i) => nodes.slice(i + 1).forEach(b => {
    assert.ok(a.x + 174 <= b.x || b.x + 174 <= a.x || a.y + 58 <= b.y || b.y + 58 <= a.y);
  }));
}

test('linear execution runs downward and layout never changes serialized data', () => {
  const input = graph(['a', 'b', 'c'], [['a', 'b'], ['b', 'c']]);
  const original = structuredClone(input);
  const {positions} = layout(input);
  assert.equal(positions.get('a').x, positions.get('c').x);
  assert.ok(positions.get('a').y < positions.get('b').y);
  assert.ok(positions.get('b').y < positions.get('c').y);
  positions.get('a').x = -500;
  assert.deepEqual(input, original);
});

test('parallel branches, nested fan-out and joins occupy distinct centered layers', () => {
  const input = graph(['entry', 'left', 'right', 'x', 'y', 'z', 'join', 'exit'],
    [['entry', 'left'], ['entry', 'right'], ['left', 'x'], ['left', 'y'], ['right', 'z'],
      ['x', 'join'], ['y', 'join'], ['z', 'join'], ['join', 'exit']]);
  const {positions: p} = layout(input);
  assert.equal(p.get('left').y, p.get('right').y);
  assert.notEqual(p.get('left').x, p.get('right').x);
  assert.equal(p.get('x').y, p.get('z').y);
  assert.ok(p.get('join').y > p.get('x').y);
  assert.equal(p.get('entry').x, p.get('join').x);
  noOverlap(p);
  assert.deepEqual(layout(input).positions, p);
});

test('loop bodies and downstream joins retain direction even with unordered definitions', () => {
  const input = graph(['exit', 'body', 'entry', 'head', 'retry'],
    [['entry', 'head'], ['head', 'body'], ['body', 'retry'], ['retry', 'head'], ['retry', 'exit']]);
  const {positions: p, edges} = layout(input);
  for (const [a, b] of [['entry', 'head'], ['head', 'body'], ['body', 'retry'], ['retry', 'exit']])
    assert.ok(p.get(a).y < p.get(b).y);
  assert.deepEqual(edges, input.edges);
  noOverlap(p);
});

test('barycentric ordering uncrosses branches whose definitions are reversed', () => {
  const {positions: p} = layout(graph(['start', 'a', 'b', 'd', 'c', 'end'],
    [['start', 'a'], ['start', 'b'], ['a', 'c'], ['b', 'd'], ['c', 'end'], ['d', 'end']]));
  assert.ok((p.get('a').x - p.get('b').x) * (p.get('c').x - p.get('d').x) > 0);
});

test('self loops, disconnected cycles, isolated nodes and missing endpoints remain finite', () => {
  const input = graph(['a', 'b', 'c', 'isolated'], [['a', 'a'], ['b', 'c'], ['c', 'b'], ['missing', 'a']]);
  const {positions, edges} = layout(input);
  assert.equal(positions.size, 4);
  assert.equal(edges.length, 3);
  for (const p of positions.values()) assert.ok(Number.isFinite(p.x) && Number.isFinite(p.y));
  noOverlap(positions);
  assert.equal(layout(graph([], [])).positions.size, 0);
});

test('large cycles do not depend on the JavaScript recursion limit', () => {
  const ids = Array.from({length: 12000}, (_, i) => String(i));
  const edges = ids.map((id, i) => [id, ids[(i + 1) % ids.length]]);
  assert.equal(layout(graph(ids, edges)).positions.size, ids.length);
});
