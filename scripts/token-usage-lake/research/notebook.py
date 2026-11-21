import marimo

__generated_with = "0.25.2"

app = marimo.App(width="full", app_title="Token Diet Research")


@app.cell
def _():
    import duckdb
    import marimo as mo
    import plotly.express as px
    from science import desktop_client, latest_snapshot

    return mo, duckdb, px, latest_snapshot, desktop_client


@app.cell
def _(mo):
    mo.md("""
    # Token Diet Research

    Explore the event history, write Python and SQL, and test a hypothesis against a fixed dataset.
    Native calls and gateway requests represent different observations. Their totals overlap.
    Missing intervals are unobserved; an empty bucket does not establish zero activity.
    """)
    return


@app.cell
def _(latest_snapshot, mo):
    import datetime as dt

    snapshot, manifest = latest_snapshot()
    through = dt.datetime.fromtimestamp(
        manifest["observed_through_epoch"], dt.timezone(dt.timedelta(hours=-3))
    )
    mo.vstack(
        [
            mo.md(
                f"**Dataset:** `{manifest['run_id']}` · "
                f"**Observed through:** {through:%d/%m/%Y %H:%M} BRT"
            ),
            mo.callout(
                f"Source quality: {manifest.get('data_quality', 'not recorded')}. "
                "A completed ingestion run is separate from source freshness acceptance.",
                kind="info",
            ),
        ]
    )
    return snapshot, manifest


@app.cell
def _(snapshot, duckdb):
    import pandas as pd

    con = duckdb.connect(config={"threads": 1, "memory_limit": "512MB"})
    for name in ("calls", "requests", "tool_invocations"):
        con.read_parquet(str(snapshot / name / "**/*.parquet")).create_view(name)
    hourly = con.read_parquet(str(snapshot / "calls_3600s" / "**/*.parquet")).df()
    hourly = (
        hourly.groupby(["bucket_epoch", "provider", "model"], dropna=False)["events"]
        .sum()
        .reset_index()
    )
    hourly["hour_utc"] = pd.to_datetime(hourly["bucket_epoch"], unit="s", utc=True)
    return con, hourly


@app.cell
def _(hourly, px, mo):
    chart = px.line(
        hourly,
        x="hour_utc",
        y="events",
        color="provider",
        line_dash="model",
        title="Native model calls by hour · UTC",
        template="plotly_dark",
    )
    chart.update_xaxes(tickformat="%d/%m %H:%M", hoverformat="%d/%m/%Y %H:%M")
    mo.ui.plotly(chart)
    return


@app.cell
def _(con, mo):
    mo.ui.table(
        con.execute(
            "SELECT provider, model, effort_used, count(*) AS calls, "
            'sum("output") AS output_tokens FROM calls GROUP BY ALL'
        ).df()
    )
    return


@app.cell
def _(mo):
    mo.md("""
    ## First experiment

    **Hypothesis:** shorter section-aware compaction summaries reduce total tokens per accepted task
    while preserving executable task success.

    Freeze this dataset and the environment. Use paired held-out tasks, record failures and rework,
    and report effect sizes with uncertainty. Historical correlations do not establish causality.

    Use `desktop_client()` explicitly for remote CPU work. Record experiments with MLflow and
    attach Quarto HTML reports as artifacts. Notebook execution does not require an LLM call.
    """)
    return


if __name__ == "__main__":
    app.run()
