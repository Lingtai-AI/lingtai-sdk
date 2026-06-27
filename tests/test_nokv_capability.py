from __future__ import annotations

from pathlib import Path

from lingtai.agent import Agent
from tests._service_helpers import make_gemini_mock_service as make_mock_service


class FakeNoKVClient:
    def __init__(self):
        self.objects = {
            "/lingtai/projects/p/artifacts/a.md": {
                "content": "alpha\nneedle\n",
                "generation": "g1",
                "metadata": {"producer": "test"},
            },
            "/lingtai/projects/p/reports/r.md": {
                "content": "report body\n",
                "generation": "g2",
                "metadata": {"producer": "test"},
            },
        }

    def list(self, path: str):
        prefix = path.rstrip("/") + "/"
        return [
            {"path": key, "generation": value["generation"], "metadata": value["metadata"]}
            for key, value in sorted(self.objects.items())
            if key == path or key.startswith(prefix)
        ]

    def read(self, path: str):
        return self.objects[path]

    def stat(self, path: str):
        value = self.objects[path]
        return {"path": path, "generation": value["generation"], "metadata": value["metadata"]}

    def snapshot(self, path: str):
        value = self.objects[path]
        return {"path": path, "generation": value["generation"]}


def _mk_agent(tmp_path: Path, nokv_kwargs: dict):
    return Agent(
        service=make_mock_service(),
        agent_name="test",
        working_dir=tmp_path / "agent",
        capabilities={"nokv": nokv_kwargs},
    )


def test_nokv_capability_registers_explicit_tool_and_lists_namespace(tmp_path):
    agent = _mk_agent(tmp_path, {"client": FakeNoKVClient()})
    try:
        result = agent._tool_handlers["nokv"](
            {"action": "ls", "path": "nokv://lingtai/projects/p"}
        )

        assert result["status"] == "ok"
        assert result["action"] == "ls"
        assert [entry["path"] for entry in result["entries"]] == [
            "nokv://lingtai/projects/p/artifacts/a.md",
            "nokv://lingtai/projects/p/reports/r.md",
        ]
    finally:
        agent.stop(timeout=1.0)


def test_nokv_capability_reads_greps_and_reports_snapshot_generations(tmp_path):
    agent = _mk_agent(tmp_path, {"client": FakeNoKVClient()})
    try:
        read = agent._tool_handlers["nokv"](
            {"action": "read", "path": "nokv://lingtai/projects/p/artifacts/a.md"}
        )
        grep = agent._tool_handlers["nokv"](
            {"action": "grep", "path": "nokv://lingtai/projects/p", "pattern": "needle"}
        )
        snapshot = agent._tool_handlers["nokv"](
            {"action": "snapshot", "path": "nokv://lingtai/projects/p/artifacts/a.md"}
        )

        assert read["content"] == "alpha\nneedle\n"
        assert read["generation"] == "g1"
        assert grep["matches"] == [
            {
                "path": "nokv://lingtai/projects/p/artifacts/a.md",
                "line": 2,
                "text": "needle",
                "generation": "g1",
                "metadata": {"producer": "test"},
            }
        ]
        assert snapshot["snapshot"]["generation"] == "g1"
    finally:
        agent.stop(timeout=1.0)


def test_nokv_capability_reports_actionable_error_when_unconfigured(tmp_path):
    agent = _mk_agent(tmp_path, {"enabled": True})
    try:
        result = agent._tool_handlers["nokv"](
            {"action": "ls", "path": "nokv://lingtai/projects/p"}
        )

        assert result["status"] == "error"
        assert "NoKV is not configured" in result["message"]
    finally:
        agent.stop(timeout=1.0)


def test_nokv_capability_does_not_change_default_local_file_io(tmp_path):
    agent = _mk_agent(tmp_path, {"client": FakeNoKVClient()})
    try:
        agent._file_io.write("local.txt", "local")

        assert agent._file_io.read("local.txt") == "local"
    finally:
        agent.stop(timeout=1.0)
