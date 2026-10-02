# Design and pilot record

Pilot on Midway3, 2026-10-01. Everything below was measured on that date
unless marked otherwise.

## Architecture

```
Mac  --HTTPS-->  Globus Compute service  <--AMQPS (outbound)--  endpoint on midway3-login4
                                                                (backup: midway3-login3)
```

- **Endpoint**: `globus-compute-endpoint` 4.17.1 in `~/gc-endpoint/venv`
  (Python 3.12, built with uv). Single-user endpoint named `midway3-login`, ID
  `5d10f495-9235-481a-b347-240ec010f44d`. Only the owner's Globus identity can
  send it tasks.
- **Engine**: `ThreadPoolEngine`, 4 workers. Tasks run as threads in a worker
  process the endpoint starts on the login node. They never run on compute
  nodes; jobs go through `sbatch` like any other login-node command.
- **Locked-down template** (`src/gcx/data/endpoint/user_config_template.yaml.j2`).
  The default template lets each task install arbitrary packages, `curl | sh`
  the uv installer and `eval` user-supplied `worker_init`. All of that is
  removed, and the schema rejects every user-supplied variable. The worker's
  `PATH` is pinned in `user_environment.yaml`.
- **Sandboxing is off.** It would run each command in a fresh per-task
  directory and break relative paths such as `tail slurm-123.out`. `gcx` drops
  the resulting per-task warning.
- **Network**: the Midway3 login node reaches `compute.api.globus.org:443` and
  `compute.amqps.globus.org` on 443 and 5671.

## Restricted commands

Each `gcx` verb calls one registered function. All of them come from
`src/gcx/capabilities/runtime.py`, with the cluster's policy written into the
`POLICY = {}` line at registration (`build.py`). The text is registered from
source (`register_source_code`), and arguments travel as JSON, so the endpoint
never unpickles anything and its Python version need not match the laptop's.
The code is exec'd under 3.9 and 3.13 in `tests/test_source_compat.py`.

| Capability | Functions | Policy keys |
|---|---|---|
| status (always on) | `gcx_ping`, `gcx_jobs`, `gcx_history`, `gcx_queues` | `scheduler` |
| submit | `gcx_submit`, `gcx_cancel` | `script_roots`, `accounts`, `queues`, `qos`, `submit_extra` |
| read | `gcx_ls`, `gcx_read` (tail/head), `gcx_du` | `read_roots` |
| shell (opt-in) | `gcx_shell` | none; cancels every other restriction |

How each request is restricted:

- **No shell** except `gcx_shell`. Programs run with argv lists, so arguments
  cannot inject commands.
- **Paths** are resolved with `realpath`, so `..` and symlinks are followed
  before the root check (`os.path.commonpath`).
- **Submit** takes a script that already exists under `script_roots`. It runs
  with its own directory as the working directory. The only flags are
  account, queue, QoS, walltime, dependency and job name, each validated;
  `--wrap`, `-o` and `--export` are unreachable.
- **Cancel** uses `scancel -u $USER`, so only your own jobs can be cancelled.
- **Output** is capped at 1 MB.

**Registered code cannot be changed**, so a policy can only be widened by
registering new functions *and* putting their IDs on the endpoint's
allowlist, which takes SSH access to the cluster.

### Enforcement

There are two layers, both in the endpoint's own config:

- **`allowed_functions`** in the manager's `config.yaml`, written by
  `gcx allowlist <cluster> --apply`. It is sent to Globus when the endpoint
  starts. The service then rejects any other function at submit with
  `403 FUNCTION_NOT_PERMITTED`, and the endpoint re-checks each task.
- **`allowed_serializers`** on the engine in `user_config_template.yaml.j2`:
  source text for code, JSON for data. Even an allowed function is refused if
  its arguments arrive pickled (`Data serializer DillDataBase64 disabled by
  current configuration`).

Changing the policy is therefore two steps:

