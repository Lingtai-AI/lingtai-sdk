"""The committed ``lingtai-sdk-skill`` asset, its bundle manifest, and host.

Stage 6 of the SDK migration: a *real, low-risk* CapabilityBundle adoption. Where
:func:`lingtai_sdk.capabilities.proof_bundle` was a synthetic metadata-only echo,
this module ships a **committed asset** — the ``lingtai-sdk-skill`` ``SKILL.md``
under :mod:`lingtai_sdk.assets` — and expresses it as a non-privileged bundle
hosted in process:

    asset (SKILL.md, importlib.resources)
       -> load_sdk_skill()        # read the asset text
       -> sdk_skill_bundle()      # declare a BundleManifest over it
       -> sdk_skill_host()        # BundleHost exposing tool/resource/prompt
       -> read_resource / invoke / read_prompt   # deterministic, network-free

The skill is the top-level SDK *observation entry* (architecture-foundation §7):
it explains the SDK/kernel/wrapper split, the runtime contract, the
CapabilityBundle contract, and the privileged-core deferral, so a later
coding-agent or system prompt can point at one stable, skill-shaped surface.

The bundle is deliberately **non-privileged, replaceable, ``in_process``** and
read-only. The privileged core bundles (``system`` / ``psyche`` / ``soul``) are
NOT migrated here and ``BundleHost`` would refuse them anyway.

Import purity: this module imports only :mod:`importlib.resources` and the
import-pure ``.capabilities`` / ``.capability_host`` siblings. It does NOT import
the ``lingtai`` wrapper or any provider SDK at module load.
"""
from __future__ import annotations

from importlib import resources

from .capabilities import (
    BackendReplaceability,
    BundleManifest,
    CapabilitySurfaces,
    RoleFlags,
    SecurityPolicy,
    TransportSpec,
)
from .capability_host import BundleHost

# The asset's package-relative location. ``importlib.resources`` resolves this
# whether the SDK is run from a source checkout or an installed wheel.
_SKILL_PACKAGE = "lingtai_sdk.assets"
_SKILL_DIR = "lingtai-sdk-skill"
_SKILL_FILE = "SKILL.md"

#: The manifest-relative manual pointer recorded in ``BundleManifest.manual``.
SKILL_MANUAL_PATH = f"{_SKILL_DIR}/{_SKILL_FILE}"


def load_sdk_skill() -> str:
    """Read the committed ``lingtai-sdk-skill`` ``SKILL.md`` text.

    Resolves the asset through :mod:`importlib.resources` (not a raw filesystem
    path), so it works identically from a source checkout and an installed
    wheel. Network-free and deterministic.
    """
    asset = resources.files(_SKILL_PACKAGE).joinpath(_SKILL_DIR, _SKILL_FILE)
    return asset.read_text(encoding="utf-8")


def sdk_skill_bundle() -> BundleManifest:
    """Declare the non-privileged ``lingtai-sdk-skill`` CapabilityBundle.

    A real, low-risk bundle (not a synthetic proof): it declares one read-only
    tool (``read_sdk_skill``), one resource (``sdk_skill``), and one prompt
    (``sdk_skill_orientation``), all backed by the committed skill asset, with
    no privileges and free backend-replaceability over an ``in_process``
    transport. ``manual`` points at the shipped ``SKILL.md``.
    """
    return BundleManifest(
        name="lingtai_sdk_skill",
        version="0.1.0",
        summary=(
            "Top-level SDK observation entry: a read-only skill describing the "
            "SDK/kernel/wrapper split, the runtime and CapabilityBundle "
            "contracts, and the privileged-core deferral."
        ),
        roles=RoleFlags(
            required=False,
            privileged=False,
            native_only=False,
            can_override=False,
            backend_replaceability=BackendReplaceability.REPLACEABLE,
        ),
        surfaces=CapabilitySurfaces(
            tools=("read_sdk_skill",),
            resources=("sdk_skill",),
            prompts=("sdk_skill_orientation",),
        ),
        security=SecurityPolicy(danger="safe"),
        transport=TransportSpec(kind="in_process"),
        manual=(SKILL_MANUAL_PATH,),
        metadata={"asset": SKILL_MANUAL_PATH, "read_only": True},
    )


def _read_sdk_skill_tool() -> dict[str, str]:
    """The bundle's read-only tool: return the skill text as a payload."""
    return {"name": SKILL_MANUAL_PATH, "text": load_sdk_skill()}


def _sdk_skill_resource() -> str:
    """The bundle's resource handler: the raw skill text."""
    return load_sdk_skill()


def _sdk_skill_orientation_prompt(audience: str = "coding agent") -> str:
    """The bundle's prompt handler: a short, deterministic orientation header.

    Renders a stable one-paragraph pointer at the skill for the given
    ``audience``. Pure text assembly — no I/O beyond reading the committed
    asset's title line.
    """
    return (
        f"You are a {audience} orienting to the LingTai SDK. Read the "
        f"{SKILL_MANUAL_PATH!r} skill to learn the SDK/kernel/wrapper split, the "
        "runtime contract, and the CapabilityBundle contract before touching "
        "code. The privileged core bundles (system/psyche/soul) are not part of "
        "the SDK skill bundle and must not be migrated here."
    )


def sdk_skill_host() -> BundleHost:
    """A ready :class:`BundleHost` for :func:`sdk_skill_bundle`.

    Wires the declared tool/resource/prompt to deterministic, network-free
    handlers reading the committed asset. The end-to-end Stage 6 adoption:
    ``sdk_skill_host().read_resource("sdk_skill")`` returns the ``SKILL.md`` text;
    ``invoke("read_sdk_skill")`` returns it wrapped with its manual path; and
    ``read_prompt("sdk_skill_orientation")`` renders the orientation header.
    """
    return BundleHost(
        sdk_skill_bundle(),
        handlers={"read_sdk_skill": _read_sdk_skill_tool},
        resources={"sdk_skill": _sdk_skill_resource},
        prompts={"sdk_skill_orientation": _sdk_skill_orientation_prompt},
    )


__all__ = [
    "SKILL_MANUAL_PATH",
    "load_sdk_skill",
    "sdk_skill_bundle",
    "sdk_skill_host",
]
