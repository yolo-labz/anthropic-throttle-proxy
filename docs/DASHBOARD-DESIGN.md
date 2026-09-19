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

### Finish pass (29/08/2026, #216)

The structure above had landed; the page still read as unfinished, for five
reasons that are all execution rather than arrangement:

1. **#215's icons rendered as tofu boxes.** Provider, meter and status icons
   were added inside spans inheriting `--mono` / `--sans`, and both stacks
   ended at the bare `monospace` / `sans-serif` generic — which resolves an
   emoji code point through a fallback with no colour-emoji glyph. The stacks
   now name the emoji families explicitly, locked by `tests/test_ui_icons.py`.
   This one was a live regression, not taste: it was merged and undeployed.
2. **Three tables, three right edges, floating on the page background.** The
   shrink-to-fit of #163 is correct and stays; each table now sits in a
   bordered panel that *hugs* it. A full-width panel was tried first and is
   worse — it converts the ragged edge into ~800px of empty bordered surface,
   and the only way to fill it is to hand the slack to a column, which is the
   #163 void again.
3. **The signal value sat ~500px from its label**, with a stretched trace
   between the two. A sparkline is a dataword *beside* its number (Tufte), not
   a rule separating a label from its value.
4. **`spent` rendered as an 8rem outlined box.** The tag is a direct grid item
   of `.lane-meter` (its `.util` wrapper is `display: contents`), so it
   stretched to fill its track and read as a broken text input.
5. **The binding row was a 7% tint** next to a zebra stripe — not a difference
   the eye finds while scanning, despite being the page's decision object.

The falsifier below is unchanged, and so is every column: this pass moved no
data and added no panel.

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

### Hierarchy pass (18/09/2026, #232)

The structure above had landed twice and the page still read as unfinished.
This pass measured it instead of arguing about it, because "it still looks
wrong" is not actionable and a computed-style census is.

**What the census found** (`getComputedStyle` + `getBoundingClientRect` over
21 named elements and every panel, at 2560x1080):

| | before | after |
|---|---|---|
| distinct font sizes on the page | **9** (8.7, 9.57, 10.44, 10.73, 11.31, 11.6, 11.89px) | **6** (10.5 → 24px) |
| panel title (`h2`) | 10.73px | 14.25px |
| its own column headers | 11.31px | 10.5px |
| row identity | 11.6px | 14.25px |
| hero verdict | 11.89px | 24px |
| meter track | 348px wide, 5px tall | ≤ 9rem, 7px |
| panel right edges | 1794 / 1349 / **1137** | 2536 / 2536 / 2536 |
| total page height | 1255px | 1000px |
| sparkline aspect | 22:1 | ~15:1, with a drawn baseline |

Three of those are the whole story. **The panel title was smaller than its own
column headers, which were smaller than the row identities under them** — every
level of the document was the same size as every other, which is what finding 5
("uniform typographic weight") actually describes and why two previous passes
did not remove it. **The bearers panel ended 1423px short of the viewport**,
i.e. 56% of the screen was painted with nothing, with three different right
edges stacked above it. And **the page did not fit the screen it was read on**.

**One scale, seven ranks, and a test.** `--fs-hero / value / title / body /
meta / head / tag`, plus the root `15px` literal. `tests/test_ui_layout.py`
fails the build on any `font-size` that does not resolve to one of them, on a
non-monotonic scale, on an inline size in a template, and on a panel title that
does not outrank its headers. The census is the reason: a hand-written 13px is
how the old spread came back twice.

**The hero.** Verdict + duration + binding constraint + reopen time + way out
were a thin strip, a conditional strip, and a clause inside a sentence naming a
bearer hash. They are one panel, at 24px, at the top.

**Proximity.** The verdict moved beside the name it judges — it was 1200px away,
a head-turn per row while scanning. `family`, observed plan, provenance and
billing moved into the identity cell; `pace` and `exhausts` became one BURN
column, so the two decision numbers are adjacent instead of two empty columns
apart. Seven columns became three; ten became six, with the always-`—` ones
(`requests-remaining`, `retry-after`, client fan-out, AIMD hard cap) in the row
tooltip — the per-row expand S4.3 asked for, in the cheapest form that keeps
the data on the page.

**The shell.** Two columns above 1600px, one below, and the breakpoint is the
sum of the two columns' own minimums rather than a round number. `auto-fit` was
tried first and measured opening **four** tracks at 2560px and drawing two: the
dead third of the viewport came straight back. `tests/test_ui_layout.py` now
fails an intrinsic track count in the shell.

**A disconnect detector with no JavaScript.** `#stats` is re-rendered every 2s,
so `.as-of` is a new element every 2s and its CSS animation restarts with it.
Stop the swaps — dead server, killed proxy, dropped network — and the animation
completes, turning the stamp amber and appending "last known". Verified in a
real browser by aborting `/ui/stats` mid-poll: still `· live` at 2.5s, `· last
known` on a `--warn` chip at 7.5s, back to `· live` after reload. It is
explicitly exempted from the global `prefers-reduced-motion` kill, because here
the animation *is* the information: zeroing its duration would delete the
signal for exactly the users who asked for less motion.

**What is still not done.** The rail leaves empty space below it at 2560px when
there are few providers. The single-column fallback (1180–1600px) runs ~1350px
tall, so a laptop still scrolls for the last panel. Neither breaks the
falsifier, which is about the operator's own screen; both are recorded rather
than quietly rounded off.

**Falsifier, re-run.** At 2560x1080 the page is 1000px tall and answers, in one
screen without scrolling: what is limiting us (hero, 24px, with the measured
reason), since when (the same line), which subscription still has room (BURN
column, ranked), and when the binding window reopens (the hero's second fact).
