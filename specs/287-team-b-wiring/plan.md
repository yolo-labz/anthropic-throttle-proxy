# Team B wiring: independent identity, conservative failure

## Plan and hypothesis

The report contract in #286 exposes a Team B row but the executable sampler
does not collect it. The draft G2 change used the owner browser for both
`/my/seat` requests; another project ID cannot establish a distinct account.
Reuse the existing seat validator and attach-only browser client, with the
existing separate `xiaomi-mimo2` profile and an independently configured
expected B identity. Preserve the two-row default and never add allowances.

The falsifier is a healthy owner profile presented as B: it must produce no B
seat request and no usable B meter. An absent or unavailable B profile must
never cause a browser launch or borrow owner capacity.

## Tasks and implementation

- [x] Transport pH's frozen G2 candidate without editing its owner worktree.
- [x] Reconcile the fixed synthetic row ID as `mimo:team-b` across producer,
  consumer and tests. Nix G1 validates the exact configured identity set.
- [x] Reject B configuration without Team or an independent expected identity
  before browser attachment. `MIMO_TEAM_B_PROJECT_ID` remains opt-in.
- [x] Read B through `xiaomi-mimo2`, using `start_if_down=False`; verify its
  profile against `MIMO_TEAM_B_EXPECTED_ACCOUNT_ID` before reading its seat.
- [x] Retain the fresh unknown B row for failed/mismatched readings; preserve
  owner results and keep independent seat meters separate.
- [x] Exercise missing identity, owner-identity reuse, wrong live identity,
  unavailable profile, failed reading, unassigned seat and legacy default.
- [ ] Final-head full pytest, Ruff and normal PR checks.
- [ ] Coherent Nix G1 + final proxy pin delivery, owned by the runtime lane.
- [ ] Protected G4 credential binding and fresh B quota verification, followed
  by G5 observed useful traffic; no synthetic test substitutes for these.

## Delivery boundaries

The browser helper opens and closes only its own background tab. Each attach,
navigation and fetch is bounded; the existing service's 90-second deadline
remains the overall ceiling. Timeout keeps the previous report aging out;
there is no retry loop or lifecycle recovery in the sampler.

This change does not enable B, invent account/project identifiers, collect
keys, purchase capacity, alter inference routing or establish provider quota
independence. Protected configuration must come from independent identity
evidence. Roll back through one revert PR plus the retained prior sampler
wrapper; preserve reports and sessions.

## Provenance

pH authored the producer/row rename and synthetic fixtures on
`292-team-b-wiring` from `676cfefa7e1cd88b41c2b552014edcd361efd435`, then froze
and released the three-file diff after desktop I/O admission refused execution.
Mac integration added the independent-profile correction and its falsifiers.
The original owner worktree remains intact.
