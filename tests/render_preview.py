"""Render /ui to a standalone HTML file with synthetic data, for eyeballing.

Not a test — a dev tool. `uv run python tests/render_preview.py /tmp/ui.html`
then screenshot it. Reading CSS is not verification; the 04/08 "broken spaces"
regression was only visible in a 2560px screenshot.
"""

from __future__ import annotations

import math
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import jinja2

from anthropic_throttle_proxy import history
from anthropic_throttle_proxy.ui import presentation, routes, signals

NOW = time.time()


def _seed_history() -> None:
    """Deterministic traffic shape: a slow tide plus a pushback storm at the end.

    Deterministic on purpose — two renders of the same code must produce the
    same picture, or a screenshot cannot be compared against the last one.
    """
    for i in range(360):
        tide = math.sin(i / 28.0)
        jitter = math.sin(i * 2.4)  # stands in for noise, without an RNG
        for _ in range(int(14 + 9 * tide + jitter)):
            history.observe(200, 0.6 + 0.4 * (1 + jitter))
        if i > 250 and jitter > 0.2:
            history.observe(429, 0.2)
        history.record(
            queued=max(0, int(4 * tide + jitter)),
            inflight=max(0, int(5 + 3 * tide)),
            cap=8,
            now=NOW - (360 - i) * history.RESOLUTION_S,
        )


def _meter(
    label: str,
    pct: float | None,
    reset_in: str = "",
    note: str = "",
    *,
    resets_at: float | None = None,
    reset_at: str = "",
    window_mins: int | None = None,
) -> dict:
    # #227 derives the icon from `window_mins` before falling back to the label,
    # and renders the absolute UTC reset beside the countdown. The preview
    # derives `reset_at` the same way `presentation._reset_at` does rather than
    # leaving it empty: the stamp is what overflowed a meter cell on the live
    # page (18/09/2026), and a preview that omits the field cannot show it.
    if not reset_at and resets_at is not None:
        reset_at = datetime.fromtimestamp(resets_at, UTC).strftime("%d/%m/%Y %H:%M UTC")
    return {
        "label": label,
        "icon": {"5h": "⏱️", "7d": "📅"}.get(label, "📊"),
        "pct": pct,
        "reset_in": reset_in,
        "note": note,
        "resets_at": resets_at,
        "reset_at": reset_at,
        "window_mins": window_mins,
    }


