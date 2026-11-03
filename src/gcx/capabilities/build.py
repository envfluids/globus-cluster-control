"""Turn runtime.py plus one cluster's policy into the source text to register.

The same text is registered once per entry function (each gets its own UUID
for the endpoint's allowlist). Only functions for enabled capabilities are
registered; the source also stays the same for a given policy, so its hash
tells whether re-registration is needed.
"""

import ast
import hashlib
import pprint
import re
from importlib import resources

# capability -> entry functions it enables. "status" is always on.
CAPABILITIES = {
    "status": ["gcx_ping", "gcx_jobs", "gcx_history", "gcx_queues"],
    "submit": ["gcx_submit", "gcx_cancel"],
    "run": ["gcx_run"],
    "read": ["gcx_ls", "gcx_read", "gcx_du"],
    "shell": ["gcx_shell"],
}
# Calls that change state on the cluster: submitted at-most-once.
MUTATING = {"gcx_submit", "gcx_cancel", "gcx_run", "gcx_shell"}

POLICY_LINE = re.compile(r"^POLICY = \{\}.*$", re.M)
SCHEDULERS = ("slurm", "pbs")
LAUNCHER_KEYS = {"root", "argv", "timeout", "block_risky"}
OLDEST_PYTHON = (3, 9)  # endpoint Pythons we must still parse on


class PolicyError(ValueError):
    pass


def validate(policy):
    if policy.get("scheduler") not in SCHEDULERS:
        raise PolicyError(f"scheduler must be one of {SCHEDULERS}")
    caps = policy.get("capabilities", [])
    unknown = set(caps) - set(CAPABILITIES)
    if unknown:
        raise PolicyError(f"unknown capabilities {sorted(unknown)}")
    if "status" not in caps:
        raise PolicyError("'status' is always on")
    if "read" in caps and not policy.get("read_roots"):
        raise PolicyError("'read' needs read_roots")
    if "submit" in caps and not policy.get("script_roots"):
        raise PolicyError("'submit' needs script_roots")
    roots = [(key, root) for key in ("read_roots", "script_roots") for root in policy.get(key, [])]
    if "run" in caps:
        _validate_run(policy)
        roots += [("launchers", launcher.get("root")) for launcher in policy["launchers"]]
    for key, root in roots:
        if not isinstance(root, str) or root in ("/", "") or not root.startswith(("/", "~", "$")):
            raise PolicyError(f"{key}: {root!r} must be an absolute, ~ or $VAR path, not /")


def _validate_run(policy):
    if policy["scheduler"] != "slurm":
        raise PolicyError("'run' is Slurm-only for now")
    if not policy.get("accounts") or not policy.get("queues"):
        raise PolicyError("'run' needs accounts and queues")
    launchers = policy.get("launchers")
    if not isinstance(launchers, list) or not launchers:
        raise PolicyError("'run' needs launchers")
    for launcher in launchers:
        if not isinstance(launcher, dict) or set(launcher) - LAUNCHER_KEYS:
            raise PolicyError(f"launcher {launcher!r}: keys must be among {sorted(LAUNCHER_KEYS)}")
        argv = launcher.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a for a in argv):
            raise PolicyError(f"launcher {launcher.get('root')!r}: argv must be a non-empty list of strings")
        if not isinstance(launcher.get("block_risky", True), bool):
            raise PolicyError(f"launcher {launcher.get('root')!r}: block_risky must be true or false")
        timeout = launcher.get("timeout", 120)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise PolicyError(f"launcher {launcher.get('root')!r}: timeout must be a positive number")


def entry_functions(policy):
    return [f for cap in policy["capabilities"] for f in CAPABILITIES[cap]]


def source(policy):
    validate(policy)
    text = resources.files("gcx.capabilities").joinpath("runtime.py").read_text()
    literal = pprint.pformat(policy, sort_dicts=True, width=88)
    try:
        same = ast.literal_eval(literal) == policy  # only plain literals round-trip
    except (ValueError, SyntaxError):
        same = False
    if not same:
        raise PolicyError("policy must hold only strings, numbers, lists and dicts")
    text, n = POLICY_LINE.subn(lambda m: "POLICY = " + literal, text, count=1)
    assert n == 1, "POLICY placeholder missing from runtime.py"
    ast.parse(text, feature_version=OLDEST_PYTHON)  # SyntaxError if too new
    return text


def sha256(text):
    return hashlib.sha256(text.encode()).hexdigest()
