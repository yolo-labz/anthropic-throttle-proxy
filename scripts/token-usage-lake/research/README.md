# Token Diet research workspace

Marimo provides the Python/SQL editor. MLflow stores experiment parameters, metrics and artifacts.
Quarto renders scientific reports. DuckDB, Polars, NumPy, SciPy, statsmodels, Plotly, Altair and
JupyterLab are installed in the same hash-locked environment. Python execution needs no LLM call.

The browser editor and tracking service run on Dokku. A Dask CPU worker runs on the desktop,
with mutual TLS over Tailscale, a two-CPU quota and a 4 GiB systemd memory limit. GPU inference
is not part of this deployment. No running model or provider selection changes.

## Canonical workflow

Use **this directory** for executable research code. The Notes vault holds protocols, decisions
and private evidence, not another production science engine. The promoted `outcomes.py` and
`trial_stats.py` retain the existing prototype's outcome and session-bootstrap contracts.
Do not start another collector, scheduler, editor, tracking server or model router.

| Stage | Authority | What a pass establishes |
|---|---|---|
| Collection and processing | Existing feed and single Dagster writer | Its explicit collection/quality contract, not task success |
| Dataset | `snapshots.py` publication; `science.latest_snapshot()` reader | Pinned generation and verified declared file hashes |
| Serve | `serving.py` | Read-only views over declared Parquet; never the live lake file |
| Accept a result | `oracles.py` | Invariants, replay, one-shot holdout — mechanical evidence, never a savings claim |
| Explore | Existing `notebook.py` in Marimo | Descriptive totals; native/gateway grains stay separate |
| Analyze an outcome comparison | `experiment.py` | Reproducible **exploratory** paired-session analysis |
| Track and publish | Existing MLflow and Quarto | Stored parameters/reports, not independent acceptance |

The loader rejects changed/missing files, path escapes, symlinks and a latest pointer that
contradicts its immutable manifest. Notebook reads use manifest file lists, never directory
globs that can silently include undeclared parts. Pin the generation before analysis.

## Serving contract

DuckDB allows **multiple concurrent readers only when no process writes**; a read-write file is
single-process, enforced by a file lock. A dashboard therefore never opens `/data/lake.duckdb`.
It opens a verified published generation:

```bash
python -B scripts/token-usage-lake/research/serving.py   # verify + print the receipt
```

```python
import serving
connection, receipt = serving.open_verified("/workspace/data/snapshots", deep=True)
connection.sql("SELECT count(*) FROM calls").show()   # views only; the lake is untouched
```

`receipt["mode"]` is `deep` or `cached`. Only a `deep` receipt can support the strongest label:
`oracles.verdict(layers, verification=receipt)` refuses `fully_verified` for a cached one, so the
cheap path can never promote a result across a trust boundary.

