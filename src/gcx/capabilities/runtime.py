"""Code that runs on the cluster endpoint, registered as source text.

`build.py` replaces the POLICY line below with a literal for one cluster and
registers the result once per entry function. Registered code is immutable, so
the policy cannot be changed by whoever holds the Globus tokens; it can only be
replaced by registering new functions and updating the endpoint's allowlist.

Constraints (this text is exec'd by the endpoint's own Python):
- standard library only;
- Python 3.9 syntax, no decorators (PureSourceTextInspect cannot handle them);
- arguments arrive JSON-decoded; return plain dicts;
- never a shell, except gcx_shell, which exists only when the policy allows it.
"""

import getpass
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time

POLICY = {}  # replaced at registration; see build.py

SLURM_ID = re.compile(r"^\d+(_\d+)?$")
PBS_ID = re.compile(r"^\d+(\[\d*\])?(\.[\w.-]+)?$")
NAME = re.compile(r"^[\w.-]{1,64}$")
SLURM_TIME = re.compile(r"^(\d{1,2}-)?\d{1,3}(:\d\d){0,2}$")
PBS_TIME = re.compile(r"^\d{1,3}(:\d\d){0,2}$")
DATE = re.compile(r"^\d{4}-\d\d-\d\d(T\d\d:\d\d(:\d\d)?)?$")
MAX_LINES = 5000
MAX_ENTRIES = 5000


class Refused(Exception):
    pass


def _limit():
    return int(POLICY.get("max_output", 1000000))


def _clip(text):
    n = _limit()
    if len(text) > n:
        return text[-n:], True
    return text, False


def _run(argv, cwd=None, timeout=None):
    try:
        p = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, errors="replace",
                           timeout=timeout or POLICY.get("timeout", 120))
    except FileNotFoundError:
        return {"rc": 127, "stdout": "", "stderr": argv[0] + ": not found on the endpoint's PATH\n"}
    except subprocess.TimeoutExpired:
        return {"rc": 124, "stdout": "", "stderr": argv[0] + ": timed out\n"}
    out, t1 = _clip(p.stdout)
    err, t2 = _clip(p.stderr)
    return {"rc": p.returncode, "stdout": out, "stderr": err, "truncated": t1 or t2}


def _refuse(msg):
    return {"rc": 2, "stdout": "", "stderr": "gcx refused: " + msg + "\n", "refused": True}


def _user():
    return os.environ.get("USER") or getpass.getuser()


def _require(capability):
    if capability not in POLICY.get("capabilities", []):
        raise Refused(capability + " is not enabled on this cluster")


def _expand(path):
    return os.path.realpath(os.path.expandvars(os.path.expanduser(path)))


def _under(path, roots_key):
    """Resolve path (symlinks included) and require it inside an allowed root."""
    if not isinstance(path, str) or not path or "\x00" in path:
        raise Refused("bad path")
    full = os.path.expandvars(os.path.expanduser(path))
    if not os.path.isabs(full):
        full = os.path.join(os.path.expanduser("~"), full)
    real = os.path.realpath(full)
    for root in POLICY.get(roots_key, []):
        r = _expand(root)
        if os.path.commonpath([real, r]) == r:
            return real
    raise Refused(path + " is outside the allowed directories (" + ", ".join(POLICY.get(roots_key, [])) + ")")


def _ids(job_ids):
    if job_ids is None:
        return []
    if isinstance(job_ids, (str, int)):
        job_ids = [job_ids]
    pat = SLURM_ID if POLICY["scheduler"] == "slurm" else PBS_ID
    out = []
    for j in job_ids:
        j = str(j)
        if not pat.match(j):
            raise Refused("bad job id " + repr(j))
        out.append(j)
    return out


def _choice(value, key, what):
    if value is None:
        return None
    allowed = POLICY.get(key) or []
    if value not in allowed:
        raise Refused(what + " " + repr(value) + " not allowed (allowed: " + ", ".join(allowed) + ")")
    return value


def _guard(fn, *args):
    try:
        return fn(*args)
    except Refused as e:
        return _refuse(str(e))
    except OSError as e:  # permission denied, vanished file, ...
        return {"rc": 1, "stdout": "", "stderr": str(e) + "\n"}


# ---- always ---------------------------------------------------------------

