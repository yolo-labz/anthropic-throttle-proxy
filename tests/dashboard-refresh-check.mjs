import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const html = readFileSync(new URL('../src/anthropic_throttle_proxy/ui/templates/dashboard.html', import.meta.url), 'utf8');
const script = html.slice(html.lastIndexOf('<script>') + 8, html.lastIndexOf('</script>'))
  .replace('{{ asset_v | tojson }}', '"test"')
  .replace('{{ (show_local | default(true)) | tojson }}', 'false');
const events = new Map();
const classes = new Set();
const state = { textContent: '', classList: {
  add: key => classes.add(key),
  toggle: (key, enabled) => enabled ? classes.add(key) : classes.delete(key),
} };
const meta = { dataset: { revision: 'test', local: 'false' } };
let clock = 0;
let watchdog;
vm.runInNewContext(script, {
  Date: { now: () => clock },
  setInterval: fn => { watchdog = fn; },
  document: {
    getElementById: id => id === 'refresh-state' ? state : meta,
    body: { addEventListener: (name, fn) => events.set(name, fn) },
  },
});
const swap = () => events.get('htmx:afterSwap')({ detail: { target: { id: 'stats' } } });
swap();
assert.match(state.textContent, /Page refresh/);
for (const event of ['htmx:responseError', 'htmx:sendError', 'htmx:timeout']) {
  events.get(event)({ detail: { elt: { id: 'config' } } });
  assert.match(state.textContent, /Page refresh/);
  events.get(event)({ detail: { elt: { id: 'stats' } } });
  assert.match(state.textContent, /last known/);
  swap();
  assert.equal(classes.has('refresh-lost'), false);
}
clock = 11001;
watchdog();
assert.match(state.textContent, /last known/);
swap();
meta.dataset.revision = 'changed';
swap();
assert.match(state.textContent, /reload/);
assert.equal(classes.has('refresh-lost'), true);
console.log('PASS refresh errors, recovery, watchdog, unrelated requests and revision mismatch');
