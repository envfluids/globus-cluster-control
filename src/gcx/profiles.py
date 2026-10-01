"""Cluster profiles: facts about a cluster that are the same for every user.

Shipped in src/gcx/data/profiles/<name>.toml. Personal choices live in
~/.config/gcx/ (see config.py).
"""

import tomllib
from importlib import resources

REQUIRED = ("name", "ssh_host", "scheduler", "transfer_collection")
DEFAULTS = {
    "login_nodes": [],
    "transfer_home": "/~/",
    "max_workers": 4,
    "submit_extra": [],
    "read_roots": ["~"],
    "script_roots": ["~"],
    "uv_env": {},
    "mfa_single_use": False,
    "test_job": {},
    "notes": "",
}


class UnknownCluster(Exception):
    pass


def _dir():
    return resources.files("gcx.data").joinpath("profiles")


def available():
    return sorted(p.name.removesuffix(".toml") for p in _dir().iterdir() if p.name.endswith(".toml"))


def load(name):
    f = _dir().joinpath(f"{name}.toml")
    if not f.is_file():
        raise UnknownCluster(f"no profile for {name!r}; known: {', '.join(available())}")
    prof = {**DEFAULTS, **tomllib.loads(f.read_text())}
    missing = [k for k in REQUIRED if not prof.get(k)]
    if missing:
        raise ValueError(f"profile {name} lacks {missing}")
    if prof["scheduler"] not in ("slurm", "pbs"):
        raise ValueError(f"profile {name}: scheduler must be slurm or pbs")
    return prof


def short(node):
    """midway3-login4.rcc.uchicago.edu -> midway3-login4 (what hostname -s says)."""
    return node.split(".")[0]
