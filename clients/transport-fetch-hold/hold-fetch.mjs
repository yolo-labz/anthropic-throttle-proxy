// 308 — reusable transport FETCH HOLD (source rehearsal).
//
// Wraps the transport's `fetch` seam — the one `anthropic-messages.js` passes
// to `createClient(model, apiKey, headers, options.fetch, …)` — and adds ONE
// capability: a hold gate that closes BEFORE an internal retry (`retryProviderRequest`
// re-enters `client.beta.messages.create`, which re-enters this fetch), so no
// new wire send happens until `resume()`.
//
// Deliberate scope: retry, SSE, provider/model/billing ids and the native
// provider registry are NOT reimplemented or touched — the built-in transport
// keeps owning them; this wrapper only gates new sends at the fetch boundary.
//
// Semantics:
//   * gate open (default): every call passes straight through to the inner
//     fetch — including streaming/SSE responses, which are returned unchanged
//     (no buffering, no transformation).
//   * hold(): new calls do not reach the wire. A SLOW body (stream/async
//     iterable) is CLOSED before any upload instead of parking (a streamed
//     body must never start uploading into a closed gate); a buffered body
//     parks until resume.
//   * cancellation releases: a parked call rejects promptly on its
//     AbortSignal, and cleans up its waiter.
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
      waiters.delete(waiter);
      waiter.resolve();
    }
  }

  async function gatedFetch(input, init = {}) {
    if (!held) {
      return innerFetch(input, init); // admitted: transport untouched
    }
    // Closed gate: a slow body is closed BEFORE any upload; a buffered body
    // parks (zero wire sends) until resume or cancellation.
    if (isSlowBody(init.body)) {
      await closeSlowBody(init.body);
      throw new FetchHoldAbort("fetch-hold: slow body closed before upload");
    }
    await new Promise((resolve, reject) => {
      const waiter = { resolve, reject };
      waiters.add(waiter);
      const signal = init.signal;
      const onAbort = () => {
        if (waiters.delete(waiter)) {
          reject(signal?.reason ?? new FetchHoldAbort("fetch-hold: cancelled under hold"));
        }
      };
      if (signal) {
        if (signal.aborted) return onAbort();
        signal.addEventListener("abort", onAbort, { once: true });
      }
    });
    return innerFetch(input, init); // released by resume()
  }

  return {
    fetch: gatedFetch,
    hold,
    resume,
    isHeld: () => held,
    parked: () => waiters.size,
  };
}
