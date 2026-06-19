# Codex OAuth Stateless Responses — Patch Spec

**Date:** 2026-05-03
**Origin:** Field fix applied directly to a deployed venv (`/Users/dt/.lingtai-tui/runtime/venv/.../lingtai/llm/openai/adapter.py` and `_register.py`) after Cohen agent went STUCK on Codex LLM calls. Source tree never received the change. This patch ports the fix to canonical source so it survives reinstalls.

## Problem

When an agent's preset is `provider=codex` (ChatGPT-OAuth login, not OpenAI API key), our adapter wires up `OpenAIAdapter` pointing at `https://chatgpt.com/backend-api` with `use_responses=True, force_responses=True`. Three things break against that endpoint:

1. **Wrong base URL.** ChatGPT's backend serves Codex's stateless Responses loop at `/backend-api/codex/responses`, not `/backend-api/responses`. Hitting the latter returns 404 / a Cloudflare HTML page, which surfaces in our retry loop as "JSON decode error" → STUCK.
   - Reference: [OpenAI — Unrolling the Codex Agent Loop](https://openai.com/index/unrolling-the-codex-agent-loop/) — Codex CLI calls `chatgpt.com/backend-api/codex/responses` and runs a stateless loop.

2. **Wrong reasoning param name.** The Chat Completions SDK takes `reasoning_effort: "high"|"low"`. The Responses API takes `reasoning: { effort: "high"|"low"|"minimal" }`. Sending `reasoning_effort` to a Responses endpoint is silently rejected (no error from the SDK; the model just runs at default effort), and on Codex specifically the request 400s.
   - Reference: [OpenAI Responses API reference](https://platform.openai.com/docs/api-reference/responses/create?api-mode=responses) — `reasoning` is an object with an `effort` field.
   - Reference: [Codex config reference](https://developers.openai.com/codex/config-reference#configtoml) — `model_reasoning_effort` documented under the Responses-API path.

3. **Stateful flow on a stateless backend.** Our `OpenAIResponsesSession` threads `previous_response_id` so the server retains turn state. Codex's backend is **stateless** — every request must carry the full conversation as `input`, no `previous_response_id`, and Codex CLI runs with `store=false, stream=true`. Sending `previous_response_id` against the Codex endpoint either 400s or causes silent drift (server doesn't have the parent turn).

4. **Tools schema rejection.** The Responses API rejects `function` tools that include `allOf` / `oneOf` / `anyOf` / `not` / `enum` at the **top level** of the parameters schema (only inside individual properties is fine). Some of our intrinsics build schemas with a top-level `oneOf` for variant-shaped tool inputs — those need to be flattened, or the tool call fails before the model ever sees it. The deployed-venv fix scrubs these top-level keys when building Responses tools.

All four together produced the symptom the user saw: Cohen sends a Codex LLM call, gets a Cloudflare error or `reasoning_effort` SDK rejection, retries, escalates to STUCK / AED. State machine working as intended; LLM call shape is wrong.

## What changed in the deployed venv

The user described four edits, at line numbers that don't match this source tree (the venv had extra wiring or a slightly drifted version). I'm porting **intent**, not line numbers. Mapping to current source:

| Venv line | Intent | Current-source target |
|-----------|--------|------------------------|
| `adapter.py:58` | Flatten Responses tools schema; strip top-level `allOf/oneOf/anyOf/not/enum` | `_build_tools()` already flat; add a new `_build_responses_tools()` that scrubs |
| `adapter.py:1017` | New stateless-streaming session for Codex OAuth | New class `CodexResponsesSession` + new `CodexOpenAIAdapter` subclass |
| `adapter.py:1246` | `reasoning_effort` → `reasoning.effort` on Responses path | `_create_responses_session` line 1029 |
| `_register.py:54` | Codex provider → `CodexOpenAIAdapter`, `base_url=".../backend-api/codex"` | `_codex` factory at `_register.py:54-82` |

## Files to change

1. `src/lingtai/llm/openai/adapter.py`
2. `src/lingtai/llm/_register.py`
3. (No tests pin the changed values — `tests/test_codex.py` and `tests/unit/auth/test_codex_auth.py` cover other surfaces. Recommend adding one test in §4 below.)

---

## Patch 1 — `src/lingtai/llm/openai/adapter.py`

### 1a. Add a Responses-aware tools builder (after existing `_build_tools`, ~line 56)

The Responses API rejects top-level `allOf/oneOf/anyOf/not/enum` in `parameters`. Add a sibling builder that strips them. Keep `_build_tools` unchanged for the Chat Completions path (which accepts those keys).

**Insert** after the existing `_build_tools` definition (around line 55):

```python
# Top-level JSON-Schema combinators that the Responses API rejects on
# function-tool `parameters`. Allowed inside individual properties, just
# not as a top-level key. Scrubbing is shallow on purpose — we only
# remove these when they appear at the schema root.
_RESPONSES_DISALLOWED_TOP_LEVEL = ("allOf", "oneOf", "anyOf", "not", "enum")


def _build_responses_tools(schemas: list[FunctionSchema] | None) -> list[dict] | None:
    """Convert FunctionSchema list to Responses API tool format.

    Responses uses a flat shape (`type: function`, fields hoisted) instead
    of Chat Completions' nested `{type: function, function: {...}}`. Also
    scrubs top-level JSON-Schema combinators that the Responses API
    rejects on tool parameters; combinators inside individual properties
    are left alone.
    """
    if not schemas:
        return None
    tools = []
    for s in schemas:
        params = dict(s.parameters or {})
        for key in _RESPONSES_DISALLOWED_TOP_LEVEL:
            params.pop(key, None)
        tools.append(
            {
                "type": "function",
                "name": s.name,
                "description": s.description,
                "parameters": params,
            }
        )
    return tools
```

### 1b. Switch the Responses path from Chat-Completions tool shape → Responses tool shape

In `_create_responses_session` (~line 1011), change:

```python
        openai_tools = _build_tools(tools)
```

to:

```python
        openai_tools = _build_responses_tools(tools)
```

### 1c. Fix the reasoning param shape on the Responses path

In `_create_responses_session` (~line 1028-1029), change:

```python
        if thinking != "default":
            extra_kwargs["reasoning_effort"] = "high" if thinking == "high" else "low"
```

to:

```python
        if thinking != "default":
            # Responses API takes `reasoning: { effort: ... }`, not the
            # Chat Completions SDK's flat `reasoning_effort`. Sending the
            # wrong shape silently drops the field on the OpenAI Responses
            # endpoint and 400s on Codex's `/backend-api/codex/responses`.
            extra_kwargs["reasoning"] = {"effort": "high" if thinking == "high" else "low"}
```

**Do not change the Chat-Completions path** at line 1088-1089 — that one correctly uses `reasoning_effort` because that is the documented Chat Completions SDK param. Codex never goes through that branch (we set `force_responses=True`).

### 1d. Add a stateless Codex Responses session and adapter subclass

The default `OpenAIResponsesSession` is built around server-side conversation state (`previous_response_id`). Codex's backend is stateless: every request must carry the full input itself, never `previous_response_id`, with `store=false, stream=true` baked in. Subclassing keeps the change isolated — OpenAI standard's Responses behavior is untouched.

**Insert** after the `OpenAIResponsesSession` class definition (right before the `# ----- OpenAIAdapter -----` divider at line 918):

```python
# ---------------------------------------------------------------------------
# CodexResponsesSession — stateless variant for ChatGPT-OAuth backend
# ---------------------------------------------------------------------------


class CodexResponsesSession(OpenAIResponsesSession):
    """Stateless Responses session for Codex's `/backend-api/codex/responses`.

    Differences from the parent:
      * `previous_response_id` is never sent — Codex's backend doesn't
        persist turns server-side. The full input must be carried each
        request by the caller (interface layer accumulates messages).
      * `store=False` is forced — same reason.
      * Streaming is forced (`stream=True` on send/send_stream alike) —
        non-streaming Codex requests return data the SDK can't unmarshal.
    """

    def send(self, message) -> LLMResponse:
        # Force the streaming path — Codex doesn't serve non-streaming JSON.
        return self.send_stream(message, on_chunk=None)

    def send_stream(self, message, on_chunk=None) -> LLMResponse:
        input_items = self._convert_input(message)

        kwargs: dict[str, Any] = {
            "model": self._model,
            "input": input_items,
            "stream": True,
            "store": False,
            **self._extra_kwargs,
        }
        if self._instructions:
            kwargs["instructions"] = self._instructions
        if self._tools:
            kwargs["tools"] = self._tools
            if self._tool_choice:
                kwargs["tool_choice"] = self._tool_choice
        # Deliberately omit previous_response_id — backend is stateless.
        if self._compact_threshold:
            kwargs["context_management"] = [
                {"type": "compaction", "compact_threshold": self._compact_threshold}
            ]

        acc = StreamingAccumulator()
        response_id = None
        usage = UsageMetadata()

        stream = self._client.responses.create(**kwargs)
        for event in stream:
            if event.type == "response.output_text.delta":
                acc.add_text(event.delta)
                if on_chunk:
                    on_chunk(event.delta)
            elif event.type == "response.function_call_arguments.delta":
                acc.add_tool_args(event.delta)
            elif event.type == "response.output_item.added":
                if getattr(event.item, "type", None) == "function_call":
                    acc.start_tool(id=event.item.call_id, name=event.item.name)
            elif event.type == "response.output_item.done":
                if getattr(event.item, "type", None) == "function_call":
                    acc.finish_tool()
            elif event.type == "response.completed":
                response_id = event.response.id
                if event.response.usage:
                    cached = getattr(event.response.usage, "input_tokens_details", None)
                    cached_tokens = (getattr(cached, "cached_tokens", 0) or 0) if cached else 0
                    usage = UsageMetadata(
                        input_tokens=getattr(event.response.usage, "input_tokens", 0) or 0,
                        output_tokens=getattr(event.response.usage, "output_tokens", 0) or 0,
                        thinking_tokens=getattr(
                            event.response.usage, "output_tokens_details", None
                        )
                        and getattr(
                            event.response.usage.output_tokens_details,
                            "reasoning_tokens",
                            0,
                        )
                        or 0,
                        cached_tokens=cached_tokens,
                    )

        # Stateless: don't persist the response_id beyond this single turn.
        # Stored only as a transient debug aid; never threaded into the next
        # request. (Parent class assigns to self._response_id and reads it
        # in send/send_stream — we override both, so the assignment here is
        # informational only.)
        self._response_id = response_id
        return acc.finalize(usage=usage)


class CodexOpenAIAdapter(OpenAIAdapter):
    """OpenAIAdapter variant that builds CodexResponsesSession instead of the
    standard server-stateful OpenAIResponsesSession.

    Use this with `provider=codex` only. Always set `use_responses=True,
    force_responses=True, base_url='https://chatgpt.com/backend-api/codex'`.
    """

    def _create_responses_session(
        self,
        model: str,
        system_prompt: str,
        tools: list[FunctionSchema] | None = None,
        json_schema: dict | None = None,
        force_tool_call: bool = False,
        interface: ChatInterface | None = None,
        thinking: str = "default",
    ) -> CodexResponsesSession:
        if interface is None:
            interface = ChatInterface()
            interface.add_system(system_prompt, tools=FunctionSchema.list_to_dicts(tools))

        openai_tools = _build_responses_tools(tools)
        tool_choice: str | None = None
        if force_tool_call and openai_tools:
            tool_choice = "required"

        extra_kwargs: dict[str, Any] = {}

        if json_schema is not None:
            extra_kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": json_schema.get("title", "response"),
                    "strict": True,
                    "schema": json_schema,
                },
            }

        if thinking != "default":
            extra_kwargs["reasoning"] = {"effort": "high" if thinking == "high" else "low"}

        # Codex's backend doesn't accept context_management compaction —
        # leave compact_threshold unset.
        return CodexResponsesSession(
            client=self._client,
            model=model,
            instructions=system_prompt,
            tools=openai_tools,
            tool_choice=tool_choice,
            extra_kwargs=extra_kwargs,
            previous_response_id=None,
            compact_threshold=None,
            interface=interface,
        )
```

**Justification for subclassing rather than flagging:** A `is_codex` boolean threaded through `OpenAIResponsesSession.send/send_stream` would touch the OpenAI-standard hot path with conditionals it doesn't need. The Codex backend's contract is different enough (stateless, store=false, forced streaming, no compaction) that a separate session class keeps the OpenAI-standard one auditable.

---

## Patch 2 — `src/lingtai/llm/_register.py`

### 2. Switch Codex factory to `CodexOpenAIAdapter` and `/backend-api/codex`

Current (lines 54-82):

```python
    def _codex(*, model=None, defaults=None, **kw):
        from .openai.adapter import OpenAIAdapter
        from lingtai.auth.codex import CodexTokenManager
        kw.pop("model", None)
        kw.pop("api_key", None)
        kw.pop("base_url", None)
        mgr = CodexTokenManager()
        adapter = OpenAIAdapter(
            api_key=mgr.get_access_token(),
            base_url="https://chatgpt.com/backend-api",
            use_responses=True,
            force_responses=True,
        )
        adapter._codex_token_mgr = mgr
        _orig_create_chat = adapter.create_chat
        def _refreshing_create_chat(*a, **kwa):
            adapter._client.api_key = mgr.get_access_token()
            return _orig_create_chat(*a, **kwa)
        adapter.create_chat = _refreshing_create_chat
        _orig_generate = adapter.generate
        def _refreshing_generate(*a, **kwa):
            adapter._client.api_key = mgr.get_access_token()
            return _orig_generate(*a, **kwa)
        adapter.generate = _refreshing_generate
        return adapter
```

Replace with:

```python
    def _codex(*, model=None, defaults=None, **kw):
        from .openai.adapter import CodexOpenAIAdapter
        from lingtai.auth.codex import CodexTokenManager
        kw.pop("model", None)
        kw.pop("api_key", None)
        kw.pop("base_url", None)
        mgr = CodexTokenManager()
        adapter = CodexOpenAIAdapter(
            api_key=mgr.get_access_token(),
            base_url="https://chatgpt.com/backend-api/codex",
            use_responses=True,
            force_responses=True,
        )
        adapter._codex_token_mgr = mgr
        _orig_create_chat = adapter.create_chat
        def _refreshing_create_chat(*a, **kwa):
            adapter._client.api_key = mgr.get_access_token()
            return _orig_create_chat(*a, **kwa)
        adapter.create_chat = _refreshing_create_chat
        _orig_generate = adapter.generate
        def _refreshing_generate(*a, **kwa):
            adapter._client.api_key = mgr.get_access_token()
            return _orig_generate(*a, **kwa)
        adapter.generate = _refreshing_generate
        return adapter
```

Two edits: import `CodexOpenAIAdapter`, append `/codex` to the base URL.

---

## Patch 3 — recommended test (optional, low blast radius)

`tests/test_codex.py` doesn't currently assert the adapter wiring. One test pinning the four load-bearing values would catch future drift:

```python
def test_codex_adapter_wiring(monkeypatch, tmp_path):
    """Pin Codex provider's adapter type, base URL, and Responses flags.

    Drift on any of these manifests as Cohen-style STUCK loops in the wild —
    we want CI to catch it instead.
    """
    # Provide a fake token file so CodexTokenManager doesn't 404.
    token_file = tmp_path / "codex-auth.json"
    token_file.write_text(json.dumps({
        "access_token": "test",
        "refresh_token": "test",
        "expires_at": int(time.time()) + 3600,
    }))
    monkeypatch.setenv("LINGTAI_TUI_DIR", str(tmp_path))

    from lingtai.llm._register import register_all_adapters
    from lingtai_kernel.llm.service import LLMService
    from lingtai.llm.openai.adapter import CodexOpenAIAdapter

    register_all_adapters()
    factory = LLMService._adapter_factories["codex"]
    adapter = factory()

    assert isinstance(adapter, CodexOpenAIAdapter)
    assert adapter.base_url == "https://chatgpt.com/backend-api/codex"
    assert adapter._use_responses is True
    assert adapter._force_responses is True
```

---

## Verification checklist (run after applying)

1. **Import smoke test:**
   ```bash
   ~/.lingtai-tui/runtime/venv/bin/python -c "
   from lingtai.llm.openai.adapter import CodexOpenAIAdapter, CodexResponsesSession, _build_responses_tools
   from lingtai.llm._register import register_all_adapters
   register_all_adapters()
   print('ok')
   "
   ```
2. **Tools-scrub unit check:**
   ```bash
   ~/.lingtai-tui/runtime/venv/bin/python -c "
   from lingtai.llm.openai.adapter import _build_responses_tools
   from lingtai_kernel.llm.base import FunctionSchema
   s = FunctionSchema(name='t', description='x', parameters={'type':'object','oneOf':[{'a':1},{'b':2}],'properties':{'k':{'enum':['a','b']}}})
   out = _build_responses_tools([s])
   assert 'oneOf' not in out[0]['parameters']
   assert out[0]['parameters']['properties']['k'].get('enum') == ['a','b']  # nested enum preserved
   print('scrub ok')
   "
   ```
3. **Test sweep:**
   ```bash
   cd ~/Documents/GitHub/lingtai-kernel && ~/.lingtai-tui/runtime/venv/bin/python -m pytest tests/test_codex.py tests/unit/auth/test_codex_auth.py -q
   ```
4. **End-to-end (manual):** restart Cohen via TUI; tail `cohen/logs/agent.log`; confirm absence of `reasoning_effort`, Cloudflare HTML, AED messages, and that `cohen` reports state `idle` with fresh heartbeat.

## Lines of change estimate

- **Adds:** ~140 (`_build_responses_tools` ~25, `CodexResponsesSession` ~70, `CodexOpenAIAdapter._create_responses_session` ~45)
- **Edits:** 3 lines in `_create_responses_session` (tool builder swap + reasoning shape) + 2 lines in `_register.py` (import + base_url suffix)
- **No deletes.** Existing OpenAI-standard Responses path unchanged.

## What's deliberately left out

- **No `previous_response_id` removal from `OpenAIResponsesSession`.** OpenAI standard's Responses API *does* support it; only Codex doesn't. Subclassing keeps the difference isolated.
- **No change to `_build_tools`.** Chat Completions accepts `oneOf/allOf/etc.` at the top level. Stripping there would silently degrade other providers.
- **No reasoning-shape change on Chat Completions path** (line 1088). That path correctly uses `reasoning_effort`.
- **`compact_threshold` set to `None` in Codex session.** If Codex's backend later supports `context_management`, lift it from `OpenAIResponsesSession`.

## Rollout note

Once applied to source, the deployed-venv hand-edits on `/Users/dt/...` should be reverted (`pip install --force-reinstall lingtai` or equivalent). Otherwise a future kernel pull will trigger import-time conflicts between the patched venv copy and the new source.
