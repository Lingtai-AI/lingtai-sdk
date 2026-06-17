"""LingTaiOptions: defaults, cwd alias, secret redaction, replace, serialization."""
from __future__ import annotations

from lingtai_sdk import LingTaiOptions, MCPHttpServerConfig, SystemPromptAssets


def test_defaults_are_all_none():
    o = LingTaiOptions()
    assert o.model is None
    assert o.provider is None
    assert o.working_dir is None
    assert o.capabilities is None
    assert o.mcp_servers is None


def test_cwd_alias_fills_working_dir():
    o = LingTaiOptions(cwd="/agents/a")
    assert str(o.working_dir) == "/agents/a"


def test_explicit_working_dir_wins_over_cwd():
    o = LingTaiOptions(working_dir="/explicit", cwd="/alias")
    assert str(o.working_dir) == "/explicit"


def test_repr_never_shows_api_key():
    o = LingTaiOptions(api_key="supersecret")
    assert "supersecret" not in repr(o)
    assert "api_key='set'" in repr(o)


def test_to_dict_redacts_api_key_by_default():
    o = LingTaiOptions(api_key="supersecret")
    assert o.to_dict()["api_key"] == "***"
    assert o.to_dict(redact=False)["api_key"] == "supersecret"


def test_to_dict_redacts_top_level_env_by_default():
    o = LingTaiOptions(env={"API_KEY": "supersecret"})
    assert o.to_dict()["env"] == {"API_KEY": "***"}
    assert o.to_dict(redact=False)["env"] == {"API_KEY": "supersecret"}


def test_to_dict_serializes_mcp_servers_with_redaction():
    o = LingTaiOptions(
        mcp_servers={"web": MCPHttpServerConfig(url="https://x", headers={"A": "tok"})}
    )
    d = o.to_dict()
    assert d["mcp_servers"]["web"]["headers"] == {"A": "***"}
    assert d["mcp_servers"]["web"]["url"] == "https://x"


def test_to_dict_serializes_system_prompt_assets():
    o = LingTaiOptions(system_prompt=SystemPromptAssets(covenant="be good", pad=""))
    d = o.to_dict()
    assert d["system_prompt"] == {"covenant": "be good"}


def test_replace_returns_modified_copy():
    o = LingTaiOptions(model="m1")
    o2 = o.replace(model="m2")
    assert o.model == "m1"
    assert o2.model == "m2"
    assert o is not o2


def test_system_prompt_assets_to_kwargs_skips_empty():
    assets = SystemPromptAssets(covenant="c", principle="", brief="b")
    assert assets.to_kwargs() == {"covenant": "c", "brief": "b"}
    assert SystemPromptAssets().is_empty()
