from gcx import sshconfig

TOOLKIT = """# >>> hpc-agent-toolkit >>>
Host midway3
  HostName midway3.rcc.uchicago.edu
  User awikner
# <<< hpc-agent-toolkit <<<
"""
STAR = "Host *\n  User someone-else\n  IdentityFile ~/.ssh/id_ed25519\n"


def test_fresh_config_gets_fenced_aliases_and_node_aliases():
    new, skipped = sshconfig.render("", [("midway3", "alice")])
    assert new.startswith(sshconfig.BEGIN) and new.rstrip().endswith(sshconfig.END)
    assert "Host midway3\n" in new and "User alice" in new and "ControlPersist 24h" in new
    assert "Host midway3-login3\n" in new and "HostName midway3-login3.rcc.uchicago.edu" in new
    assert "ForwardAgent" not in new and skipped == []


def test_existing_alias_is_never_shadowed():
    new, skipped = sshconfig.render(TOOLKIT, [("midway3", "alice")])
    assert skipped == ["midway3"]
    assert new.count("Host midway3\n") == 1 and "Host midway3-login4\n" in new
    assert TOOLKIT.strip() in new  # untouched


def test_block_goes_above_host_star():
    new, _ = sshconfig.render("Host work\n  HostName w\n\n" + STAR, [("dsi", "alice")])
    assert new.index("Host dsi") < new.index("Host *")
    assert new.index("Host work") < new.index("Host dsi")


def test_rerun_is_idempotent_and_replaces_only_its_block():
    once, _ = sshconfig.render(STAR, [("dsi", "alice")])
    twice, _ = sshconfig.render(once, [("dsi", "alice")])
    assert once == twice
    changed, _ = sshconfig.render(once, [("dsi", "bob")])
    assert "User bob" in changed and "User alice" not in changed and changed.count(sshconfig.BEGIN) == 1


def test_polaris_persists_and_is_flagged():
    new, _ = sshconfig.render("", [("polaris", "alice")])
    assert "ControlPersist yes" in new and "single-use MFA" in new


def test_apply_writes_backup_and_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(sshconfig, "CONTROL_DIR", str(tmp_path / "cm"))
    cfg = tmp_path / "config"
    cfg.write_text(STAR)
    sshconfig.run([("dsi", "alice")], path=cfg, apply=False)
    assert cfg.read_text() == STAR  # dry run by default
    sshconfig.run([("dsi", "alice")], path=cfg, apply=True)
    assert "Host dsi" in cfg.read_text() and oct(cfg.stat().st_mode)[-3:] == "600"
    assert list(tmp_path.glob("config.bak.*")) and (tmp_path / "cm").is_dir()


def test_adding_a_cluster_keeps_the_others():
    first, _ = sshconfig.render("", [("dsi", "alice"), ("polaris", "apw")])
    second, _ = sshconfig.render(first, [("midway3", "alice")])
    assert "Host dsi\n" in second and "Host polaris\n" in second and "Host midway3\n" in second
    assert "User apw" in second  # polaris keeps its own username
    assert sshconfig.fenced_clusters(second) == {"dsi": "alice", "midway3": "alice", "polaris": "apw"}


def test_rendered_config_parses_with_openssh(tmp_path):
    import shutil
    import subprocess
    if not shutil.which("ssh"):
        return
    cfg = tmp_path / "config"
    cfg.write_text(sshconfig.render("Host *\n  User wrong\n", [("midway3", "alice")])[0])
    out = subprocess.run(["ssh", "-G", "-F", str(cfg), "midway3-login3"], capture_output=True,
                         text=True, stdin=subprocess.DEVNULL).stdout
    assert "hostname midway3-login3.rcc.uchicago.edu" in out and "user alice" in out
