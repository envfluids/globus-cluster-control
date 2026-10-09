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
import shlex
import socket
import subprocess
import sys
import tempfile
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


# ---- templated runs (Slurm) -------------------------------------------------
#
# gcx_run renders a repo script with the repo's own launcher (a command that
# prints the finished batch script, e.g. `submit.py --dry-run`), checks the
# rendered #SBATCH header against the policy, and submits it with the
# request's arguments as the script's "$@". The request picks only the script,
# its arguments and the same validated overrides as gcx_submit.

# #SBATCH options refused in rendered scripts unless the launcher sets
# "block_risky": false. Each changes who the job runs as or what it runs,
# rather than what resources it gets, so a template mistake or a tampered
# template reaches past the job itself:
#   --wrap          replaces the script body with an inline command string;
#   --export(-file) sets or overrides the job's environment (PATH, LD_PRELOAD,
#                   PYTHONPATH), changing what code actually runs;
#   --uid / --gid   ask Slurm to run the job as another user or group;
#   --get-user-env  loads a fresh login environment that nobody reviewed.
RISKY_SBATCH = ("--wrap", "--export", "--export-file", "--uid", "--gid", "--get-user-env")
MAX_ARGS = 64
MAX_ARGS_BYTES = 4096

# The header options gcx reads: long name -> short flag.
SBATCH_TRACKED = {"--account": "-A", "--partition": "-p", "--qos": "-q", "--job-name": "-J",
                  "--output": "-o", "--error": "-e"}


def _long_match(name, full):
    """sbatch accepts unambiguous prefixes of long options (--part for --partition).
    Ambiguous prefixes are rejected by sbatch, so matching them too only errs strict."""
    return len(name) > 2 and full.startswith(name)


def _header(text):
    """The #SBATCH options sbatch would read: from the top of the script up to the
    first line that is neither blank nor a comment."""
    tokens = []
    for line in text.splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            break
        if s.startswith("#SBATCH"):
            try:
                tokens += shlex.split(s[len("#SBATCH"):], comments=True)
            except ValueError as e:
                raise Refused("cannot parse header line " + repr(s) + ": " + str(e))
    return tokens


def _sbatch_options(tokens, block_risky):
    """{long name: value} for the tracked options (last wins, as in sbatch)."""
    shorts = {v: k for k, v in SBATCH_TRACKED.items()}
    found = {}
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        i += 1
        if tok.startswith("--"):
            name, eq, value = tok.partition("=")
            if block_risky and any(_long_match(name, r) for r in RISKY_SBATCH):
                raise Refused("rendered script uses " + name + ", which this launcher blocks "
                              "(it changes who the job runs as or what it runs)")
            for full in SBATCH_TRACKED:
                if _long_match(name, full):
                    if not eq:
                        value = tokens[i] if i < len(tokens) else ""
                        i += 1
                    found[full] = value
                    break
        elif tok[:2] in shorts:
            if len(tok) > 2:
                found[shorts[tok[:2]]] = tok[2:]
            else:
                found[shorts[tok]] = tokens[i] if i < len(tokens) else ""
                i += 1
    return found


def _launcher_for(script):
    """The launcher whose root holds script, and the resolved script path."""
    if not isinstance(script, str) or not script or "\x00" in script:
        raise Refused("bad path")
    full = os.path.expandvars(os.path.expanduser(script))
    if not os.path.isabs(full):
        full = os.path.join(os.path.expanduser("~"), full)
    real = os.path.realpath(full)
    best = None
    for launcher in POLICY.get("launchers", []):
        r = _expand(launcher["root"])
        if os.path.commonpath([real, r]) == r and (best is None or len(r) > len(best[1])):
            best = (launcher, r)
    if best is None:
        roots = ", ".join(l["root"] for l in POLICY.get("launchers", []))
        raise Refused(script + " is not under a launcher root (" + roots + ")")
    return best[0], best[1], real


