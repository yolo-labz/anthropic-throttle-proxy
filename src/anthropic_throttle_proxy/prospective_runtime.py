"""Dispatch bridge for spec245; explicit app config, default off.

Generation consumers enter AFTER final shaping/auth selection and pacing, call
handoff synchronously immediately before transport, and catch local refusals
outside the context. Transport exceptions are never reclassified here.

Observe is a bounded shadow sample, NOT complete spend or restart evidence for
strict mode. Its files require separate custody. The inherited estimator remains
uncalibrated; this module does not authorize activation or guarantee vendor TPM.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping, Sequence
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass
from functools import partial
from types import MappingProxyType

from aiohttp import web

from .ledger import _pos_int, _valid_time
from .metrics import M_PROSPECTIVE_OBSERVATIONS, M_PROSPECTIVE_REFUSALS
from .prospective import account_request
from .prospective_admission import BudgetRefused, OwnerUnavailable, PersistenceOwner, Scope
from .prospective_calibration import CalibrationAttempt
from .prospective_refusal import ProspectiveRefusal, RefusalReason
from .prospective_scope import ScopeResolver, UnknownScope


@dataclass(frozen=True)
class SelectedDispatch:
    """Internal routing facts, never inferred from headers/hashes by this API."""

    credential_source: str | None
    endpoint: str | None
    topology: str
    internal_probe: bool


class LocalProspectiveRefusal(Exception):
    """Local policy only; consumers adapt .refusal to HTTP or terminal SSE."""

    def __init__(self, refusal: ProspectiveRefusal) -> None:
        if not isinstance(refusal, ProspectiveRefusal):
            raise TypeError("ProspectiveRefusal required")
        self.refusal = refusal
        super().__init__(refusal.message)


class DispatchPermit:
    """A context-bound synchronous marker, not evidence of wire delivery."""

    def __init__(self, action: Callable[[], None], calibration=None) -> None:
        self.calibration = calibration
        self._action = action
        self._active = True
        self._used = False

    def handoff(self) -> None:
        if not self._active or self._used:
            raise ValueError("dispatch permit already used or context exited")
        self._action()
        self._used = True
        if self.calibration is not None:
            self.calibration.sent = True


class _OffPermit:
    __slots__ = ()

    def handoff(self) -> None:
        # Off grants no ledger authority and has no state to mark as sent.
        pass


_OFF_CONTEXT = nullcontext(_OffPermit())
_EMPTY_COUNTS = MappingProxyType({})


class _OffRuntime:
    """Immutable shared fallback: no owner, counters, tasks, parsing or I/O."""

    __slots__ = ()
    mode = "off"

    async def start(self) -> None:
        # The immutable off fallback owns no worker or file to initialize.
        pass

    async def aclose(self) -> None:
        # Off never acquired resources, so app shutdown has nothing to drain.
        pass

    def reserve(self, *_accounting_args, **_accounting_kwargs):
        # Preserve positional/keyword calls without inspecting accounting inputs.
        return _OFF_CONTEXT

    def observations(self) -> Mapping[str, int]:
        return _EMPTY_COUNTS


class ProspectiveRuntime(_OffRuntime):
    """One explicitly configured app authority; mode fixed for its lifetime.

    Only start constructs a PersistenceOwner. Off skips ALL accounting inputs.
    Enabled config is validated in memory, with scope/budget agreement checked
    at resolution (ScopeResolver exposes no catalogue enumeration API).
    """

    def __init__(
        self,
        *,
        mode: str = "off",
        resolver: ScopeResolver | None = None,
        scopes: Sequence[Scope] = (),
        max_pending: int | None = None,
        wait_timeout: float | None = None,
        budget_label: str | None = None,
        retry_after_s: int | None = None,
    ) -> None:
        if mode not in ("off", "observe", "strict"):
            raise ValueError("mode must be off, observe or strict")
        self._mode = mode
        if mode == "off":
            return
        self._validate(resolver, scopes, max_pending, wait_timeout)
        self._policy = ProspectiveRefusal(RefusalReason.UNKNOWN, retry_after_s, budget_label)
        self._resolver = resolver
        self._max_pending = max_pending
        self._wait_timeout = wait_timeout
        self._owner: PersistenceOwner | None = None
        self._started = False
        self._closing = False
        self._observing: set[asyncio.Task] = set()
        self._observation_drain: asyncio.Future | None = None
        self._counts = dict.fromkeys(("admitted", "exhausted", "unbound", "unknown"), 0)

    @property
    def mode(self) -> str:
        return self._mode

    def _validate(self, resolver, scopes, max_pending, wait_timeout) -> None:
        if not isinstance(resolver, ScopeResolver):
            raise ValueError("an explicit ScopeResolver is required")
        scopes = tuple(scopes)
        if not scopes or any(not isinstance(scope, Scope) for scope in scopes):
            raise ValueError("explicit persistence scopes required")
        self._scopes = {scope.key: scope for scope in scopes}
        if len(self._scopes) != len(scopes):
            raise ValueError("duplicate persistence scope")
        if not _pos_int(max_pending) or not _valid_time(wait_timeout) or wait_timeout == 0:
            raise ValueError("positive pending and finite wait bounds required")

    def _refuse(self, reason: RefusalReason) -> LocalProspectiveRefusal:
        if self.mode == "strict":
            M_PROSPECTIVE_REFUSALS.labels(reason=reason.value).inc()
        return LocalProspectiveRefusal(
            ProspectiveRefusal(reason, self._policy.retry_after_s, self._policy.budget_label)
        )

    async def start(self) -> None:
        if self.mode == "off":
            return
        if self._closing:
            raise OwnerUnavailable("runtime is closing")
        if self._owner is None:
            self._owner = PersistenceOwner(
                tuple(self._scopes.values()),
                max_pending=self._max_pending,
                wait_timeout=self._wait_timeout,
            )
        try:
            await self._owner.start()
        except Exception:
            if self.mode == "strict":
                raise
            self._count("unknown")
            return
        self._started = True

    def _prepare(self, selected, body):
        if not isinstance(selected, SelectedDispatch):
            raise self._refuse(RefusalReason.UNBOUND)
        if selected.topology != "direct" or selected.internal_probe is not False:
            raise self._refuse(RefusalReason.UNKNOWN)
        if not isinstance(body, bytes):
            raise self._refuse(RefusalReason.UNKNOWN)
        try:
            payload = json.loads(body)
        except (ValueError, RecursionError):
            raise self._refuse(RefusalReason.UNKNOWN) from None
        model = payload.get("model") if isinstance(payload, dict) else None
        match = self._resolver.resolve(selected.credential_source, selected.endpoint, model)
        if isinstance(match, UnknownScope):
            raise self._refuse(RefusalReason.UNBOUND)
        custody = self._scopes.get(match.scope.key)
        if custody is None or custody.budgets != match.budgets:
            raise self._refuse(RefusalReason.UNBOUND)
        defaults = {} if match.output_default is None else {model: match.output_default}
        try:
            cost = account_request(body, defaults)
        except (ValueError, RecursionError):
            cost = None
        if cost is None:
            raise self._refuse(RefusalReason.UNKNOWN)
        return match.scope.key, cost

    def reserve(self, selected: SelectedDispatch | None, final_body: bytes | None):
        """Generation-only boundary; caller has ALREADY shaped and paced."""
        if self.mode == "off":
            return _OFF_CONTEXT
        return self._reserve(selected, final_body)

    @asynccontextmanager
    async def _reserve(self, selected, body):
        calibration = None
        try:
            prepared = self._prepare(selected, body)
        except LocalProspectiveRefusal as exc:
            if self.mode == "strict":
                raise
            action = partial(self._count, exc.refusal.reason.value)
        else:
            if self.mode == "strict":
                async with self._strict(*prepared) as permit:
                    yield permit
                return
            action = partial(self._enqueue_observation, *prepared)
            calibration = CalibrationAttempt(
                self._policy.budget_label, prepared[1], single_scope=len(self._scopes) == 1
            )
        permit = DispatchPermit(action, calibration)
        try:
            yield permit
        finally:
            permit._active = False

    async def _enter_owner(self, key, cost):
        if not self._started or self._closing:
            raise self._refuse(RefusalReason.UNKNOWN)
        context = self._owner.reserve(key, cost.input_tokens, cost.output_bound)
        try:
            ticket = await context.__aenter__()
        except BudgetRefused:
            raise self._refuse(RefusalReason.EXHAUSTED) from None
        except Exception:
            raise self._refuse(RefusalReason.UNKNOWN) from None
        return context, ticket

    def _handoff(self, ticket) -> None:
        if self._closing:
            raise self._refuse(RefusalReason.UNKNOWN)
        try:
            ticket.handoff()
        except Exception:
            raise self._refuse(RefusalReason.UNKNOWN) from None

    @asynccontextmanager
    async def _strict(self, key, cost):
        context, ticket = await self._enter_owner(key, cost)
        permit = DispatchPermit(partial(self._handoff, ticket))
        try:
            yield permit
        except BaseException as exc:
            permit._active = False
            # Consumer exceptions go through the owner's unsent/sent cleanup,
            # NOT through our local-refusal translation around owner entry.
            await context.__aexit__(type(exc), exc, exc.__traceback__)
            raise
        else:
            permit._active = False
            try:
                await context.__aexit__(None, None, None)
            except Exception:
                raise self._refuse(RefusalReason.UNKNOWN) from None

    def _count(self, outcome: str) -> None:
        self._counts[outcome] += 1
        M_PROSPECTIVE_OBSERVATIONS.labels(outcome=outcome).inc()

    def _enqueue_observation(self, key, cost) -> None:
        if not self._started or self._closing or len(self._observing) >= self._max_pending:
            self._count("unknown")
            return
        task = asyncio.create_task(self._observe_one(key, cost))
        self._observing.add(task)
        task.add_done_callback(self._observed)

    def _observed(self, task: asyncio.Task) -> None:
        self._observing.discard(task)
        if task.cancelled() or task.exception() is not None:
            self._count("unknown")

    async def _observe_one(self, key, cost) -> None:
        try:
            async with self._owner.reserve(key, cost.input_tokens, cost.output_bound) as ticket:
                ticket.handoff()  # shadow only: never physical transport authority
        except BudgetRefused:
            self._count("exhausted")
        except Exception:
            self._count("unknown")
        else:
            self._count("admitted")

    def observations(self) -> Mapping[str, int]:
        if self.mode == "off":
            return _EMPTY_COUNTS
        return dict(self._counts)

    async def aclose(self) -> None:
        if self.mode == "off":
            return
        self._closing = True
        if self._observation_drain is None:
            self._observation_drain = asyncio.gather(*self._observing, return_exceptions=True)
        await asyncio.wait_for(asyncio.shield(self._observation_drain), self._wait_timeout)
        if self._owner is not None:
            await self._owner.aclose()


RUNTIME_KEY = web.AppKey("prospective_runtime", ProspectiveRuntime)
_SELECTED_KEY = "anthropic_throttle_proxy.selected_dispatch"
_OFF_RUNTIME = _OffRuntime()


def get_runtime(request) -> ProspectiveRuntime | _OffRuntime:
    """App-owned authority or immutable shared OFF; no headers or lazy owner."""
    runtime = request.app.get(RUNTIME_KEY, _OFF_RUNTIME)
    if runtime is _OFF_RUNTIME or isinstance(runtime, ProspectiveRuntime):
        return runtime
    raise TypeError("invalid app prospective runtime")


def get_selected_dispatch(request) -> SelectedDispatch | None:
    selected = request.get(_SELECTED_KEY)
    return selected if isinstance(selected, SelectedDispatch) else None


def set_selected_dispatch(request, selected: SelectedDispatch | None) -> None:
    if selected is None:
        request.pop(_SELECTED_KEY, None)
    elif isinstance(selected, SelectedDispatch):
        request[_SELECTED_KEY] = selected
    else:
        raise TypeError("selected metadata must be SelectedDispatch or None")
