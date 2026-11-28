"""Render the real Observatory template for the approved owned-file UI verifier.

Default data is synthetic and visibly labeled. An explicit --source reads only
that already-published usage-summary.v1 aggregate. No service, network or capture.
"""

from __future__ import annotations

import argparse
import copy
import json
import time
from pathlib import Path

import jinja2

from anthropic_throttle_proxy import usage_summary as us
from anthropic_throttle_proxy.ui import observatory as ob
from anthropic_throttle_proxy.ui import routes

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "specs/380-usage-panel/usage-summary.v1.example.json"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--source", type=Path)
    parser.add_argument(
        "--scenario",
        choices=("normal", "stale", "missing", "many", "unknown", "zero"),
        default="normal",
    )
    args = parser.parse_args()
    if args.source and args.scenario != "normal":
        parser.error("Actual source snapshots may not be altered into fixtures.")
    source = us.load(args.source or FIXTURE)
    now = time.time() if args.source else source.generated_at.timestamp() + 30
    label = (
        "RETAINED SOURCE SNAPSHOT"
        if args.source
        else f"SYNTHETIC FIXTURE · observatory-v1/{args.scenario}"
    )
    if args.scenario == "many":
        doc = json.loads(FIXTURE.read_text())
        seed = doc["rows"][0]
        doc["rows"] = []
        for index in range(20):
            for model in range(2):
                row = copy.deepcopy(seed)
                row.update(
                    provider=f"provider-{index:02}",
                    model=f"model-{model}-" + "long-name-" * 7,
                    pane=None,
                )
                row["cache_read"] = 500000 * (index + 1)
                doc["rows"].append(row)
        source = us.parse(doc)
    elif args.scenario in {"unknown", "zero"}:
        doc = json.loads(FIXTURE.read_text())
        for row in doc["rows"]:
            row.update(
                {
                    key: None if args.scenario == "unknown" else 0
                    for key in ("requests", *us.TOKEN_CLASSES)
                }
            )
        source = us.parse(doc)
    if args.scenario == "stale":
        now += 3 * 86400
    view = ob.build_view(source, {}, now=now)
    if args.scenario == "missing":
        view = {
            "state": "missing",
            "error": "Synthetic fixture: no source publication is configured.",
        }
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(routes._TEMPLATES)),
        autoescape=True,
        undefined=jinja2.StrictUndefined,
    )
    html = env.get_template("observatory.html").render(
        ob=view,
        measures=ob.MEASURES,
        roles=ob.ROLES,
        columns=ob.COLUMNS,
        asset_v=routes._ASSET_V,
        preview_label=label,
    )
    for name in ("style.css", "observatory.css", "favicon.svg"):
        html = html.replace(f"/ui/static/{name}", (routes._STATIC / name).as_uri())
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(html)
    print(
        json.dumps(
            {
                "file": str(args.out),
                "scenario": args.scenario,
                "kind": label,
                "source_rows": len(source.rows),
                "asset_version": routes._ASSET_V,
                "generated_at": source.generated_at.isoformat(),
                "source_module": ob.__file__,
                "html_bytes": len(html.encode()),
            }
        )
    )


if __name__ == "__main__":
    main()
