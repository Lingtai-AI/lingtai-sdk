"""CapabilityBundle host boundary.

Where :mod:`lingtai_sdk.capabilities` is the public *schema* (what a bundle
declares), this module is the smallest possible *host* for one: it takes a
validated :class:`~lingtai_sdk.capabilities.BundleManifest` plus name→callable
handler mappings and proves the manifest/load/host boundary —

    declared manifest -> load_manifest() -> host -> invoke / read_*(...)

There are two hosts, sharing one boundary contract but differing in *which*
bundles they will accept:

* :class:`BundleHost` is the **non-native** host. It **refuses privileged /
  native-only** bundles — those may only be hosted by the native runtime, never
  by this in-process host — and hosts only ``in_process`` transports. This is
  exactly why the core ``system`` / ``psyche`` / ``soul`` bundles are *not*
  migrated here.
* :class:`NativeBundleHost` is the **native-authority** host. It may host a
  privileged / native-only bundle, but only when **explicitly constructed as
  native authority** (``native_authority=True``) and only for a ``native``
  transport. It is the conservative seam through which a future stage could
  declare the privileged core; this stage hosts only a harmless synthetic
  ``native_privileged_proof`` — no real privileged surface is named here.

Both hosts share the rest of the contract:

* They **validate** the manifest on registration (a host never trusts an
  unvalidated declaration).
* They enforce the **manifest ↔ implementation contract** per surface: every
  declared ``surfaces.tools`` / ``surfaces.resources`` / ``surfaces.prompts``
  name has a callable handler, and no handler is undeclared.
* They host three read-only surfaces — **tools** (:meth:`invoke`),
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

from .capabilities import (
    BackendReplaceability,
    BundleManifest,
    CapabilitySurfaces,
    RoleFlags,
    TransportKind,
    TransportSpec,
    proof_bundle,
)
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


class _BaseBundleHost:
    """Shared host boundary for one validated bundle.

    Holds everything common to the non-native and native hosts: manifest
    validation on registration, the per-surface declared↔provided handler
    contract, the handler storage, and the three read-only surfaces
    (:meth:`invoke` / :meth:`read_resource` / :meth:`read_prompt`).

    Subclasses differ only in *admission* — which manifests they accept — via
    :meth:`_admit`, called after ``validate()`` and before any handler is
    stored. A subclass that wants to refuse a bundle raises ``BundleHostError``
    from ``_admit``; the base never stores handlers for a refused manifest.
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

        self._admit(manifest)

        _check_contract(manifest.name, "tools", manifest.surfaces.tools, handlers)
        _check_contract(
            manifest.name, "resources", manifest.surfaces.resources, resources
        )
        _check_contract(manifest.name, "prompts", manifest.surfaces.prompts, prompts)

        self._manifest = manifest
        self._handlers: dict[str, ToolHandler] = dict(handlers)
        self._resources: dict[str, ResourceHandler] = dict(resources)
        self._prompts: dict[str, PromptHandler] = dict(prompts)

    def _admit(self, manifest: BundleManifest) -> None:
        """Accept or refuse ``manifest``. Override to enforce a host's policy."""
        raise NotImplementedError

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


class BundleHost(_BaseBundleHost):
    """An in-process host for a single validated, **non-privileged** bundle.

    Registers ``manifest`` together with handler mappings covering exactly the
    manifest's declared surfaces (tools / resources / prompts). It **refuses**
    any ``privileged`` / ``native_only`` bundle (only a native authority may host
    those) and hosts only ``in_process`` transports — the guardrail that keeps
    the privileged core (``system`` / ``psyche`` / ``soul``) out of this host.

    Construction is where every boundary check happens, so a constructed host is
    always safe to invoke.
    """

    def _admit(self, manifest: BundleManifest) -> None:
        if manifest.roles.privileged or manifest.roles.native_only:
            raise BundleHostError(
                f"refusing to host privileged/native-only bundle "
                f"{manifest.name!r}: only the native runtime may host it"
            )
        if manifest.transport.kind != TransportKind.IN_PROCESS.value:
            raise BundleHostError(
                f"refusing to host bundle {manifest.name!r} with "
                f"transport {manifest.transport.kind!r}: "
                "BundleHost only hosts in_process bundles"
            )


