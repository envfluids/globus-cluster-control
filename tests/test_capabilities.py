"""The registered capability code, exercised the way the endpoint runs it.

Each test builds the source for a policy with build.source() and exec's it
(PureSourceTextInspect does exactly this), with scheduler commands replaced by
tests/fakebin stubs that log their argv. Real files in tmp_path stand in for
home, scratch and an outside directory.
"""

import os
from pathlib import Path

import pytest

from gcx.capabilities import build

FAKEBIN = Path(__file__).parent / "fakebin"


@pytest.fixture
def env(tmp_path, monkeypatch):
    home, scratch, outside = (tmp_path / d for d in ("home", "scratch", "outside"))
    for d in (home, scratch, outside):
        d.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USER", "alice")
    monkeypatch.setenv("SCRATCH", str(scratch))
    monkeypatch.setenv("PATH", f"{FAKEBIN}:{os.environ['PATH']}")
    log = tmp_path / "fake.log"
    monkeypatch.setenv("FAKE_LOG", str(log))
    return type("Env", (), dict(home=home, scratch=scratch, outside=outside, log=log))


def load(**overrides):
    policy = {
        "scheduler": "slurm",
        "capabilities": ["status", "submit", "read"],
        "read_roots": ["~", "$SCRATCH"],
        "script_roots": ["~"],
        "accounts": ["pi-good"],
        "queues": ["caslake"],
        "qos": ["schmidt"],
    }
    policy.update(overrides)
    ns = {}
    exec(build.source(policy), ns)
    return ns


def calls(env):
    """Logged invocations as [(cwd, [argv...]), ...], argv[0] as a bare name."""
    out, cur = [], None
    for line in env.log.read_text().splitlines():
        if line.startswith("cwd="):
            cur = (line[4:], [])
        elif line == "--":
            out.append(cur)
        elif not cur[1]:
            cur[1].append(os.path.basename(line))
        else:
            cur[1].append(line)
    return out


def refused(res):
    return res.get("refused") and res["rc"] == 2


# ---- build -----------------------------------------------------------------

def test_source_embeds_policy_and_parses_on_oldest_python():
    text = build.source({"scheduler": "pbs", "capabilities": ["status"]})
    assert "POLICY = {'capabilities': ['status'], 'scheduler': 'pbs'}" in text
    assert build.sha256(text) == build.sha256(build.source(
        {"capabilities": ["status"], "scheduler": "pbs"}))  # key order irrelevant


@pytest.mark.parametrize("policy, msg", [
    ({"scheduler": "lsf", "capabilities": ["status"]}, "scheduler"),
    ({"scheduler": "slurm", "capabilities": ["read"]}, "always on"),
    ({"scheduler": "slurm", "capabilities": ["status", "read"]}, "read_roots"),
    ({"scheduler": "slurm", "capabilities": ["status", "read"], "read_roots": ["/"]}, "not /"),
    ({"scheduler": "slurm", "capabilities": ["status", "read"], "read_roots": ["rel"]}, "absolute"),
    ({"scheduler": "slurm", "capabilities": ["status", "nuke"]}, "unknown"),
    ({"scheduler": "slurm", "capabilities": ["status"], "x": Path("/")}, "only strings"),
])
def test_bad_policies_rejected(policy, msg):
    with pytest.raises(build.PolicyError, match=msg):
        build.source(policy)


def test_entry_functions_follow_capabilities():
    fns = build.entry_functions({"capabilities": ["status", "read"]})
    assert "gcx_read" in fns and "gcx_submit" not in fns and "gcx_shell" not in fns


# ---- read --------------------------------------------------------------------

def test_read_tail_and_head(env):
    (env.home / "log.out").write_text("".join(f"line {i}\n" for i in range(1, 1001)))
    ns = load()
    tail = ns["gcx_read"]("log.out", "tail", 3)  # relative = under home
    assert tail["stdout"] == "line 998\nline 999\nline 1000\n"
    head = ns["gcx_read"]("~/log.out", "head", 2)
    assert head["stdout"] == "line 1\nline 2\n"


def test_tail_of_short_file_and_file_without_final_newline(env):
    (env.home / "a").write_text("x\ny")
    ns = load()
    assert ns["gcx_read"]("~/a", "tail", 10)["stdout"] == "x\ny"
    assert ns["gcx_read"]("~/a", "tail", 1)["stdout"] == "y"


def test_read_outside_roots_refused(env):
    (env.outside / "secret").write_text("s")
    ns = load()
    assert refused(ns["gcx_read"]("/etc/passwd"))
    assert refused(ns["gcx_read"](str(env.outside / "secret")))
    assert refused(ns["gcx_read"]("~/../outside/secret"))


def test_symlink_escaping_roots_refused(env):
    (env.outside / "secret").write_text("s")
    (env.home / "link").symlink_to(env.outside / "secret")
    (env.home / "dirlink").symlink_to(env.outside)
    ns = load()
    assert refused(ns["gcx_read"]("~/link"))
    assert refused(ns["gcx_ls"]("~/dirlink"))


def test_env_var_root(env):
    (env.scratch / "run").mkdir()
    ns = load()
    res = ns["gcx_ls"]("$SCRATCH")
    assert res["rc"] == 0 and [e["name"] for e in res["entries"]] == ["run"]


def test_read_disabled_when_not_in_policy(env):
    (env.home / "f").write_text("x")
    ns = load(capabilities=["status"])
    assert refused(ns["gcx_read"]("~/f"))


