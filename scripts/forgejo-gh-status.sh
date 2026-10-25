#!/usr/bin/env bash
# forgejo-gh-status — the Forgejo→GitHub status bridge (posts ONE commit status).
#
# Vendored from phsb5321/gpu-media-toolkit (22/10/2026, fleet Forgejo migration).
# Why: GitHub Actions dispatch is billing-blocked for phsb5321 private repos
# (every run a startup_failure), so required GitHub checks can never turn green
# from GitHub itself. Forgejo is the CI-of-record; this script reports each
# Forgejo job's verdict back to GitHub as a COMMIT STATUS — the exact artefact
# required contexts are made of (NixOS: `self-hosted-ci/flake-checks`) — which
# unblocks required checks WITHOUT billing and WITHOUT admin bypass.
#
# Reusable across repos (NixOS port = vendor this file + append the step below
# with STATUS_CONTEXT set to the GitHub-side required context name). Keep job
# names/contexts stable: they are the bridge's identity.
#
# Env contract:
#   GH_STATUS_TOKEN  GitHub token with repo scope (statuses:write). Stored as a
#                    FORGEJO repo secret (the Actions secrets API works — PUT
#                    body field is `data`, 201 on forgejo 15.0.9). Never commit.
#   GH_REPO          owner/repo on GitHub, e.g. phsb5321/gpu-media-toolkit
#   GH_SHA           commit sha to report on. Mirror preserves SHAs, so the
#                    Forgejo run's sha exists on GitHub verbatim. For PR runs
#                    use the PR HEAD sha (the merge-ref sha is ephemeral):
#                    ${{ github.event.pull_request.head.sha || github.sha }}
#   STATUS_CONTEXT   status context name — MUST equal the GitHub-side required
#                    check name to unblock it.
#   STATUS_STATE     success|failure|error|pending (job.status maps cancelled
#                    and skipped onto failure so a dead job is never green).
#   STATUS_DESC      short description (optional; no double quotes)
#   STATUS_URL       target URL — the Forgejo run (optional)
#
# Step shape (each job, last step):
#   - name: Status bridge → GitHub
#     if: always()
#     continue-on-error: true     # bridge failure ≠ gate failure; a stuck
#                                 # PENDING status on GitHub is the real signal
#     env:
#       GH_STATUS_TOKEN: ${{ secrets.GH_STATUS_TOKEN }}
#       GH_REPO: phsb5321/gpu-media-toolkit
#       GH_SHA: ${{ github.event.pull_request.head.sha || github.sha }}
#       STATUS_CONTEXT: <job-name / required context>
#       STATUS_STATE: ${{ job.status }}
#       STATUS_URL: ${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}
#     run: bash scripts/forgejo-gh-status.sh
set -eu

: "${GH_STATUS_TOKEN:?GH_STATUS_TOKEN missing}" "${GH_REPO:?GH_REPO missing}" \
  "${GH_SHA:?GH_SHA missing}" "${STATUS_CONTEXT:?STATUS_CONTEXT missing}" \
  "${STATUS_STATE:?STATUS_STATE missing}"

state="$STATUS_STATE"
case "$state" in
  success | failure | error | pending) ;;
  cancelled | skipped) state="failure" ;;
  *) echo "forgejo-gh-status: unknown state '$state', mapping to error" >&2; state="error" ;;
esac

# Plain-string JSON: the contract forbids double quotes in DESC/CONTEXT/URL.
payload=$(printf '{"state":"%s","context":"%s","description":"%s","target_url":"%s"}' \
  "$state" "$STATUS_CONTEXT" "${STATUS_DESC:-}" "${STATUS_URL:-}")

curl -fsS -X POST \
  -H "Authorization: token $GH_STATUS_TOKEN" \
  -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/$GH_REPO/statuses/$GH_SHA" \
  -d "$payload" -o /dev/null

echo "forgejo-gh-status: posted '$STATUS_CONTEXT'=$state on $GH_REPO@$GH_SHA"
