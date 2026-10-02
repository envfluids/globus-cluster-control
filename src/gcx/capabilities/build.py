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
    "read": ["gcx_ls", "gcx_read", "gcx_du"],
    "shell": ["gcx_shell"],
}
# Calls that change state on the cluster: submitted at-most-once.
MUTATING = {"gcx_submit", "gcx_cancel", "gcx_shell"}

POLICY_LINE = re.compile(r"^POLICY = \{\}.*$", re.M)
SCHEDULERS = ("slurm", "pbs")
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
    for key in ("read_roots", "script_roots"):
        for root in policy.get(key, []):
            if root in ("/", "") or not (root.startswith(("/", "~", "$"))):
                raise PolicyError(f"{key}: {root!r} must be an absolute, ~ or $VAR path, not /")


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
