"""The registered source must run on whatever Python the endpoint has, not just
the laptop's. Exec it under the oldest and newest supported interpreters (uv
downloads them on first use; skipped without uv)."""

import json
import shutil
import subprocess

import pytest

from gcx.capabilities import build

PROBE = """
import json, os, sys
ns = {}
exec(sys.stdin.read(), ns)
home = os.path.expanduser("~")
open(home + "/gcx-compat.txt", "w").write("a\\nb\\n")
out = {
    "ping": ns["gcx_ping"]()["python"],
    "tail": ns["gcx_read"]("~/gcx-compat.txt", "tail", 1)["stdout"],
    "ls": [e["name"] for e in ns["gcx_ls"]("~")["entries"]],
    "refused": ns["gcx_read"]("/etc/passwd").get("refused"),
}
print(json.dumps(out))
"""


@pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv to fetch interpreters")
@pytest.mark.parametrize("version", ["3.9", "3.13"])
def test_source_runs_on_endpoint_pythons(version, tmp_path):
    text = build.source({"scheduler": "slurm", "capabilities": ["status", "read"],
                         "read_roots": ["~"]})
    p = subprocess.run(["uv", "run", "-q", "--no-project", "--python", version, "python",
                        "-c", PROBE], input=text, capture_output=True, text=True,
                       env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin:" + str(shutil.which("uv")).rsplit("/", 1)[0],
                            "USER": "alice"}, timeout=300)
    assert p.returncode == 0, p.stderr
    out = json.loads(p.stdout)
    assert out["ping"].startswith(version)
    assert out["tail"] == "b\n" and "gcx-compat.txt" in out["ls"] and out["refused"]