def _context() -> dict:
    return {
        "asset_v": "preview",
        "signals": signals.collect(),
        "status": {
            "level": "pacing",
            "verdict": "PACING",
            "since": "23m",
            "detail": "2 of 4 bearers pacing · binding: 7d window 94% on b144f62f",
            # The hero renders verdict + binding + way-out as one object, so the
            # preview has to carry a binding or it accepts a hero that never
            # shows the thing the page exists to show.
            "binding": {
                "subscription": "pedro@pm.me",
                "sub": "pedro@pm.me",
                "window": "7d",
                "pct": 94,
                "resets_in": "2d 04h",
                "next_usable": "pedro@proton.me",
                "next_usable_pct": 38,
            },
        },
        # #227 context. The preview has to carry them or the template raises on
        # `lanes.age_s` — which is exactly how this tool was found broken: the
        # PR's own acceptance gate needs a rendered page, and the page would
        # not render at all.
        "as_of": time.strftime("%H:%M:%S", time.localtime(NOW)),
        "show_local": True,
        "partial_response": False,
        "config_count": 14,
        "override_count": 3,
        "build": "/nix/store/…-anthropic-throttle-proxy-0.1.0/lib/python3.13/site-packages",
        "fleet_ui_config_error": None,
        "providers": [
            {
                "name": "anthropic",
                "icon": "✳️",
                "kind": "primary",
                "level": "pacing",
                "ok": True,
                "upstream": "https://api.anthropic.com",
                "served": 5258,
                "inflight": 3,
                "queued": 2,
                "max_concurrent": 8,
                "egress_ok": True,
                "auth_dead": False,
                "err": "",
            },
            {
                "name": "zai",
                "icon": "✨",
                "kind": "sibling",
                "level": "healthy",
                "ok": True,
                "upstream": "http://127.0.0.1:8766",
                "served": 191,
                "inflight": 0,
                "queued": 0,
                "max_concurrent": 4,
                "egress_ok": True,
                "auth_dead": False,
                "err": "",
            },
        ],
        "subscriptions": [
            {
                "id": "A",
                "identity": "pedro@pm.me",
                "sub": "pedro@pm.me",
                "family": "anthropic",
                "plan": "max20",
                # A configured caption that disagrees with the observed plan is
                # the #227 conflict shape: it must render as an annotation, not
                # as a replacement for the observation.
                "plan_caption": "max 20×",
                "plan_conflict": True,
                "src": "endpoint",
                "meters": [
                    _meter("7d", 94, "2d 04h", resets_at=NOW + 2 * 86400, window_mins=10080),
                    _meter("5h", 61, "1h 12m", resets_at=NOW + 4320, window_mins=300),
                ],
                "pace": 1.42,
                "pace_warn": True,
                "eta": "1d 22h",
                "status": "ok",
                "detail": "",
            },
            {
                "id": "B",
                "identity": "pedro@proton.me",
                "sub": "pedro@proton.me",
                "family": "anthropic",
                "plan": "max20",
                "src": "proxy",
                "meters": [
                    _meter("7d", 38, "2d 04h", resets_at=NOW + 2 * 86400, window_mins=10080),
                    _meter("5h", 12, "1h 12m", resets_at=NOW + 4320, window_mins=300),
                ],
                "pace": 0.71,
                "pace_warn": False,
                "eta": "",
                "status": "ok",
                "detail": "",
            },
            {
                "id": "chatgpt:work",
                "identity": "Codex A",
                "icon": "🌀",
                "account_badge": "A",
                "sub": "",
                "family": "openai",
                "plan": "pro",
                "src": "report",
                # Full meter: the preview must show what an exhausted lane looks
                # like, because that is the state the table used to render `ok`.
                "meters": [
                    _meter("codex", 100, "14h 59m", resets_at=NOW + 53940),
                    _meter("codex_bengalfox", 54, "17h", resets_at=NOW + 61200),
                ],
                # EXHAUSTED, so no burn projection: _build_subscriptions drops
                # pace + ETA once a subscription is already refusing. The
                # preview builds rows directly, so it has to mirror that or the
                # screenshot teaches a shape the code no longer produces.
                "pace": None,
                "pace_warn": False,
                "eta": "",
                "status": "exhausted",
                "detail": "binding meter at 100% — upstream answers 'you've hit your usage limit'",
            },
            {
                "id": "zai:plan",
                "identity": "Z.AI",
                "icon": "✨",
                "sub": "",
                "family": "chinese-frontier",
                "plan": "Pro V3",
                "src": "Pi meter report",
                "meters": [
                    _meter("7d", 2, "5d 05h", resets_at=NOW + 450000, window_mins=10080),
                    _meter("5h", 1, "1h 52m", resets_at=NOW + 6720, window_mins=300),
                ],
                "pace": 0.4,
                "pace_warn": False,
                "eta": "",
                "status": "ok",
                "status_icon": "✅",
                "detail": "",
                "billing": {
                    "current": True,
                    "auto_renew": True,
                    "renewal_label": "$80/mo",
                    "next_renew_display": "27/09",
                    "plan_status": "VALID",
                    "payment_type": "WAIT_PAY",
                },
            },
            {
                "id": "deepseek",
                "identity": "DeepSeek",
                "icon": "🐋",
                "sub": "",
                "family": "chinese-frontier",
                "plan": "pay-go",
                "src": "report",
                # Money, not a percentage — the row a windowed table could not show.
                "meters": [_meter("balance", None, note="$15.20")],
                "pace": None,
                "pace_warn": False,
                "eta": "",
                "status": "ok",
                "detail": "",
            },
            {
                "id": "copilot:personal",
                "identity": "copilot:personal",
                "sub": "",
                "family": "github",
                "plan": "individual",
                "src": "report",
                "meters": [
                    {
                        "label": "premium_interactions",
                        "pct": 100.0,
                        "reset_in": "",
                        "note": "",
                        "exhausted_ok": True,
                    },
                    {
                        "label": "chat",
                        "pct": None,
                        "reset_in": "",
                        "note": "unlimited",
                        "exhausted_ok": False,
                    },
                    {
                        "label": "completions",
                        "pct": None,
                        "reset_in": "",
                        "note": "unlimited",
                        "exhausted_ok": False,
                    },
                ],
                "pace": None,
                "pace_warn": False,
                "eta": "",
                "status": "unknown",
                "detail": "no usage API for individual seats",
            },
        ],
        "lanes": {
            "stale": False,
            # #227 freshness block: the meters come from an out-of-process
            # probe whose cadence is NOT the 2 s page poll, and the page has to
            # say so on its own.
            "age_s": 42.0,
            "interval_s": 900.0,
            "observed_at": time.strftime("%d/%m/%Y %H:%M UTC", time.gmtime(NOW - 42)),
            "next_sample_in_s": 858.0,
            "error": "",
            "registry": [
                {"icon": "✳️", "provider": "Claude"},
                {"icon": "🌀", "provider": "Codex"},
                {"icon": "🌙", "provider": "DeepInfra"},
                {"icon": "🚀", "provider": "Groq"},
                {"icon": "✨", "provider": "Z.AI"},
            ],
        },
        "identity": {"collapsed": False},
        "copilot": [],
        "bearers": [
            {
                "bearer_id": "b144f62f",
                "account": "A",
                "identity": "pedro@pm.me",
                "inflight": 3,
                "queued": 2,
                "served": 4102,
                "unified": {"util_5h": 0.61, "status": "allowed"},
                "unified_5h_stale": False,
                "last_ratelimit": {"retry-after": "12"},
                "limiter": {"max_concurrent": 4, "hard_max": 8, "queued_per_client": {"a": 1}},
            },
            {
                "bearer_id": "c0de9a11",
                "account": "B",
                # The longest shape the real host produces, not a short stub: an
                # 18-character address with no break opportunity is what painted
                # 22px past its column at 1600 and 1920px on the live page
                # (18/09/2026), and a preview that only ever renders
                # `pedro@pm.me` cannot show it. Same lesson as `reset_at`.
                "identity": "phsb5321@gmail.com",
                "inflight": 0,
                "queued": 0,
                "served": 1156,
                "unified": {"util_5h": 0.12, "status": "allowed"},
                "unified_5h_stale": False,
                "last_ratelimit": {},
                "limiter": {"max_concurrent": 8, "hard_max": 8, "queued_per_client": {}},
            },
        ],
        "inflight": 3,
        "queued": 2,
        # Non-zero so the preview shows the hold row; at rest it is hidden.
        "holds": 2,
        "served": 5258,
        "disconnects": 9,
        "retries": 4,
        "max_concurrent": 8,
        "queue_mode": "fair",
        "min_dispatch_gap_ms": 50,
        "upstream": "https://api.anthropic.com",
        "central_url": "(direct)",
        "central_status": "unknown",
        "advisor_enabled": True,
        "last_advisor": {
            "trigger": "429 on b144f62f",
            "text": "Account A's 7d window is the binding constraint at 94% with a 1.42x pace — "
            "it exhausts before reset. Move bulk traffic to B (38%) rather than lowering "
            "MAX, which would only lengthen the queue.",
        },
    }


