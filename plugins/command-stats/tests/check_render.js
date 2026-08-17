/* Runs the report's script under a minimal DOM shim.
   No jsdom in the sandbox, so this covers what matters numerically: every
   figure and table builder actually executes, and every attribute it emits is
   scanned for NaN / Infinity / undefined. innerHTML prose is stored but not
   parsed; the real browser pass is verify_artifact. */
const fs = require('fs');
const vm = require('vm');

class N {
  constructor(tag, ns) { this.tagName = tag; this.ns = ns || null; this.attrs = {};
    this.children = []; this._text = ''; this._html = ''; this.dataset = {}; this.style = {}; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return this.attrs[k]; }
  appendChild(c) { this.children.push(c); return c; }
  removeChild(c) { this.children = this.children.filter(x => x !== c); }
  get firstChild() { return this.children[0] || null; }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
  set innerHTML(v) { this._html = String(v); this.children = []; }
  get innerHTML() { return this._html; }
  set className(v) { this.attrs.class = String(v); }
  get className() { return this.attrs.class || ''; }
  click() {}
  querySelectorAll(sel) { return this.walk([]).slice(1).filter(n => n.tagName === sel); }
  walk(out) { out.push(this); this.children.forEach(c => c.walk(out)); return out; }
}
const root = new N('div');
root.id = 'report';
const doc = {
  title: '',
  body: root,
  createElement: t => new N(t),
  createElementNS: (ns, t) => new N(t, ns),
  getElementById: id => (id === 'report' ? root : null)
};
const sandbox = {
  document: doc, console,
  Blob: class { constructor() {} },
  URL: {createObjectURL: () => 'blob:', revokeObjectURL: () => {}},
  setTimeout, Set, Map, Math, JSON, Intl
};
const html = fs.readFileSync('report.html', 'utf8');
const src = html.slice(html.indexOf('<script>') + 8, html.lastIndexOf('</script>'));

let ok = true;
try {
  vm.createContext(sandbox);
  vm.runInContext(src, sandbox, {filename: 'report.js'});
} catch (e) {
  ok = false;
  console.log('RUNTIME ERROR :', e.message, '\n', (e.stack || '').split('\n').slice(1, 4).join('\n'));
}

const all = root.walk([]);
const svgs = all.filter(n => n.tagName === 'svg');
const tables = all.filter(n => n.tagName === 'table');
const bad = [];
all.forEach(n => Object.entries(n.attrs).forEach(([k, v]) => {
  if (/NaN|Infinity|undefined|null/.test(v)) bad.push(n.tagName + '@' + k + '=' + v);
}));
const texts = all.filter(n => n.tagName === 'text').map(n => n.textContent);
const badText = texts.filter(t => /NaN|undefined|Infinity/.test(t));
const proseBad = all.map(n => n._html).join(' ').match(/NaN|undefined|Infinity/g) || [];

console.log('ran clean     :', ok);
console.log('doc title     :', sandbox.document.title);
console.log('svg figures   :', svgs.length);
console.log('svg elements  :', all.filter(n => n.ns).length);
console.log('svg <text>    :', texts.length);
console.log('tables        :', tables.length);
console.log('csv buttons   :', all.filter(n => n.className === 'csv').length);
console.log('heat cells    :', all.filter(n => n.className === 'heat').length);
console.log('sortable ths  :', all.filter(n => n.tagName === 'th' && n.onclick).length);
console.log('bad attrs     :', bad.length, bad.slice(0, 6));
console.log('bad svg text  :', badText.length, badText.slice(0, 6));
console.log('bad prose     :', proseBad.length, [...new Set(proseBad)].slice(0, 4));

// exercise the sort + CSV paths, which the browser would only reach on click
const ths = all.filter(n => n.tagName === 'th' && n.onclick);
try { ths.slice(0, 12).forEach(t => { t.onclick(); t.onclick(); }); console.log('sort paths    : ok (' + Math.min(12, ths.length) + ' headers, both directions)'); }
catch (e) { ok = false; console.log('SORT ERROR    :', e.message); }
const btns = all.filter(n => n.className === 'csv');
try { btns.forEach(b => b.onclick()); console.log('csv paths     : ok (' + btns.length + ' tables)'); }
catch (e) { ok = false; console.log('CSV ERROR     :', e.message); }

process.exit(ok && !bad.length && !badText.length && !proseBad.length ? 0 : 1);
