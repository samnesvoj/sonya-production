// Prototype implementation of the proposed apiFetch()/computeUploadTimeoutMs()
// changes for auth.js -- see docs/patches/generation-upload-timeout-plan.md
// for the full plan and the exact diff this mirrors.
//
// NOT wired into the production app. It exists so the timeout/classification
// logic can be written and tested in isolation *before* touching auth.js,
// which has an unrelated URL-ingest feature actively in flight from another
// session. Keep this in sync with the diff in the plan doc; once the real
// patch lands in auth.js, this file and its test can be deleted (or kept
// as a reference -- see the plan's rollback section).
//
// Mirrors the real apiFetch()'s exact contract: {ok, status, json()} shape,
// credentials:'include', Content-Type stripped for a FormData body, and the
// existing `_networkError` flag (kept true for ALL of timeout/aborted/network
// so every existing `if (res._networkError)` call site in auth.js keeps
// working completely unchanged -- see the plan's "why _networkError stays
// true" note). `_networkErrorKind` is new and purely additive.

export const UPLOAD_BASE_TIMEOUT_MS = 20_000;
// ~4 Mbps: a conservative "slow mobile connection that is still actually
// working" floor, not a "this connection is unusable" floor. Below this,
// we'd rather report a clear timeout than let the button hang silently for
// tens of minutes with zero feedback. See the plan doc for the explicit
// trade-off this implies for very large files on very slow connections.
export const UPLOAD_MIN_THROUGHPUT_BYTES_PER_SEC = 500 * 1024;
// 20 minutes: matches the existing precedent already in this codebase --
// apiCreateVideoJobFromUrl()'s own polling loop (400 iterations * 3s) caps
// the URL-ingest flow at the same ~20-minute ceiling. Reusing that number
// keeps "how long SONYA will wait before giving up" consistent across both
// upload paths instead of inventing a second, different number.
export const UPLOAD_MAX_TIMEOUT_MS = 20 * 60 * 1000;
// Short fixed timeout for small JSON calls (starting with /auth/me) that
// never carry a large body -- see the plan doc's "also recommended"
// section for why this is proposed as a separate, separable change.
export const AUTH_CHECK_TIMEOUT_MS = 15_000;

/**
 * size-aware timeout for the multipart upload+job-create request:
 * a fixed floor (backend validate + S3 PUT + DB insert + round-trip
 * overhead for a near-zero-size file) plus a size-proportional budget
 * assuming a conservative-but-real minimum throughput, capped at a firm
 * maximum no matter how large the file is.
 */
export function computeUploadTimeoutMs(fileSizeBytes) {
  const size = Math.max(0, Number(fileSizeBytes) || 0);
  const sizeBudgetMs = (size / UPLOAD_MIN_THROUGHPUT_BYTES_PER_SEC) * 1000;
  return Math.min(UPLOAD_BASE_TIMEOUT_MS + sizeBudgetMs, UPLOAD_MAX_TIMEOUT_MS);
}

export const DEFAULT_NETWORK_ERROR_MESSAGES = {
  timeout: 'Превышено время ожидания ответа сервера. Проверьте соединение и попробуйте снова.',
  aborted: 'Запрос отменён.',
  network: 'Сервер недоступен. Проверьте соединение.',
};

function classifyFetchError(err, { timedOut }) {
  if (err && err.name === 'AbortError') return timedOut ? 'timeout' : 'aborted';
  return 'network';
}

/**
 * `fetchImpl`/`windowRef` are injected purely so this prototype is testable
 * standalone, without a DOM -- the real auth.js diff uses the ambient
 * `fetch`/`window` directly (see the plan doc's diff), no injection there.
 */
export function makeApiFetch({ apiBase = '', fetchImpl = fetch, windowRef } = {}) {
  return async function apiFetch(path, options = {}) {
    const { timeoutMs, timeoutMessage, signal: callerSignal, ...fetchOptions } = options;
    const url = apiBase + path;
    const defaults = {
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
    };

    const controller = new AbortController();
    let timedOut = false;
    let timer = null;
    if (timeoutMs) {
      timer = setTimeout(() => {
        timedOut = true;
        controller.abort();
      }, timeoutMs);
    }
    let onCallerAbort = null;
    if (callerSignal) {
      if (callerSignal.aborted) {
        controller.abort();
      } else {
        onCallerAbort = () => controller.abort();
        callerSignal.addEventListener('abort', onCallerAbort, { once: true });
      }
    }

    const startedAt = Date.now();
    try {
      const headers = { ...defaults.headers, ...(fetchOptions.headers || {}) };
      if (typeof FormData !== 'undefined' && fetchOptions.body instanceof FormData) {
        delete headers['Content-Type'];
      }
      const res = await fetchImpl(url, { ...defaults, ...fetchOptions, headers, signal: controller.signal });
      return res;
    } catch (e) {
      const kind = classifyFetchError(e, { timedOut });
      const elapsedMs = Date.now() - startedAt;
      // Diagnostic only: path, classification, and timing -- never headers,
      // cookies, the request body, or any token/secret.
      console.error(`[SONYA API] ${kind} error path=${path} elapsedMs=${elapsedMs}` +
        (timeoutMs ? ` timeoutMs=${timeoutMs}` : ''));
      if (windowRef) {
        windowRef.SONYA_LAST_NETWORK_ERROR = { path, kind, timeoutMs: timeoutMs || null, elapsedMs, timestamp: Date.now() };
      }
      const detail = (kind === 'timeout' && timeoutMessage) || DEFAULT_NETWORK_ERROR_MESSAGES[kind];
      return {
        ok: false,
        status: 0,
        _networkError: true,
        _networkErrorKind: kind,
        json: async () => ({ detail }),
      };
    } finally {
      if (timer) clearTimeout(timer);
      if (callerSignal && onCallerAbort) callerSignal.removeEventListener('abort', onCallerAbort);
    }
  };
}
