// queue-wait.mjs — retry engine for the anthropic-throttle-proxy queue-timeout
// rejection on the local ZAI or MiMo lane (specs 227/243).
//
// Public contract (see specs/227-pi-queue-wait/contract.md):
//   createQueueWaitStream(deps) -> (model, context, options) => AssistantMessageEventStream
//
// A queue rejection is a temporary, locally stamped 503 from the loopback
// proxy lane. It must keep the current model request pending and retry it
// after the advised delay. EVERYTHING else passes through the native Pi
// stream path untouched, byte-for-byte: auth/quota errors, raw 503s without
// the proxy stamps, foreign origins, redirects, partial streams with any
// prior event, and non-empty/non-zero native error payloads.
//
// No vendor SDK, no runtime dependency, no module-level mutable state.

/** Exact public proxy body sentence (no trailing newline). */
export const QUEUE_BODY_TEXT =
  "proxy queue wait exceeded; slots saturated — retrying will re-enter the fair queue";

/** The HTTP body is exactly the sentence plus one trailing newline. */
export const QUEUE_BODY_FULL = QUEUE_BODY_TEXT + "\n";

/** Stamp present on every response this proxy tier serves (provenance only). */
export const QUEUE_MARKER_HEADER = "x-anthropic-throttle-proxy";

/** Stamp present ONLY on the proxy-generated queue-timeout 503. */
export const QUEUE_TIMEOUT_HEADER = "x-anthropic-throttle-queue-timeout";

/** Existing ZAI provider/model/lane constants (kept for compatibility). */
export const QUEUE_PROVIDER_ID = "zai";
export const QUEUE_MODEL_ID = "glm-5.3-flash";

/** Exact completion path and default loopback port of the z.ai lane. */
export const QUEUE_COMPLETIONS_PATH = "/api/coding/paas/v4/chat/completions";
export const QUEUE_DEFAULT_PORT = 8766;

export const QUEUE_WAIT_DEFAULTS = Object.freeze({
  /** Total admission-wait budget in ms (waits only — never a running stream). */
  maxWaitMs: 1_800_000,
  /**
   * Maximum number of queue rejections that are each followed by exactly one
   * re-dispatch of the same request. The first maxRejections rejections retry;
   * the (maxRejections + 1)-th rejection ends the request with the synthetic
   * give-up error. It never surfaces the original stamped 503.
   */
  maxRejections: 32,
  /** Used when Retry-After is missing, unparsable, or <= 0 (conservative, non-hot). */
  fallbackRetryAfterMs: 15_000,
  /** Strictly positive jitter upper bound in ms. */
  jitterMaxMs: 1_000,
});

/**
 * Parse a Retry-After header value. Seconds ("3", "2.5") or HTTP-date.
 * Returns the delay in ms (> 0) or null when absent/unparsable/not positive.
 */
export function parseRetryAfterMs(value, nowMs) {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  if (trimmed === "") return null;
  if (/^\d+(\.\d+)?$/.test(trimmed)) {
    const ms = Math.round(parseFloat(trimmed) * 1000);
    return ms > 0 ? ms : null;
  }
  const date = Date.parse(trimmed);
  if (Number.isNaN(date)) return null;
  const ms = Math.round(date - nowMs);
  return ms > 0 ? ms : null;
}

function isLoopbackHostname(hostname) {
  const host = hostname.toLowerCase();
  if (host === "localhost" || host === "::1" || host === "[::1]") return true;
  return /^127\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(host);
}

/** Normalize a DI base-url entry to `protocol//host`; null when not strict http loopback. */
function normalizeAllowedBase(entry) {
  if (typeof entry !== "string" || entry.trim() === "") return null;
  let url;
  try {
    url = new URL(entry);
  } catch {
    return null;
  }
  if (url.protocol !== "http:") return null;
  if (!isLoopbackHostname(url.hostname)) return null;
  return `${url.protocol}//${url.host}`;
}

/**
 * The request-URL gate: http scheme, loopback host, the exact completions
 * path and port for the selected provider (or an allowed DI test base). Foreign
 * origins, https, other paths, and central proxies never qualify.
 */
