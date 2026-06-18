"""CapabilityBundle host boundary.

Where :mod:`lingtai_sdk.capabilities` is the public *schema* (what a bundle
declares), this module is the smallest possible *host* for one: it takes a
validated :class:`~lingtai_sdk.capabilities.BundleManifest` plus name→callable
handler mappings and proves the manifest/load/host boundary —

    declared manifest -> load_manifest() -> BundleHost -> invoke / read_*(...)

This is the *non-native* host boundary, so it is deliberately conservative:

* It **validates** the manifest on registration (a host never trusts an
  unvalidated declaration).
* It **refuses privileged / native-only** bundles — those may only be hosted by
  the native runtime, never by this in-process host. This is exactly why the
  core ``system`` / ``psyche`` / ``soul`` bundles are *not* migrated here.
* It enforces the **manifest ↔ implementation contract** per surface: every
  declared ``surfaces.tools`` / ``surfaces.resources`` / ``surfaces.prompts``
  name has a handler, and no handler is undeclared.

``BundleHost`` hosts three read-only surfaces — **tools** (:meth:`invoke`),
**resources** (:meth:`read_resource`), and **prompts** (:meth:`read_prompt`).
Handlers are plain callables. The synthetic ``proof_bundle()`` exercises a
deterministic ``echo`` tool; the *real* committed ``sdk_skill_bundle()`` (see
:mod:`lingtai_sdk.sdk_skill`) exercises all three surfaces against a shipped
asset. This module imports only the import-pure ``capabilities`` and ``errors``
siblings, so ``import lingtai_sdk.capability_host`` pulls in no wrapper or
provider SDK.
"""
from __future__ import annotations

from typing import Any, Callable, Mapping

from .capabilities import BundleManifest, proof_bundle
from .errors import BundleHostError

ToolHandler = Callable[..., Any]
ResourceHandler = Callable[[], Any]
PromptHandler = Callable[..., Any]


def _check_contract(
    name: str, surface: str, declared: tuple[str, ...], handlers: Mapping[str, Any]
) -> None:
    """Enforce declared-names ↔ provided-handlers parity for one surface."""
    declared_set = set(declared)
    provided = set(handlers)
    missing = declared_set - provided
    extra = provided - declared_set
    if missing:
        raise BundleHostError(
            f"bundle {name!r} declares {surface} with no handler: {sorted(missing)}"
        )
    if extra:
        raise BundleHostError(
            f"bundle {name!r} has {surface} handlers for undeclared names: "
            f"{sorted(extra)}"
        )
    non_callable = sorted(n for n, fn in handlers.items() if not callable(fn))
    if non_callable:
        raise BundleHostError(
            f"bundle {name!r} has non-callable {surface} handlers: {non_callable}"
        )


class BundleHost:
    """An in-process host for a single validated, non-privileged bundle.

    Registers ``manifest`` together with handler mappings covering exactly the
    manifest's declared surfaces:

    * ``tools`` — invoked via :meth:`invoke` (``name → callable(**kwargs)``);
    * ``resources`` — read via :meth:`read_resource` (``name → callable()``);
    * ``prompts`` — rendered via :meth:`read_prompt` (``name → callable(**kwargs)``).

    Construction is where every boundary check happens, so a constructed host is
    always safe to invoke.
    """

    def __init__(
        self,
        manifest: BundleManifest,
        handlers: Mapping[str, ToolHandler],
        *,
        resources: Mapping[str, ResourceHandler] | None = None,
        prompts: Mapping[str, PromptHandler] | None = None,
    ) -> None:
        resources = resources or {}
        prompts = prompts or {}

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
                "BundleHost only hosts in_process bundles"
            )

        _check_contract(manifest.name, "tools", manifest.surfaces.tools, handlers)
        _check_contract(
            manifest.name, "resources", manifest.surfaces.resources, resources
        )
        _check_contract(manifest.name, "prompts", manifest.surfaces.prompts, prompts)

        self._manifest = manifest
        self._handlers: dict[str, ToolHandler] = dict(handlers)
        self._resources: dict[str, ResourceHandler] = dict(resources)
        self._prompts: dict[str, PromptHandler] = dict(prompts)

    @property
    def manifest(self) -> BundleManifest:
        return self._manifest

    @property
    def tools(self) -> tuple[str, ...]:
        """The tool names this host can invoke (the manifest's declared tools)."""
        return self._manifest.surfaces.tools

    @property
    def resources(self) -> tuple[str, ...]:
        """The resource names this host can read (the manifest's declared resources)."""
        return self._manifest.surfaces.resources

    @property
    def prompts(self) -> tuple[str, ...]:
        """The prompt names this host can render (the manifest's declared prompts)."""
        return self._manifest.surfaces.prompts

    def invoke(self, tool: str, **kwargs: Any) -> Any:
        """Invoke a declared tool by name. Unknown tools raise ``BundleHostError``."""
        handler = self._handlers.get(tool)
        if handler is None:
            raise BundleHostError(
                f"bundle {self._manifest.name!r} does not host tool {tool!r}; "
                f"available: {sorted(self._handlers)}"
            )
        return handler(**kwargs)

    def read_resource(self, name: str) -> Any:
        """Read a declared resource by name. Unknown names raise ``BundleHostError``."""
        handler = self._resources.get(name)
        if handler is None:
            raise BundleHostError(
                f"bundle {self._manifest.name!r} does not host resource {name!r}; "
                f"available: {sorted(self._resources)}"
            )
        return handler()

    def read_prompt(self, name: str, **kwargs: Any) -> Any:
        """Render a declared prompt by name. Unknown names raise ``BundleHostError``."""
        handler = self._prompts.get(name)
        if handler is None:
            raise BundleHostError(
                f"bundle {self._manifest.name!r} does not host prompt {name!r}; "
                f"available: {sorted(self._prompts)}"
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
    "ResourceHandler",
    "PromptHandler",
    "BundleHost",
    "proof_host",
]
