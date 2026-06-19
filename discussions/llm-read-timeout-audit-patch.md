# Proposal: LLM read-timeout audit

> **Status:** discussion / design proposal. Not yet implemented.
> **Motivated by:** live incident 2026-05-03 22:00–22:45 (codex-gpt5.5 hung for 45 minutes)
> **Companion:** `discussions/llm-hang-watchdog-patch.md` (visibility layer)

## Root cause analysis

### The timeout chain

When `session.send()` is called, the timeout chain is:

1. **`llm_utils._send()`** (`llm_utils.py:27-53`): main-thread watchdog. Polls `future.result(timeout=20)` every `_LLM_WARN_INTERVAL` (20s). After `retry_timeout` (300s), raises `TimeoutError` → AED.

2. **`_SubmitFn.__call__()`** (`llm_utils.py:83-108`): sets `chat._request_timeout = retry_timeout` on the session adapter before submitting to the thread pool.

3. **Adapter `send()` method**: passes `kw["timeout"] = self._request_timeout` to the SDK client call. For OpenAI: `client.chat.completions.create(timeout=300)`. For Anthropic: `client.messages.create(timeout=300)`.

4. **SDK HTTP client** (httpx): `timeout=300` becomes `httpx.Timeout(300)`, which sets `connect=300, read=300, write=300, pool=300`.

### Where it breaks

**httpx's `read` timeout is per-read-operation, not total request time.** It measures the time waiting for bytes on the socket, not the total time for the response. If the server:

1. Accepts the connection (connect timeout passes)
2. Sends HTTP headers (first read succeeds)
3. Holds the connection open without sending body data
4. Sends a byte every 299 seconds (keeping the per-read timer below 300s)

Then the read timeout never fires, the worker thread blocks indefinitely, and the main-thread watchdog in `_send()` keeps polling every 20s and logging warnings — but the agent is effectively hung.

**This is exactly what we observed.** The logs showed continuous "LLM API not responding after Xs..." warnings climbing to 280s, then resetting to 20s (indicating a retry cycle), climbing again — for 45 minutes. The HTTP connection was alive but not progressing.

### The gap

The `_request_timeout` is passed as a single numeric value to the SDK. The SDK passes it to httpx as a single `timeout=N`. httpx interprets this as per-phase timeout (connect, read, write, pool all set to N). There is no **total request timeout** — a wall-clock limit on the entire request from start to finish, regardless of how many bytes arrive.

### Per-adapter analysis

#### OpenAI adapter (`src/lingtai/llm/openai/adapter.py`)

**Lines 520-521, 673-674:**
```python
if self._request_timeout is not None:
    kw["timeout"] = self._request_timeout
```

Passed to `client.chat.completions.create(**kw)`. The OpenAI Python SDK (v1.x) accepts `timeout` as a numeric value and creates `httpx.Timeout(timeout)`. **Read timeout = 300s per read-operation. No total timeout.**

**Verdict: vulnerable.** A slow-drip server can hang the agent indefinitely.

#### Anthropic adapter (`src/lingtai/llm/anthropic/adapter.py`)

**Lines 317-320:**
```python
if self._request_timeout is not None:
    kwargs["timeout"] = self._request_timeout
```

Passed to `client.messages.create(**kwargs)`. The Anthropic Python SDK uses httpx internally. **Same pattern as OpenAI. Read timeout = 300s per read-operation. No total timeout.**

**Verdict: vulnerable.**

#### Gemini adapter (`src/lingtai/llm/gemini/adapter.py`)

**Line 648:**
```python
timeout=timeout_ms,
```

The Gemini SDK uses `google-api-core` which uses `requests` (not httpx). The `timeout` parameter is passed as `timeout=300` (seconds) to `requests.post()`. In `requests`, `timeout=N` sets both `connect` and `read` timeouts to N. **Same pattern — per-read-operation timeout.**

**Verdict: vulnerable.**

#### OpenRouter adapter (`src/lingtai/llm/openrouter/adapter.py`)

**Line 41:**
```python
timeout_ms=timeout_ms,
```

Inherits from the OpenAI adapter. **Same vulnerability.**

#### MiniMax adapter (`src/lingtai/llm/minimax/adapter.py`)

**Line 20:**
```python
super().__init__(api_key=api_key, base_url=effective_url, timeout_ms=timeout_ms)
```

Inherits from the OpenAI adapter. **Same vulnerability.**

#### DeepSeek adapter (`src/lingtai/llm/deepseek/adapter.py`)

**Line 129:**
```python
timeout_ms=timeout_ms,
```

Inherits from the OpenAI adapter. **Same vulnerability.**

#### Xiaomi MiMo adapter

Not found in the codebase. Likely configured via the OpenAI-compatible adapter path. **Same vulnerability if using httpx.**

### Summary

| Adapter | HTTP Client | Timeout Type | Vulnerable? |
|---|---|---|---|
| OpenAI | httpx | per-read-operation (300s) | **Yes** |
| Anthropic | httpx | per-read-operation (300s) | **Yes** |
| Gemini | requests | per-read-operation (300s) | **Yes** |
| OpenRouter | httpx (via OpenAI) | per-read-operation (300s) | **Yes** |
| MiniMax | httpx (via OpenAI) | per-read-operation (300s) | **Yes** |
| DeepSeek | httpx (via OpenAI) | per-read-operation (300s) | **Yes** |

