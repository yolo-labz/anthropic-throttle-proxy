# Plan

Generator: OpenAI GPT-6 Astra/xhigh. Original PR generator family unknown.

1. Cherry-pick only the original four commits, preserving base d83f637's unrelated auth-probe fix. A two-dot diff against original head would incorrectly remove that fix.
2. Turn the original top-level assertions into pytest cases; retain valid coverage and replace the incorrect native-tool-deletion expectation with preservation. Add the four reproduced blockers, nested malformed inputs, falsey/long values, endpoint controls and transport tests before repairing code.
3. Keep the small normalizer in `routing.py`: exact parsed scheme/host/port/path check, transactional malformed-input passthrough, no native-protocol rewriting, no source truncation. Only thinking-only internal turns may be dropped; appending a final placeholder must not slice prior valid turns.
4. Remove the ineffective ingress call. Ingress only buffers `/v1/messages`, then appends that path to the lane URL; its branch never covers `/api/coding/paas/v4/chat/completions`. Normalize in `forwarding._forward_once`, where the actual egress URL is known, for POST bodies only. Refresh Content-Length case-insensitively without mutating caller headers/body. This one seam covers initial, pushback and central-to-direct retries. The separate prepared-SSE path is guarded by exact `path == 'v1/messages'` and cannot serve chat-completions.
5. Execute targeted regressions against archived exact-head source, then targeted/full suites and lint against the repair. Store logs and a canonical report in this repository; push/open a replacement PR without closing #236 or merging.

## Hypothesis and falsifier

The proposal conflates unsupported internal blocks with native protocol fields, uses substring endpoint matching, slices the already-filtered turn list, and performs unsafe/unbounded-type joins; additionally its only production call site cannot reach the failing endpoint. The hypothesis is falsified if exact-head regression execution preserves those shapes or if a synthetic real-handler chat request reaches the wire normalized before the repair.

## Boundaries / constitution

No vendor SDK or dependency, secrets, queue/capacity policy, Nix pins, workflows or routing configuration changes. Constitution I/II/V remain intact: an endpoint identity guard does not redirect traffic. No service restart or upstream API call. Tests use synthetic fixtures and local HTTP stubs only. Legacy speckit-make launcher is not used: it hardcodes retired models; the coordinator supplies the independent gate.