1. `gcx register <cluster>`, which creates new IDs.
2. `gcx allowlist <cluster> --apply`.

The allowlist step writes `config.yaml` over SSH and touches
`state/restart-request`. The keepalive on whichever node holds the endpoint
restarts it on its next tick, keeping its claim throughout so the other node
never starts a second copy. `gcx` then polls the service until it reports
exactly the new set. **Rollback**: `gcx allowlist <cluster> --off`.

`gcx doctor <cluster>` checks all of this:

- the functions match the current policy;
- the endpoint runs that policy (compared by policy hash);
- the service's allowlist equals the local functions;
- a raw `ShellFunction` is refused;
- reading `/etc/passwd` is refused;
- the scheduler answers.

Locked down on Midway3 2026-10-01, 16:43:

- **Apply**: the allowlist was reported in force 37 s after `--apply`. login4
  restarted the endpoint on request at 16:44:22, leaving one process and no
  errors.
- **`doctor`**: all checks passed. A raw `ShellFunction('id')` got 403, and
  pickled arguments to an allowed function were refused.
- **Outage test**: PASS.
- **Failover under enforcement**:
  - 16:49:06: login4's cron disabled and its endpoint stopped.
  - 16:54:22: login3 took over, and `doctor` passed there too, allowlist
    included.
  - 16:54:56: cron restored. 16:58:12: login3 handed back. 17:00:16: login4
    holds the endpoint.

Live check on Midway3 (2026-10-01), each request refused on the cluster:
- reading `/etc/passwd`;
- a symlink to `/etc` placed in home;
- a `~/../../` escape;
- account `ai4s-hackathon` and queue `bigmem`, which aren't in the policy;
- an injected walltime (`1:00 --wrap=id`);
- submitting a script that is readable but outside `script_roots`;
- a job ID with flags appended (`1 -u root`).

A real dependent pair on `caslake`/`pi-dfreedman` (59848684 → 59848685) was
submitted, followed, cancelled and read back. The outage test passes through
`gcx_shell`.

## Login-node failover

Midway3 has six login nodes, each with its own public name
(`midway3-login{1..6}.rcc.uchicago.edu`). The round-robin `midway3.rcc.uchicago.edu`
covers 1, 3, 4, 5 and 6; login2 is out of rotation, so it is avoided.
**Login nodes cannot SSH to each other** (publickey refused), so they cannot
manage each other. They coordinate through files in the shared home instead.
Cron is permitted (`/etc/cron.deny` is empty), and crontabs live on each node's
local `/var`, so they survive a reboot. A reimage of the node would wipe them.

Roles: **primary `midway3-login4`, backup `midway3-login3`.** Both run
`~/gc-endpoint/keepalive.sh <role>` from cron every 2 minutes. The script
(`src/gcx/data/keepalive.sh`) is the same everywhere; its `keepalive.env`
names the mode (`failover` or `single`) and nodes, and each node derives its
role from `hostname -s`. State lives in
`~/gc-endpoint/state/`:

| File | Meaning |
|---|---|
| `heartbeat.<node>` | touched every run; proves that node's cron is alive |
| `owner` | node running the endpoint; the owner refreshes it every run |
| `owner-is.<node>` | mirrors `owner` as a filename, because a Globus Transfer listing shows names and mtimes but not contents |
| `keepalive.log` | state changes only |

Rules:

- **Primary**: if the endpoint is running locally, refresh the lease. If the
  backup holds a fresh lease, wait. Otherwise start the endpoint.
- **Backup**: if the primary's heartbeat is fresh (under 360 s), stop any local
  endpoint and release the lease. Otherwise start or keep the endpoint.

The primary never starts while the backup's lease is fresh, so the two never
run concurrently.

- **"Is it running?"** is answered with `pgrep` on the local node, anchored to
  the endpoint's process title. The endpoint's own `daemon.pid` sits in shared
  home and may hold a process ID from the other node, so the script deletes it
  before every start.
