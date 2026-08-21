#!/usr/bin/env bash
#
# specs/205-statusline/verify.sh — mechanical falsifier for Spec 205.
#
# Judges a LIVE proxy against the three Success Criteria in spec.md. Every check
# prints PASS/FAIL plus the raw evidence it judged; any FAIL exits non-zero.
#
# Check ORDER here follows the request that commissioned this script
# (payload → state/stale → latency). spec.md numbers the latency criterion
# SC-002 and the stale-window criterion SC-003, so every check below carries its
# canonical spec.md ID in brackets — the two orderings cannot silently drift.
#
#   CHECK 0  FR-001/FR-002  route is registered ABOVE the catch-all
#   CHECK 1  SC-001         payload ≤1024 B + EXACT 18-leaf key set, O(1)
#   CHECK 2  US2/FR-008 + SC-003   queued-vs-throttled split + stale-window drop
#   CHECK 3  SC-002         p95 < 50 ms AND strictly below health's own p95
#   CHECK 4  (self-test)    every predicate above is two-sided, on fixtures
#
# `curl -q` MUST be first on every invocation. Pedro's ~/.curlrc sets:
#   continue-at -  → a 2nd run against an existing `-o` target tries to RESUME
#                    and reports size_download=0 (reproduced 16/08/2026)
#   compressed     → %{size_download} would measure WIRE bytes, not payload
#                    bytes, against the 1 KB bound
#   retry = 3 / retry-delay = 2 → retry sleeps fold into %{time_total} and
#                    poison the p95
# Payload size is therefore measured with `wc -c` on the BODY, never on the wire.
#
# CHECK 4 runs even when the endpoint is absent, so this script is meaningful
# BEFORE implementation: it proves the judging predicates actually discriminate.
#
# Unlike specs/094-subscription-eligibility/verify.sh this script NEVER mutates
# src/ — it is safe to run while a sibling worker edits the source tree. Its
# falsifiers act on the live HTTP surface and on synthetic /tmp fixtures.
#
# Usage:  ./specs/205-statusline/verify.sh
# Env:    HOST (default 127.0.0.1:8765)   SAMPLES (default 200)
#
# Exit: 0 = all judged checks PASS (or cleanly SKIPPED: proxy not answering)
#       1 = at least one FAIL

set -uo pipefail   # deliberately NOT -e: run every check, then aggregate.

HOST=${HOST:-127.0.0.1:8765}
SAMPLES=${SAMPLES:-200}
BASE="http://${HOST}/__throttle"

root=$(git rev-parse --show-toplevel 2>/dev/null || echo "")
case "$root" in
  *205-statusline) cd "$root" ;;
  *) echo "verify.sh must run in the Spec 205 worktree (got: ${root:-<none>})" >&2
     exit 1 ;;
esac

pass=0; fail=0; skip=0
CURL=(curl -q -fsS --max-time 10)

ok()   { printf '  \033[32mPASS\033[0m  %s\n' "$1"; pass=$((pass+1)); }
no()   { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; fail=$((fail+1)); }
sk()   { printf '  \033[33mSKIP\033[0m  %s\n' "$1"; skip=$((skip+1)); }
ev()   { printf '        %s\n' "$1"; }             # raw evidence line
hdr()  { printf '\n== %s ==\n' "$1"; }

for tool in curl jq awk; do
  command -v "$tool" >/dev/null || { echo "missing required tool: $tool" >&2; exit 1; }
done

# ── The normative 18-leaf key set (spec.md "Response shape (normative)") ──────
read -r -d '' EXPECTED_LEAVES <<'EOF'
account.bearer
account.label
account.reset
account.stale
account.status
account.util
account.window
blocked_until
fleet.configured
fleet.usable
now
queue.cap
queue.depth
queue.inflight
queue_mode
schema
state
state_since_s
EOF

# Leaf paths of a JSON doc. `jq 'paths(scalars)'` is WRONG here: its select
# drops `false` and `null`, so `account.stale:false` and `blocked_until:null`
# would silently vanish and a payload MISSING them would pass. Verified
# 16/08/2026 — this type filter keeps them.
leaves() {
  jq -r '[paths as $p
    | select((getpath($p)|type) | . != "object" and . != "array")
    | $p | join(".")] | sort | .[]' "$1"
}

# p95 of whitespace-separated floats on stdin (index = ceil(0.95*N), 1-based).
p95() { sort -n | awk '{v[NR]=$1} END{if(NR==0){print "NaN";exit} print v[int(NR*0.95+0.999999)]}'; }