def test_read_bad_args(env):
    (env.home / "f").write_text("x")
    ns = load()
    assert refused(ns["gcx_read"]("~/f", "tail", 0))
    assert refused(ns["gcx_read"]("~/f", "cat", 5))
    assert refused(ns["gcx_read"]("~"))  # a directory
    assert refused(ns["gcx_du"]("~", 9))


# ---- submit / cancel -------------------------------------------------------

def test_submit_slurm_argv(env):
    (env.home / "jobs").mkdir()
    script = env.home / "jobs" / "run.sbatch"
    script.write_text("#!/bin/bash\n")
    ns = load()
    res = ns["gcx_submit"]("~/jobs/run.sbatch", account="pi-good", queue="caslake",
                           walltime="00:10:00", depends_on=["11", "12"], job_name="t1",
                           qos="schmidt")
    assert res["rc"] == 0 and res["job_id"] == "4242"
    [(cwd, argv)] = calls(env)
    assert cwd == str(script.parent.resolve())
    assert argv == ["sbatch", "--parsable", "-A", "pi-good", "-p", "caslake", "--qos", "schmidt",
                    "-t", "00:10:00", "--dependency=afterok:11:12", "-J", "t1",
                    str(script.resolve())]


def test_submit_pbs_argv_with_site_extra(env):
    script = env.home / "run.pbs"
    script.write_text("#!/bin/bash\n")
    ns = load(scheduler="pbs", queues=["debug"],
              submit_extra=["-l", "filesystems=home:eagle"])
    res = ns["gcx_submit"]("~/run.pbs", queue="debug", walltime="01:00:00")
    assert res["job_id"] == "4242.polaris-pbs-01"
    [(_, argv)] = calls(env)
    assert argv == ["qsub", "-q", "debug", "-l", "walltime=01:00:00",
                    "-l", "filesystems=home:eagle", str(script.resolve())]


@pytest.mark.parametrize("kwargs", [
    {"account": "pi-other"},
    {"queue": "bigmem"},
    {"qos": "high"},
    {"walltime": "10:00; rm -rf ~"},
    {"job_name": "x y"},
    {"job_name": "--wrap=id"},
    {"depends_on": ["1; id"]},
])
def test_submit_bad_args_refused_without_calling_sbatch(env, kwargs):
    (env.home / "run.sbatch").write_text("#!/bin/bash\n")
    ns = load()
    assert refused(ns["gcx_submit"]("~/run.sbatch", **kwargs))
    assert not env.log.exists()


def test_submit_script_must_be_under_script_roots(env):
    (env.scratch / "run.sbatch").write_text("#!/bin/bash\n")  # readable, not submittable
    ns = load()
    assert refused(ns["gcx_submit"]("$SCRATCH/run.sbatch"))
    assert refused(ns["gcx_submit"]("~/missing.sbatch"))


def test_cancel_only_own_jobs(env):
    ns = load()
    assert ns["gcx_cancel"](["123", "124_7"])["rc"] == 0
    [(_, argv)] = calls(env)
    assert argv == ["scancel", "-u", "alice", "123", "124_7"]
    assert refused(ns["gcx_cancel"](["123 -u bob"]))
    assert refused(ns["gcx_cancel"]([]))


def test_submit_disabled(env):
    (env.home / "run.sbatch").write_text("#!/bin/bash\n")
    ns = load(capabilities=["status", "read"])
    assert refused(ns["gcx_submit"]("~/run.sbatch"))
    assert refused(ns["gcx_cancel"](["1"]))


# ---- status ---------------------------------------------------------------

def test_jobs_and_history_argv(env):
    ns = load()
    ns["gcx_jobs"]()
    ns["gcx_jobs"](["5"])
    ns["gcx_history"](since="2026-10-01")
    (_, a), (_, b), (_, c) = calls(env)
    assert a[:1] == ["squeue"] and a[-2:] == ["-u", "alice"]
    assert b[-2:] == ["-j", "5"]
    assert c[0] == "sacct" and c[-2:] == ["-S", "2026-10-01"]
    assert refused(ns["gcx_history"](since="yesterday; id"))


def test_pbs_status_argv(env):
    ns = load(scheduler="pbs")
    ns["gcx_jobs"]()
    ns["gcx_history"](["99.pbs"])
    (_, a), (_, b) = calls(env)
    assert a == ["qstat", "-u", "alice"]
    assert b == ["qstat", "-x", "-f", "99.pbs"]


def test_ping(env):
    ping = load()["gcx_ping"]()
    assert ping["user"] == "alice" and len(ping["policy_sha256"]) == 64
    assert ping["capabilities"] == ["status", "submit", "read"]


# ---- shell -----------------------------------------------------------------

def test_shell_only_when_enabled(env):
    assert refused(load()["gcx_shell"]("echo hi"))
    res = load(capabilities=["status", "shell"])["gcx_shell"]("echo hi; pwd")
    assert res["rc"] == 0 and res["stdout"] == f"hi\n{env.home}\n"


def test_job_id_after_site_banner(env, tmp_path):
    # TACC prints a banner and an env dump to stdout before sbatch's own line.
    (env.home / "run.sbatch").write_text("#!/bin/bash\n")
    fake = tmp_path / "bin2"
    fake.mkdir()
    (fake / "sbatch").write_text("#!/bin/sh\necho '---- Welcome ----'\necho 'SLURM_TACC_NODES=1'\necho 3557959\n")
    (fake / "sbatch").chmod(0o755)
    os.environ["PATH"] = f"{fake}:{os.environ['PATH']}"
    res = load()["gcx_submit"]("~/run.sbatch")
    assert res["job_id"] == "3557959"
