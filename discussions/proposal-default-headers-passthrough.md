# Proposal: plumb `manifest.llm.default_headers` from preset → LLMService

## Problem

`LLMService._default_headers_for` (`src/lingtai/llm/service.py:133-149`) is documented to honor caller-supplied `default_headers` — its docstring says "Caller-supplied headers in *defaults* (under the `default_headers` key) win; provider-policy defaults only fill in what the caller did not specify."

That contract is **not actually wired up.** The two call sites that construct `provider_defaults` (`cli.py:81-85` for fresh agent boot, `agent.py:945-947` for runtime preset swap via `/refresh`) only ever populate `max_rpm`. The `default_headers` field never flows from `manifest.llm` into the dict, so the kernel always falls through to its own provider-policy defaults (e.g. the Kimi `User-Agent: LingTai-Agent/1.0` at `service.py:147`).

## Why this matters

A user who wants to override HTTP headers for their own provider (third-party reverse proxy that needs an `X-Internal-Auth` token, custom corporate gateway, an LLM service that requires a specific UA per their ToS, etc.) currently has no path: they can put `default_headers` in their saved preset JSON, but the kernel silently drops it.

The escape hatch is documented in code comments but doesn't exist in the data path. We should either remove the misleading comments or make the path real. This proposal does the latter — it's strictly additive and follows the architecture already implied by the docstring.

## What ships in the kernel (no policy change)

- The kernel still ships honest defaults — `LingTai-Agent/1.0` for Kimi, nothing for other providers.
- Templates under `~/.lingtai-tui/presets/templates/` do not gain a `default_headers` field; they continue to be silent on headers.
- No new schema validation; `default_headers` is treated as opaque pass-through (any dict[str, str] the user puts there flows to the OpenAI/HTTP client).
- The TUI preset editor is **not** changed. Users who need this hand-edit their saved preset JSON. This is intentional friction: the feature is for users with a specific need, not a default UX.

## Proposed patch

### File 1 — `src/lingtai/cli.py` (lines 80-85)

**Before:**
```python
    max_rpm = m.get("max_rpm", 60)
    provider_defaults: dict | None = None
    if max_rpm > 0:
        # provider_defaults is dict[provider_name, defaults_dict]; scope to
        # the agent's configured provider so other providers stay unaffected.
        provider_defaults = {llm["provider"].lower(): {"max_rpm": max_rpm}}
```

**After:**
```python
    max_rpm = m.get("max_rpm", 60)
    provider_key = llm["provider"].lower()
    per_provider: dict = {}
    if max_rpm > 0:
        per_provider["max_rpm"] = max_rpm
    user_headers = llm.get("default_headers")
    if isinstance(user_headers, dict) and user_headers:
        # Pass-through; LLMService._default_headers_for honors caller-supplied
        # headers and fills only the gaps with provider policy.
        per_provider["default_headers"] = dict(user_headers)
    # provider_defaults is dict[provider_name, defaults_dict]; scope to
    # the agent's configured provider so other providers stay unaffected.
    provider_defaults: dict | None = {provider_key: per_provider} if per_provider else None
```

### File 2 — `src/lingtai/agent.py` (lines 944-947)

**Before:**
```python
        new_max_rpm = m.get("max_rpm", 60)
        new_provider_defaults: dict | None = None
        if new_max_rpm > 0:
            new_provider_defaults = {new_provider.lower(): {"max_rpm": new_max_rpm}}
```

**After:**
```python
        new_max_rpm = m.get("max_rpm", 60)
        new_provider_key = new_provider.lower()
        new_per_provider: dict = {}
        if new_max_rpm > 0:
            new_per_provider["max_rpm"] = new_max_rpm
        new_user_headers = llm.get("default_headers")
        if isinstance(new_user_headers, dict) and new_user_headers:
            new_per_provider["default_headers"] = dict(new_user_headers)
        new_provider_defaults: dict | None = (
            {new_provider_key: new_per_provider} if new_per_provider else None
        )
```

Note: the swap-detection condition at lines 949-956 currently compares only `max_rpm`. After this change it will *not* trigger a re-create when only `default_headers` changed. That's acceptable for now (header overrides are rare; user can `/refresh` after editing the preset). If we want header changes to also trigger re-create, add an equivalent comparison — but I'd defer that to a follow-up unless you want it bundled.

## What does NOT change

- `service.py:_default_headers_for` (already correct — receives `defaults` dict, honors `default_headers` key, fills Kimi UA only when caller didn't set one).
- `service.py:_create_adapter` (already passes `headers_kw` through to factory).
- All adapter factories (already accept `default_headers` via `**kwargs` to OpenAI client).
- Preset templates — none gain a `default_headers` field.
- `init_schema.validate_init` — no schema change; field is optional and untyped.
- TUI — no editor UI changes; users hand-edit JSON.

## Test plan

1. **Smoke test the import path.** After applying:
   ```bash
   ~/.lingtai-tui/runtime/venv/bin/python -c "from lingtai.cli import load_init; from lingtai.agent import Agent"
   ```
   Catches any typo or undefined name immediately.

2. **Existing tests.** All pre-existing `cli.py` / `agent.py` tests should pass unchanged. The only new behavior is "if you put `default_headers` in `manifest.llm`, it flows through" — additive.

3. **Manual verification (optional).** A user puts:
   ```json
   "llm": {
     ...
     "default_headers": {"X-Test-Marker": "lingtai-passthrough"}
   }
   ```
   in a saved preset, launches an agent, observes via packet capture / provider-side logs that `X-Test-Marker` reaches the API.

4. **Regression check.** A preset *without* `default_headers` (any existing preset) behaves identically — `provider_defaults` is unchanged from today.

## Acceptance criteria

- [ ] `pytest` passes (no new failures).
- [ ] Smoke import returns 0.
- [ ] A preset with `default_headers` field loads without error and the headers reach the wire.
- [ ] A preset without `default_headers` field loads without error and behavior is bit-identical to pre-patch.
- [ ] No mention of "Kimi", "spoofing", "User-Agent override" in commit message — this is a generic plumbing fix, not a workaround for a specific provider's gate.

## Suggested commit message

```
feat(llm): plumb manifest.llm.default_headers from preset into provider_defaults

LLMService._default_headers_for already honors caller-supplied default_headers
per its docstring, but the call sites in cli.load_init and agent._read_init
never populated the field — only max_rpm. Pass user-declared headers through
verbatim so presets that need custom HTTP headers (corporate gateways,
third-party proxies, providers with specific UA requirements per their ToS)
work end-to-end.

Strictly additive: presets without the field behave identically.
```

## Risks

- **None to existing functionality** — purely additive. Presets without the field follow the same code path as today.
- **User responsibility** — anyone who puts a header value in their preset is sending it to the upstream provider. That's their call, against their provider's ToS. The kernel is a transport.
- **One small caveat** — the swap-detection at `agent.py:949-956` doesn't compare headers, so editing only `default_headers` and calling `/refresh` won't re-create the adapter. Workaround: change `max_rpm` (or any other field) at the same time, OR restart the agent. Acceptable for an undocumented power-user feature.
