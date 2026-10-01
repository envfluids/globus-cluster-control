"""Personal, per-user configuration (never in the repo).

One JSON file per cluster in ~/.config/gcx/clusters/<cluster>.json, written
by `gcx setup`:

    endpoint        endpoint UUID
    state           <transfer collection>:<keepalive state dir>, for `gcx status`
    policy          what the cluster's functions allow (see capabilities/build.py)
    functions       {name: {uuid, sha256}} registered from that policy
    keepalive       {mode: failover|single|none, primary, backup}
    ssh, remote_root, endpoint_name, remote_state, email

The pilot's single file, ~/.config/gc-endpoints.json, is still read as a
fallback until setup writes the new layout.
"""

import json
import os
import shutil
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("GCX_CONFIG_DIR", Path.home() / ".config" / "gcx"))
LEGACY = Path.home() / ".config" / "gc-endpoints.json"


class NotConfigured(Exception):
    pass


def cluster_file(cluster):
    return CONFIG_DIR / "clusters" / f"{cluster}.json"


def load(cluster):
    f = cluster_file(cluster)
    if f.exists():
        return json.loads(f.read_text())
    if LEGACY.exists():
        entry = json.loads(LEGACY.read_text()).get(cluster)
        if entry is not None:
            return entry if isinstance(entry, dict) else {"endpoint": entry}
    raise NotConfigured(f"{cluster} is not set up: no {f}")


def configured():
    names = {p.stem for p in (CONFIG_DIR / "clusters").glob("*.json")}
    if LEGACY.exists():
        names |= set(json.loads(LEGACY.read_text()))
    return sorted(names)


def save(cluster, data):
    f = cluster_file(cluster)
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(f)


def globus_cli():
    return shutil.which("globus") or str(Path.home() / ".local" / "bin" / "globus")
