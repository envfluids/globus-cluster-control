from gcx import config, skill


def test_skill_lists_each_cluster_and_its_policy(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "cfg")
    monkeypatch.setattr(skill, "SKILL_DIR", tmp_path / "skills" / "gcx")
    config.save("delta", {"endpoint": "e", "keepalive": {"mode": "on-use"}, "policy": {
        "scheduler": "slurm", "capabilities": ["status", "submit", "read"],
        "read_roots": ["~", "/work/nvme/bdiu/$USER"], "script_roots": ["~"],
        "accounts": ["bdiu-delta-gpu"], "queues": ["gpuA40x4"]}})
    config.save("polaris", {"endpoint": "p", "keepalive": {"mode": "on-use"},
                            "policy": {"scheduler": "pbs", "capabilities": ["status"]}})
    path = skill.install(quiet=True)
    text = path.read_text()
    assert text.startswith("---\nname: gcx\ndescription: ") and "(delta, polaris)" in text
    assert "### delta (NCSA Delta, slurm)" in text and "`/work/nvme/bdiu/$USER`" in text
    assert "`bdiu-delta-gpu`" in text and "`sh 'CMD'`" not in text
    assert "### polaris (ALCF Polaris, pbs)" in text and "gcx login polaris" in text
    assert "Never run the commands that need the human" in text
    assert skill.install(quiet=True).read_text() == text  # idempotent


def test_skill_describes_run_launchers(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "cfg")
    config.save("midway3", {"endpoint": "e", "keepalive": {"mode": "failover"}, "policy": {
        "scheduler": "slurm", "capabilities": ["status", "run"], "accounts": ["pi-a"],
        "queues": ["caslake"], "launchers": [{"root": "~/repo", "argv": ["uv", "run", "s.py"]}]}})
    text = skill.render()
    assert "`run [-A ACCT]" in text and "`~/repo` (launcher `uv run s.py`)" in text
    assert "`pi-a`" in text and "Submit scripts from" not in text


def test_run_author_skill_is_written_with_each_clusters_launchers(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "cfg")
    monkeypatch.setattr(skill, "SKILL_DIR", tmp_path / "skills" / "gcx")
    config.save("midway3", {"endpoint": "e", "policy": {
        "scheduler": "slurm", "capabilities": ["status", "run"], "accounts": ["pi-a"],
        "queues": ["caslake"], "qos": ["q1"],
        "launchers": [{"root": "~/repo", "argv": ["cat"], "timeout": 60}]}})
    config.save("delta", {"endpoint": "d", "policy": {"scheduler": "slurm",
                                                       "capabilities": ["status"]}})
    config.save("polaris", {"endpoint": "p", "policy": {"scheduler": "pbs",
                                                         "capabilities": ["status"]}})
    skill.install(quiet=True)
    text = (tmp_path / "skills" / "gcx-run-author" / "SKILL.md").read_text()
    assert text.startswith("---\nname: gcx-run-author\ndescription: ")
    assert "**midway3**: launchers `~/repo` (`cat`, 60 s)." in text
    assert "Accounts: `pi-a`; partitions: `caslake`; QoS: `q1`." in text
    assert "**delta**: `run` is off" in text and "gcx setup delta --menu" in text
    assert "**polaris** (pbs): `run` is Slurm-only" in text
    assert "never run it yourself" in text and "--dry-run -- <command>" in text
    usage = (tmp_path / "skills" / "gcx" / "SKILL.md").read_text()
    assert "run scripts with arguments" in usage and "`gcx-run-author` skill" in usage
    assert "`gcx launcher add`/`remove`" in usage