# ── Pre-flight: is the proxy answering at all? ────────────────────────────────
hdr "pre-flight: ${HOST}"
if ! "${CURL[@]}" --max-time 3 -o /tmp/sl-health.json "${BASE}/health" 2>/dev/null; then
  sk "proxy not answering at ${HOST} — nothing to judge"
  ev "probe: curl -q -fsS ${BASE}/health"
  ev "start it with: uv run python -m anthropic_throttle_proxy"
  ev "(a SKIP is not a failure; exiting 0)"
  exit 0
fi
health_bytes=$(wc -c < /tmp/sl-health.json)
clients=$(jq '[.bearers[].clients // {} | length] | add // 0' /tmp/sl-health.json)
served_before=$(jq -r '.served' /tmp/sl-health.json)
ok "health reachable"
ev "health payload   = ${health_bytes} B"
ev "tracked clients  = ${clients}   (the collection SC-001 must stay O(1) against)"
ev "served           = ${served_before}"

# ── CHECK 0 — FR-001/FR-002: locally handled, above the catch-all ────────────
# The proxy's last route is add_route("*", "/{path:.*}", handler), so an
# UNIMPLEMENTED /__throttle/statusline is not a local 404 — it is forwarded to
# THROTTLE_UPSTREAM and burns a bearer slot. Measured 17/08/2026: the probe came
# back `Server: cloudflare` + `CF-RAY: …-GRU` and served went 6147 → 6148.
# `served` is NOT a sound gate here: on Pedro's desktop ~35 live panes move it
# concurrently (measured 17/08/2026 — one probe, delta +3), so a naive delta
# false-positives. The DEFINITIVE local-handling signals are (a) absence of the
# upstream edge's own headers and (b) a body that parses as our schema. The
# served delta stays as informational evidence only; FR-002's slot accounting is
# gated deterministically by the unit test (tasks.md T-05), not by this probe.
hdr "CHECK 0 — route is local, not forwarded  [FR-001, FR-002]"
# No -f: we want the true status code, not a curl failure, on 404.
code=$(curl -q -sS --max-time 10 -o /tmp/sl.json -D /tmp/sl-hdr.txt \
         -w '%{http_code}' "${BASE}/statusline" 2>/dev/null || echo "000")
sleep 1
served_after=$("${CURL[@]}" "${BASE}/health" 2>/dev/null | jq -r '.served' || echo "$served_before")
upstream_edge=$(grep -icE '^(server: *cloudflare|cf-ray:)' /tmp/sl-hdr.txt 2>/dev/null || true)
schema_ok=$(jq -r 'if .schema == "statusline/1" then "yes" else "no" end' /tmp/sl.json 2>/dev/null || echo "unparsable")
[ -z "$schema_ok" ] && schema_ok="unparsable(empty body)"

IMPLEMENTED=0
if [ "$code" != "200" ]; then
  no "GET /__throttle/statusline did not answer 200 (got ${code})"
  ev "code=${code}  body=$(wc -c < /tmp/sl.json)B  schema=${schema_ok}"
  ev "upstream-edge header hits=${upstream_edge} (>0 ⇒ FORWARDED, not handled locally)"
  if [ "${upstream_edge:-0}" -gt 0 ]; then
    ev "$(grep -iE '^(server|cf-ray):' /tmp/sl-hdr.txt | tr -d '\r' | paste -sd'; ' -)"
    ev "⇒ the catch-all swallowed it: register the route ABOVE add_route(\"*\", ...)"
    ev "⇒ and it reached upstream, so it also spent a bearer slot (FR-002)"
  fi
  ev "served ${served_before} → ${served_after} (informational: live panes move this too)"
elif [ "${upstream_edge:-0}" -gt 0 ]; then
  no "answered 200 but carries UPSTREAM edge headers — proxied, not handled locally"
  ev "$(grep -iE '^(server|cf-ray):' /tmp/sl-hdr.txt | tr -d '\r' | paste -sd'; ' -)"
elif [ "$schema_ok" != "yes" ]; then
  no "answered 200 but the body is not a statusline/1 document (schema=${schema_ok})"
  ev "$(head -c 200 /tmp/sl.json)"
else
  IMPLEMENTED=1
  ok "handled locally: 200, schema=statusline/1, zero upstream edge headers"
  ev "marker=$(grep -ic '^x-anthropic-throttle-proxy:' /tmp/sl-hdr.txt || true)  edge-headers=0"
  ev "served ${served_before} → ${served_after} (informational: live panes move this too)"
  cc=$(grep -i '^cache-control:' /tmp/sl-hdr.txt | tr -d '\r' | head -1)
  if printf '%s' "$cc" | grep -qi 'no-store'; then
    ok "FR-010: Cache-Control: no-store present"
  else
    no "FR-010: Cache-Control: no-store missing"
  fi
  ev "${cc:-<no Cache-Control header>}"
