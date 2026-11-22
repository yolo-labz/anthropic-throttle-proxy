
### 19/11/2026: Astra API PR #94 merged, integration handoff

Forgejo confirms PR #94 is closed and merged:
pull request 94
Squash merge SHA: 1a003d7c5ced583d11fd36a30ab18a23fea75e6d
Reviewed and tested head: 48d43535318be972fe98d4015e21e85deb834260
Both required CI contexts passed on that exact head: CI / ruff-pytest (push)
in 1m36s and CI / ruff-pytest (pull_request) in 1m35s. The final frozen Python
suite passed 3211 tests in 77.51 seconds; synthetic/native qualification passed
31 checks with zero skipped. The unguarded control reproduced the capacity
collision. Spec and Standards reviewers found no concrete residual blocker.

The merged source adds same-host cross-worker organization token reservations
at the native direct-Astra API fetch boundary and shared Retry-After holds.
Rate-limit retries preserve request bytes, provider/model, effort and auth.
In-flight and crashed-sender debt retain a full conservative window. No real
provider traffic or production load testing was used for qualification.

The main-branch post-merge CI run has started; its result is not yet claimed.
The integration owner explicitly owns post-merge Pi integration and session-safe
deployment. No live workers, model selections, subscriptions, gateway pacing or
admission were changed here. Explicit verified organization/rate-group scope
and same-host shared storage remain installation requirements. This thread is
finishing after this save-state handoff so the safe GPT upgrade can resume it.
Reversal: git revert 1a003d7c5ced583d11fd36a30ab18a23fea75e6d, through the normal PR workflow.