**All adapters are vulnerable.** The root cause is systemic: the `_request_timeout` is passed as a single numeric value, which becomes a per-read-operation timeout in the HTTP client. No adapter sets a total request timeout.

## Proposed fix

### Option A: Reduce per-read timeout (recommended)

Change the per-read timeout from 300s to 60s. This means:
- If the server stops sending data for 60s, the HTTP connection times out.
- The worker thread raises an exception, the future completes, and `_send()` sees it.
- AED handles the retry (up to `max_aed_attempts` times).
- Total hang time before ASLEEP: `60 × max_aed_attempts` seconds (default: 180s = 3 min, vs current 900s = 15 min).

**Implementation:** Change `_SubmitFn` to set a shorter per-read timeout:

```python
# In llm_utils.py, _SubmitFn.__call__()
if self._retry_timeout is not None and hasattr(self.chat, "_request_timeout"):
    # Per-read timeout: 60s. If the server stops sending data for 60s,
    # the HTTP connection times out. This is separate from the main-thread
    # watchdog (retry_timeout) which controls total wall-clock time.
    self.chat._request_timeout = min(self._retry_timeout, 60.0)
```

Wait — this changes the semantics. The `_request_timeout` is used by the adapter to set `kw["timeout"]` which becomes `httpx.Timeout(N)`. If we set it to 60, the connect timeout also becomes 60s (might be too short for slow connections). Better approach: set an explicit `httpx.Timeout` object.

### Option B: Pass explicit httpx.Timeout (recommended for OpenAI/Anthropic adapters)

Instead of passing a single numeric value, pass an `httpx.Timeout` object with explicit per-phase values:

```python
import httpx

timeout = httpx.Timeout(
    connect=30.0,      # 30s to establish connection
    read=60.0,         # 60s between bytes — catches slow-drip servers
    write=30.0,        # 30s to send request
    pool=10.0,         # 10s waiting for connection from pool
)
```

**Implementation:** In each adapter's `send()` method, instead of `kw["timeout"] = self._request_timeout`, construct an explicit `httpx.Timeout`:

```python
# In openai/adapter.py, anthropic/adapter.py
if self._request_timeout is not None:
    from httpx import Timeout
    kw["timeout"] = Timeout(
        connect=30.0,
        read=min(self._request_timeout, 60.0),
        write=30.0,
        pool=10.0,
    )
```

**Trade-off:** This requires importing `httpx` in the adapter (already available since the SDK depends on it). The 60s read timeout is conservative — most LLM responses stream data within seconds of connection. Legitimate slow responses (1M context loads) still complete because data is streaming (each chunk arrives within 60s).

### Option C: Add total timeout via wrapper (belt-and-suspenders)

Wrap the SDK call in a function that enforces a total wall-clock timeout, independent of httpx:

```python
import threading

def _call_with_total_timeout(fn, timeout_seconds):
    result = []
    error = []
    def worker():
        try:
            result.append(fn())
        except Exception as e:
            error.append(e)
    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout=timeout_seconds)
    if t.is_alive():
        raise TimeoutError(f"Total request timeout after {timeout_seconds}s")
    if error:
        raise error[0]
    return result[0]
```

This is essentially what `_send()` already does at the kernel level. Adding it at the adapter level would be redundant. **Not recommended** — the kernel-level watchdog + reduced read timeout (Option B) is sufficient.

## Recommendation

**Implement Option B** (explicit `httpx.Timeout` in adapters) for the OpenAI and Anthropic adapters (the two most common). For Gemini (using `requests`), set a shorter `timeout` tuple `(connect_timeout, read_timeout)`:

```python
# requests supports (connect_timeout, read_timeout) tuple
timeout=(30.0, 60.0)  # 30s connect, 60s read
```

Combined with the watchdog (120s visibility threshold), this gives:
- **60s**: HTTP read timeout fires → worker raises → future completes → `_send()` sees error → AED retries
- **120s**: Watchdog fires → `STUCK` state → TUI shows "LLM API unresponsive"
- **300s**: `_send()` total timeout fires → AED escalates
- **Total before ASLEEP**: ~180s (3 × 60s retries) vs current ~900s (3 × 300s retries)

## Open questions

1. **Should the read timeout be configurable?** Currently proposed as a constant (60s). Making it a `config.py` field (`llm_read_timeout`) would let the human tune it. **Suggested:** start with a constant; make configurable if needed.

2. **Should this be in `_SubmitFn` or in each adapter?** `_SubmitFn` sets `chat._request_timeout` which is a single numeric value. Changing this to an `httpx.Timeout` object would require all adapters to handle the new type. Better to keep `_SubmitFn` as-is and change each adapter's `send()` method to construct the appropriate timeout object.

3. **Backward compatibility?** If someone passes a custom `timeout_ms` to the adapter constructor, it's used as the SDK-level timeout at construction time. The per-request `_request_timeout` override happens in `send()`. Both need to be compatible.

## Files to change

| File | Change |
|---|---|
| `src/lingtai/llm/openai/adapter.py:520-521` | Replace `kw["timeout"] = self._request_timeout` with explicit `httpx.Timeout` |
| `src/lingtai/llm/openai/adapter.py:673-674` | Same for streaming path |
| `src/lingtai/llm/anthropic/adapter.py:317-320` | Same pattern |
| `src/lingtai/llm/gemini/adapter.py` | Use `(connect, read)` tuple if using requests |

Adapters that inherit from OpenAI (OpenRouter, MiniMax, DeepSeek) get the fix for free.
