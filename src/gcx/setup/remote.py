"""SSH access used by `gcx setup` (setup, not day-to-day use).

Everything rides the ControlMaster socket for the alias, with BatchMode so a
missing socket fails fast instead of hanging on an MFA prompt. Only
`interactive()` allocates a terminal, for steps where the human must type.
"""

import re
import shlex
import subprocess
from importlib import resources

SAFE_ROOT = re.compile(r"^~(/[\w.-]+)+$")   # install roots live under the cluster home
SAFE_NAME = re.compile(r"^[\w.-]+$")
SAFE_VAR = re.compile(r"^[A-Z_][A-Z0-9_]*$")


class RemoteError(Exception):
    pass


class Remote:
    def __init__(self, alias, home=None):
        self.alias = alias
        self.home = home  # absolute cluster home, known after probe()

    def run(self, cmd, stdin=None, check=True, timeout=300):
        p = subprocess.run(["ssh", "-o", "BatchMode=yes", self.alias, cmd], input=stdin,
                           capture_output=True, text=True, timeout=timeout)
        if check and p.returncode:
            raise RemoteError(f"ssh {self.alias}: `{cmd[:80]}` failed (rc {p.returncode}): "
                              f"{p.stderr.strip()[-400:]}")
        return p.stdout

    def reachable(self):
        try:
            return subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
                                   self.alias, "true"], capture_output=True,
                                  timeout=60).returncode == 0
        except subprocess.TimeoutExpired:
            return False

    def interactive(self, cmd, host=None):
        """Run with a terminal (MFA prompts, login URLs). Returns the exit code."""
        return subprocess.call(["ssh", "-t", host or self.alias, cmd])

    def abs(self, path):
        """~/x -> /home/u/x, so paths can be quoted safely."""
        if path == "~" or path.startswith("~/"):
            if not self.home:
                raise RemoteError("cluster home unknown; probe first")
            return self.home + path[1:]
        return path

    def q(self, path):
        return shlex.quote(self.abs(path))

    def read(self, path):
        out = self.run(f"cat {self.q(path)} 2>/dev/null || printf '\\0MISSING'")
        return None if out == "\0MISSING" else out

    def write(self, path, text, mode=None):
        p = self.q(path)
        tmp = shlex.quote(self.abs(path) + ".gcx-new")
        chmod = f" && chmod {mode} {tmp}" if mode else ""
        self.run(f"mkdir -p $(dirname {p}) && cat > {tmp}{chmod} && mv {tmp} {p}", stdin=text)

    def probe(self, root, endpoint_name, env_vars=()):
        """Run probe.sh in one round trip; returns its key=value facts.

        env_vars: login-shell variables whose values to report (env_<NAME>).
        """
        if not SAFE_ROOT.match(root) or not SAFE_NAME.match(endpoint_name):
            raise RemoteError(f"unsafe install root {root!r} or endpoint name {endpoint_name!r}")
        if not all(SAFE_VAR.match(v) for v in env_vars):
            raise RemoteError(f"unsafe variable names {list(env_vars)!r}")
        script = resources.files("gcx.setup").joinpath("probe.sh").read_text()
        head = (f'GCX_ROOT="$HOME{root[1:]}"\nGCX_EP={endpoint_name}\n'
                f'GCX_ENV_VARS="{" ".join(env_vars)}"\n')
        facts = {}
        for line in self.run("bash -s", stdin=head + script).splitlines():
            k, sep, v = line.partition("=")
            if sep:
                facts[k] = v
        self.home = facts.get("home") or self.home
        return facts
