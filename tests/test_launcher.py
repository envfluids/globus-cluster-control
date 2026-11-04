"""`gcx launcher`: add, replace and remove `run` launchers, and apply them the way
setup does. Setup itself and SSH are stubbed; the policy rules are the real ones."""

import pytest

from gcx import config, launcher
from gcx.setup import remote, steps

BASE = {"endpoint": "e", "ssh": "midway3", "policy": {
    "scheduler": "slurm", "capabilities": ["status", "run"], "accounts": ["pi-a"],
    "queues": ["caslake"], "launchers": [{"root": "~/repo", "argv": ["cat"]}]}}


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "cfg")
    config.save("midway3", BASE)
    applied = []
    monkeypatch.setattr(remote.Remote, "reachable", lambda self: True)
    monkeypatch.setattr(steps, "run", lambda cluster, **kw: applied.append((cluster, kw)) or 0)
    return applied


def launchers():
    return config.load("midway3")["policy"]["launchers"]


def test_add_defaults_to_cat_and_applies_with_setup(cfg):
    assert launcher.main(["add", "midway3", "~/other/"]) == 0
    assert launchers()[-1] == {"root": "~/other", "argv": ["cat"], "timeout": 120,
                               "block_risky": True}
    assert cfg == [("midway3", {"test_job": False, "offers": False})]


def test_command_after_double_dash_is_kept_verbatim(cfg):
    launcher.main(["add", "midway3", "~/r2", "--timeout", "30", "--",
                   "sh", "render.sh", "--timeout", "5", "--dry-run"])
    assert launchers()[-1]["argv"] == ["sh", "render.sh", "--timeout", "5", "--dry-run"]
    assert launchers()[-1]["timeout"] == 30


def test_laptop_expanded_home_is_mapped_back(cfg, monkeypatch, tmp_path):
    from gcx import cli
    monkeypatch.setattr(cli.Path, "home", lambda: tmp_path)
    launcher.main(["add", "midway3", f"{tmp_path}/proj"])
    assert launchers()[-1]["root"] == "~/proj"


def test_dry_run_changes_nothing(cfg, capsys):
    assert launcher.main(["add", "midway3", "~/r2", "--dry-run", "--", "sh", "x.sh"]) == 0
    assert len(launchers()) == 1 and cfg == []
    out = capsys.readouterr().out
    assert "would add ~/r2: sh x.sh (120 s)" in out and "  ~/repo: cat (120 s)" in out


def test_replace_and_remove(cfg):
    launcher.main(["add", "midway3", "~/r2"])
    with pytest.raises(SystemExit, match="already has a launcher"):
        launcher.main(["add", "midway3", "~/r2", "--", "sh", "x.sh"])
    launcher.main(["add", "midway3", "~/r2", "--replace", "--allow-risky", "--", "sh", "x.sh"])
    assert launchers()[-1] == {"root": "~/r2", "argv": ["sh", "x.sh"], "timeout": 120,
                               "block_risky": False}
    assert len(launchers()) == 2
    launcher.main(["remove", "midway3", "~/r2/"])
    assert [l["root"] for l in launchers()] == ["~/repo"]


@pytest.mark.parametrize("argv, msg", [
    (["remove", "midway3", "~/repo"], "only launcher"),
    (["remove", "midway3", "~/nope"], "no launcher for ~/nope"),
    (["add", "midway3", "/"], "not /"),
    (["add", "midway3", "relative/dir"], "absolute"),
    (["add", "midway3", "$SCRATCH/x"], "not set in the endpoint's environment"),
    (["add", "midway3", "~/r", "--timeout", "soon"], "not a number"),
    (["add", "midway3", "~/r", "--timeout", "0"], "not a number"),
])
def test_refusals_change_nothing(cfg, argv, msg):
    with pytest.raises(SystemExit, match=msg):
        launcher.main(argv)
    assert len(launchers()) == 1 and cfg == []


def test_run_must_already_be_on(cfg):
    config.save("midway3", dict(BASE, policy={"scheduler": "slurm", "capabilities": ["status"]}))
    with pytest.raises(SystemExit, match="gcx setup midway3 --menu"):
        launcher.main(["add", "midway3", "~/r"])
    assert cfg == []


def test_no_ssh_connection_changes_nothing(cfg, monkeypatch):
    monkeypatch.setattr(remote.Remote, "reachable", lambda self: False)
    with pytest.raises(SystemExit, match="gcx login midway3"):
        launcher.main(["add", "midway3", "~/r"])
    assert len(launchers()) == 1 and cfg == []


def test_list(cfg, capsys):
    launcher.main(["list", "midway3"])
    assert capsys.readouterr().out == "~/repo: cat (120 s)\n"