class NativeBundleHost(_BaseBundleHost):
    """A native-authority host that may host a **privileged / native-only** bundle.

    This is the conservative counterpart to :class:`BundleHost`. It accepts a
    privileged (and/or ``native_only``) bundle, but only when:

    * it is **explicitly constructed as native authority** —
      ``native_authority=True`` (keyword-only, defaulting to ``False``, so
      privileged hosting is never granted by accident); and
    * the manifest's ``transport.kind`` is ``native``.

    Everything else — manifest validation and the per-surface declared↔provided
    handler contract — is identical to :class:`BundleHost`. It is the seam a
    future stage could use to declare the privileged core; *this* stage hosts
    only a harmless synthetic ``native_privileged_proof`` and names no real
    privileged surface. The host never relaxes the contract: a privileged bundle
    with a missing/undeclared/non-callable handler is still refused.
    """

    def __init__(
        self,
        manifest: BundleManifest,
        handlers: Mapping[str, ToolHandler],
        *,
        resources: Mapping[str, ResourceHandler] | None = None,
        prompts: Mapping[str, PromptHandler] | None = None,
        native_authority: bool = False,
    ) -> None:
        self._native_authority = native_authority
        super().__init__(
            manifest, handlers, resources=resources, prompts=prompts
        )

    def _admit(self, manifest: BundleManifest) -> None:
        if not self._native_authority:
            raise BundleHostError(
                f"refusing to host bundle {manifest.name!r}: NativeBundleHost "
                "must be explicitly constructed with native_authority=True to "
                "host a privileged/native-only bundle"
            )
        if manifest.transport.kind != TransportKind.NATIVE.value:
            raise BundleHostError(
                f"refusing to host bundle {manifest.name!r} with "
                f"transport {manifest.transport.kind!r}: "
                "NativeBundleHost only hosts native bundles"
            )


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


def native_privileged_proof_bundle() -> BundleManifest:
    """A harmless, native-proof **privileged** manifest exercising native hosting.

    Deliberately NOT one of the core bundles (``system`` / ``psyche`` / ``soul``).
    It is privileged + ``native_only`` with a ``native`` transport and a single
    deterministic ``native_noop`` tool — the lowest-risk surface that proves the
    *native-authority* hosting boundary end to end, the privileged-side mirror of
    :func:`~lingtai_sdk.capabilities.proof_bundle`.
    """
    return BundleManifest(
        name="native_privileged_proof",
        version="0.0.1",
        summary="Synthetic native-proof privileged bundle for the SDK foundation.",
        roles=RoleFlags(
            required=False,
            privileged=True,
            native_only=True,
            can_override=False,
            backend_replaceability=BackendReplaceability.NATIVE_ONLY,
        ),
        surfaces=CapabilitySurfaces(tools=("native_noop",)),
        transport=TransportSpec(kind=TransportKind.NATIVE.value),
        metadata={"proof": True, "native": True},
    )


def _native_noop() -> dict[str, bool]:
    """The native proof bundle's single tool: deterministic, pure, network-free."""
    return {"native_noop": True}


def native_proof_host() -> NativeBundleHost:
    """A ready :class:`NativeBundleHost` for ``native_privileged_proof_bundle()``.

    The privileged-side end-to-end proof: a privileged, ``native_only``,
    ``native``-transport bundle hosted *only* because the host is constructed
    with ``native_authority=True``. ``native_proof_host().invoke("native_noop")``
    returns ``{"native_noop": True}`` with no I/O of any kind. No real privileged
    surface is named — ``system`` / ``psyche`` / ``soul`` are not migrated here.
    """
    return NativeBundleHost(
        native_privileged_proof_bundle(),
        {"native_noop": _native_noop},
        native_authority=True,
    )


__all__ = [
    "ToolHandler",
    "ResourceHandler",
    "PromptHandler",
    "BundleHost",
    "NativeBundleHost",
    "proof_host",
    "native_privileged_proof_bundle",
    "native_proof_host",
]