function urlGate(rawUrl, allowedBases, port, completionsPath) {
  let url;
  try {
    url = rawUrl instanceof URL ? rawUrl : new URL(String(rawUrl));
  } catch {
    return false;
  }
  if (url.protocol !== "http:") return false;
  if (!isLoopbackHostname(url.hostname)) return false;
  // STRICT FULL URL: credentials, query, and fragment never qualify.
  if (url.username !== "" || url.password !== "") return false;
  if (url.search !== "") return false;
  if (url.hash !== "") return false;
  if (url.pathname !== completionsPath) return false;
  if (url.port === String(port) && (url.host === `127.0.0.1:${port}` || url.host === `localhost:${port}` || url.host === `[::1]:${port}`)) {
    return true;
  }
  const origin = `${url.protocol}//${url.host}`;
  return allowedBases.has(origin);
}

function effectiveMethod(input, init) {
  if (init && typeof init.method === "string" && init.method !== "") {
    return init.method.toUpperCase();
  }
  if (typeof Request !== "undefined" && input instanceof Request) {
    return input.method.toUpperCase();
  }
  return "GET";
}

function effectiveUrl(input) {
  if (typeof Request !== "undefined" && input instanceof Request) return input.url;
  if (input instanceof URL) return input.href;
  return String(input);
}

function sameLocation(a, b) {
  try {
    // FULL normalized href: a final URL differing only in query/fragment/
    // credentials is a different location and must never authorize a retry.
    return new URL(a).href === new URL(b).href;
  } catch {
    return false;
  }
}

/**
 * Every mandatory usage and cost scalar must be the NUMBER 0 — null,
 * undefined, strings, empty objects, and missing fields all fail closed, so
 * a partially-billed or shape-broken error can never be swallowed.
 */
function allUsageZero(usage) {
  if (usage === null || typeof usage !== "object") return false;
  const isZero = (value) => typeof value === "number" && value === 0;
  for (const key of ["input", "output", "cacheRead", "cacheWrite", "totalTokens"]) {
    if (!isZero(usage[key])) return false;
  }
  const cost = usage.cost;
  if (cost === null || typeof cost !== "object") return false;
  for (const key of ["input", "output", "cacheRead", "cacheWrite", "total"]) {
    if (!isZero(cost[key])) return false;
  }
  return true;
}

/**
 * Build the retrying stream function.
 *
 * Required deps:
 *   - streamSimple: the native OpenAI-completions StreamFunction.
 *   - createEventStream: () => AssistantMessageEventStream (Pi's factory).
 * Optional deps:
 *   - now(): ms clock (default Date.now)
 *   - sleep(ms, signal): abortable promise (default setTimeout + abort reject)
 *   - random(): 0..1 (default Math.random)
 *   - onWait(info | null): counters-only wait signal; null clears the UI
 *   - maxWaitMs, maxRejections: ceilings (see QUEUE_WAIT_DEFAULTS)
 *   - allowedBaseUrls: DI-ONLY test seam. Exact http loopback base URLs whose
 *     origin additionally permits interception. Never wired in production:
 *     index.ts must not pass it. Omission keeps each provider's strict port/path.
 */