def _subscription_only(ctx: dict) -> dict:
    """The `defaults.show_primary: false` projection, through the real code path.

    Not a hand-built variant: `presentation.apply_display` is what production
    calls, so a preview that diverges from it would accept a layout the server
    never renders.
    """
    return presentation.apply_display(ctx, {"defaults": {"show_primary": False}})


def main(out: Path, mode: str = "local") -> None:
    _seed_history()
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(routes._TEMPLATES)), autoescape=True
    )
    ctx = _context()
    if mode == "subscriptions":
        ctx = _subscription_only(ctx)
    # Production derives the badge icon from the status word; the fixture used
    # to omit it on every row but Z.AI, so the preview drew `❔ ok` — a shrug
    # next to a healthy lane. A preview that misreports the thing it exists to
    # let you eyeball is worse than no preview.
    for row in ctx["subscriptions"]:
        row.setdefault("status_icon", routes._STATUS_ICONS.get(row["status"], "❔"))
    html = env.get_template("dashboard.html").render(**ctx)
    html = html.replace("/ui/static/style.css?v=preview", str(routes._STATIC / "style.css"))
    out.write_text(html)
    print(out)


if __name__ == "__main__":
    if not 1 < len(sys.argv) < 4:
        sys.exit("usage: render_preview.py <out.html> [local|subscriptions]")
    main(Path(sys.argv[1]), sys.argv[2] if len(sys.argv) == 3 else "local")
