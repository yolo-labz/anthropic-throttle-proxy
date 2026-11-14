// Offline discriminator: installed native TUI drops input while /reload focuses its box.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { resolve, join } from 'node:path';
import { pathToFileURL } from 'node:url';

for (const argument of process.argv.slice(2)) {
  const install = resolve(argument);
  const native = join(install, 'node_modules/@earendil-works/pi-tui/dist/tui.js');
  const { TuiBase, Container } = await import(pathToFileURL(native).href);
  const tui = new TuiBase({});
  tui.doRender = () => {};
  const received = [];
  const editor = { handleInput: data => received.push(data), render: () => [], invalidate: () => {} };
  const reloadBox = new Container();
  const map = JSON.parse(readFileSync(join(install, 'dist/modes/interactive/interactive-mode.js.map'), 'utf8'));
  const source = map.sourcesContent.find(content => content.includes('private async handleReloadCommand'));
  assert.equal(typeof source, 'string', 'installed reload implementation must be available');
  const reload = source.slice(source.indexOf('private async handleReloadCommand'));
  const positions = ['this.ui.setFocus(reloadBox)', 'await this.session.reload(', 'dismissReloadBox(this.editor']
    .map(fragment => reload.indexOf(fragment));
  assert.ok(positions.every(position => position >= 0), 'all native reload stages must be found');
  assert.ok(positions[0] < positions[1] && positions[1] < positions[2]);
  tui.setFocus(reloadBox);
  // Extension loading can finish while session_start and UI restoration remain awaited.
  tui.handleTerminalInput('/core-upgrade-state ' + 'a'.repeat(64));
  tui.handleTerminalInput('\r');
  assert.deepEqual(received, [], 'reload focus must demonstrate lost input, not queued delivery');
  tui.setFocus(editor);
  tui.handleTerminalInput('/core-upgrade-state ' + 'b'.repeat(64));
  tui.handleTerminalInput('\r');
  assert.equal(received.length, 2, 'restoring native editor focus must discriminate successful routing');
  tui.stopped = true;
  console.log(JSON.stringify({ install, reloadFocusDropsInput: true, restoredEditorRoutesInput: true }));
}
