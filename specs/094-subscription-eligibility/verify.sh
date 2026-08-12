#!/usr/bin/env bash
set -euo pipefail

root=$(git rev-parse --show-toplevel)
case "$root" in
  *-094-subscription-eligibility) ;;
  *) echo "verify.sh must run in the Spec 094 worktree: $root" >&2; exit 1 ;;
esac
cd "$root"

jobs=${JOBS:-$(nproc)}

echo '== targeted ADR-6a contract =='
uv run pytest -q tests/test_ingress_adr6a.py

echo '== ruff =='
uv run ruff check src tests
uv run ruff format --check src tests

echo '== full pytest =='
# The suite mutates process-global limiter/config state by design; cross-file
# process fan-out makes timing-sensitive tests contend for host resources and
# has produced false reds. Pytest still exercises the full suite deterministically.
uv run pytest -q

echo '== changed-module type check =='
# ponytail: isolate the changed modules; repo-wide mypy remains report-only
# with legacy baseline debt outside this slice.
uvx mypy@2.1.0 --ignore-missing-imports --no-error-summary --follow-imports=skip \
  src/anthropic_throttle_proxy/routing.py \
  src/anthropic_throttle_proxy/ingress.py

echo '== wheel/sdist build =='
rm -rf dist
uv build

echo '== throwaway container build =='
sha=$(git rev-parse HEAD)
docker build --build-arg GIT_SHA="$sha" \
  --tag "anthropic-throttle-proxy:k3-${sha:0:12}" .

echo '== mutation falsifiers (each must fail) =='

# 1) Neuter pre-egress enforcement
cp src/anthropic_throttle_proxy/ingress.py /tmp/k3-mutation-ingress.good
python - <<'PY'
from pathlib import Path
p = Path('src/anthropic_throttle_proxy/ingress.py')
s = p.read_text()
old = '''        if (
            lane_id is not None
            and required_mode is not None
            and lane_id not in tried
            and (lane := LANES.get(lane_id)) is not None
        ):
            await _probe_lane_health(session, lane)
            if not _mode_is_usable_eligible(lane_state.get(lane_id)):
                tried.add(lane_id)
                lane_id = None
                continue  # re-select down the chain; never spill to ineligible
'''
assert s.count(old) == 1, 'enforcement block not found'
p.write_text(s.replace(old, '', 1))
PY
if uv run pytest -q tests/test_ingress_adr6a.py::test_refusal_no_eligible_lane_is_pre_egress_403 tests/test_ingress_adr6a.py::test_constrained_never_spills_to_direct_key_lane; then
  echo 'FAIL: enforcement mutation did not fail its test' >&2
  mv /tmp/k3-mutation-ingress.good src/anthropic_throttle_proxy/ingress.py
  exit 1
fi
mv /tmp/k3-mutation-ingress.good src/anthropic_throttle_proxy/ingress.py
echo 'enforcement mutation OK (tests failed as required)'

# 2) Neuter the response stamp
cp src/anthropic_throttle_proxy/ingress.py /tmp/k3-mutation-ingress.good
python - <<'PY'
from pathlib import Path
p = Path('src/anthropic_throttle_proxy/ingress.py')
s = p.read_text()
old = '''            if required_mode is not None and 200 <= upstream.status < 300:
                resp.headers[CREDENTIAL_MODE_HEADER] = CREDENTIAL_MODE_SUBSCRIPTION
'''
assert s.count(old) == 1, 'stamp block not found'
p.write_text(s.replace(old, '', 1))
PY
if uv run pytest -q tests/test_ingress_adr6a.py::test_response_stamped_subscription_on_constrained_success; then
  echo 'FAIL: stamp mutation did not fail its test' >&2
  mv /tmp/k3-mutation-ingress.good src/anthropic_throttle_proxy/ingress.py
  exit 1
fi
mv /tmp/k3-mutation-ingress.good src/anthropic_throttle_proxy/ingress.py
echo 'stamp mutation OK (tests failed as required)'

# 3) Neuter spoof stripping
cp src/anthropic_throttle_proxy/ingress.py /tmp/k3-mutation-ingress.good
python - <<'PY'
from pathlib import Path
p = Path('src/anthropic_throttle_proxy/ingress.py')
s = p.read_text()
old = '''                if k.lower() not in _HOP_BY_HOP
                and k.lower() not in _RESERVED_CREDENTIAL_RESPONSE_HEADERS
'''
assert s.count(old) == 1, 'strip block not found'
p.write_text(s.replace(old, '''                if k.lower() not in _HOP_BY_HOP
''', 1))
PY
if uv run pytest -q tests/test_ingress_adr6a.py::test_spoofed_upstream_stamp_is_stripped; then
  echo 'FAIL: spoof mutation did not fail its test' >&2
  mv /tmp/k3-mutation-ingress.good src/anthropic_throttle_proxy/ingress.py
  exit 1
fi
mv /tmp/k3-mutation-ingress.good src/anthropic_throttle_proxy/ingress.py
echo 'spoof mutation OK (tests failed as required)'

# Restore pristine state after mutations
git diff --check
echo '== all gates green =='