export function createQueueWaitStream(deps) {
  const streamSimple = deps?.streamSimple;
  const createEventStream = deps?.createEventStream;
  if (typeof streamSimple !== "function") {
    throw new TypeError("createQueueWaitStream: deps.streamSimple is required");
  }
  if (typeof createEventStream !== "function") {
    throw new TypeError("createQueueWaitStream: deps.createEventStream is required");
  }

  const now = typeof deps?.now === "function" ? deps.now : Date.now;
  const sleep = typeof deps?.sleep === "function" ? deps.sleep : defaultSleep;
  const random = typeof deps?.random === "function" ? deps.random : Math.random;
  const onWait = typeof deps?.onWait === "function" ? deps.onWait : () => {};
  const maxWaitMs = positiveInt(deps?.maxWaitMs, QUEUE_WAIT_DEFAULTS.maxWaitMs);
  const maxRejections = positiveInt(deps?.maxRejections, QUEUE_WAIT_DEFAULTS.maxRejections);
  const fallbackRetryAfterMs = positiveInt(deps?.fallbackRetryAfterMs, QUEUE_WAIT_DEFAULTS.fallbackRetryAfterMs);
  const jitterMaxMs = positiveInt(deps?.jitterMaxMs, QUEUE_WAIT_DEFAULTS.jitterMaxMs);
  const allowedBases = new Set(
    Array.isArray(deps?.allowedBaseUrls)
      ? deps.allowedBaseUrls.map(normalizeAllowedBase).filter((base) => base !== null)
      : [],
  );

  return function queueWaitStream(model, context, options) {
    // Provider/model gate: anything else is the native function, un-wrapped.
    const mimo = model?.provider === "mimo-desktop" &&
      typeof model?.id === "string" && model.id.startsWith("mimo-") &&
      model?.api === "openai-completions";
    if (!mimo && (model?.provider !== QUEUE_PROVIDER_ID || ![QUEUE_MODEL_ID, "glm-5.3"].includes(model?.id))) {
      return streamSimple(model, context, options);
    }
    // Bind the tuple to the provider: MiMo must never inherit ZAI's lane.
    const requestUrlGate = (url) => urlGate(url, allowedBases,
      mimo ? 8773 : QUEUE_DEFAULT_PORT,
      mimo ? "/v1/chat/completions" : QUEUE_COMPLETIONS_PATH);

    const outer = createEventStream();
    const signal = options?.signal;

    const run = async () => {
      let rejections = 0;
      let waitedMs = 0;

      const finish = () => {
        try {
          onWait(null);
        } catch {
          /* wait-cleanup failure must not suppress the terminal event */
        }
        outer.end();
      };
      const finishWithEvent = (event) => {
        try {
          onWait(null);
        } catch {
          /* wait-cleanup failure must not suppress the terminal event */
        }
        outer.push(event);
        outer.end();
      };
      const emitSynthetic = (stopReason, message) => {
        finishWithEvent(syntheticErrorEvent(model, stopReason, message, Date.now()));
      };

      while (true) {
        if (signal?.aborted) {
          emitSynthetic("aborted", "Request was aborted");
          return;
        }

        // Per-attempt capture state. Reset before every delegated fetch so a
        // stale stamped 503 from an earlier internal attempt can never
        // justify a later retry.
        const attempt = { rejection: null, capture: null, sawPriorEvent: false };
        const innerFetch = wrapFetch(options, attempt, requestUrlGate, now);
        const inner = streamSimple(model, context, { ...(options ?? {}), fetch: innerFetch });

        let terminal = null; // { event } when the inner stream ended with an error event
        for await (const event of inner) {
          if (event?.type !== "error") {
            attempt.sawPriorEvent = true;
            outer.push(event);
            continue;
          }
          // Settle the in-flight body capture BEFORE classifying, racing the
          // parent abort so a stalled clone body can never delay termination.
          if (attempt.capture) {
            const capture = await awaitCapture(attempt.capture, signal);
            if (capture.aborted) {
              emitSynthetic("aborted", "Request was aborted");
              return;
            }
          }
          terminal = { event };
          break;
        }

        if (!terminal) {
          // Native stream ended with `done`: forward-ended, nothing to retry.
          finish();
          return;
        }

        const event = terminal.event;

        // Never retry after ANY emitted event of any type, on abort, or
        // without a fully-gated captured rejection on THIS attempt.
        if (
          attempt.sawPriorEvent ||
          event?.reason === "aborted" ||
          signal?.aborted ||
          !attempt.rejection ||
          !isZeroErrorEvent(event)
        ) {
          finishWithEvent(event);
          return;
        }

        // Eligible queue rejection. Book-keeping first: maxRejections is the
        // number of rejections that may be retried; seeing more ends the
        // request with the synthetic give-up error (never the stamped 503).
        rejections += 1;
        if (rejections > maxRejections) {
          emitSynthetic(
            "error",
            `queue-wait: gave up after ${rejections} queue rejections (limit ${maxRejections}); the model was never streamed`,
          );
          return;
        }

        const retryAfterMs = attempt.rejection.retryAfterMs;
        const usedFallback = retryAfterMs === null;
        const baseMs = usedFallback ? fallbackRetryAfterMs : retryAfterMs;
        const jitterMs = Math.max(1, Math.ceil(random() * jitterMaxMs));
        const delayMs = baseMs + jitterMs;

        // Budget bounds admission waits only. Checked BEFORE sleeping so a
        // wait never starts that would push past the ceiling.
        if (waitedMs + delayMs > maxWaitMs) {
          emitSynthetic(
            "error",
            `queue-wait: admission wait budget exhausted after ${rejections} queue rejections and ${waitedMs} ms of waiting; the model was never streamed`,
          );
          return;
        }

        onWait({
          attempt: rejections,
          delayMs,
          retryAfterMs: usedFallback ? null : retryAfterMs,
          fallback: usedFallback,
          waitedMs,
        });
        try {
          await sleep(delayMs, signal);
        } catch (sleepError) {
          // Abort only when the parent signal or the rejection itself
          // establishes it; any other failure stays an error with its
          // ORIGINAL message — never relabelled aborted.
          if (signal?.aborted || isAbortError(sleepError)) {
            emitSynthetic("aborted", "Request was aborted");
          } else {
            finishWithEvent(
              syntheticErrorEvent(model, "error", String(sleepError?.message ?? sleepError), Date.now()),
            );
          }
          return;
        }
        // The parent signal may have fired in a race with sleep resolution.
        if (signal?.aborted) {
          emitSynthetic("aborted", "Request was aborted");
          return;
        }
        waitedMs += delayMs;
        onWait(null);
        // loop: re-dispatch the SAME model request (same model/context/options).
      }
    };

    run().catch((runError) => {
      // The engine must never throw past the stream edge: emit exactly ONE
      // native-shaped terminal error with the original message (aborted only
      // when the parent signal/AbortError establishes abort), clear the wait
      // state, then end. Each cleanup step is guarded so its failure can
      // never suppress the terminal event.
      const aborted = signal?.aborted === true || isAbortError(runError);
      try {
        onWait(null);
      } catch {
        /* wait-cleanup failure must not suppress the terminal event */
      }
      try {
        outer.push(
          syntheticErrorEvent(
            model,
            aborted ? "aborted" : "error",
            aborted ? "Request was aborted" : String(runError?.message ?? runError),
            Date.now(),
          ),
        );
      } catch {
        /* stream already torn down */
      }
      try {
        outer.end();
      } catch {
        /* already ended */
      }
    });

    return outer;
  };
}

