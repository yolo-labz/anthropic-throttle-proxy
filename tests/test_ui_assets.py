"""Static-asset cache busting.

Regression for the 03/08/2026 "UI completely broken" report: the dashboard
rendered the current markup with a stylesheet from a previous build. Nix pins
every store file to mtime 1, so aiohttp answered with ``Last-Modified: 1970``
and no ``Cache-Control``; the browser's heuristic freshness window (10% of the
apparent age) then spanned years and it never revalidated ``style.css``.
"""

from anthropic_throttle_proxy.ui import routes


def test_asset_version_tracks_content(tmp_path):
    css = tmp_path / "style.css"
    css.write_text("body{color:red}")
    before = routes._asset_version(tmp_path)

    css.write_text("body{color:blue}")
    after = routes._asset_version(tmp_path)

    assert before != after
    assert routes._asset_version(tmp_path) == after  # deterministic
    assert len(after) == 12


def test_asset_version_survives_missing_static_dir(tmp_path):
    # UI failure must never take the proxy down at import time.
    assert routes._asset_version(tmp_path / "nope")


def test_asset_version_tracks_catalogue_rendering_code(tmp_path, monkeypatch):
    here = tmp_path / "ui"
    here.mkdir()
    for module in ("routes.py", "signals.py", "presentation.py"):
        (here / module).write_text("# synthetic renderer")
    (tmp_path / "output_usage.py").write_text("# synthetic journal")
    catalogue = tmp_path / "fleet_ui_config.py"
    catalogue.write_text("# original catalogue")
    monkeypatch.setattr(routes, "_HERE", here)
    before = routes._asset_version(here, here, here / "routes.py")
    # The catalogue is outside the directories hashed above.
    catalogue.write_text("# changed catalogue")
    after = routes._asset_version(here, here, here / "routes.py")
    assert before != after
