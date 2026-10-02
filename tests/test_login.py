"""The bundled morning-login (`gcx login`), against a fake ssh that logs its
arguments. Never touches real sockets: HOME and PATH point into tmp_path."""

import stat
import subprocess
from importlib import resources
from pathlib import Path

SCRIPT = str(resources.files("gcx.data").joinpath("morning-login"))

FAKE_SSH = """#!/bin/bash
echo "$*" >> "$FAKE_LOG"
case "$*" in
  "-G "*) echo "hostname $2.example.org"; echo "controlpath $HOME/cm/$2" ;;
  "-O check "*) [ -e "$HOME/up.${@: -1}" ] ;;
  "-O exit "*) rm -f "$HOME/up.${@: -1}" ;;
  *-fNM*) touch "$HOME/up.${@: -1}" ;;
  *) exit 0 ;;
esac
"""


def run(tmp_path, args, clusters="polaris dsi", single_use="polaris", up=()):
    bin_ = tmp_path / "bin"
    bin_.mkdir(exist_ok=True)
    for name, body in {"ssh": FAKE_SSH, "dig": "#!/bin/bash\nexit 0\n"}.items():
        f = bin_ / name
        f.write_text(body)
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    for c in up:
        (tmp_path / f"up.{c}").touch()
    log = tmp_path / "ssh.log"
    log.write_text("")
    env = {"PATH": f"{bin_}:/usr/bin:/bin", "HOME": str(tmp_path), "FAKE_LOG": str(log),
           "GCX_CLUSTERS": clusters, "GCX_SINGLE_USE_MFA": single_use}
    p = subprocess.run(["bash", SCRIPT, *args], env=env, capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, timeout=60)
    return p.returncode, p.stdout + p.stderr, log.read_text()


def test_connects_configured_clusters(tmp_path):
    rc, out, log = run(tmp_path, [])
    assert rc == 0 and "polaris — connecting" in out and "dsi — connecting" in out
    assert "-fNM" in log and (tmp_path / "up.dsi").exists()


def test_already_connected_is_left_alone(tmp_path):
    rc, out, log = run(tmp_path, [], up=("polaris", "dsi"))
    assert out.count("already connected") == 2 and "-fNM" not in log


def test_refresh_sweep_skips_single_use_cluster_without_a_terminal(tmp_path):
    rc, out, log = run(tmp_path, ["--refresh"], up=("polaris", "dsi"))
    assert "skipped: refreshing polaris costs a single-use MFA token" in out
    assert "-O exit dsi" in log and "-O exit polaris" not in log


def test_naming_the_single_use_cluster_refreshes_it(tmp_path):
    rc, out, log = run(tmp_path, ["--refresh", "polaris"], up=("polaris",))
    assert "-O exit polaris" in log


def test_yes_refreshes_everything(tmp_path):
    rc, out, log = run(tmp_path, ["--refresh", "--yes"], up=("polaris", "dsi"))
    assert "-O exit polaris" in log and "-O exit dsi" in log


def test_no_clusters_configured(tmp_path):
    rc, out, log = run(tmp_path, [], clusters="")
    assert rc == 2 and "no clusters configured" in out
