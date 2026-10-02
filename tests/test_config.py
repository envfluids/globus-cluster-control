import json

import pytest

from gcx import config


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "gcx")
    return tmp_path


def test_save_then_load_and_list(dirs):
    config.save("midway3", {"endpoint": "uuid"})
    assert config.load("midway3") == {"endpoint": "uuid"}
    assert config.configured() == ["midway3"]


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
