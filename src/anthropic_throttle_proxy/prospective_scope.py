"""T003 foundation — pure operator-config scope authority for spec 245.

Resolves one dispatch's TRUSTED identity facts — the credential-source label,
the exact configured upstream endpoint and the model alias exactly as sent —
to ONE canonical ``(upstream, account, model)`` scope with its explicit
:class:`ledger.Budgets` and its optional canonical output default (T001's
"model default" for the output bound when ``max_tokens`` is absent).

Authority is operator configuration ONLY. No caller header, bearer hash, quota
percentage or provider-registry membership may establish a scope: a bearer hash
identifies a credential, not an account (spec 245), and a quota percentage says
nothing about whose quota it is. The model string merely SELECTS a configured
alias — it can never create authority; an unconfigured spelling is
:class:`UnknownScope`.

Trusted-callsite requirement: the source label must arrive from the routing
decision's configured account metadata (the operator's credential-source
labels), and the endpoint must be the proxy's configured upstream target —
never request data. Strict/observe/off handling of an unknown scope belongs to
the future caller (spec 245: fail closed only under strict mode).

Probe allocations belong to the future dispatch integration. This pure
resolver only binds the configured scope; probe routing and budget carve-outs
remain caller obligations described in the T003 plan.

Pure module: stdlib + :mod:`.ledger` types only. No I/O, no network, no env, no
runtime flags, no model rewriting, no wiring.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .ledger import Budgets, _pos_int

_STR_FIELDS = ("source", "endpoint", "alias", "upstream", "account", "model")
_BUDGET_FIELDS = ("max_requests", "max_tokens")


@dataclass(frozen=True)
class Scope:
    """One canonical budget scope: where, whose, and which model."""

    upstream: str
    account: str
    model: str

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.upstream, self.account, self.model)


@dataclass(frozen=True)
class ScopeMatch:
    """The resolved authority for one dispatch: scope, budgets, output default."""

    scope: Scope
    budgets: Budgets
    output_default: int | None = None


@dataclass(frozen=True)
class UnknownScope:
    """Distinct non-answer for an unmatched dispatch — never a guessed scope."""

    reason: str


def _text(value: object) -> str | None:
    """A non-empty exact string, or None (bools and numbers are not labels)."""
    return value if type(value) is str and value.strip() else None


def _budgets_from(value: object) -> Budgets | None:
    """Reuse :class:`ledger.Budgets` validation; None means an invalid shape."""
    if isinstance(value, Budgets):
        return value
    if isinstance(value, Mapping) and set(value) == set(_BUDGET_FIELDS):
        return Budgets(value["max_requests"], value["max_tokens"])
    return None


def _validated_entry(
    row: object,
    sources: frozenset[str],
    endpoints: frozenset[str],
    models: frozenset[str],
) -> tuple[tuple[str, str, str], ScopeMatch]:
    """Validate one already-parsed mapping row; raise ValueError on anything off."""
    if not isinstance(row, Mapping):
        raise ValueError("scope entry must be a mapping")
    texts = {name: _text(row.get(name)) for name in _STR_FIELDS}
    bad = [name for name, value in texts.items() if value is None]
    if bad:
        raise ValueError(f"scope entry fields must be non-empty strings: {bad}")
    if texts["source"] not in sources:
        raise ValueError(f"unknown credential source: {texts['source']!r}")
    if texts["endpoint"] not in endpoints:
        raise ValueError(f"unknown upstream endpoint: {texts['endpoint']!r}")
    if texts["alias"] not in models or texts["model"] not in models:
        raise ValueError(f"unknown model: {texts['alias']!r} / {texts['model']!r}")
    try:
        budgets = _budgets_from(row.get("budgets"))
    except ValueError as exc:  # ledger.Budgets rejects bools/floats/non-positive
        raise ValueError(f"invalid budgets: {exc}") from exc
    if budgets is None:
        raise ValueError("budgets must carry exactly max_requests and max_tokens")
    output_default = row.get("output_default")
    if output_default is not None and not _pos_int(output_default):
        raise ValueError("output_default must be a positive integer")
    scope = Scope(texts["upstream"], texts["account"], texts["model"])
    return (texts["source"], texts["endpoint"], texts["alias"]), ScopeMatch(
        scope, budgets, output_default
    )


class ScopeResolver:
    """Pure validated operator-config resolver for canonical dispatch scopes.

    ``entries`` is already-parsed mapping input — a sequence of row mappings,
    or a mapping of named rows. Every row binds one triple (credential-source
    label, exact upstream endpoint, model alias) to one canonical scope with
    explicit budgets. Construction rejects ambiguous duplicates, inconsistent
    budgets/output defaults for a shared canonical scope, invalid types and
    unknown sources/endpoints/models; :meth:`resolve` answers a distinct
    :class:`UnknownScope` for anything it cannot bind.
    """

    def __init__(
        self,
        entries: Iterable[Mapping[str, object]] | Mapping[str, Mapping[str, object]],
        *,
        sources: Iterable[str],
        endpoints: Iterable[str],
        models: Iterable[str],
    ) -> None:
        self._sources = frozenset(sources)
        self._endpoints = frozenset(endpoints)
        self._models = frozenset(models)
        rows = entries.values() if isinstance(entries, Mapping) else entries
        by_triple: dict[tuple[str, str, str], ScopeMatch] = {}
        scope_config: dict[tuple[str, str, str], tuple[Budgets, int | None]] = {}
        canonical_scopes: dict[tuple[str, str, str], Scope] = {}
        for row in rows:
            triple, match = _validated_entry(row, self._sources, self._endpoints, self._models)
            if triple in by_triple:
                raise ValueError(f"ambiguous duplicate mapping for {triple!r}")
            prior = scope_config.get(match.scope.key)
            if prior is not None and prior != (match.budgets, match.output_default):
                raise ValueError(
                    f"inconsistent budgets for shared canonical scope {match.scope.key!r}"
                )
            canonical = (triple[0], triple[1], match.scope.model)
            if canonical_scopes.setdefault(canonical, match.scope) != match.scope:
                raise ValueError("canonical model maps to conflicting account/upstream scope")
            scope_config[match.scope.key] = (match.budgets, match.output_default)
            by_triple[triple] = match
        for triple, match in by_triple.items():
            if triple in canonical_scopes and canonical_scopes[triple] != match.scope:
                raise ValueError("canonical model cannot be remapped through an alias")
        self._by_triple = by_triple

    def resolve(self, source: object, endpoint: object, model: object) -> ScopeMatch | UnknownScope:
        """Bind one dispatch to its canonical scope, or answer UnknownScope.

        Exact configured matching only — no normalization, no prefixing, no
        fallback: an endpoint with a trailing slash is a different endpoint.
        """
        if any(_text(value) is None for value in (source, endpoint, model)):
            return UnknownScope("invalid-input")
        if source not in self._sources:
            return UnknownScope("unknown-source")
        if endpoint not in self._endpoints:
            return UnknownScope("unknown-endpoint")
        if model not in self._models:
            return UnknownScope("unknown-model")
        match = self._by_triple.get((source, endpoint, model))
        return match if match is not None else UnknownScope("unmapped")
