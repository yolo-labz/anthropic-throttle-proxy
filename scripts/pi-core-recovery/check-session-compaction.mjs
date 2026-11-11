#!/usr/bin/env node
// Inspect an existing transcript in memory; never open a persistent SessionManager.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const [install, transcript, provider, model] = process.argv.slice(2);
assert(install && transcript && provider && model,
  'usage: INSTALL TRANSCRIPT PROVIDER MODEL');
const load = (name) => import(pathToFileURL(resolve(install, `dist/core/${name}.js`)));
const { SessionManager } = await load('session-manager');
const { prepareCompaction } = await load('compaction/compaction');
const bytes = readFileSync(transcript);
const entries = bytes.toString('utf8').split('\n').filter(Boolean).map(JSON.parse);
const header = entries.find((entry) => entry.type === 'session');
assert(header?.id, 'missing native session header');
const settings = JSON.parse(readFileSync(resolve(process.env.HOME, '.pi/agent/settings.json'), 'utf8')).compaction ?? {};
const { modelOverrides, ...globalSettings } = settings;
const effective = { enabled: true, reserveTokens: 16384, keepRecentTokens: 20000,
  ...globalSettings, ...modelOverrides?.[`${provider}/${model}`] };
const manager = SessionManager.inMemory();
assert(manager.persist === false && !manager.getSessionFile(), 'persistent manager refused');
// Index the snapshot without file loading, migration, or persistent session opening.
manager.fileEntries = entries;
manager.sessionId = header.id;
manager._buildIndex();
const branch = manager.getBranch();
const preparation = prepareCompaction(branch, effective);
assert(readFileSync(transcript).equals(bytes), 'transcript changed during observation; discard this snapshot');
console.log(JSON.stringify({ install, transcript, sessionId: header.id,
  transcriptSha256: createHash('sha256').update(bytes).digest('hex'),
  leaf: manager.getLeafId(), rawEntries: entries.length, branchEntries: branch.length,
  contextMessages: manager.buildSessionContext().messages.length,
  provider, model, settings: effective, prepared: !!preparation,
  summaryMessages: preparation?.messagesToSummarize.length ?? 0,
  prefixMessages: preparation?.turnPrefixMessages.length ?? 0,
  tokensBefore: preparation?.tokensBefore ?? null,
  previousSummaryBytes: Buffer.byteLength(preparation?.previousSummary ?? '', 'utf8'),
  inferenceCalls: 0, persistentManagerOpened: false }));
