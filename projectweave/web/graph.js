'use strict';

// A dependency-free directed layout and SVG canvas. Positions belong only to the
// view, keyed by run/execution; neither layout nor interaction changes API data.
const WorkflowGraph = (() => {
  const WIDTH = 174, HEIGHT = 58, X_GAP = 54, Y_GAP = 58;
  const sessions = new Map();
  let sequence = 0, interacting = false;

  function layout(graph) {
    const ids = graph.nodes.map(node => node.id);
    const outgoing = new Map(ids.map(id => [id, []]));
    const incoming = new Map(ids.map(id => [id, []]));
    const edges = graph.edges.filter(([a, b]) => outgoing.has(a) && outgoing.has(b));
    edges.forEach(([a, b]) => { outgoing.get(a).push(b); incoming.get(b).push(a); });
    // Remove only DFS back edges for ranking. Keep every edge for rendering,
    // so loops do not strand their body or subsequent joins in arbitrary ranks.
    const colors = new Map(), back = new Set();
    const roots = ids.filter(id => !incoming.get(id).length);
    for (const root of [...roots, ...ids]) {
      if (colors.has(root)) continue;
      colors.set(root, 1);
      const stack = [{id: root, next: 0}];
      while (stack.length) {
        const frame = stack.at(-1), targets = outgoing.get(frame.id);
        if (frame.next === targets.length) { colors.set(frame.id, 2); stack.pop(); continue; }
        const target = targets[frame.next++];
        if (colors.get(target) === 1) back.add(JSON.stringify([frame.id, target]));
        else if (!colors.has(target)) { colors.set(target, 1); stack.push({id: target, next: 0}); }
      }
    }
    const forward = edges.filter(edge => !back.has(JSON.stringify(edge)));
    const parents = new Map(ids.map(id => [id, []]));
    const children = new Map(ids.map(id => [id, []]));
    forward.forEach(([a, b]) => { parents.get(b).push(a); children.get(a).push(b); });
    const remaining = new Map(ids.map(id => [id, parents.get(id).length]));
    const rank = new Map(ids.map(id => [id, 0]));
    const queue = ids.filter(id => !remaining.get(id));
    for (let i = 0; i < queue.length; i++) {
      const id = queue[i];
      for (const child of children.get(id)) {
        rank.set(child, Math.max(rank.get(child), rank.get(id) + 1));
        remaining.set(child, remaining.get(child) - 1);
        if (!remaining.get(child)) queue.push(child);
      }
    }
    const layers = [];
    for (const id of ids) (layers[rank.get(id)] ||= []).push(id);
    const order = new Map();
    const measure = () => layers.forEach(layer => layer.forEach((id, i) => order.set(id, i - (layer.length - 1) / 2)));
    measure();
    // Alternating barycentric sweeps reduce crossings while keeping ties stable.
    for (let pass = 0; pass < 6; pass++) {
      const neighbors = pass % 2 ? children : parents;
      const sweep = pass % 2 ? [...layers].reverse() : layers;
      for (const layer of sweep) {
        const center = id => {
          const adjacent = neighbors.get(id);
          return adjacent.length ? adjacent.reduce((sum, other) => sum + order.get(other), 0) / adjacent.length : order.get(id);
        };
        layer.sort((a, b) => center(a) - center(b) || order.get(a) - order.get(b));
        layer.forEach((id, i) => order.set(id, i - (layer.length - 1) / 2));
      }
    }
    const columns = Math.max(1, ...layers.map(layer => layer.length));
    const positions = new Map();
    layers.forEach((layer, row) => layer.forEach((id, column) => positions.set(id,
      {x: 32 + ((columns - layer.length) / 2 + column) * (WIDTH + X_GAP), y: 32 + row * (HEIGHT + Y_GAP)})));
    return {positions, edges};
  }

  function create(graph, selected, selectNode, key, names) {
    const signature = JSON.stringify([graph.nodes.map(node => node.id), graph.edges]);
    let session = sessions.get(key);
    if (!session || session.signature !== signature) {
      const automatic = layout(graph);
      session = {signature, positions: automatic.positions, edges: automatic.edges, x: 0, y: 0, scale: 1, initialized: false};
      sessions.set(key, session);
    }
    const positions = session.positions, edges = session.edges;
    const html = (tag, text, className) => {
      const node = document.createElement(tag);
      if (text !== undefined) node.textContent = text;
      if (className) node.className = className;
      return node;
    };
    const svgNode = (tag, attrs = {}, text) => {
      const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
      Object.entries(attrs).forEach(([name, value]) => node.setAttribute(name, value));
      if (text !== undefined) node.textContent = text;
      return node;
    };
    const container = html('div', undefined, 'graph-canvas');
    const toolbar = html('div', undefined, 'graph-controls');
    toolbar.setAttribute('role', 'group'); toolbar.setAttribute('aria-label', 'Workflow canvas controls');
    const wrap = html('div', undefined, 'graph-wrap');
    wrap.dataset.focus = `graph:${key}`;
    wrap.tabIndex = 0;
    wrap.setAttribute('role', 'region'); wrap.setAttribute('aria-label', 'Interactive workflow graph');
    const hint = html('p', 'Drag nodes to arrange · drag background to pan · scroll to zoom', 'graph-hint muted');
    hint.id = `graph-hint-${sequence++}`;
    wrap.setAttribute('aria-describedby', hint.id);
    const svg = svgNode('svg', {class: 'graph', width: '100%', height: '100%', role: 'group', 'aria-label': 'GitWeave graph progress'});
    const marker = svgNode('marker', {id: `arrow-${sequence++}`, markerWidth: 8, markerHeight: 8, refX: 7, refY: 4, orient: 'auto'});
    marker.append(svgNode('path', {d: 'M0,0 L8,4 L0,8', class: 'arrow'}));
    const defs = svgNode('defs'); defs.append(marker); svg.append(defs);
    const world = svgNode('g', {class: 'graph-world'}); svg.append(world);
    const paths = edges.map(([a, b]) => {
      const path = svgNode('path', {class: 'edge', 'marker-end': `url(#${marker.id})`, 'data-source': a, 'data-target': b});
      world.append(path); return path;
    });
    const groups = new Map();
    let initializedView = false;
    for (const node of graph.nodes) {
      const label = names[node.status] || node.status;
      const group = svgNode('g', {class: `graph-node ${node.status}${node.id === selected ? ' selected' : ''}`,
        role: 'button', tabindex: 0, 'aria-label': `${node.id} (${node.kind || 'node'}): ${label}`,
        'aria-pressed': node.id === selected, 'data-focus': `node:${key}:${node.id}`, 'data-node-id': node.id});
      group.append(svgNode('title', {}, `${node.id} (${node.kind || 'node'}): ${label}`),
        svgNode('rect', {width: WIDTH, height: HEIGHT, rx: 6}),
        svgNode('text', {x: 12, y: 23}, node.id.length > 21 ? node.id.slice(0, 20) + '…' : node.id),
        svgNode('text', {x: 12, y: 44, class: 'node-status'}, label));
      group.addEventListener('click', event => {
        if (suppressClick) { suppressClick = false; event.preventDefault(); return; }
        selectNode(node.id);
      });
      group.addEventListener('keydown', event => {
        if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); selectNode(node.id); }
      });
      group.addEventListener('focus', () => { if (initializedView && !interacting) reveal(node.id); });
      groups.set(node.id, group); world.append(group);
    }
    const zoomLabel = html('output'); zoomLabel.setAttribute('aria-label', 'Canvas zoom');
    function paintViewport() {
      world.setAttribute('transform', `translate(${session.x} ${session.y}) scale(${session.scale})`);
      zoomLabel.textContent = `${Math.round(session.scale * 100)}%`;
    }
    function bounds() {
      const values = [...positions.values()];
      const left = Math.min(...values.map(p => p.x)), top = Math.min(...values.map(p => p.y)) - 24;
      return {left, top, right: Math.max(...values.map(p => p.x + WIDTH)) + 60 + edges.length * 3,
        bottom: Math.max(...values.map(p => p.y + HEIGHT)) + 24};
    }
    function paintNodes() {
      groups.forEach((group, id) => {
        const p = positions.get(id); group.setAttribute('transform', `translate(${p.x} ${p.y})`);
      });
      const right = bounds().right;
      edges.forEach(([a, b], i) => {
        const start = positions.get(a), end = positions.get(b);
        const x1 = start.x + WIDTH / 2, y1 = start.y + HEIGHT, x2 = end.x + WIDTH / 2, y2 = end.y;
        let d;
        if (y2 > y1 && y2 - y1 <= Y_GAP + 1) {
          const middle = (y1 + y2) / 2;
          d = `M${x1},${y1} C${x1},${middle} ${x2},${middle} ${x2},${y2}`;
        } else {
          // Route feedback, self loops and long skip edges outside node columns.
          const lane = right - 12 - i * 3;
          d = `M${start.x + WIDTH},${start.y + HEIGHT / 2} C${lane},${start.y + HEIGHT / 2} ${lane},${start.y + HEIGHT + 24} ${lane},${start.y + HEIGHT + 24}` +
            ` L${lane},${end.y - 24} C${lane},${end.y - 24} ${x2},${end.y - 24} ${x2},${y2}`;
        }
        paths[i].setAttribute('d', d);
      });
    }
    function fit(minimum = .2) {
      const box = bounds();
      session.scale = Math.max(minimum, Math.min(1, (wrap.clientWidth - 40) / (box.right - box.left),
        (wrap.clientHeight - 40) / (box.bottom - box.top)));
      session.x = (wrap.clientWidth - (box.left + box.right) * session.scale) / 2;
      session.y = (wrap.clientHeight - (box.top + box.bottom) * session.scale) / 2;
      paintViewport();
    }
    function reveal(id) {
      if (!wrap.isConnected) return;
      const p = positions.get(id), s = session.scale, margin = 12;
      if (p.x * s + session.x < margin || (p.x + WIDTH) * s + session.x > wrap.clientWidth - margin)
        session.x = (wrap.clientWidth - WIDTH * s) / 2 - p.x * s;
      if (p.y * s + session.y < margin || (p.y + HEIGHT) * s + session.y > wrap.clientHeight - margin)
        session.y = (wrap.clientHeight - HEIGHT * s) / 2 - p.y * s;
      paintViewport();
    }
    function zoom(factor, x = wrap.clientWidth / 2, y = wrap.clientHeight / 2) {
      const next = Math.max(.2, Math.min(2.5, session.scale * factor)), ratio = next / session.scale;
      session.x = x - (x - session.x) * ratio; session.y = y - (y - session.y) * ratio;
      session.scale = next; paintViewport();
    }
    for (const [label, action] of [['Zoom out', () => zoom(1 / 1.2)], ['Zoom in', () => zoom(1.2)],
      ['Fit graph', () => fit()], ['Auto layout', () => { const fresh = layout(graph).positions;
        positions.clear(); fresh.forEach((p, id) => positions.set(id, p)); paintNodes(); fit(); }]]) {
      const control = html('button', label); control.type = 'button';
      control.dataset.focus = `${label}:${key}`; control.addEventListener('click', action); toolbar.append(control);
    }
    toolbar.append(zoomLabel);
    wrap.addEventListener('wheel', event => {
      event.preventDefault(); const box = wrap.getBoundingClientRect();
      const delta = event.deltaY * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? wrap.clientHeight : 1);
      zoom(Math.exp(-Math.max(-200, Math.min(200, delta)) * .003), event.clientX - box.left, event.clientY - box.top);
    }, {passive: false});
    wrap.addEventListener('keydown', event => {
      const movement = {ArrowLeft: [40, 0], ArrowRight: [-40, 0], ArrowUp: [0, 40], ArrowDown: [0, -40]}[event.key];
      if (movement) { session.x += movement[0]; session.y += movement[1]; paintViewport(); }
      else if (event.key === '+' || event.key === '=') zoom(1.2);
      else if (event.key === '-') zoom(1 / 1.2);
      else if (event.key === '0') fit();
      else return;
      event.preventDefault();
    });
    let gesture = null, suppressClick = false;
    wrap.addEventListener('pointerdown', event => {
      if (gesture || event.button !== 0) return;
      const group = event.target.closest('.graph-node'), id = group?.dataset.nodeId;
      const start = id ? positions.get(id) : {x: session.x, y: session.y};
      gesture = {pointer: event.pointerId, id, clientX: event.clientX, clientY: event.clientY, x: start.x, y: start.y, moved: false};
      suppressClick = false; interacting = true;
      // Capture on the node for click selection, or on the canvas for panning.
      (group || wrap).setPointerCapture(event.pointerId);
      (group || wrap).focus({preventScroll: true});
      wrap.classList.add('dragging'); event.preventDefault();
    });
    wrap.addEventListener('pointermove', event => {
      if (!gesture || event.pointerId !== gesture.pointer) return;
      const dx = event.clientX - gesture.clientX, dy = event.clientY - gesture.clientY;
      if (!gesture.moved && Math.hypot(dx, dy) < 4) return;
      gesture.moved = true;
      if (gesture.id) {
        positions.set(gesture.id, {x: gesture.x + dx / session.scale, y: gesture.y + dy / session.scale}); paintNodes();
      } else { session.x = gesture.x + dx; session.y = gesture.y + dy; paintViewport(); }
    });
    function finish(event) {
      if (!gesture || event.pointerId !== gesture.pointer) return;
      suppressClick = gesture.moved || event.type === 'pointercancel';
      gesture = null; interacting = false; wrap.classList.remove('dragging');
    }
    wrap.addEventListener('pointerup', finish);
    wrap.addEventListener('pointercancel', finish);
    wrap.addEventListener('lostpointercapture', finish);
    paintNodes(); paintViewport(); wrap.append(svg); container.append(toolbar, wrap, hint);
    wrap.initializeGraph = () => {
      if (!session.initialized) { fit(.55); session.initialized = true; }
      else if (session.width !== wrap.clientWidth) { session.x += (wrap.clientWidth - session.width) / 2; paintViewport(); }
      session.width = wrap.clientWidth;
      if (session.selected !== selected) reveal(selected);
      session.selected = selected; initializedView = true;
    };
    return container;
  }
  return {layout, create, get interacting() { return interacting; }};
})();

if (typeof module !== 'undefined') module.exports = WorkflowGraph;