- **Alerts**: the script prints only on state changes, so cron's `MAILTO`
  sends exactly those.
- **Crash loop**: an endpoint that keeps crashing on a healthy primary is
  restarted there and mailed each time; the backup does not take over, because
  the primary's heartbeat stays fresh. A crashing endpoint usually means a
  config or token problem, not a node problem.

### Failover test (2026-10-01)

| Time | Event |
|---|---|
| 11:42:18 | login4 "fails": its cron entry is commented out and the endpoint stopped |
| 11:48:24 | login3 starts the endpoint (primary heartbeat 364 s old) |
| 11:48:35 | a `gcx` task from the Mac runs on login3; same endpoint ID, nothing changed on the Mac |
| 11:49:21 | login4's cron entry restored |
| 11:52:15 | login3 sees the fresh primary heartbeat and stops |
| 11:54:16 | login4 starts the endpoint again |

- Takeover took 6 minutes. Handing back took 5 minutes, with 2 minutes of no
  endpoint while control passed between nodes, which is the price of never
  running two at once.
- The shared `endpoint.log` shows strictly alternating begin/end pairs, so the
  two nodes never ran the endpoint at once.
- A real reboot was not tested; cron was disabled instead.

## Setup

`gcx setup <cluster>` (`src/gcx/setup/steps.py`) runs ordered, idempotent
steps. Each one checks the current state, mostly from one probe round trip
(`probe.sh`), and either reports OK or applies a change and re-checks:

ssh → probe → venv → tokens → configure → endpoint-files → policy →
register → allowlist → running → config → keepalive → keepalive-files →
cron → enforced

then `doctor` and a test job.

Rules:

- **The allowlist goes into `config.yaml` before the first start**, so a new
  endpoint is never briefly unrestricted.
- **A first start by setup also writes the keepalive lease**, so on a
  failover cluster the other node's cron sees a holder and does not start a
  second copy.
- **Cron is proven by heartbeats, not by reading crontabs.** Login nodes can't
  reach each other, so a crontab on another node can't be read. A node whose
  heartbeat is fresh has working cron.
- **Keepalive mode is probe, then degrade**: `failover` when the profile has
  named login nodes and cron works, `single` with cron only, `none` without
  cron.

**Adoption.** Midway3 was installed by hand during the pilot. Its personal
config records `remote_root ~/gc-endpoint` and the failover roles, so setup
manages it in place. The first run rewrote four files with no change in
behaviour: the generic keepalive and its `.env`, and two endpoint files. It
restarted the endpoint through the keepalive. After that,
`gcx setup midway3 --dry-run` reports "nothing to change". Deleting the
endpoint ID and state path from the personal config and re-running restored
them exactly, without touching the cluster. The test job (59852493 on
`caslake`/`pi-dfreedman`) was submitted, followed and read back through
`gcx`, matching a random token.

## SSH bootstrap

Setup needs SSH once per cluster, so the repo carries what a colleague needs
to get there. Both pieces are taken from `physicsnemo/hpc/agent-toolkit`.

**`gcx ssh-config`** (`sshconfig.py`) writes one alias per cluster:
ControlMaster, a shared ControlPath, and ControlPersist from the profile
(`yes` for Polaris). It also writes one alias per named login node for
failover clusters, which setup prefers when it has to install cron on
another node.

- **Where it goes**: between `# >>> gcx >>>` fences, above any `Host *` (ssh
  takes the first value it sees).
- **What it leaves alone**: an alias already defined outside the fence, such
  as hpc-agent-toolkit's.
- **Adding a cluster** keeps the existing ones and their usernames, in their
  order.
- **No `ForwardAgent`**: forwarding your SSH agent to shared login nodes is a
  risk this tool doesn't need.
- **Checked against OpenSSH**: `ssh -G` resolves every generated alias in the
  tests.

**`gcx login`** is the bundled `morning-login` (`data/morning-login`, bash).
It keeps the toolkit's protections:

