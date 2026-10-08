#!/usr/bin/env bash
# Revert only this combined-throughput delta; never restart bridge/quota/siblings.
set -euo pipefail
[[ $# -eq 0 || ( $# -eq 1 && "$1" = --check ) ]] || { echo 'usage: rollback [--check]' >&2; exit 2; }
unit=anthropic-throttle-proxy.service
override="$HOME/.config/systemd/user/$unit.d/99z-combined-throughput-2026-10-07.conf"
[ "$(readlink "$override")" = /nix/store/79slywzvsyhdna951w1jv73wp4damm91-99z-combined-throughput-2026-10-07.conf ]
[ "$(readlink "$HOME/.config/systemd/user/$unit.d/99-ui-runtime-2026-10-07.conf")" = /nix/store/fkabv7iwwlvv16iqdyrpgs6spz6mv6fn-99-ui-runtime-2026-10-07.conf ]
python - <<'PY'
import json, urllib.request
with urllib.request.urlopen('http://127.0.0.1:8765/__throttle/health', timeout=3) as response:
    health = json.load(response)
assert health['build'].startswith('/nix/store/939kr6a14jdl692yn675lih7n2b12d42-anthropic-throttle-proxy-0.1.0/')
assert health['inflight'] == 0 and health['upstream'] == 'http://127.0.0.1:1' and health['central_url'] == ''
bearers = health.get('bearers') or []
for bearer in bearers.values() if isinstance(bearers, dict) else bearers:
    limiter = bearer.get('limiter') or {}
    assert limiter.get('queued', sum((limiter.get('queued_per_client') or {}).values())) == 0
PY
if [[ "${1:-}" = --check ]]; then
  echo 'PASS: exact combined delta and predecessor identity; idle/closed runtime; reversal not executed.'
  exit 0
fi
rm -- "$override"
systemctl --user daemon-reload
systemctl --user show "$unit" -p ExecStart --value | grep -Fq /nix/store/95ll5i712vsrzri85hsv675ljjr09kk2-anthropic-throttle-proxy-0.1.0/bin/anthropic-throttle-proxy
systemctl --user restart "$unit"
python - <<'PY'
import json, time, urllib.request
end = time.monotonic() + 12
while True:
    try:
        with urllib.request.urlopen('http://127.0.0.1:8765/__throttle/health', timeout=1) as response:
            health = json.load(response)
        break
    except (OSError, ValueError):
        if time.monotonic() >= end:
            raise
        time.sleep(.3)  # Only bounded startup of the service just restarted.
assert health['build'].startswith('/nix/store/95ll5i712vsrzri85hsv675ljjr09kk2-anthropic-throttle-proxy-0.1.0/')
assert health['upstream'] == 'http://127.0.0.1:1' and health['central_url'] == ''
print('Verified predecessor95ll5i7 imported; inherited99/quota timer and all other services untouched.')
PY
