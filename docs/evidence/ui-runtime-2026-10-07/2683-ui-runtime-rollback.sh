#!/usr/bin/env bash
# Revert only the additive UI override, after proving its exact identity and idle state.
set -euo pipefail
[[ $# -eq 0 || ( $# -eq 1 && "$1" = --check ) ]] || { echo 'usage: rollback [--check]' >&2; exit 2; }
unit=anthropic-throttle-proxy.service
override="$HOME/.config/systemd/user/$unit.d/99-ui-runtime-2026-10-07.conf"
[ "$(readlink "$override")" = /nix/store/fkabv7iwwlvv16iqdyrpgs6spz6mv6fn-99-ui-runtime-2026-10-07.conf ]
python - <<'PY'
import json, urllib.request
with urllib.request.urlopen('http://127.0.0.1:8765/__throttle/health', timeout=3) as response:
    health = json.load(response)
assert health['build'].startswith('/nix/store/95ll5i712vsrzri85hsv675ljjr09kk2-anthropic-throttle-proxy-0.1.0/')
assert health['inflight'] == 0 and health['upstream'] == 'http://127.0.0.1:1' and health['central_url'] == ''
bearers = health.get('bearers') or []
for bearer in bearers.values() if isinstance(bearers, dict) else bearers:
    limiter = bearer.get('limiter') or {}
    assert limiter.get('queued', sum((limiter.get('queued_per_client') or {}).values())) == 0
PY
if [[ "${1:-}" = --check ]]; then
  echo 'PASS: exact additive override and idle/closed runtime; rollback not executed.'
  exit 0
fi
rm -- "$override"
systemctl --user daemon-reload
systemctl --user show "$unit" -p ExecStart --value | grep -Fq /nix/store/jmak9s5hhv33bp53npypcnlkcrjn2cxz-anthropic-throttle-proxy-0.1.0/bin/anthropic-throttle-proxy
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
        time.sleep(.3)  # Bounded startup of the service this script just restarted.
assert health['build'].startswith('/nix/store/jmak9s5hhv33bp53npypcnlkcrjn2cxz-anthropic-throttle-proxy-0.1.0/')
assert health['upstream'] == 'http://127.0.0.1:1' and health['central_url'] == ''
print('Rollback verified: old imported UI package; closed upstream; no other service changed.')
PY
