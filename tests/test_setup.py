"""Setup's decisions for clusters Midway3 cannot exercise: fresh installs,
no cron, single nodes, PBS. No SSH: a stub Remote with a known home."""

import pytest

from gcx import profiles
from gcx.capabilities import build
from gcx.setup import menu, steps
from gcx.setup.remote import Remote, RemoteError

FACTS = {"host": "midway3-login4", "user": "alice", "home": "/home/alice", "cron": "ok",
         "scheduler": "slurm", "sched_bin": "/opt/slurm/bin",
         "accounts": "pi-a,pi-b", "net_api_443": "ok", "net_amqps_5671": "ok"}


def ctx(cfg=None, facts=None, prof=None, yes=True):
    c = steps.Ctx("midway3", prof or profiles.load("midway3"), dict(cfg or {}),
                  Remote("midway3", home="/home/alice"), menu.Prompter(yes))
    c.facts = dict(FACTS, **(facts or {}))
    return c


def test_profile_loads_and_lists():
    p = profiles.load("midway3")
    assert p["scheduler"] == "slurm" and "midway3" in profiles.available()
    with pytest.raises(profiles.UnknownCluster):
        profiles.load("nowhere")


def test_fresh_install_defaults_to_dot_gcx():
    c = ctx()
    assert (c.root, c.ep) == ("~/.gcx", "gcx")
    assert c.gce == "/home/alice/.gcx/venv/bin/globus-compute-endpoint"


def test_yes_policy_defaults_keep_shell_off_and_allow_test_dir(capsys):
    c = ctx()
    pol = menu.build_policy("midway3", c.prof, c.facts, c.root, c.ask)
    assert pol["capabilities"] == ["status", "submit", "read"]
    assert pol["accounts"] == ["pi-a", "pi-b"] and pol["queues"] == ["caslake"]
    assert "~/.gcx/test" not in pol["read_roots"]  # already covered by ~
    build.validate(pol)


def test_test_dir_added_when_roots_do_not_cover_it():
    prof = dict(profiles.load("midway3"), read_roots=["/scratch/x"], script_roots=["/scratch/x"])
    c = ctx(prof=prof)
    pol = menu.build_policy("midway3", prof, c.facts, c.root, c.ask)
    assert pol["read_roots"] == ["/scratch/x", "~/.gcx/test"]
    assert pol["script_roots"] == ["/scratch/x", "~/.gcx/test"]


@pytest.mark.parametrize("facts, mode", [
    ({"cron": "denied"}, "none"),
    ({"host": "midway3-login2"}, "single"),      # not a named login node
    ({}, "failover"),                            # named node, cron ok
])
def test_keepalive_probe_then_degrade(facts, mode):
    c = ctx(facts=facts)
    ka = menu.choose_keepalive("midway3", c.prof, c.facts, c.ask)
    assert ka["mode"] == mode
    if mode == "failover":
        assert ka["primary"] == "midway3-login4" and ka["backup"] != "midway3-login4"


def test_single_node_profile_never_offers_failover():
    prof = dict(profiles.load("midway3"), login_nodes=[])
    c = ctx(prof=prof)
    assert menu.choose_keepalive("midway3", prof, c.facts, c.ask) == \
        {"mode": "single", "primary": "midway3-login4"}


def test_keepalive_env_and_cron_line():
    c = ctx({"keepalive": {"mode": "failover", "primary": "n4", "backup": "n3"},
             "email": "a@b.edu"})
    files = steps.keepalive_files(c)
    assert list(files) == ["~/.gcx/keepalive.env", "~/.gcx/keepalive.sh"]  # settings first
    env = files["~/.gcx/keepalive.env"]
    assert "MODE=failover\nPRIMARY=n4\nBACKUP=n3\nEP=gcx\n" in env
    assert "STATE=/home/alice/.gcx/state\n" in env
    cmd = steps._cron_cmd(c, "backup")
    assert "/home/alice/.gcx/keepalive.sh backup # gcx-keepalive midway3" in cmd
    assert "grep -v 'gcx-keepalive midway3$'" in cmd  # replaces only its own line
    assert "MAILTO=a@b.edu" in cmd


def test_endpoint_files_fill_workers_and_path():
    prof = dict(profiles.load("midway3"), max_workers=2)
    files = steps.endpoint_files(ctx(prof=prof))
    tmpl = files["~/.globus_compute/gcx/user_config_template.yaml.j2"]
    assert "max_workers: 2 " in tmpl and "@MAX_WORKERS@" not in tmpl
    assert "JSONData" in tmpl and "PureSourceTextInspect" in tmpl
    env = files["~/.globus_compute/gcx/user_environment.yaml"]
    assert "PATH: /home/alice/.gcx/venv/bin:/home/alice/.local/bin:/opt/slurm/bin:" in env


def test_personal_config_and_transfer_state_path():
    c = ctx(facts={"ep_id": "ep-1"})
    p = steps.personal(c)
    assert p["endpoint"] == "ep-1" and p["remote_state"] == "~/.gcx/state"
    assert p["state"] == "2fde89c0-6fb4-11eb-8c47-0eb1aa8d4337:/~/.gcx/state/"


def test_probe_blocks_unreachable_globus(monkeypatch):
    c = ctx()
    monkeypatch.setattr(c, "probe", lambda: None)
    c.facts.update(net_amqps_5671="blocked", net_amqps_443="blocked")
    assert steps.s_probe(c).status == steps.BLOCKED
    c.facts.update(net_amqps_443="ok")
    assert steps.s_probe(c).status == steps.OK
    c.facts.update(scheduler="pbs")
    assert steps.s_probe(c).status == steps.BLOCKED


def test_cron_step_targets_the_right_node():
    cfg = {"keepalive": {"mode": "failover", "primary": "midway3-login4",
                         "backup": "midway3-login3"}}
    c = ctx(cfg, facts={"heartbeats": "midway3-login4:30"})
    r = steps.s_cron(c)
    assert r.status == steps.HUMAN and "backup" in r.detail and "midway3-login3" in r.detail
    c.facts["heartbeats"] = "midway3-login4:30,midway3-login3:31"
    assert steps.s_cron(c).status == steps.OK
    c.facts["heartbeats"] = "midway3-login3:31"  # primary (this node) missing
    assert steps.s_cron(c).status == steps.CHANGE


def test_unsafe_root_refused():
    with pytest.raises(RemoteError):
        Remote("x").probe("~/a; rm -rf ~", "gcx")
    with pytest.raises(RemoteError):
        Remote("x").probe("/etc", "gcx")


def test_login_shell_variables_resolved_into_paths(capsys):
    facts = {"env_WORK": "/work2/09979/alice/stampede3", "env_SCRATCH": ""}
    roots = menu.resolve_roots(["~", "$WORK", "$WORK/runs/", "$SCRATCH", "/scratch/$USER"], facts)
    assert roots == ["~", "/work2/09979/alice/stampede3", "/work2/09979/alice/stampede3/runs",
                     "/scratch/$USER"]
    assert "dropping $SCRATCH" in capsys.readouterr().out
