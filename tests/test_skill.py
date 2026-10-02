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
