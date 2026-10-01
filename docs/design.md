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
- **Locked-down template** (`endpoint/midway3/user_config_template.yaml.j2`).
  The default template lets each task install arbitrary packages, `curl | sh`
  the uv installer and `eval` user-supplied `worker_init`. All of that is
  removed, and the schema rejects every user-supplied variable. The worker's
  `PATH` is pinned in `user_environment.yaml`.
- **Sandboxing is off.** It would run each command in a fresh per-task
  directory and break relative paths such as `tail slurm-123.out`. `gc` drops
  the resulting per-task warning.
- **Network**: the Midway3 login node reaches `compute.api.globus.org:443` and
  `compute.amqps.globus.org` on 443 and 5671.

## Login-node failover

Midway3 has six login nodes, each with its own public name
(`midway3-login{1..6}.rcc.uchicago.edu`). The round-robin `midway3.rcc.uchicago.edu`
covers 1, 3, 4, 5 and 6; login2 is out of rotation, so it is avoided.
**Login nodes cannot SSH to each other** (publickey refused), so they cannot
manage each other. They coordinate through files in the shared home instead.
Cron is permitted (`/etc/cron.deny` is empty), and crontabs live on each node's
local `/var`, so they survive a reboot. A reimage of the node would wipe them.

Roles: **primary `midway3-login4`, backup `midway3-login3`.** Both run
`~/gc-endpoint/keepalive.sh <role>` from cron every 2 minutes. State lives in
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
| 11:48:35 | a `gc` task from the Mac runs on login3; same endpoint ID, nothing changed on the Mac |
| 11:49:21 | login4's cron entry restored |
| 11:52:15 | login3 sees the fresh primary heartbeat and stops |
| 11:54:16 | login4 starts the endpoint again |

- Takeover took 6 minutes. Handing back took 5 minutes, with 2 minutes of no
  endpoint while control passed between nodes, which is the price of never
  running two at once.
- The shared `endpoint.log` shows strictly alternating begin/end pairs, so the
  two nodes never ran the endpoint at once.
- A real reboot was not tested; cron was disabled instead.

## Submission safety

`globus_sdk`'s transport retries **any** network error up to 5 times, POSTs
included. A dropped reply to a submit would therefore resubmit, which means a
second `sbatch`. `gc` makes submission at-most-once:

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
retry, `gc` crashed outright if the network was down at startup; it is now
retried. The live outage test found this.

## Test record

- `uv run pytest`: 10 tests. They cover the classifier (against real socket
  failures wrapped the way globus_sdk wraps them) and the submit path (with a
  stand-in client: runs counted, SDK retries off and then restored).
- `tests/outage_test.sh` routes `gc` through `tests/flaky_proxy.py`, which
  refuses connections and tears down open tunnels while "down". PASS:
  - **A**: a 90 s outage while a 30 s command runs. `gc` retries and prints
    the result 9 s after the network returns.
  - **B**: the network is down when `gc` starts and comes up after 40 s. `gc`
    retries and the command runs **exactly once** (counted on the cluster).
- Job round trip on `caslake` with `-A pi-dfreedman` (there is no
  dfreedman-only CPU partition; the only one restricted to that account is
  `schmidt-gpu`): job 59840137 was submitted, followed with `sacct` and its
  output read, all through `gc`.
- **Not tested: a real Wi-Fi drop.** The proxy simulates one, but this
  session could not drop the real network without cutting itself off.

## Open items

- **Security.** Anything holding the Mac's Globus Compute tokens can run
  arbitrary commands as the user on Midway3. Before adding clusters, narrow
  this: the endpoint's `allowed_functions` list with a fixed set of registered
  functions, and/or a Globus authentication policy.
- **Other clusters.** ALCF (Polaris) officially supports user endpoints on
  login nodes. Globus's docs carry example configs for Delta, Stampede3 and
  Midway; site policy on long-running login-node daemons still needs checking.
  Nothing was found for Derecho, which is being retired.
- **Cron email** from the failover test was not confirmed received.
- **Reimage risk.** If both nodes' crontabs are wiped, nothing restarts the
  endpoint. `gc --status` shows both heartbeats as STALE in that case.