def gcx_ping():
    text = json.dumps(POLICY, sort_keys=True)
    return {"host": socket.gethostname().split(".")[0], "user": _user(),
            "python": sys.version.split()[0], "time": time.time(),
            "policy_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "capabilities": POLICY.get("capabilities", [])}


# ---- job status (always on) ----------------------------------------------

def gcx_jobs(job_ids=None):
    def go():
        ids = _ids(job_ids)
        if POLICY["scheduler"] == "slurm":
            argv = ["squeue", "-o", "%.12i %.10P %.24j %.8T %.10M %.10l %.5D %R"]
            return _run(argv + (["-j", ",".join(ids)] if ids else ["-u", _user()]))
        return _run(["qstat", "-f"] + ids if ids else ["qstat", "-u", _user()])
    return _guard(go)


def gcx_history(job_ids=None, since=None):
    def go():
        ids = _ids(job_ids)
        if since is not None and not DATE.match(str(since)):
            raise Refused("since must be YYYY-MM-DD[THH:MM[:SS]]")
        if POLICY["scheduler"] == "slurm":
            argv = ["sacct", "-X", "-o",
                    "JobID,JobName%24,Partition,Account,State,Elapsed,ExitCode,NodeList%20"]
            argv += ["-j", ",".join(ids)] if ids else ["-u", _user()]
            return _run(argv + (["-S", str(since)] if since else []))
        return _run(["qstat", "-x", "-f"] + ids if ids else ["qstat", "-x", "-u", _user()])
    return _guard(go)


def gcx_queues():
    if POLICY["scheduler"] == "slurm":
        return _run(["sinfo", "-s"])
    return _run(["qstat", "-Q"])


# ---- submit / cancel -------------------------------------------------------

def gcx_submit(script, account=None, queue=None, walltime=None, depends_on=None,
               job_name=None, qos=None):
    def go():
        _require("submit")
        path = _under(script, "script_roots")
        if not os.path.isfile(path):
            raise Refused(script + " is not a file")
        acct = _choice(account, "accounts", "account")
        q = _choice(queue, "queues", "queue")
        qq = _choice(qos, "qos", "qos")
        deps = _ids(depends_on)
        if job_name is not None and not NAME.match(str(job_name)):
            raise Refused("job name must match " + NAME.pattern)
        slurm = POLICY["scheduler"] == "slurm"
        if walltime is not None and not (SLURM_TIME if slurm else PBS_TIME).match(str(walltime)):
            raise Refused("bad walltime " + repr(walltime))
        if slurm:
            argv = ["sbatch", "--parsable"]
            argv += ["-A", acct] if acct else []
            argv += ["-p", q] if q else []
            argv += ["--qos", qq] if qq else []
            argv += ["-t", str(walltime)] if walltime else []
            argv += ["--dependency=afterok:" + ":".join(deps)] if deps else []
            argv += ["-J", str(job_name)] if job_name else []
        else:
            argv = ["qsub"]
            argv += ["-A", acct] if acct else []
            argv += ["-q", q] if q else []
            argv += ["-l", "walltime=" + str(walltime)] if walltime else []
            argv += ["-W", "depend=afterok:" + ":".join(deps)] if deps else []
            argv += ["-N", str(job_name)] if job_name else []
        argv += list(POLICY.get("submit_extra", []))
        res = _run(argv + [path], cwd=os.path.dirname(path))
        if res["rc"] == 0:
            res["job_id"] = _job_id(res["stdout"], slurm)
        return res
    return _guard(go)


def _job_id(stdout, slurm):
    """The last line that looks like a job id: site submit filters may print
    banners to stdout first (TACC prints a welcome banner and an env dump)."""
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if slurm:
            line = line.split(";")[0]  # sbatch --parsable: id[;cluster]
        if (SLURM_ID if slurm else PBS_ID).match(line):
            return line
    return None


def gcx_cancel(job_ids):
    def go():
        _require("submit")
        ids = _ids(job_ids)
        if not ids:
            raise Refused("no job ids given")
        if POLICY["scheduler"] == "slurm":
            return _run(["scancel", "-u", _user()] + ids)  # -u: only this user's jobs
        return _run(["qdel"] + ids)
    return _guard(go)


# ---- read files & logs ------------------------------------------------------

def gcx_ls(path="~"):
    def go():
        _require("read")
        real = _under(path, "read_roots")
        entries = []
        with os.scandir(real) as it:
            for e in it:
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                kind = "link" if e.is_symlink() else "dir" if e.is_dir() else "file"
                entries.append({"name": e.name, "type": kind, "size": st.st_size,
                                "mtime": st.st_mtime})
                if len(entries) >= MAX_ENTRIES:
                    break
        entries.sort(key=lambda d: d["name"])
        return {"rc": 0, "path": real, "entries": entries,
                "truncated": len(entries) >= MAX_ENTRIES}
    return _guard(go)


def gcx_read(path, mode="tail", lines=200):
    def go():
        _require("read")
        real = _under(path, "read_roots")
        if not os.path.isfile(real):
            raise Refused(path + " is not a regular file")
        n = int(lines)
        if not 1 <= n <= MAX_LINES:
            raise Refused("lines must be 1.." + str(MAX_LINES))
        if mode not in ("head", "tail"):
            raise Refused("mode must be head or tail")
        limit = _limit()
        with open(real, "rb") as f:
            if mode == "head":
                chunks, count = [], 0
                for line in f:
                    chunks.append(line)
                    count += 1
                    if count >= n or sum(map(len, chunks)) >= limit:
                        break
                data = b"".join(chunks)
            else:
                # Read backwards until more than n line breaks are in hand (the
                # first, possibly partial, line is then dropped) or the start.
                f.seek(0, os.SEEK_END)
                end = pos = f.tell()
                data = b""
                while pos > 0 and data.count(b"\n") <= n and end - pos < limit:
                    step = min(65536, pos)
                    pos -= step
                    f.seek(pos)
                    data = f.read(step) + data
                data = b"".join(data.splitlines(keepends=True)[-n:])
        text, cut = _clip(data.decode("utf-8", "replace"))
        return {"rc": 0, "stdout": text, "stderr": "", "truncated": cut, "path": real}
    return _guard(go)


def gcx_du(path="~", depth=1):
    def go():
        _require("read")
        real = _under(path, "read_roots")
        d = int(depth)
        if not 0 <= d <= 3:
            raise Refused("depth must be 0..3")
        return _run(["du", "-h", "-d", str(d), real], timeout=POLICY.get("du_timeout", 300))
    return _guard(go)


# ---- arbitrary shell (opt-in) ----------------------------------------------

def gcx_shell(cmd, walltime=600):
    def go():
        _require("shell")
        if not isinstance(cmd, str):
            raise Refused("cmd must be a string")
        return _run(["/bin/bash", "-c", cmd], cwd=os.path.expanduser("~"),
                    timeout=float(walltime))
    return _guard(go)
