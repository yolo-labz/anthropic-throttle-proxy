// 308/310 — reusable transport FETCH HOLD (source rehearsal).
//
// Wraps the transport's `fetch` seam — the one `anthropic-messages.js` passes
// to `createClient(model, apiKey, headers, options.fetch, …)` — and adds ONE
// capability: a hold gate that closes BEFORE an internal retry
// (`retryProviderRequest` re-enters `client.beta.messages.create`, which
// re-enters this fetch), so no new wire send happens until `resume()`.
//
// Deliberate scope: retry, SSE, provider/model/billing ids and the native
// provider registry are NOT reimplemented or touched — the built-in transport
// keeps owning them; this wrapper only gates new sends at the fetch boundary.
//
// Semantics (310 fixes included):
//   * gate open (default): every call passes straight through to the inner
//     fetch — including streaming/SSE responses, returned unchanged.
//   * hold(): new calls do not reach the wire. A SLOW body (stream/async
//     iterable) is CLOSED before any upload instead of parking; a buffered
//     body parks until resume.
//   * RE-LOOP after every park (defect 1, held/re-held race): a call woken by
//     `resume()` RE-CHECKS the gate before touching the wire, so
//     `resume(); hold()` in the same turn keeps it parked — zero sends.
//   * cancellation releases: a parked call rejects promptly on its
//     AbortSignal and removes its listener in BOTH outcomes (defect 3 — a
//     resumed waiter used to leak the abort listener).
//   * resume(): the only opener. Idempotent; opening with nothing parked is a
//     no-op; calls made after resume pass through normally.

export class FetchHoldAbort extends Error {
  constructor(message) {
    super(message);
    this.name = "FetchHoldAbort";
  }
}

function isSlowBody(body) {
  if (body == null || typeof body === "string" || body instanceof Uint8Array) {
    return false;
  }
  if (typeof ArrayBuffer !== "undefined" && body instanceof ArrayBuffer) return false;
  if (typeof FormData !== "undefined" && body instanceof FormData) return false;
  if (typeof URLSearchParams !== "undefined" && body instanceof URLSearchParams) return false;
  // Streams / async iterables / node streams: slow by definition — never
  // upload one into a closed gate.
  return (
    typeof body[Symbol.asyncIterator] === "function" ||
    typeof body.getReader === "function" ||
    typeof body.pipe === "function"
  );
}

async function closeSlowBody(body) {
  try {
    if (typeof body.return === "function") await body.return();
    else if (typeof body.cancel === "function") await body.cancel();
    else if (typeof body.destroy === "function") body.destroy();
    else if (typeof body.getReader === "function") await body.getReader().cancel();
  } catch {
    // Closing is best-effort; the rejection below is the caller-facing fact.
  }
}

export function createHoldFetch(innerFetch = globalThis.fetch) {
  const waiters = new Set();
  let held = false;

  function hold() {
    held = true;
  }

  function resume() {
    held = false;
    for (const waiter of [...waiters]) {
      waiter.resolve();
    }
  }

  // One park cycle. The abort listener is removed on EVERY outcome — release
  // or cancellation — so no waiter can leak a listener (defect 3).
  function park(signal) {
    return new Promise((resolve, reject) => {
      const waiter = { resolve: null, reject: null };
      const settle = (action, arg) => {
        if (!waiters.delete(waiter)) return; // already settled elsewhere
        if (signal) signal.removeEventListener("abort", onAbort);
        action(arg);
      };
      const onAbort = () =>
        settle(reject, signal?.reason ?? new FetchHoldAbort("fetch-hold: cancelled under hold"));
      waiter.resolve = () => settle(resolve);
      waiter.reject = (error) => settle(reject, error);
      waiters.add(waiter);
      if (signal?.aborted) onAbort();
      else if (signal) signal.addEventListener("abort", onAbort, { once: true });
    });
  }

  async function gatedFetch(input, init = {}) {
    if (isSlowBody(init.body)) {
      if (!held) return innerFetch(input, init); // admitted: transport untouched
      await closeSlowBody(init.body);
      throw new FetchHoldAbort("fetch-hold: slow body closed before upload");
    }
    // Re-loop (defect 1): resume() resolves the park, but a re-hold may land
    // before this continuation runs. Re-check the gate every wake so the wire
    // is only touched while it is open.
    while (held) {
      await park(init.signal);
    }
    return innerFetch(input, init);
  }

  return {
    fetch: gatedFetch,
    hold,
    resume,
    isHeld: () => held,
    parked: () => waiters.size,
  };
}