- a connect timeout and retries;
- pinning a healthy node when a round-robin address is wedged;
- clearing stale sockets on `--refresh`.

It takes the cluster list from your configured clusters. The "ask before
spending a token" rule now applies to any profile with
`mfa_single_use = true`. The physicsnemo Globus preflight is dropped, because
it depended on that repo's files. It is tested against a fake `ssh`, never
against real connections.

**Profiles for all seven clusters** carry SSH and scheduler facts from the
toolkit and research. Only Midway3's is `verified`. Three clusters have no
Globus Transfer collection exposing home, so `gcx status` is unavailable
there: Polaris (`/eagle`), DSI (group storage) and DeltaAI (home not exposed).
`gcx doctor` still works on all of them.

**Login-shell variables** such as `$WORK` and `$SCRATCH` (Stampede3) are not
set in the endpoint worker's environment. Setup therefore resolves them into
literal paths from the probe when it builds the policy. Only `$USER` and
`$HOME` stay symbolic.

## Submission safety

`globus_sdk`'s transport retries **any** network error up to 5 times, POSTs
included. A dropped reply to a submit would therefore resubmit, which means a
second `sbatch`. `gcx` makes submission at-most-once:

1. The SDK's own retries are off for the submit request
   (`retry_config.tune(max_retries=0)`).
2. Each submit opens a fresh connection. A pooled keep-alive socket that died
   during an outage fails on write, which looks exactly like a delivered
   request.
3. A failure is retried only if `never_sent` proves the request never left the
   Mac: connection refused, DNS failure, connect timeout or an unreachable
   proxy, plus HTTP 429/503. Anything else exits 75.

Two things count as possibly sent, both found by tests:

- A **proxy dropping its tunnel** is reported by `requests` exactly like a
  server hanging up after reading the request (`Connection aborted`).
- A **TLS error** may come from a failed handshake or from a connection lost
  mid-response. urllib3 raises the same `SSLError` for both.

Registering the function and waiting for results are retried freely. A
duplicate registration is harmless, and result checks only read.

`Client()` makes a version-check request when it is constructed. Without a
retry, `gcx` crashed outright if the network was down at startup; it is now
retried. The live outage test found this.

## Test record

- `uv run pytest`: 10 tests. They cover the classifier (against real socket
  failures wrapped the way globus_sdk wraps them) and the submit path (with a
  stand-in client: runs counted, SDK retries off and then restored).
- `tests/outage_test.sh` routes `gcx` through `tests/flaky_proxy.py`, which
  refuses connections and tears down open tunnels while "down". PASS:
  - **A**: a 90 s outage while a 30 s command runs. `gcx` retries and prints
    the result 9 s after the network returns.
  - **B**: the network is down when `gcx` starts and comes up after 40 s. `gcx`
    retries and the command runs **exactly once** (counted on the cluster).
- Job round trip on `caslake` with `-A pi-dfreedman` (there is no
  dfreedman-only CPU partition; the only one restricted to that account is
  `schmidt-gpu`): job 59840137 was submitted, followed with `sacct` and its
  output read, all through `gcx`.
- **Not tested: a real Wi-Fi drop.** The proxy simulates one, but this
  session could not drop the real network without cutting itself off.

## Open items

- **Security.** Midway3 is locked down to its registered functions (see
  Enforcement). Shell remains enabled there by choice; anything holding the
  Mac's tokens can still run `gcx_shell` until it is turned off in the policy.
- **Other clusters.** ALCF (Polaris) officially supports user endpoints on
  login nodes. Globus's docs carry example configs for Delta, Stampede3 and
  Midway; site policy on long-running login-node daemons still needs checking.
  Nothing was found for Derecho, which is being retired.
- **Cron email** from the failover test was not confirmed received.
- **Reimage risk.** If both nodes' crontabs are wiped, nothing restarts the
  endpoint. `gcx --status` shows both heartbeats as STALE in that case.