function positiveInt(value, fallback) {
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? value : fallback;
}

/** Abort identity, never message text: an ordinary Error("aborted") stays an error. */
function isAbortError(error) {
  return error?.name === "AbortError";
}

function defaultSleep(ms, signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException("Request was aborted", "AbortError"));
      return;
    }
    const timer = setTimeout(() => {
      cleanup();
      resolve();
    }, ms);
    const onAbort = () => {
      cleanup();
      reject(new DOMException("Request was aborted", "AbortError"));
    };
    const cleanup = () => {
      clearTimeout(timer);
      signal?.removeEventListener("abort", onAbort);
    };
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

/** The native error event must be empty-content with EVERY usage/cost field zero. */
function isZeroErrorEvent(event) {
  const error = event?.error;
  if (event?.reason !== "error" || error?.stopReason !== "error") return false;
  if (!error || error.role !== "assistant") return false;
  if (Array.isArray(error.content) ? error.content.length !== 0 : true) return false;
  return allUsageZero(error.usage);
}

/** Reject unrelated responses before cloning or waiting for any body proof. */
function isQueueResponse(res, requestUrl, requestUrlGate) {
  return res?.status === 503 && !res.redirected &&
    sameLocation(res.url, requestUrl) && requestUrlGate(requestUrl) &&
    res.headers.get(QUEUE_MARKER_HEADER) === "1" &&
    res.headers.get(QUEUE_TIMEOUT_HEADER) === "1";
}

/** Full response gate: status, both stamps, exact body, same-location, no redirect. */
function gateResponse(res, requestUrl, text, requestUrlGate, nowFn) {
  if (!isQueueResponse(res, requestUrl, requestUrlGate) || text !== QUEUE_BODY_FULL) return null;
  const nowMs = typeof nowFn === "function" ? nowFn() : nowFn;
  return { retryAfterMs: parseRetryAfterMs(res.headers.get("retry-after"), nowMs) };
}

