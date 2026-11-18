import { test } from 'node:test';
import assert from 'node:assert/strict';
import { validateBranch } from './restore.ts';

const request = { expectedLeaf: 'b', targetLeaf: 'a', transcriptSha256: '0'.repeat(64), provider: 'claude-lane', model: 'claude-opus-5-5', effort: 'max' };
const entries = () => [
  { id: 'm', parentId: null, type: 'model_change', provider: request.provider, modelId: request.model },
  { id: 't', parentId: 'm', type: 'thinking_level_change', thinkingLevel: 'max' },
  { id: 'u', parentId: 't', type: 'message', message: { role: 'user' } },
  { id: 'a', parentId: 'u', type: 'message', message: { role: 'toolResult' } },
  { id: 'b', parentId: null, type: 'custom', customType: 'pi-artifact-cleanup' },
];
test('qualifies only latest real nonuser branch and matching historical selection', () => {
  assert.deepEqual(validateBranch(request, entries(), 'b'), { root: 'b', currentEntries: 1, targetEntries: 4 });
});
for (const [name, change] of [
  ['cycle', a => { a[0].parentId = 'a'; }],
  ['missing parent', a => { a[0].parentId = 'unknown'; }],
  ['duplicate identity', a => { a.push({ ...a[0] }); }],
  ['historical model', a => { a[0].modelId = 'claude-fable-5-2'; }],
  ['historical effort', a => { a[1].thinkingLevel = 'high'; }],
  ['unknown custom root', a => { a[4].customType = 'other'; }],
  ['current work', a => { a[4].type = 'message'; a[4].message = { role: 'user' }; }],
  ['user target', a => { a[3].message.role = 'user'; }],
  ['no human history', a => { a[2].message.role = 'assistant'; }],
]) {
  test(`refuses ${name}`, () => { const a = entries(); change(a); assert.throws(() => validateBranch(request, a, 'b')); });
}