fi

blocked() { no "$1"; ev "blocked by CHECK 0 — endpoint not serving locally yet"; }

# ── CHECK 1 — SC-001: bounded, exact shape, O(1) in client count ─────────────
hdr "CHECK 1 — payload ≤1024 B + EXACT normative key set  [SC-001]"
if [ "$IMPLEMENTED" -eq 0 ]; then
  blocked "SC-001 not judged"
else
  body_bytes=$(wc -c < /tmp/sl.json)
  if [ "$body_bytes" -le 1024 ]; then
    ok "payload ${body_bytes} B ≤ 1024 B, at ${clients} tracked clients"
  else
    no "payload ${body_bytes} B EXCEEDS the 1024 B bound"
  fi
  ev "$(jq -c . /tmp/sl.json | head -c 400)"

  if diff <(leaves /tmp/sl.json) <(printf '%s\n' "$EXPECTED_LEAVES" | sort) >/tmp/sl-shape.diff 2>&1; then
    ok "key set is EXACTLY the normative 18 leaves"
  else
    no "key-set drift vs the normative shape"
    while IFS= read -r l; do ev "$l"; done < /tmp/sl-shape.diff
  fi
  ev "leaf count = $(leaves /tmp/sl.json | wc -l) (expected 18)"

  # O(1): no collection that grows with clients/bearers may appear at all.
  forbidden=$(jq -r '[paths | join(".")]
    | map(select(test("(^|\\.)(clients|bearers|rr_order|queued_per_client|last_ratelimit|last_advisor)$")))
    | join(", ")' /tmp/sl.json)
  if [ -z "$forbidden" ]; then
    ok "carries no client/bearer-scaled collection (O(1) in client count)"
  else
    no "carries an unbounded collection: ${forbidden}"
  fi
  ev "health is ${health_bytes} B at ${clients} clients; statusline is ${body_bytes} B"
fi

# ── CHECK 2 — US2/FR-008 state split + SC-003 stale-window drop ───────────────
hdr "CHECK 2 — queued-vs-throttled split + stale-window drop  [US2/FR-008, SC-003]"
if [ "$IMPLEMENTED" -eq 0 ]; then
  blocked "state split and stale-window drop not judged"
else
  now=$(date +%s)
  st=$(jq -r '.state' /tmp/sl.json)
  ev "state=${st}  $(jq -c '{queue,blocked_until,fleet,account:{status:.account.status,stale:.account.stale,reset:.account.reset}}' /tmp/sl.json)"

  case "$st" in
    down|exhausted|throttled|queued|warn|ok) ok "state '${st}' is in the FR-008 enum" ;;
    *) no "state '${st}' is NOT in the FR-008 enum (down|exhausted|throttled|queued|warn|ok)" ;;
  esac

  # FR-008 severity resolution — each state implies its own precondition.
  if jq -e --argjson now "$now" '
      (.state == "queued")     as $q  | (.state == "throttled") as $t
    | (.state == "exhausted")  as $x
    | if   $q then (.queue.depth > 0) and (.blocked_until == null)
      elif $t then ((.blocked_until != null) and (.blocked_until > $now))
                   or (.account.status == "rejected")
      elif $x then (.fleet.usable == 0)
      else true end' /tmp/sl.json >/dev/null; then
    ok "state '${st}' is consistent with its FR-008 precondition"
  else
    no "state '${st}' contradicts its FR-008 precondition"
    ev "queued ⇒ depth>0 ∧ blocked_until=null; throttled ⇒ blocked_until future ∨ rejected; exhausted ⇒ usable=0"
  fi

  # queued and throttled must be DISTINGUISHABLE, never conflated.
  if jq -e '(.state == "queued") and (.blocked_until != null)' /tmp/sl.json >/dev/null 2>&1; then
    no "conflated: state=queued while a hard pause epoch is set"
  else
    ok "queued and throttled are not conflated"
  fi

  # SC-003 — a past-reset window may never be presented as live capacity.
  if jq -e --argjson now "$now" \
       '.account.reset != null and .account.reset <= $now and .account.stale == false' \
       /tmp/sl.json >/dev/null 2>&1; then
    no "SC-003 FALSIFIED: past-reset window rendered with stale=false"
    ev "$(jq -c --argjson now "$now" '{reset:.account.reset,now:$now,stale:.account.stale}' /tmp/sl.json)"
  else
    ok "SC-003: no past-reset window presented as live"
    ev "$(jq -c --argjson now "$now" '{window:.account.window,reset:.account.reset,now:$now,stale:.account.stale}' /tmp/sl.json)"
  fi

  # Cross-check the selected bearer against health's RAW snapshot. If the raw
  # binding window is already past its reset, FR-006 REQUIRES stale=true.
  b=$(jq -r '.account.bearer // empty' /tmp/sl.json)
  if [ -n "$b" ]; then
    raw=$(jq -c --arg b "$b" '.bearers[$b].unified // null' /tmp/sl-health.json)
    ev "raw health snapshot for ${b}: ${raw}"
    if [ "$raw" != "null" ]; then
      if jq -e --arg b "$b" --argjson now "$now" '
          (.bearers[$b].unified) as $u
        | [($u.reset_5h // empty), ($u.reset_7d // empty)]
        | map(select(. <= $now)) | length > 0' /tmp/sl-health.json >/dev/null 2>&1; then
        if jq -e '.account.stale == true' /tmp/sl.json >/dev/null 2>&1; then
          ok "FR-006: raw snapshot has a past-reset window and stale=true"
        else
          no "FR-006: raw snapshot has a past-reset window but stale=false"
        fi
      else
        ok "FR-006: raw snapshot has no past-reset window (stale correctly not forced)"
      fi
    fi
  else
    ev "account.bearer is null (central tier / no credentials) — cross-check n/a"
  fi
fi

# ── CHECK 3 — SC-002: p95 under 50 ms AND below health's own p95 ─────────────
hdr "CHECK 3 — p95 < 50 ms and strictly below health's p95  [SC-002]"
if [ "$IMPLEMENTED" -eq 0 ]; then
  blocked "latency budget not judged"
else
  for ep in statusline health; do
    : > "/tmp/sl-t-${ep}.txt"
    for _ in $(seq "$SAMPLES"); do
      curl -q -fsS -o /dev/null -w '%{time_total} %{http_code}\n' "${BASE}/${ep}" \
        >> "/tmp/sl-t-${ep}.txt" 2>/dev/null || echo "9.999 000" >> "/tmp/sl-t-${ep}.txt"
    done
  done
  sl_p95=$(awk '{print $1}' /tmp/sl-t-statusline.txt | p95)
  h_p95=$(awk '{print $1}' /tmp/sl-t-health.txt | p95)
  sl_bad=$(awk '$2!=200' /tmp/sl-t-statusline.txt | wc -l)
  ev "n=${SAMPLES} each   statusline p95=${sl_p95}s   health p95=${h_p95}s"

  awk -v v="$sl_p95" 'BEGIN{exit !(v+0 < 0.050)}' \
    && ok "statusline p95 ${sl_p95}s < 50 ms (invariant #4 ceiling)" \
    || no "statusline p95 ${sl_p95}s exceeds the 50 ms ceiling"

  # The comparative assertion is only MEANINGFUL when health is actually fat.
  # On a cold instance (0 bearers, 0 clients) health is ~841 B — measured
  # 17/08/2026 against the in-flight build — so both endpoints collapse to the
  # same ~1 ms per-request floor and "strictly below" becomes a coin flip. A
  # projection can only be proven cheaper than the blob it projects when a blob
  # exists, so gate the claim on a real size differential and SKIP (with the
  # measured reason) otherwise. The absolute 50 ms ceiling always applies.
  min_blob=4096
  if [ "$health_bytes" -ge "$min_blob" ]; then
    awk -v a="$sl_p95" -v b="$h_p95" 'BEGIN{exit !(a+0 < b+0)}' \
      && ok "statusline p95 strictly below health p95 (${sl_p95}s < ${h_p95}s)" \
      || no "statusline p95 NOT below health p95 (${sl_p95}s vs ${h_p95}s) — a projection must cost less than the blob it projects"
  else
    sk "comparative p95 not judged: health is only ${health_bytes} B (< ${min_blob} B)"
    ev "a cold proxy has no blob to project — both endpoints are per-request floor"
    ev "observed anyway: statusline ${sl_p95}s vs health ${h_p95}s (informational)"
    ev "re-run against a warm proxy (production health measured 69–91 KB) to judge SC-002 in full"
  fi

  # FR-009: state lives in the body; the status code is always 200.
  [ "$sl_bad" -eq 0 ] \
    && ok "FR-009: all ${SAMPLES} statusline responses were HTTP 200" \
    || { no "FR-009: ${sl_bad}/${SAMPLES} statusline responses were not 200"
         ev "$(awk '$2!=200{print "  code="$2}' /tmp/sl-t-statusline.txt | sort | uniq -c | head -5)"; }
fi

# ── CHECK 4 — self-test: are the predicates above actually two-sided? ────────
# Runs unconditionally. A judge that cannot fail is not a judge, and this is the
# only part of the script that is meaningful BEFORE the endpoint exists.
hdr "CHECK 4 — self-test: every predicate discriminates  [fixtures]"
fx=$(mktemp -d /tmp/sl-fixtures.XXXXXX)
trap 'rm -rf "$fx"' EXIT
cat > "$fx/good.json" <<'EOF'
{"schema":"statusline/1","now":1786933519,"state":"queued","state_since_s":754,
 "account":{"label":"C","bearer":"666a53af","window":"5h","util":0.25,
 "status":"allowed","reset":1786950600,"stale":false},
 "queue":{"depth":23,"inflight":10,"cap":5},"blocked_until":null,
 "fleet":{"usable":2,"configured":3},"queue_mode":"fair"}
EOF

# 4a — leaf-set predicate accepts the good shape and rejects a dropped field.
if diff <(leaves "$fx/good.json") <(printf '%s\n' "$EXPECTED_LEAVES" | sort) >/dev/null; then
  ok "4a: leaf-set predicate accepts the normative shape (18 leaves)"
else
  no "4a: leaf-set predicate rejects its OWN normative fixture — the judge is broken"
fi
jq 'del(.account.stale)' "$fx/good.json" > "$fx/no-stale.json"
if diff <(leaves "$fx/no-stale.json") <(printf '%s\n' "$EXPECTED_LEAVES" | sort) >/dev/null; then
  no "4a: leaf-set predicate MISSED a dropped false-valued field (paths(scalars) bug)"
else
  ok "4a: leaf-set predicate catches a dropped false-valued field"
fi

# 4b — stale predicate: past reset + stale=false must falsify; the two honest
#      shapes (past+stale, future+fresh) must not.
mk() { jq --argjson r "$1" --argjson s "$2" '.account.reset=$r | .account.stale=$s' \
        "$fx/good.json" > "$fx/t.json"; }
stale_pred() { jq -e --argjson now 1786933519 \
  '.account.reset != null and .account.reset <= $now and .account.stale == false' \
  "$fx/t.json" >/dev/null 2>&1; }
mk 1786800000 false; stale_pred \
  && ok "4b: stale predicate FIRES on past-reset + stale=false" \
  || no "4b: stale predicate missed past-reset + stale=false"
mk 1786800000 true;  stale_pred \
  && no "4b: stale predicate false-positives on an honestly-flagged stale window" \
  || ok "4b: stale predicate silent on past-reset + stale=true"
mk 1786950600 false; stale_pred \
  && no "4b: stale predicate false-positives on a fresh future window" \
  || ok "4b: stale predicate silent on a fresh future window"

# 4c — FR-008 resolver: a conflated queued+blocked_until payload must be caught.
jq '.state="queued" | .blocked_until=1786999999' "$fx/good.json" > "$fx/conflated.json"
if jq -e '(.state == "queued") and (.blocked_until != null)' "$fx/conflated.json" >/dev/null 2>&1; then
  ok "4c: conflation predicate catches queued-with-a-pause-epoch"
else
  no "4c: conflation predicate missed queued-with-a-pause-epoch"
fi
jq '.state="queued" | .queue.depth=0' "$fx/good.json" > "$fx/bad-queued.json"
if jq -e --argjson now 1786933519 '
    (.state == "queued") as $q
  | if $q then (.queue.depth > 0) and (.blocked_until == null) else true end' \
    "$fx/bad-queued.json" >/dev/null 2>&1; then
  no "4c: precondition predicate accepted state=queued with depth=0"
else
  ok "4c: precondition predicate rejects state=queued with depth=0"
fi

# 4d — p95 helper is correct on a known distribution (1..100 ⇒ p95 = 95).
got=$(seq 1 100 | p95)
[ "$got" = "95" ] \
  && ok "4d: p95 helper returns 95 for seq 1..100" \
  || no "4d: p95 helper returned '${got}', expected 95"

# ── Summary ──────────────────────────────────────────────────────────────────
hdr "summary"
printf '  %d PASS   %d FAIL   %d SKIP\n' "$pass" "$fail" "$skip"
if [ "$fail" -gt 0 ]; then
  if [ "$IMPLEMENTED" -eq 0 ]; then
    printf '  Spec 205 is NOT satisfied yet: the endpoint is still swallowed by the\n'
    printf '  catch-all route. CHECK 4 confirms the judging predicates are sound,\n'
    printf '  so these FAILs are real absences, not a broken harness.\n'
  fi
  exit 1
fi
printf '  all judged criteria hold\n'
exit 0
