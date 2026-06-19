"""Always-on agent floor: file I/O, knowledge, skills, daemon, avatar, bash.

These capabilities form the baseline every functional agent uses. They are
discovered through the registry in ``lingtai.capabilities.__init__`` (which points
at this subpackage by absolute path), so dispatch and group expansion logic stays
unchanged. ``knowledge`` is the canonical private durable memory capability; the former
``library`` and ``codex`` durable-memory aliases are removed. This package exists to make the always-on tier
visible in the import graph; it has no behavior of its own.
"""
