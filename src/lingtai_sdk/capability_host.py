"""CapabilityBundle host boundary (proof).

Where :mod:`lingtai_sdk.capabilities` is the public *schema* (what a bundle
declares), this module is the smallest possible *host* for one: it takes a
validated :class:`~lingtai_sdk.capabilities.BundleManifest` plus a mapping of
``{tool_name: callable}`` and proves the manifest/load/host boundary —

    declared manifest -> load_manifest() -> BundleHost -> invoke(tool, ...)

This is the *non-native* host boundary, so it is deliberately conservative:

* It **validates** the manifest on registration (a host never trusts an
  unvalidated declaration).
* It **refuses privileged / native-only** bundles — those may only be hosted by
  the native runtime, never by this in-process host. This is exactly why the
  core ``system`` / ``psyche`` / ``soul`` bundles are *not* migrated here.
* It enforces the **manifest ↔ implementation contract**: every declared
  ``surfaces.tools`` name has a handler, and no handler is undeclared.

Handlers are plain callables. The only handler shipped here is a deterministic,
network-free ``echo`` wired to the synthetic ``proof_bundle()``. This module
imports only the import-pure ``capabilities`` and ``errors`` siblings, so
``import lingtai_sdk.capability_host`` pulls in no wrapper or provider SDK.
"""
from __future__ import annotations

from typing import Any, Callable, Mapping

from .capabilities import BundleManifest, proof_bundle
from .errors import BundleHostError

ToolHandler = Callable[..., Any]


class BundleHost:
    """An in-process host for a single validated, non-privileged bundle.

    Registers ``manifest`` together with ``handlers`` (a name→callable mapping
    covering exactly the manifest's declared ``surfaces.tools``) and exposes
    :meth:`invoke`. Construction is where every boundary check happens, so a
    constructed host is always safe to invoke.
    """

    def __init__(
        self,
        manifest: BundleManifest,
        handlers: Mapping[str, ToolHandler],
    ) -> None:
        try:
            manifest.validate()
        except ValueError as exc:
            raise BundleHostError(
                f"cannot host an invalid manifest: {exc}"
            ) from exc

        if manifest.roles.privileged or manifest.roles.native_only:
            raise BundleHostError(
                f"refusing to host privileged/native-only bundle "
                f"{manifest.name!r}: only the native runtime may host it"
            )
        if manifest.transport.kind != "in_process":
            raise BundleHostError(
                f"refusing to host bundle {manifest.name!r} with "
                f"transport {manifest.transport.kind!r}: "
                "BundleHost only hosts in_process proof bundles"
            )

        declared = set(manifest.surfaces.tools)
        provided = set(handlers)
        missing = declared - provided
        extra = provided - declared
        if missing:
            raise BundleHostError(
                f"bundle {manifest.name!r} declares tools with no handler: "
                f"{sorted(missing)}"
            )
        if extra:
            raise BundleHostError(
                f"bundle {manifest.name!r} has handlers for undeclared tools: "
                f"{sorted(extra)}"
            )
        non_callable = sorted(name for name, fn in handlers.items() if not callable(fn))
        if non_callable:
            raise BundleHostError(
                f"bundle {manifest.name!r} has non-callable handlers: {non_callable}"
            )

        self._manifest = manifest
        self._handlers: dict[str, ToolHandler] = dict(handlers)

    @property
    def manifest(self) -> BundleManifest:
        return self._manifest

    @property
    def tools(self) -> tuple[str, ...]:
        """The tool names this host can invoke (the manifest's declared tools)."""
        return self._manifest.surfaces.tools

    def invoke(self, tool: str, **kwargs: Any) -> Any:
        """Invoke a declared tool by name. Unknown tools raise ``BundleHostError``."""
        handler = self._handlers.get(tool)
        if handler is None:
            raise BundleHostError(
                f"bundle {self._manifest.name!r} does not host tool {tool!r}; "
                f"available: {sorted(self._handlers)}"
            )
        return handler(**kwargs)


def _echo(text: str = "") -> dict[str, str]:
    """The proof bundle's single tool: deterministic, pure, network-free."""
    return {"echo": text}


def proof_host() -> BundleHost:
    """A ready :class:`BundleHost` for the synthetic ``proof_bundle()``.

    The end-to-end proof: the declared ``sdk_proof_echo`` bundle, hosted with a
    single deterministic ``echo`` handler. ``proof_host().invoke("echo",
    text="hi")`` returns ``{"echo": "hi"}`` with no I/O of any kind.
    """
    return BundleHost(proof_bundle(), {"echo": _echo})


__all__ = [
    "ToolHandler",
    "BundleHost",
    "proof_host",
]