- Deep verification hashes every declared file once and writes a receipt **outside** the
generation (`<root>/.verify/<run_id>.json`), keyed on the manifest hash plus each file's
inode/size/**mtime/ctime**. A matching receipt makes later opens cheap; any replaced, rewritten
or re-manifested file forces a fresh hash and a refusal on mismatch. `ctime` cannot be restored
from userspace, so rewriting a file and putting the old mtime back still invalidates the receipt.
- Each receipt is also appended to a hash-chained log (`<root>/.verify/chain.jsonl`) where every
entry commits to its predecessor. Cheap reuse requires the receipt's exact bytes to appear in
that chain, so a hand-edited receipt is ignored rather than trusted, and a broken chain refuses
outright. The residual limit is honest: an attacker with write access to *both* the generation and
the state root still wins — the chain forces them to rewrite every later entry and detects
partial damage, it is not a signature.
- Only declared Parquet becomes a view. An undeclared file in the generation is invisible, so a
  stray export cannot silently join an analysis.
- Threads and memory are bounded per connection, so a report cannot starve the 2-CPU guest that
  runs the writer.

**Long-term alternative, not adopted:** DuckDB 2.0 ships DuckLake 1.1 (requires DuckDB ≥ 2.0.0),
which supports concurrent read-writes. The documented mechanism is snapshot-identifier conflict
detection: writers race on a sequential snapshot id, and a conflict is either **retried** when the
changesets are compatible or **aborted with an error** when they are logically incompatible — a
lost commit is not a silent outcome. Catalog choice is the real decision: a DuckDB catalog allows
one writer process (or `READ_ONLY` attaches), SQLite retries around its single writer, and
**PostgreSQL is the documented multi-user/remote-client option**; Quack serves a DuckDB catalog to
several clients without Postgres. Migration is not a rename: DuckLake supports no indexes,
primary/foreign keys, `UNIQUE`, `CHECK` or generated columns, and `CREATE TABLE AS` drops
constraints and defaults, so macros and views need re-pointing. It stays with the Dagster release
owner, gated on a rollback image and a proof that no historical fact row, watermark or accounting
counter changes.

## Acceptance layers

`oracles.py` provides the mechanical layers and refuses to inflate what was checked:

| Layer | Mechanism | Status here |
|---|---|---|
| `invariants` | Declared SQL rules: not-null, unique, in-range, accepted values, row-count bounds, relationship, custom predicate | Implemented |
| `replay` | Re-run a query against the verified generation; compare canonical order-insensitive digest | Implemented |
| `backtest` | Pre-registered validation window + selection rule, then ONE holdout read, receipted; `trial_count` is declared and ledgered | Implemented |
| `solver` | Constraint solver as an independent oracle for combinatorial questions | Not implemented — no declared question yet |

`oracles.verdict(layers, verification=receipt)` returns `accepted_by_mechanical_evidence` only when
at least one independent layer passed and none failed. `fully_verified` additionally requires
**every** layer to have run, **no** layer reporting `multiplicity: unadjusted`, and a **deep**
verification receipt. A missing layer is `not_applicable`, never a pass; `realized_savings` stays
`null`.

Reuse is refused twice over: a second call with the same backtest specification raises instead of
re-reading the holdout, and a second *distinct* specification must declare a higher `trial_count`
than the append-only `<receipt>.trials.jsonl` ledger already records. That is the denominator: a
result claimed after silent iteration inflates the survivor rate, so the ledger counts abandoned
and rejected specifications too, and `trial_count > 1` without a declared adjustment is labelled
`unadjusted` and blocked from `fully_verified`. The declaration is a method name from
`holm-bonferroni`, `bonferroni` or `benjamini-hochberg`; this layer reports point estimates, not
p-values, so it gates the label rather than computing an adjusted statistic.

```bash
python -B scripts/token-usage-lake/research/experiment.py \
  --input /private/normalized-study.json \
  --snapshots /private/published-snapshots \
  --out /private/new-study-report.json
```

The input reuses the normalized `attempts`, `outcomes`, `freeze_epoch` and
`coverage_complete` contract in `outcomes.py`; a complete synthetic example is `bundle()` in
`test_workflow.py`. Also supply `snapshot_run_id`, the SHA256 of that generation's exact
`manifest.json` bytes as `snapshot_manifest_sha256`, `usage_grain` (`native` OR `gateway`),
`assignment_unit: paired_session`, and `metric` (`tokens`, default, or `points`). Only opaque
metadata identifiers and numerical measures belong in this input, never prompts or raw bodies.

Every attempt remains in cost, including failed, aborted and rework attempts. Task acceptance
must come from the declared independent oracle/human provenance contract; an artifact or an
agent's end-of-turn is not acceptance. Unknown outcomes/costs remain unknown. Analyze one
project/resource/calibration stratum at a time. Every included independent session must have
both arms; incomplete sessions are not silently dropped. The report resamples paired sessions,
not calls, and records input, dataset, source-code and Python provenance.

**Exit codes:** 0 = exploratory analysis computed, **not verified savings**; 1 = report written
but data/denominator/pairing blocks analysis; 2 = invalid input, integrity error or output failure.
Output is create-only. Reports always retain `realized_savings: null`. The conservative quality
screen is not confirmatory cluster-adjusted inference; fitted-point calibration uncertainty is
not included. A manifest binding does not independently prove the normalized adapter's lineage.
No adapter from current task proxies to independently accepted trial outcomes is asserted here.

One offline acceptance command, with no network, service changes or provider calls:

```bash
python -B -m unittest discover -s scripts/token-usage-lake/research -v
```

This checks snapshot publication/loading, wrong-data rejection, all-attempt cost conservation,
unknown/proxy rejection, resource separation, paired analysis and the CLI's success/blocked paths,
plus read-only serving (tamper, forged manifest+pointer, undeclared-file isolation, cheap
receipt reuse, in-place rewrite with restored mtime, forged receipt, corrupt chain) and the oracle
layers (invariant failure, replay mismatch, holdout reuse refusal, trial-ledger disagreement, and a
verdict that reports absent evidence as `not_applicable`). 51 tests, no network.
The older Notes sizing/solver tools remain retained research prototypes. The historical Airflow
packages are not the active orchestrator. Nothing is deleted or enabled by this consolidation.

## Access

- Editor: <https://dokku.tailf59220.ts.net:8845/>, no application login.
- Experiments: <https://dokku.tailf59220.ts.net:8846/>, no application login.
- Existing Dagster: <https://dokku.tailf59220.ts.net:8444/>.

Tailscale controls browser access. Nginx binds only host loopback, Docker publishes no application
ports, and Tailscale Funnel stays disabled. The editor and MLflow require no separate login.
MLflow retains its host/origin checks. The containers communicate over a private Docker network;
browser traffic uses Tailscale HTTPS and desktop computation uses mutual TLS. Old application
passwords remain in rbw as inactive recovery material and are not mounted into either app.

## Data and time series

The existing desktop facts feed runs once a minute. A separate bounded history follower scans
retained sources, keeps restricted raw data on the desktop, and transfers sanitized structural
facts to Dagster's acquisition staging directory. Backfill completion and missing producer
events must be measured separately; a running service does not establish complete capture.

Dagster remains the sole writer of the live lake. The publisher copies the file before opening
DuckDB, rejects active/uncheckpointed writes and validates counts against its completed-run
receipt. New generations have immutable Parquet files, UTC day/hour partitions, checksums and a
manifest. Identical files share storage through hard links. A latest pointer changes atomically;
experiments can retain a specific run ID. No historical dataset is automatically deleted.

Native calls, gateway requests and tool invocations have separate minute, hour and day series.
The native series includes provider, model and effort; the gateway series includes account,
model and status; the tool series includes tool, direct/nested source and status. Missing
measures remain null, absent bins are unobserved, and bins record whether their end precedes
the source watermark. Late arrivals rebuild affected exports in a new generation.

The desktop acquisition overlay bounds its output descriptor cache at 128 files. Its original
source hash and installed hash are recorded privately. The 13-case capture/recovery acceptance
and a 600-output descriptor test cover the installed version.

## Use

In a notebook, `latest_snapshot()` returns the current path and manifest. Pin the returned run ID
for an experiment with `SCIENCE_SNAPSHOT_RUN_ID`; this keeps every cell and report on the same
generation while ingestion advances. `desktop_client()` opens an explicit remote CPU connection; pass data or
aggregate arrays as task arguments. `configure_tracking()` selects the private MLflow endpoint
without loading application credentials. Keep full prompts and raw payloads out of the server workspace.

Copy `report.qmd` and `report.scss` into a writable report directory and run
`quarto render report.qmd --to html`. The light/dark report uses system fonts without remote imports.
Attach the HTML to an MLflow run with `mlflow.log_artifact(...)`. The report template records
dataset provenance and readiness, not realized savings or accepted-task success.

## Verify and deploy

Create a Python 3.12 virtual environment and use `uv pip sync --require-hashes requirements.lock`.
Run `python -m unittest discover -s . -p 'test_*.py' -v`, `ruff check .` and
`marimo check notebook.py`. `smoke.py` checks APIs without application credentials, a real Dokku-to-desktop numerical
task, and a synthetic MLflow run. It deliberately labels the result as infrastructure validation.

`deploy.sh` expects the source context at `/tmp/token-science-build` on `ProxMox.Dokku`, with
Quarto's `quarto-1.8.27-linux-amd64.deb` named `quarto.deb`. The Dockerfile checks its SHA256.
Provision the private compute TLS mount first. Image builds use CPU/memory limits; image IDs
produce immutable release tags. The acquisition and compute unit files are enabled user services.

## Reversal

Stop `token-research` and `token-experiments` with `dokku ps:stop`. Disable
`token-science-compute.service` and `token-lake-acquire.service`. Remove only the Tailscale Serve
handlers for HTTPS ports 8845 and 8846. The previous feed timer configuration is saved as
`token-lake-feed-bootstrap.timer.before-research`. Keep all volumes, raw history, datasets and
experiment artifacts. Dagster's existing route and data remain in place.

## Object store and dashboards

The lake is a single DuckDB file with one writer, which is correct and also the reason no BI tool
could read it. Readers now get an object store instead: a marts layer is exported from a warehouse
copy, published as immutable Parquet under an S3 prefix, verified, and read by DuckDB/AWS tools
without touching the writer.

The home server already runs MinIO at `https://minio.home301server.com.br` (the `dokku-minio`
alias), so no new service was installed. Bucket `token-lake`, prefix `marts/`. Credentials resolve
from `S3_ENDPOINT`/`S3_ACCESS_KEY_ID`/`S3_SECRET_ACCESS_KEY`, else from the `mc` alias; no key is
written into this repo or into a project file.

```sh
# 1. export the marts from a warehouse copy (source opened read-only)
.venv/bin/python -m marts --warehouse <warehouse.duckdb> --out ~/.local/state/token-usage-lake/marts/<stamp>
# 2. publish as an immutable generation and verify size + MD5 ETag per object
.venv/bin/python -m objectstore --base dokku-minio/token-lake/marts publish <marts_dir> --run-id <id>
# 3. optionally re-hash the bytes that came back (downloads every object)
.venv/bin/python -m objectstore --base dokku-minio/token-lake/marts verify --run-id <id> --deep
# 4. give a tool that cannot configure httpfs a local mirror, SHA-256 checked against the manifest
.venv/bin/python -m objectstore --base dokku-minio/token-lake/marts download <dir>
```

DuckDB reads the published generation directly; `latest.json` is the only pointer:

```python
import objectstore
connection, generation = objectstore.open_s3("dokku-minio/token-lake/marts")
connection.execute("SELECT model, sum(points) FROM fact_calls GROUP BY 1 ORDER BY 2 DESC").fetchall()
```

Trust chain, in order: the marts manifest records each file's bytes, SHA-256 and MD5; publish
uploads under `<base>/<run_id>/`, re-reads size and ETag per object and refuses an empty ETag (an
`s3://` path handed to `mc` is a *local* directory, so a silent copy would otherwise certify
itself); the manifest is uploaded last as the commit marker and `latest.json` follows; a second
publish of the same run id must match byte-for-byte or it is refused. Deep verification downloads
each object and compares SHA-256. A truncated or swapped download during `download` fails against
the manifest instead of feeding a dashboard.

Text-bearing columns never leave: `marts.py` drops `cwd`, `brief`, `prelude`, `path`, `prompt`,
`text`, `content`, `body`, `raw` and `message` by name, records what it dropped in the manifest,
and refuses a table whose every column is denied.

Rill v0.92 runs the dashboards (`rill/token-diet`: four models, two metrics views, two
explores). Two constraints, both measured: a model whose name equals the table it reads fails with
`cannot be reconciled due to cyclic dependency`, so the models are named `marts_<table>`; and Rill's
embedded DuckDB is older than 2.0.0 and cannot attach a database file this environment writes
(`version number 999`), so Rill reads the Parquet mirror from `download` rather than the `.duckdb`
produced by `materialize`. `materialize` still serves notebooks and other tools that want one local
file, and it records `_generation` (run id, S3 URI, manifest SHA-256) inside the file so a stale
dashboard is provable.

What this does not do: it does not publish from the live lake. Exporting the marts is a desktop
step against a warehouse copy, and making the live lake publish on a schedule stays with the lake's
single writer (Steward).
