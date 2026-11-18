// No session manager or inference: exercise actual extension discovery in synthetic HOME.
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, readFileSync, readlinkSync, rmSync, symlinkSync, existsSync, unlinkSync, writeFileSync } from 'node:fs';
import { homedir, tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
const realHome = homedir();
const globalEntry = join(realHome, '.pi/agent/extensions/compaction-loop-fence.ts');
let target;
try { target = readlinkSync(globalEntry); } catch (error) {
  if (error.code !== 'EINVAL') throw error;
  const match = /^export \{ default \} from (".*");\s*$/.exec(readFileSync(globalEntry, 'utf8'));
  assert.ok(match, 'global entry must be the reviewed absolute forwarder');
  target = JSON.parse(match[1]);
}
assert.ok(target.startsWith('/'), 'reviewed target must be absolute');
const [install] = process.argv.slice(2);
assert.ok(install, 'pass one native installed root; each oracle needs a fresh process');
const home = mkdtempSync(join(tmpdir(), 'pi-fence-discovery-'));
process.env.HOME = home;
process.env.PI_FLEET_INSTALL = resolve(install);
const agent = join(home, '.pi/agent');
const directory = join(home, '.local/state/throttler-recovery-20261111/core-upgrades');
mkdirSync(join(agent, 'extensions'), { recursive: true });
mkdirSync(directory, { recursive: true, mode: 0o700 });
const link = join(agent, 'extensions/compaction-loop-fence.ts');
symlinkSync(target, link);
try {
  const { discoverAndLoadExtensions, loadExtensions, createExtensionRuntime } = await import(pathToFileURL(join(resolve(install), 'dist/core/extensions/loader.js')));
  const runtime = () => {
    const value = createExtensionRuntime();
    for (const name of ['setModel', 'setThinkingLevel', 'sendMessage', 'sendUserMessage']) value[name] = () => { throw Error('forbidden inference or selection'); };
    return value;
  };
  const discovered = await discoverAndLoadExtensions([], home, agent);
  const receipt = join(directory, `${process.pid}.compaction-fence-loaded.json`);
  const symlinkReceipt = existsSync(receipt);
  unlinkSync(link);
  if (existsSync(receipt)) unlinkSync(receipt);
  const forwarderDirectory = join(home, 'private-reviewed-entry');
  mkdirSync(forwarderDirectory, { mode: 0o700 });
  const forwarder = join(forwarderDirectory, 'entry.ts');
  writeFileSync(forwarder, `export { default } from ${JSON.stringify(target)};\n`, { mode: 0o444 });
  symlinkSync(forwarder, link);
  const shim = await discoverAndLoadExtensions([], home, agent);
  assert.deepEqual(shim.errors, [], 'absolute immutable shim must load');
  assert.equal(shim.extensions.length, 1);
  assert.deepEqual([...shim.extensions[0].commands.keys()].sort(), ['compaction-loop-fence-disable', 'compaction-loop-fence-enable']);
  assert.ok(existsSync(receipt), 'shim factory must write its genuine loaded receipt');
  const direct = await loadExtensions([target], home, undefined, runtime());
  console.log(JSON.stringify({ symlinkReceipt, shimPassed: true, forwarderSymlink: true, install: resolve(install), exactSymlinkTarget: target, syntheticOnly: true,
    discovered: discovered.extensions.map(extension => ({ path: extension.path, commands: [...extension.commands.keys()] })),
    discoveryErrors: discovered.errors, directErrors: direct.errors, directCount: direct.extensions.length,
    loadedReceipt: existsSync(receipt) ? JSON.parse(readFileSync(receipt)) : null, providerCalls: 0 }));
} finally { rmSync(home, { recursive: true, force: true }); }
