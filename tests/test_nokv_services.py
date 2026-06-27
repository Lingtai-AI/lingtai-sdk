from __future__ import annotations

from dataclasses import dataclass

import pytest

from lingtai.agent import Agent
from lingtai.services.file_io import (
    GrepMatch,
    HybridFileIOBackend,
    LocalFileIOBackend,
    LocalFileIOService,
    NoKVFileIOBackend,
    NoKVUnsupportedError,
)
from lingtai.services.nokv import classify_lingtai_subtree
from tests._service_helpers import make_gemini_mock_service as make_mock_service


@dataclass
class FakeObject:
    content: str
    generation: str
    metadata: dict


class FakeNoKVClient:
    def __init__(self):
        self.objects: dict[str, FakeObject] = {}
        self.write_calls: list[tuple[str, str, dict | None]] = []

    def read(self, path: str) -> dict:
        obj = self.objects[path]
        return {
            "content": obj.content,
            "generation": obj.generation,
            "metadata": obj.metadata,
        }

    def write(self, path: str, content: str, metadata: dict | None = None) -> dict:
        generation = f"gen-{len(self.write_calls) + 1}"
        self.objects[path] = FakeObject(content, generation, metadata or {})
        self.write_calls.append((path, content, metadata))
        return {"path": path, "generation": generation}

    def list(self, path: str) -> list[dict]:
        prefix = path.rstrip("/") + "/"
        return [
            {"path": obj_path, "generation": obj.generation, "metadata": obj.metadata}
            for obj_path, obj in sorted(self.objects.items())
            if obj_path == path or obj_path.startswith(prefix)
        ]

    def stat(self, path: str) -> dict:
        obj = self.objects[path]
        return {"path": path, "generation": obj.generation, "metadata": obj.metadata}

    def snapshot(self, path: str) -> dict:
        return {"path": path, "generation": self.objects[path].generation}


def test_selected_subtree_policy_allows_outputs_and_keeps_runtime_state_local():
    assert (
        classify_lingtai_subtree(".lingtai/alice/artifacts/report.md")
        == "nokv-candidate"
    )
    assert (
        classify_lingtai_subtree(".lingtai/alice/checkpoints/rank0.pt")
        == "nokv-candidate"
    )
    assert classify_lingtai_subtree(".lingtai/alice/mailbox/inbox/msg.json") == "local-runtime"
    assert classify_lingtai_subtree(".lingtai/alice/logs/events.jsonl") == "local-runtime"
    assert classify_lingtai_subtree(".lingtai/alice/.agent.heartbeat") == "local-runtime"


def test_hybrid_file_io_routes_local_paths_to_local_backend(tmp_path):
    fake = FakeNoKVClient()
    backend = HybridFileIOBackend(
        local_backend=LocalFileIOBackend(root=tmp_path),
        nokv_backend=NoKVFileIOBackend(fake),
    )
    svc = LocalFileIOService(backend=backend)

    svc.write("local.txt", "local")

    assert svc.read("local.txt") == "local"
    assert fake.write_calls == []


def test_hybrid_file_io_routes_nokv_uris_to_nokv_backend(tmp_path):
    fake = FakeNoKVClient()
    backend = HybridFileIOBackend(
        local_backend=LocalFileIOBackend(root=tmp_path),
        nokv_backend=NoKVFileIOBackend(fake),
    )
    svc = LocalFileIOService(backend=backend)

    svc.write("nokv://project/artifacts/a.md", "hello\nneedle\n")

    assert fake.write_calls[0][0] == "/project/artifacts/a.md"
    assert svc.read("nokv://project/artifacts/a.md") == "hello\nneedle\n"
    assert svc.grep("needle", path="nokv://project") == [
        GrepMatch("nokv://project/artifacts/a.md", 2, "needle")
    ]


def test_nokv_file_io_edit_preserves_unique_replace_contract():
    fake = FakeNoKVClient()
    backend = NoKVFileIOBackend(fake)
    backend.write("nokv://project/reports/r.md", "alpha beta")

    assert backend.edit("nokv://project/reports/r.md", "beta", "gamma") == "alpha gamma"
    assert backend.read("nokv://project/reports/r.md") == "alpha gamma"

    with pytest.raises(ValueError, match="not found"):
        backend.edit("nokv://project/reports/r.md", "missing", "x")

    backend.write("nokv://project/reports/r.md", "dup dup")
    with pytest.raises(ValueError, match="appears 2 times"):
        backend.edit("nokv://project/reports/r.md", "dup", "x")


def test_hybrid_file_io_rejects_nokv_uri_when_backend_disabled(tmp_path):
    backend = HybridFileIOBackend(local_backend=LocalFileIOBackend(root=tmp_path))

    with pytest.raises(NoKVUnsupportedError, match="NoKV is not configured"):
        backend.read("nokv://project/artifacts/a.md")


def test_file_capability_handlers_preserve_nokv_uri_paths(tmp_path):
    fake = FakeNoKVClient()
    file_io = LocalFileIOService(
        backend=HybridFileIOBackend(
            local_backend=LocalFileIOBackend(root=tmp_path),
            nokv_backend=NoKVFileIOBackend(fake),
        )
    )
    agent = Agent(
        service=make_mock_service(),
        agent_name="test",
        working_dir=tmp_path / "agent",
        file_io=file_io,
        capabilities=["read", "write", "glob", "grep"],
    )
    try:
        write = agent._tool_handlers["write"]({
            "file_path": "nokv://project/artifacts/a.md",
            "content": "hello\nneedle\n",
        })
        read = agent._tool_handlers["read"]({"file_path": "nokv://project/artifacts/a.md"})
        grep = agent._tool_handlers["grep"]({
            "path": "nokv://project",
            "pattern": "needle",
        })
        glob = agent._tool_handlers["glob"]({
            "path": "nokv://project",
            "pattern": "**/*.md",
        })

        assert write["status"] == "ok"
        assert fake.write_calls[0][0] == "/project/artifacts/a.md"
        assert "needle" in read["content"]
        assert grep["matches"][0]["file"] == "nokv://project/artifacts/a.md"
        assert glob["matches"] == ["nokv://project/artifacts/a.md"]
    finally:
        agent.stop(timeout=1.0)
