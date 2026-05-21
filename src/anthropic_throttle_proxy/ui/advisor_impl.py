"""GROQ advisor — cheap, fast, Anthropic-independent control-plane diagnosis.

Reads a snapshot of the proxy state and proposes knob tweaks in natural
language. Off by default (`ADVISOR_ENABLED=false`). Strictly out of the hot
path: imported lazily only when a throttle event fires the auto-advisor
(`proxy._maybe_advise`) or when `/ui/advisor` is invoked.

Uses GROQ's OpenAI-compatible endpoint via raw aiohttp (NO SDK), for two
reasons:
  1. The provider is INDEPENDENT of Anthropic. Asking Anthropic for advice
     during an Anthropic 429 storm hits the same exhausted limit — GROQ runs
     on its own quota, so a diagnosis still lands when it matters most.
  2. No heavy SDK is imported, so a transitive SDK bug can never reach the
     proxy (invariant #1).

Model defaults to llama-3.1-8b-instant (sub-second, well under $0.001 per
diagnosis); override via `ADVISOR_MODEL`.
"""

from __future__ import annotations

import os

import aiohttp

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_MODEL = "llama-3.1-8b-instant"
_TIMEOUT_S = 8.0

_SYSTEM = """\
You are an expert SRE advisor for `anthropic-throttle-proxy`, a reverse proxy
in front of api.anthropic.com that paces a fleet of Claude Code / opencode /
codex clients to avoid rate-limit pushback.

Rate-limit model (important — do not confuse the two regimes):
  - Claude Code on a Max/Pro OAuth token is gated by a 5-hour ROLLING window
    plus a 7-day weekly cap — NOT by RPM/ITPM/OTPM and NOT by a fixed
    concurrent-stream count. A 429 here usually means the rolling/weekly
    budget is near exhaustion, OR too many parallel streams hit an
    acceleration limit.
  - API-key (pay-as-you-go) traffic IS gated by RPM / input-TPM / output-TPM
    per model family, surfaced via `retry-after` + `anthropic-ratelimit-*`
    headers.
  - 529 = upstream OVERLOADED (Anthropic-side), NOT your usage — back off and
    retry; do not lower your own ceiling in response.

Knobs you may recommend (do NOT invent others):
  CLAUDE_API_THROTTLE_MAX      — per-bearer concurrent ceiling (int, 1..64).
  THROTTLE_QUEUE_MODE          — off | observe | fair | reactive.
  THROTTLE_MIN_DISPATCH_GAP_MS — min ms between upstream dispatches (0..500);
                                 smooths millisecond burst WITHOUT capping
                                 throughput.
  THROTTLE_AIMD_MIN            — AIMD floor for the live ceiling (>=1).
  THROTTLE_AIMD_BACKOFF_S      — cooldown after a shrink before ramp resumes.

Reasoning hints:
  - High `retries` + low `inflight` → per-bearer concurrency too high for the
    current budget; lower CLAUDE_API_THROTTLE_MAX or switch QUEUE_MODE=fair.
  - Low `retries` but bursty `inflight` (many in the same instant) → raise
    THROTTLE_MIN_DISPATCH_GAP_MS to 50-100.
  - Repeated 529 → upstream overload; widen THROTTLE_AIMD_BACKOFF_S, don't
    lower MAX.

Output format:
  - One short paragraph diagnosing the likely culprit, citing snapshot numbers.
  - A bullet list of specific knob changes (ENV_VAR = value).
  - One line on the trade-off.
Be terse. No filler.
"""


async def recommend(snapshot: dict) -> str:
    """POST the snapshot to GROQ and return the model's recommendation text.

    Raises RuntimeError if GROQ_API_KEY is unset, or aiohttp errors on HTTP
    failure. Both call sites (the auto-advisor and POST /ui/advisor) wrap this
    in try/except so a failure never reaches the proxy hot path.
    """
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY is not set")
    model = os.environ.get("ADVISOR_MODEL", DEFAULT_MODEL)
    body = {
        "model": model,
        "max_completion_tokens": 600,
        "temperature": 0.3,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {
                "role": "user",
                "content": (
                    "Current proxy snapshot. Diagnose the throttle + recommend.\n\n"
                    f"```json\n{snapshot!r}\n```"
                ),
            },
        ],
    }
    timeout = aiohttp.ClientTimeout(total=_TIMEOUT_S)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            GROQ_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=body,
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return "(advisor returned an unexpected response shape)"
    return (text or "").strip() or "(advisor returned no text)"
