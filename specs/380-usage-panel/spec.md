# 380 — Usage panel (`usage-summary.v1`)

Status: implemented on branch `380-usage-panel` (08/11/2026). Owner: assignment UI-A, Throttler coordinator p39.

## Why

The dashboard shows queue, pacing and window meters. It doesn't show what the fleet's tokens are made of. Without that, nobody can see that cache reads dominate, that the compaction summariser costs anything, or that one pane outweighs the others. A session-log scanner can measure this, and it runs outside the gateway. The gateway only needs to display that scanner's output, without inventing anything.

## Contract — `usage-summary.v1`

One JSON document, produced externally, read from `THROTTLE_USAGE_SUMMARY_PATH`. Example: [`usage-summary.v1.example.json`](usage-summary.v1.example.json). Validator: `src/anthropic_throttle_proxy/usage_summary.py`.

| key | type | meaning |
|---|---|---|
| `schema` | `"usage-summary.v1"` | exact; any other value rejects the file |
| `generated_at` | ISO-8601 with offset | **write time only**: when the producer wrote the file |
| `coverage.window_start` / `window_end` | ISO-8601 with offset | the time span the counts cover; end ≥ start. **`window_end` IS the source as-of time** |
| `coverage.files_scanned` / `files_skipped` | int ≥ 0 | how many source files went in, and how many could not be parsed |
| `rows[]` | ≤ 2000 objects | one per (provider, model, pane, role) |
| `rows[].provider`, `model` | label | `^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,95}$` |
| `rows[].pane` | label or `null` | `null` = unattributed |
| `rows[].role` | `turn` \| `compaction` | ordinary calls vs. context-summariser calls |
| `rows[].requests` | int ≥ 0 or `null` | |
| `rows[].fresh_input`, `cache_write`, `cache_read`, `output` | int ≥ 0 or `null` | additive classes; `null` = **unknown**, never zero |
| `rows[].reasoning` | int ≥ 0 or `null` | a **subset of `output`**, not an additive class; a row whose known `reasoning` > known `output` is rejected |

Rules:
- **Source as-of (Root review, 08/11/2026).** Freshness age = `now − min(generated_at, coverage.window_end)`. If `window_end > generated_at + 60 s` or `window_end > now + 60 s`, the state is `invalid-source-time`, which renders as stale and never as fresh. A producer that rewrites a frozen receipt with a new `generated_at` therefore still shows the receipt's age. The primary label reads `data as of <window_end BRT>`; `written <generated_at>` is secondary.
- **Content-free.** Exact key sets at every level, so unknown keys are rejected and no free-text field exists. Labels can't hold spaces or `@`, which rules out prompt text and e-mail identities. A bad row is dropped and counted (`N row(s) rejected`); a bad document shows a one-line reason and never echoes file content.
- **Bounds.** At most 1 MiB and 2000 rows. `NaN`/`Infinity` are rejected; floats and bools aren't counts.
- **Versioning.** A breaking change becomes `usage-summary.v2`; v1 readers reject it by `schema`.

## Panel

- Hidden when the env var is unset: the dashboard section isn't rendered, and `GET /ui/usage` returns an empty body.
- Failure-tolerant: a missing, unreadable, oversized or invalid file renders `usage summary unavailable · <reason>`, never a 500.
- Header: `data as of <window_end>` BRT and its age (amber ≥ 1 h, red ≥ 24 h, `invalid source time — stale` per the rule above), then `written <generated_at>`. Also shows the coverage window and span, files scanned and skipped, and rejected rows.
- Two tables, *model turns* and *compaction summariser*. Each has one column per class: requests, fresh input, cache write, cache read, output, reasoning. A total row gives `≥ N` when some rows are unknown, and `unknown` when all are.
- `reasoning` is shown as the column *of which reasoning* and is never a bar segment or added to other classes.
- A server-rendered SVG stacked bar per row and per total has one segment per **known** additive class, each with its own Catppuccin token. Cache is never folded into fresh. Unknown classes are listed as `+ k unknown`, not drawn as zero.
- Copy says "token counts, not spend". There is no `$` anywhere.
- Pure HTMX 1.x (`hx-get` every 30 s), no new script, tokens only (invariant #6).
- `/__throttle/health` is untouched. The file is read only by `GET /ui/usage`, in a worker thread, and cached on `(path, mtime_ns, size)`.

## Out of scope

The producer that writes the file; auth gating of `/ui/usage` — PR #28 (`ui/auth.py`) was still open when this branch was rebased on `146aba5`, so the route must be wrapped with its handler gate in a follow-up once #28 merges; `$` / spend framing.

## Acceptance

`uv run pytest tests/test_usage_panel.py`: covers the contract (valid, each rejection class, row drop), route hidden/error/ok, unknown ≠ 0, separate classes, no `$`, and that health doesn't read the file. `screenshot.png` is a **prior render**, made before the source-as-of header change; it still shows the old `generated … ago` header. No new render was made, because browser validation belongs to the registered NixOS server browser.
