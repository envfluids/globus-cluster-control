import json

import pytest

from gcx import config


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "gcx")
    monkeypatch.setattr(config, "LEGACY", tmp_path / "gc-endpoints.json")
    return tmp_path


def test_new_layout_wins_over_legacy(dirs):
    config.LEGACY.write_text(json.dumps({"midway3": "old-uuid"}))
    config.save("midway3", {"endpoint": "new-uuid"})
    assert config.load("midway3") == {"endpoint": "new-uuid"}


def test_legacy_string_and_dict_entries(dirs):
    config.LEGACY.write_text(json.dumps({"a": "uuid-a", "b": {"endpoint": "uuid-b", "state": "s"}}))
    assert config.load("a") == {"endpoint": "uuid-a"}
    assert config.load("b")["state"] == "s"
    assert config.configured() == ["a", "b"]


def test_unconfigured_cluster_raises(dirs):
    with pytest.raises(config.NotConfigured):
        config.load("polaris")


def test_laptop_home_mapped_back_to_cluster_home(monkeypatch, tmp_path):
    from gcx import cli
    monkeypatch.setattr(cli.Path, "home", lambda: tmp_path)
    assert cli._cluster_path(f"{tmp_path}/gc-endpoint") == "~/gc-endpoint"
    assert cli._cluster_path(str(tmp_path)) == "~"
    assert cli._cluster_path(f"{tmp_path}x/y") == f"{tmp_path}x/y"  # not under home
    assert cli._cluster_path("/scratch/midway3/u") == "/scratch/midway3/u"
