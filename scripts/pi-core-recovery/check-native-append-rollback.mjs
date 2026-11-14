// Offline: real native SessionManager, synthetic in-memory history and persist failure.
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';
import { resolve } from 'node:path';

const [install, expectation] = process.argv.slice(2);
assert.ok(['exposed', 'fenced'].includes(expectation));
const { SessionManager } = await import(pathToFileURL(resolve(install, 'dist/core/session-manager.js')).href);
const manager = SessionManager.inMemory();
manager.appendMessage({ role: 'user', content: [{ type: 'text', text: 'synthetic' }], timestamp: 0 });
const before = manager.getLeafId();
const original = manager._persist;
manager._persist = () => { throw new Error('synthetic persistence failure'); };
assert.throws(() => manager.appendCustomEntry('synthetic-failed-entry', {}), /synthetic persistence failure/);
const failedLeaf = manager.getLeafId();
manager._persist = original;
manager.appendCustomEntry('synthetic-later-cleanup', {});
const last = manager.getEntries().at(-1);
const exposed = failedLeaf !== before && last.parentId === failedLeaf;
assert.equal(exposed, expectation === 'exposed');
console.log(JSON.stringify({ install, expectation, failedAppendChangedLeaf: failedLeaf !== before,
  laterAppendUsesUnpersistedParent: exposed, passed: true }));
