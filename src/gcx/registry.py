"""Register a cluster's capability functions and remember their UUIDs.

The UUIDs and the hash of the source they were registered from are kept in the
cluster's personal config under "functions". Registration is skipped when the
policy (hence the source) is unchanged, so re-running is cheap and the UUIDs --
which the endpoint's allowlist names -- stay stable.
"""

from gcx import config
from gcx.capabilities import build
from gcx.transport import retry


def current(cfg):
    """The registered functions, if they still match cfg's policy."""
    funcs = cfg.get("functions") or {}
    if not funcs or "policy" not in cfg:
        return None
    sha = build.sha256(build.source(cfg["policy"]))
    wanted = set(build.entry_functions(cfg["policy"]))
    if set(funcs) == wanted and all(f["sha256"] == sha for f in funcs.values()):
        return funcs
    return None


def register(client, cluster, force=False):
    cfg = config.load(cluster)
    if "policy" not in cfg:
        raise config.NotConfigured(f"{cluster} has no policy in {config.cluster_file(cluster)}")
    if not force and current(cfg):
        return cfg["functions"], False
    text = build.source(cfg["policy"])
    sha = build.sha256(text)
    funcs = {}
    for name in build.entry_functions(cfg["policy"]):
        # Registration only creates a new function record, so retrying is safe.
        uuid = retry(client.register_source_code, text, name,
                     description=f"gcx {cluster} {name}")
        funcs[name] = {"uuid": uuid, "sha256": sha}
    cfg["functions"] = funcs
    config.save(cluster, cfg)
    return funcs, True