/**
 * Await an in-flight body capture, racing the parent abort so a stalled clone
 * can never delay termination. Resolves { aborted: true } when the signal
 * fires (listener removed in cleanup), { aborted: false } once the capture
 * settled either way (verdict published or read failure — both ineligible to
 * this caller; classification happens in the engine loop).
 */
async function awaitCapture(capture, signal) {
  let onAbort = null;
  try {
    return await Promise.race([
      capture.then(
        () => ({ aborted: false }),
        () => ({ aborted: false }),
      ),
      new Promise((resolve) => {
        if (signal?.aborted) {
          resolve({ aborted: true });
          return;
        }
        onAbort = () => resolve({ aborted: true });
        signal?.addEventListener("abort", onAbort, { once: true });
      }),
    ]);
  } finally {
    if (onAbort) signal?.removeEventListener("abort", onAbort);
  }
}

/**
 * Wrap the effective fetch for one attempt. Resets the attempt's capture and
 * bumps a per-fetch identity token before EVERY delegated fetch (including
 * ineligible and network-failed ones); on a 503 clones immediately and
 * classifies the clone asynchronously (never blocking the native path),
 * publishing the verdict ONLY while its fetch is still the current one so a
 * delayed stale clone can never authorize a retry. Unrelated responses stay
 * untouched. Only a proven queue rejection gets a retry-control header overlay
 * when caller-enabled native retries would otherwise escape outer accounting.
 */
function wrapFetch(options, attempt, requestUrlGate, nowFn) {
  const nativeFetch = options?.fetch ?? globalThis.fetch;
  let fetchToken = 0; // identity of the currently-dispatched delegated fetch
  return async (input, init) => {
    const token = ++fetchToken;
    attempt.rejection = null;
    attempt.capture = null;
    const requestUrl = effectiveUrl(input);
    if (effectiveMethod(input, init) !== "POST" || !requestUrlGate(requestUrl)) {
      return nativeFetch(input, init);
    }
    const res = await nativeFetch(input, init);
    if (!isQueueResponse(res, requestUrl, requestUrlGate)) return res;
    let clone;
    try {
      clone = res.clone();
    } catch {
      return res; // clone failure: ineligible
    }
    attempt.capture = (async () => {
      try {
        const text = await clone.text();
        if (token === fetchToken) {
          attempt.rejection = gateResponse(res, requestUrl, text, requestUrlGate, nowFn);
        }
      } catch {
        if (token === fetchToken) {
          attempt.rejection = null; // read failure: ineligible
        }
      }
    })();
    // Pi's native adapter defaults to zero retries. Preserve its asynchronous
    // capture path then; only enabled native retries need proof before return.
    if (options?.maxRetries > 0 && !attempt.sawPriorEvent) {
      const signals = [init?.signal, options?.signal].filter(Boolean);
      const signal = signals.length ? AbortSignal.any(signals) : undefined;
      const captured = await awaitCapture(attempt.capture, signal);
      if (captured.aborted) throw new DOMException("Request was aborted", "AbortError");
      if (token === fetchToken && attempt.rejection && !attempt.sawPriorEvent) {
        // Both Pi retryProviderRequest and the native SDK honor this header.
        // No admission sleep here: the request timeout ends before outer wait.
        const headers = new Headers(res.headers);
        headers.set("x-should-retry", "false");
        const controlled = new Response(res.body, {
          status: res.status, statusText: res.statusText, headers,
        });
        Object.defineProperty(controlled, "url", { value: res.url });
        return controlled;
      }
    }
    return res;
  };
}

/** A native-shaped terminal error event with empty content and zero usage/cost. */
function syntheticErrorEvent(model, stopReason, message, timestamp) {
  return {
    type: "error",
    reason: stopReason,
    error: {
      role: "assistant",
      content: [],
      api: model?.api,
      provider: model?.provider,
      model: model?.id,
      usage: {
        input: 0,
        output: 0,
        cacheRead: 0,
        cacheWrite: 0,
        totalTokens: 0,
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
      },
      stopReason,
      errorMessage: message,
      timestamp,
    },
  };
}
