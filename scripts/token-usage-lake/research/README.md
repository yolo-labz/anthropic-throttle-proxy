# Token Diet research workspace

Marimo provides the Python/SQL editor. MLflow stores experiment parameters, metrics and artifacts.
Quarto renders scientific reports. DuckDB, Polars, NumPy, SciPy, statsmodels, Plotly, Altair and
JupyterLab are installed in the same hash-locked environment. Python execution needs no LLM call.

The browser editor and tracking service run on Dokku. A Dask CPU worker runs on the desktop,
with mutual TLS over Tailscale, a two-CPU quota and a 4 GiB systemd memory limit. GPU inference
is not part of this deployment. No running model or provider selection changes.

## Access

- Editor: <https://dokku.tailf59220.ts.net:8845/>, token in rbw `api/token-research`.
- Experiments: <https://dokku.tailf59220.ts.net:8846/>, user `pedro`, password in `api/token-experiments`.
- Existing Dagster: <https://dokku.tailf59220.ts.net:8444/>.

Ingress is private to the tailnet. Nginx binds only host loopback. The editor uses native token
authentication; MLflow uses native basic authentication and its host/origin checks. Secrets and
TLS private keys stay outside this repository. Marimo starts quietly to avoid logging its token.
The two containers communicate over a private Docker network with authenticated HTTP; browser
traffic uses Tailscale HTTPS and desktop computation uses mutual TLS.

## Data and time series

The existing desktop facts feed runs once a minute. A separate bounded history follower scans
retained sources, keeps restricted raw data on the desktop, and transfers sanitized structural
facts to Dagster's acquisition staging directory. Backfill completion and missing producer
events must be measured separately; a running service does not establish complete capture.

Dagster remains the sole writer of the live lake. The publisher copies the file before opening
DuckDB, rejects active/uncheckpointed writes and validates counts against its completed-run
receipt. New generations have immutable Parquet files, UTC day partitions, checksums and a
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
for an experiment. `desktop_client()` opens an explicit remote CPU connection; pass data or
aggregate arrays as task arguments. `configure_tracking()` sets the authenticated MLflow client
without printing credentials. Keep full prompts and raw payloads out of the server workspace.

Copy `report.qmd` into a writable report directory and run `quarto render report.qmd --to html`.
Attach the HTML to an MLflow run with `mlflow.log_artifact(...)`. The report template records
dataset provenance and readiness, not realized savings or accepted-task success.

## Verify and deploy

Create a Python 3.12 virtual environment and use `uv pip sync --require-hashes requirements.lock`.
Run `python -m unittest discover -s . -p 'test_*.py' -v`, `ruff check .` and
`marimo check notebook.py`. `smoke.py` checks authenticated APIs, a real Dokku-to-desktop numerical
task, and a synthetic MLflow run. It deliberately labels the result as infrastructure validation.

`deploy.sh` expects the source context at `/tmp/token-science-build` on `ProxMox.Dokku`, with
Quarto's `quarto-1.8.27-linux-amd64.deb` named `quarto.deb`. The Dockerfile checks its SHA256.
Provision the private auth and TLS mounts first. Image builds use CPU/memory limits; image IDs
produce immutable release tags. The acquisition and compute unit files are enabled user services.

## Reversal

Stop `token-research` and `token-experiments` with `dokku ps:stop`. Disable
`token-science-compute.service` and `token-lake-acquire.service`. Remove only the Tailscale Serve
handlers for HTTPS ports 8845 and 8846. The previous feed timer configuration is saved as
`token-lake-feed-bootstrap.timer.before-research`. Keep all volumes, raw history, datasets and
experiment artifacts. Dagster's existing route and data remain in place.
