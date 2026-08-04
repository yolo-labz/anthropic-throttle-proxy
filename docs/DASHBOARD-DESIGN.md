# Dashboard design — what /ui gets wrong, and the standard it should meet

Written 03/08/2026, after Pedro's read of the live dashboard: *"this is
screaming AI made"*. He is right, and the tell is not the palette. It is that
the page answers *what are the current values* when an operator arrives asking
*is anything wrong, since when, and what do I do about it*.

This is the design brief for the redesign. It is deliberately opinionated and
cites the sources it leans on, so a future change can argue with the reasoning
rather than the taste.

## The diagnosis

What the page shows today (top to bottom): a five-tile row of large scalars, a
Providers table, an Accounts table, a Bearers table, a Copilot table, a
Subscriptions table, an advisor box. Concretely:

1. **Five giant numbers with no baseline.** `in-flight 2 · queued 0 ·
   served 87 · retries 0 · disconnects 9`. A scalar with no trend and no
   threshold cannot be judged: is 87 served a busy hour or a dead one? Is 9
   disconnects the storm from 40 minutes ago or one happening now? The
   oversized-KPI-row is the single most reliable "generated dashboard"
   fingerprint, and it is also the least informative pixel-per-inch on the
   page.
2. **No time axis anywhere.** Every value is instantaneous. The questions this
   proxy exists to answer — did the 429 storm end, is B's burn pace
   sustainable, did the AIMD cap recover after the last shrink — are all
   questions about the last 30–60 minutes.
3. **The same entity is drawn three times.** Providers (2 rows), Accounts (3
   rows), Bearers (4 rows) are three projections of one hierarchy:
   *lane → account → live traffic*. A bearer is an account's current token;
   the primary provider is the Anthropic lane. Three tables means the operator
   does the join by eye, every time.
4. **Columns that are empty by construction.** For two of three accounts,
   `7d S·O`, `credits`, `pace`, `7d ETA` are all `—`. A column that is empty
   for most rows is not a column; it is a detail belonging to the row it
   describes.
5. **Uniform typographic weight.** Nearly every label is the same 0.6–0.7rem
   uppercase, letterspaced. When everything is emphasised, nothing is. There is
   no visual difference between "this is the binding constraint on the whole
   fleet" and "this is a debug counter".
6. **Status without duration.** `THROTTLED · binding: 7d window 100% on
   b144f62f` does not say *since when*, and "since when" is what separates a
   transient from an outage.
7. **Ordering is configuration order, not severity.** The row that matters —
   the binding constraint — is wherever the credential list happened to put it.

None of this is a colour problem. The Catppuccin palette is fine and the
#157 fix already removed the colour-only status encoding (WCAG 1.4.1).

## The standard to meet

**Four Golden Signals / RED** (Google SRE; Tom Wilkie's RED method, both
recommended by Grafana's own dashboard best-practices doc). For a service, the
minimum honest header is *rate, errors, duration, saturation* — as series, not
scalars. For this proxy that maps to: requests/min, pushback (429/503/529)/min,
upstream latency p50/p95, and queue depth against the live AIMD cap. Grafana's
guidance is explicit that RED dashboards are the ones worth alerting on because
they track symptoms rather than causes.

**Tufte: sparklines and small multiples.** A sparkline is a "datawords"-sized
graphic that sits inline with the number it describes, giving the scalar the
baseline it is missing at effectively zero extra space. Small multiples must
share one scale across panels — per-panel autoscaling is the classic error that
makes comparison impossible. Applied here: one sparkline per lane row, all
lanes on the same y-scale, so "which lane is absorbing the fleet" is a glance.

**Stephen Few / information dashboard design.** One screen, no scrolling for
the primary question; encode with position and length before colour; strip
non-data ink. The current page needs three scroll-heights to reach the
subscription meters, which are the numbers that decide whether work can run at
all.

**Deviation beats absolute.** `7d 44%` is a fact; `pace 2.06×, exhausts in 2d
19h` is a decision. The accounts table already computes both — they are the two
narrowest columns on the page and should be the widest signal.

**Prior art worth copying, specifically:**

- **openusage** (`github.com/janekbaraniewski/openusage`) — a terminal-first
  local quota dashboard across Claude Code, Codex, Cursor, Copilot, OpenRouter
  and ~30 more. Its model is exactly ours: *account* rows carrying
  plan + window + reset + burn, auto-detected from local credential state. Its
  `settings.json` `accounts[]` schema (id / provider / credential source /
  probe) is the shape our lane registry converges on.
- **Grafana's own panels** for the number+sparkline pattern and the
  shared-scale rule.
- **Cloudflare / Vercel analytics** for a header that is a compact time-series
  strip rather than a KPI row.
- **Stripe's dashboard** for dense tables with one dominant column and
  progressive disclosure of the rest.

## The redesign

Status, 04/08/2026: S4.1, S4.2, S4.4 and S4.5 shipped in #167; S4.3 landed as
one Subscriptions table in #165 (the per-row expand is still open — the
Bearers table is demoted rather than folded into a row).

**S4.1 — history ring buffer (server side).** SHIPPED (`history.py`). A 60-minute, 10-second-resolution
in-memory ring (360 points) of: served, pushback events, queue depth, live cap,
p50/p95 duration, per-lane binding utilisation. ~30 KB. It is a prerequisite for
every visual below, and it is the piece the proxy genuinely lacks — everything
else is arrangement.

**S4.2 — header strip replaces the KPI row.** SHIPPED (`ui/signals.py`,
server-rendered `<svg><polyline>`, folded to one point per minute so a
sporadic-pushback series reads as a step rather than a barcode). One line: identity + mode + live
cap, then four inline sparkline+value pairs (rate / errors / p95 / saturation),
each with its 60-minute trace and the current value right-aligned. Same height
as today's row, four times the information, and the "AI dashboard" tell is gone
with it.

**S4.3 — one capacity table replaces three.** Row = a lane or an account within
it, sorted by binding constraint descending, so the top row is always the thing
limiting the fleet. Columns: name+family · live traffic (inflight/queued +
served sparkline) · binding meter (bar + %) · pace · exhausts-in · status text
+ duration. Everything else (AIMD internals, req-left, retry-after, client
fan-out, `7d S·O`, credits) moves into a per-row expand — present, not
prominent.

**S4.4 — status carries duration.** SHIPPED (`history.level_since`). `THROTTLED for 12m · binding 7d 100% on
b144f62f (account A)`. Cheap: the ring buffer already knows when the level last
changed.

**S4.5 — advisor becomes a header action.** SHIPPED. A button in the header, result
rendered as an inline strip above the capacity table when it fires. The
standing prose block is a paragraph explaining a feature to someone who already
opened the page.

## Non-goals

- No charting library. Sparklines are inline `<svg>` polylines rendered
  server-side into the existing HTMX partial; the "no JavaScript modules"
  invariant stands.
- No new palette. Catppuccin tokens only, and status keeps text + colour.
- No auto-refresh below 2 s. The dashboard is not the incident channel.

## Falsifier for the redesign

Open the page mid-incident. If an operator cannot answer, in one screen and
without scrolling: *what is limiting us, since when, which subscription still
has room, and when does the binding window reset* — the redesign has not
landed, whatever it looks like.
