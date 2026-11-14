// Verify the installed builtin JSONL export against synthetic divergent memory/disk history only.
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";

const results = [];
for (const install of process.argv.slice(2)) {
  const root = resolve(install);
  const load = (relative) => import(pathToFileURL(join(root, "dist", relative)).href);
  const { SessionManager } = await load("core/session-manager.js");
  const { AgentSession } = await load("core/agent-session.js");
  const { InteractiveMode } = await load("modes/interactive/interactive-mode.js");
  const fixture = mkdtempSync(join(tmpdir(), "pi-memory-export-"));
  try {
    const manager = SessionManager.create(fixture, fixture);
    manager.appendMessage({ role: "user", content: "synthetic retained user", timestamp: 1 });
    manager.appendMessage({
      role: "assistant", content: [{ type: "text", text: "synthetic retained reply" }],
      api: "synthetic", provider: "synthetic", model: "synthetic", timestamp: 2,
      stopReason: "stop", usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0,
        totalTokens: 0, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
    });
    const original = manager.getSessionFile();
    const memory = manager.getBranch();
    const rows = readFileSync(original, "utf8").trim().split("\n").map(JSON.parse);
    rows.at(-1).parentId = null;
    writeFileSync(original, rows.map(JSON.stringify).join("\n") + "\n");
    const before = readFileSync(original);
    const destination = join(fixture, "memory evidence.jsonl");
    const statuses = [];
    let htmlCalls = 0;
    const context = {
      getPathCommandArgument: InteractiveMode.prototype.getPathCommandArgument,
      session: {
        exportToJsonl: (path) => AgentSession.prototype.exportToJsonl.call({ sessionManager: manager }, path),
        exportToHtml: () => { htmlCalls++; throw new Error("HTML export must not run"); },
      },
      showStatus: (status) => statuses.push(status),
      showError: (error) => { throw new Error(error); },
    };
    await InteractiveMode.prototype.handleExportCommand.call(context, `/export "${destination}"`);
    const exported = readFileSync(destination, "utf8").trim().split("\n").map(JSON.parse);
    assert.deepEqual(readFileSync(original), before);
    assert.equal(exported[0].id, manager.getSessionId());
    assert.equal(exported[0].cwd, manager.getCwd());
    assert.deepEqual(exported.slice(1), memory.map((entry, index) => ({
      ...entry, parentId: index ? memory[index - 1].id : null,
    })));
    assert.equal(htmlCalls, 0);
    assert.equal(statuses.length, 1);
    assert.equal(manager.getSessionFile(), original);
    assert.equal(manager.getLeafId(), memory.at(-1).id);
    results.push({ install: root, rawEntries: memory.length, originalUnchanged: true,
      originalIdentityRetained: true, liveMemoryExported: true, htmlCalls, providerCalls: 0 });
  } finally {
    rmSync(fixture, { recursive: true, force: true });
  }
}
assert.ok(results.length, "pass at least one installed Pi root");
console.log(JSON.stringify({ syntheticOnly: true, results }, null, 2));
