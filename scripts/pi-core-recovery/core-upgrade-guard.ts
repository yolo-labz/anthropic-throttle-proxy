import { lstatSync, readFileSync, writeFileSync, renameSync, mkdirSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { createHash, randomBytes } from 'node:crypto';

export function bootstrap(argv) {
  const valueOptions = new Set(['--name', '--exclude-tools', '--session-id', '--session', '--session-dir', '--model',
    '--provider', '--thinking', '--append-system-prompt', '--skill', '--extension', '--tools']);
  const switches = new Set(['--no-skills', '--no-extensions', '--no-prompt-templates', '--no-themes', '--continue', '-c']);
  const selection = new Set(['--session', '--session-id', '--model', '--provider', '--thinking']);
  const args = argv.slice(2);
  const replayArgs = [];
  for (let index = 0; index < args.length; index++) {
    const option = args[index];
    if (switches.has(option)) {
      if (option !== '--continue' && option !== '-c') replayArgs.push(option);
      continue;
    }
    if (!valueOptions.has(option) || typeof args[index + 1] !== 'string' || args[index + 1].startsWith('--')) {
      return { qualified: false, rejectedOption: option.startsWith('-') ? option.split('=')[0] : '<positional>' };
    }
    if (!selection.has(option)) replayArgs.push(option, args[index + 1]);
    index++;
  }
  return { qualified: true, args, replayArgs, node: argv[0], hash: createHash('sha256').update(JSON.stringify(argv)).digest('hex') };
}

export const GUARD_VERSION = 'native-load-v1';

// Explicit command only: no event handlers, timer, model selection, or inference.
export function qualify(request, actual, now = Date.now()) {
  if (!request || request.schema !== 1 || !/^[a-f0-9]{64}$/.test(request.nonce)) throw Error('invalid request');
  if (!Number.isSafeInteger(request.expires) || request.expires <= now || request.expires > now + 300000) throw Error('expired request');
  for (const key of ['pid', 'session', 'cwd', 'provider', 'model', 'effort', 'install']) {
    if (actual[key] !== request[key] || !request[key]) throw Error(`identity mismatch: ${key}`);
  }
  if (request.role !== undefined && actual.role !== request.role) throw Error('identity mismatch: role');
  if (actual.idle !== true || actual.pending !== false || actual.editor !== '') throw Error('native state is not empty and idle');
}

export default function (pi) {
  for (const command of ['core-upgrade-ready', 'core-upgrade-state']) pi.registerCommand(command, {
    description: 'Graceful same-session core upgrade, only with an exact short-lived operator request',
    handler: (args, ctx) => {
      try {
        if (!/^[a-f0-9]{64}$/.test(args)) throw Error('invalid nonce');
        const directory = join(homedir(), '.local/state/throttler-recovery-20261111/core-upgrades');
        const file = join(directory, `${args}.request.json`);
        const stat = lstatSync(file);
        if (!stat.isFile() || stat.uid !== process.getuid() || (stat.mode & 0o777) !== 0o600) throw Error('unsafe request permissions');
        const request = JSON.parse(readFileSync(file, 'utf8'));
        if (request.nonce !== args) throw Error('nonce mismatch');
        const actual = { pid: process.pid, session: ctx.sessionManager.getSessionFile(), cwd: ctx.cwd,
          provider: ctx.model?.provider, model: ctx.model?.id, effort: pi.getThinkingLevel(),
          install: process.env.PI_FLEET_INSTALL, idle: ctx.isIdle(), pending: ctx.hasPendingMessages(),
          editor: ctx.hasUI ? ctx.ui.getEditorText() : null, role: process.env.PI_ROLE_RESOLVED };
        qualify(request, actual);
        const nativeBootstrap = bootstrap(process.argv);
        if (command === 'core-upgrade-ready' && (!nativeBootstrap.qualified || request.expectedBootstrapHash !== nativeBootstrap.hash)) {
          throw Error('original native bootstrap has not been qualified');
        }
        const receipt = join(directory, `${args}.${command === 'core-upgrade-ready' ? 'ready' : 'state'}.json`);
        const staging = `${receipt}.${process.pid}`;
        writeFileSync(staging, JSON.stringify({ ...request, nativeBootstrap, readyAt: Date.now(), idle: true, pending: false, editorBytes: 0 }) + '\n', { mode: 0o600, flag: 'wx' });
        renameSync(staging, receipt);
        // No await between native attestation, receipt, and graceful shutdown flag.
        if (command === 'core-upgrade-ready') ctx.shutdown();
      } catch (error) { ctx.ui.notify(`Core upgrade refused: ${error.message}`, 'warning'); }
    },
  });
  const directory = join(homedir(), '.local/state/throttler-recovery-20261111/core-upgrades');
  mkdirSync(directory, { recursive: true, mode: 0o700 });
  const stat = lstatSync(directory);
  if (!stat.isDirectory() || stat.uid !== process.getuid() || (stat.mode & 0o777) !== 0o700) throw Error('unsafe load receipt directory');
  const generation = randomBytes(32).toString('hex');
  const receipt = join(directory, `${process.pid}.loaded.json`);
  const staging = `${receipt}.${generation}`;
  writeFileSync(staging, JSON.stringify({ pid: process.pid, guardVersion: GUARD_VERSION, generation }) + '\n', { mode: 0o600, flag: 'wx' });
  renameSync(staging, receipt);
}
