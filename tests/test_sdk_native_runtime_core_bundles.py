"""Stage 9: NativeRuntime exposes the core bundle manifest/host seam.

The real ``system`` / ``psyche`` / ``soul`` implementations are still not
migrated or imported here. The runtime session exposes their native-only
manifests and, only when supplied with injected dummy handlers, builds native
hosts around those handlers.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from lingtai_sdk import core_bundles as core
from lingtai_sdk import native
from lingtai_sdk import runtime as rt
from lingtai_sdk.capability_host import NativeBundleHost
from lingtai_sdk.errors import BundleHostError

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
CORE_NAMES = ("system", "psyche", "soul")


def _handlers(calls: list[tuple[str, dict]] | None = None):
    calls = calls if calls is not None else []

    def make(name: str):
        def handler(**kwargs):
            calls.append((name, kwargs))
            return {"name": name, "kwargs": kwargs}

        return handler

    return {name: make(name) for name in CORE_NAMES}


def test_native_session_exposes_core_bundle_manifests_manifest_only(tmp_path):
    session = native.NativeRuntime(agent_factory=lambda **_: object()).create_session(
        rt.RuntimeOptions(working_dir=tmp_path)
    )

    manifests = session.core_bundle_manifests
    assert tuple(m.name for m in manifests) == CORE_NAMES
    assert tuple(m.name for m in manifests) == core.core_bundle_names()
    for manifest in manifests:
        assert manifest.roles.required is True
        assert manifest.roles.privileged is True
        assert manifest.roles.native_only is True
        assert manifest.surfaces.tools == (manifest.name,)

    assert session.core_bundle_hosts == {}
    assert session.agent is None  # no wrapper agent constructed


def test_core_bundle_hosts_are_built_from_injected_handlers(tmp_path):
    calls: list[tuple[str, dict]] = []
    session = native.NativeRuntime(
        agent_factory=lambda **_: object(), core_handlers=_handlers(calls)
    ).create_session(rt.RuntimeOptions(working_dir=tmp_path))

    hosts = session.core_bundle_hosts
    assert tuple(hosts) == CORE_NAMES
    assert all(isinstance(host, NativeBundleHost) for host in hosts.values())

    result = hosts["system"].invoke("system", action="presets")
    assert result == {"name": "system", "kwargs": {"action": "presets"}}
    assert calls == [("system", {"action": "presets"})]

    # The property returns a shallow copy: callers cannot mutate session wiring.
    hosts.pop("system")
    assert tuple(session.core_bundle_hosts) == CORE_NAMES


def test_core_handler_validation_rejects_partial_extra_or_non_callable(tmp_path):
    opts = rt.RuntimeOptions(working_dir=tmp_path)

    missing = _handlers()
    missing.pop("soul")
    with pytest.raises(BundleHostError, match="missing handler"):
        native.NativeRuntime(agent_factory=lambda **_: object(), core_handlers=missing).create_session(opts)

    extra = _handlers()
    extra["not-core"] = lambda **_: None
    with pytest.raises(BundleHostError, match="non-core"):
        native.NativeRuntime(agent_factory=lambda **_: object(), core_handlers=extra).create_session(opts)

    bad = _handlers()
    bad["psyche"] = "not callable"  # type: ignore[assignment]
    with pytest.raises(BundleHostError, match="callable"):
        native.NativeRuntime(agent_factory=lambda **_: object(), core_handlers=bad).create_session(opts)


def test_core_handlers_do_not_affect_agent_kwargs_or_start(tmp_path):
    class FakeAgent:
        last_kwargs: dict | None = None

        def __init__(self, **kwargs):
            type(self).last_kwargs = kwargs
            self.started = False
            self.working_dir = Path(kwargs["working_dir"])

        def start(self):
            self.started = True

        def stop(self, timeout: float = 5.0):
            pass

    session = native.NativeRuntime(
        agent_factory=FakeAgent, core_handlers=_handlers()
    ).create_session(rt.RuntimeOptions(working_dir=tmp_path, agent_name="core-test"))
    session.start()

    assert isinstance(session.agent, FakeAgent)
    assert session.agent.started is True
    assert FakeAgent.last_kwargs == {"working_dir": tmp_path, "agent_name": "core-test"}
    assert tuple(session.core_bundle_hosts) == CORE_NAMES


def test_import_native_with_core_handlers_stays_wrapper_free(tmp_path):
    code = f"""
import sys
from pathlib import Path
sys.path.insert(0, {str(SRC)!r})
from lingtai_sdk import native, runtime as rt
handlers = {{name: (lambda **kwargs: kwargs) for name in ('system', 'psyche', 'soul')}}
rtm = native.NativeRuntime(core_handlers=handlers)
session = rtm.create_session(rt.RuntimeOptions(working_dir={str(tmp_path)!r}))
assert tuple(session.core_bundle_hosts) == ('system', 'psyche', 'soul')
bad = [m for m in sys.modules if m == 'lingtai' or m.startswith('lingtai.')]
assert not bad, bad
print('OK')
"""
    r = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env={**os.environ, "PYTHONPATH": str(SRC)},
    )
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout
