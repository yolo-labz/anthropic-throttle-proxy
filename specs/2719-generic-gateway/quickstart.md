# Bounded acceptance

All local compute runs sequentially through `desktop-job-admit --small` with
`UV_CONCURRENT_BUILDS=1`, `UV_CONCURRENT_INSTALLS=1`, `UV_CONCURRENT_DOWNLOADS=1`,
`OMP_NUM_THREADS=1`, no bytecode compilation, and the launcher's fixed limits.

1. Frozen dev install; focused canonical entrypoint/UI plus routing/security tests.
2. Full `pytest -q`, `ruff check src tests`, `ruff format --check src tests`.
3. `uv build`: inspect wheel and sdist package/assets/console metadata; build a
   wheel from the extracted sdist. Install into a fresh isolated environment
   and run the entrypoint acceptance outside the source checkout.
4. Normal commit/push hooks; PR required CI must match the final head. Verify
   no unresolved review threads before `gh pr merge --squash --delete-branch`.

Record command, exit status and evidence path in the unique secret-free status
file under `/home/notroot/.local/state/throttler-recovery/2026-10-08-2704/`.
Admission refusal keeps the task prepared for root allocation; never launch the
payload directly or borrow a server allocation. No live provider calls.