def _script_args(args):
    if args is None:
        return []
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise Refused("script arguments must be a list of strings")
    if len(args) > MAX_ARGS:
        raise Refused("at most " + str(MAX_ARGS) + " script arguments")
    if sum(len(a.encode()) for a in args) > MAX_ARGS_BYTES:
        raise Refused("script arguments exceed " + str(MAX_ARGS_BYTES) + " bytes")
    if any("\x00" in a for a in args):
        raise Refused("script arguments may not contain NUL")
    return list(args)


def _log_path(pattern, root, job_id, job_name):
    """Absolute log path with the common % patterns filled in (%A, %a, ... stay)."""
    path = pattern if os.path.isabs(pattern) else os.path.join(root, pattern)
    for key, value in (("%j", job_id or "%j"), ("%x", job_name), ("%u", _user())):
        path = path.replace(key, value)
    return path


def _make_log_dir(pattern, root):
    """sbatch does not create log directories; create them, but only inside root."""
    d = os.path.dirname(pattern if os.path.isabs(pattern) else os.path.join(root, pattern))
    if d and "%" not in d:
        real = os.path.realpath(d)
        if os.path.commonpath([real, root]) == root:
            os.makedirs(real, exist_ok=True)


def gcx_run(script, args=None, account=None, queue=None, walltime=None, depends_on=None,
            job_name=None, qos=None):
    def go():
        _require("run")
        launcher, root, path = _launcher_for(script)
        if not os.path.isfile(path):
            raise Refused(script + " is not a file")
        argv_extra = _script_args(args)
        deps = _ids(depends_on)
        if job_name is not None and not NAME.match(str(job_name)):
            raise Refused("job name must match " + NAME.pattern)
        if walltime is not None and not SLURM_TIME.match(str(walltime)):
            raise Refused("bad walltime " + repr(walltime))
        cmd = [os.path.expanduser(a) if a.startswith("~/") else a for a in launcher["argv"]]
        rendered = _run(cmd + [path], cwd=root, timeout=launcher.get("timeout"))
        if rendered["rc"] != 0:
            rendered["stderr"] = "launcher failed:\n" + rendered["stderr"]
            return rendered
        text = rendered["stdout"]
        if rendered.get("truncated") or not text.startswith("#!"):
            raise Refused("launcher output is not a complete batch script (must start with #!)")
        found = _sbatch_options(_header(text), launcher.get("block_risky", True))

        acct = account if account is not None else found.get("--account")
        part = queue if queue is not None else found.get("--partition")
        qq = qos if qos is not None else found.get("--qos")
        if not acct:
            raise Refused("no account: the rendered script sets none; pass -A")
        if not part:
            raise Refused("no partition: the rendered script sets none; pass -p")
        _choice(acct, "accounts", "account")
        _choice(part, "queues", "queue")
        _choice(qq, "qos", "qos")
        name = job_name or found.get("--job-name") or os.path.basename(path)
        if not NAME.match(name):
            raise Refused("job name must match " + NAME.pattern)

        for key in ("--output", "--error"):
            if found.get(key):
                _make_log_dir(found[key], root)
        fd, tmp = tempfile.mkstemp(prefix="gcx-run-", suffix=".sh")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(text)
            # Overrides go on the command line, where they beat the header.
            argv = ["sbatch", "--parsable", "-A", acct, "-p", part]
            argv += ["--qos", qq] if qq else []
            argv += ["-t", str(walltime)] if walltime else []
            argv += ["--dependency=afterok:" + ":".join(deps)] if deps else []
            argv += ["-J", name]  # otherwise the job is named after the temp file
            argv += list(POLICY.get("submit_extra", []))
            res = _run(argv + [tmp] + argv_extra, cwd=root)
        finally:
            os.unlink(tmp)  # sbatch keeps its own copy
        if res["rc"] == 0:
            res["job_id"] = _job_id(res["stdout"], True)
            out = found.get("--output") or "slurm-%j.out"
            res["output"] = _log_path(out, root, res["job_id"], name)
            if found.get("--error"):
                res["error"] = _log_path(found["--error"], root, res["job_id"], name)
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
