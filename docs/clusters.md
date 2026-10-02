# Clusters: setup record

What `gcx setup` found and did on each cluster, and what is still open.
Profiles are in `src/gcx/data/profiles/`; a profile's `verified` date is the
last full setup plus test job.

| Cluster | Status | Scheduler | Keepalive | `gcx status` | Verified |
|---|---|---|---|---|---|
| midway3 | set up, locked down | Slurm | failover login4 → login3 | yes | 2026-10-01 |
| dsi | set up, locked down | Slurm | single (fe02) | no (no collection exposes home) | 2026-10-01 |
| delta | set up, locked down | Slurm | on-use (no cron) | yes, but no keepalive state to show | 2026-10-02 |
| deltaai | set up, locked down | Slurm | on-use (no cron) | no | 2026-10-02 |
| stampede3 | profiled | Slurm | unknown (cron policy unknown) | yes | — |
| derecho | profiled | PBS | expected single | yes | — |
| polaris | profiled | PBS | expected single | no | — |

## midway3

Pilot cluster, then adopted by `gcx setup` in place (`remote_root
~/gc-endpoint`, endpoint `midway3-login`). See `docs/design.md` for the
failover, enforcement and adoption records.

- Probe: Slurm in `/software/slurm-current-el8-x86_64/bin`; cron allowed;
  system Python 3.6, so the endpoint's 3.12 comes from uv; outbound 443 and
  5671 open.
- Six login nodes with public names; they cannot SSH to each other. login2 is
  outside the round-robin rotation.
- The probe's queue and QoS lists cover every partition visible on the
  cluster, not just yours, so the menu suggests the profile's test queue and
  lets you type more.

## dsi (2026-10-01)

Fresh install, run by the user in a terminal. Setup stopped at the Globus
login the first time it ran without one.

- **Probe** (`fe02`): Slurm in `/usr/bin`, one account (`general_group`),
  partitions `general`, `dev`, `Monsoon`, `ai+s`. Python 3.12.3. Cron
  allowed. Outbound 443 and 5671 open. No home quota (`/home` NFS, 67 TB free).
- **Install**: about 15 s. uv was already in `~/.local/bin`; the probe missed
  it, a bug fixed the same day.
- **Choices**:
  - commands: status, submit, read, shell;
  - read under `~`, `/net/scratch/$USER` and `/net/monsoon`. Home and
    scratch were left out by mistake in setup's menu and added the same day
    with `gcx register` + `gcx allowlist --apply`;
  - submit only from `/net/monsoon` and setup's `~/.gcx/test`;
  - account `general_group`, queue `general`;
  - keepalive `single` on fe02, since the login is behind a load balancer.
- **Result**:
  - `doctor` passes on every check: allowlist of 10 functions in force; a raw
    `ShellFunction` gets 403.
  - Test job 2003225 on `general` (node `j002-ds`) completed, and its output
    carried the run's token.
  - `gcx setup dsi --dry-run` reports nothing to change.
- **Limits**:
  - If fe02 goes down, nothing restarts the endpoint until the next
    `gcx setup dsi`, and `gcx status` can't show it (no Transfer collection
    exposes home). `gcx doctor dsi` is the check.
  - The load balancer may send a new SSH connection to fe01. Setup and
    `doctor` don't care which node they land on; only the cron entry is tied
    to fe02.

## delta (2026-10-02)

Fresh install. The automatic part (probe, install) ran without a terminal;
the user ran the rest.

- **Probe** (`dt-login04`):
  - Slurm in `/usr/bin`; Python 3.9 on the login node; uv in `~/.local/bin`.
  - Outbound 443 and 5671 open; home `/u/awikner`.
  - **cron is denied** ("not allowed to access crontab because of pam
    configuration") and **scrontab is disabled**, so nothing on Delta can
    restart the endpoint.
- **Accounts**: only `bdiu-delta-gpu` (about 10,000 GPU hours) and `noalloc`.
  The CPU account `bdiu-delta-cpu` from the agent-toolkit config no longer
  exists, so the test job uses a GPU partition.
- **Choices**:
  - commands: status, submit, read, shell;
  - read and submit under `~`, `/work/nvme/bdiu/$USER`, `/work/hdd/bdiu/$USER`.
    The first choice was the whole `/work/nvme` and `/work/hdd` filesystems;
    it was narrowed the same day;
  - account `bdiu-delta-gpu`;
  - queues `gpuA40x4-interactive` and the A100 and H200 partitions (with
    their interactive variants);
  - keepalive **on-use**.
- **Result**:
  - `doctor` passes on every check: allowlist of 10 functions in force; a raw
    `ShellFunction` gets 403.
  - Test job 22621089 on `gpuA40x4-interactive` (node `gpub034`, 4 s)
    completed, and its output carried the run's token.
  - `gcx setup delta --dry-run` reports nothing to change.
- **Restart on use, tested live**:
  - 08:15:54: the endpoint was stopped, and the Globus service reported it
    offline.
  - The next `gcx delta jobs` printed "endpoint is offline; restarting it
    over SSH", started it on dt-login04, and returned the job list. The whole
    call took 21.5 s, and one endpoint process was left running.
- **Limits**:
  - Recovery needs a live SSH connection (`gcx login delta`, 24 h). Without
    one, gcx exits 69 and says so.
  - Login nodes reboot on NCSA's schedule; dt-login04 rebooted Fri Oct 2,
    23:59 CDT. The first call after a reboot pays the restart (about 20 s).
  - Login is a round-robin with no per-node names, so a restart lands on
    whichever node the SSH connection is pinned to.
  - `gcx status delta` has a Transfer collection, but without a keepalive
    there are no heartbeat files to show. `gcx doctor delta` is the check.

## deltaai (2026-10-02)

Fresh install, run by the user. The GH200 login nodes are **aarch64**: uv's
Python 3.12 and the `globus-compute-endpoint` wheels installed without
trouble. The endpoint reports Python 3.12.11.

- **Probe** (`gh-login02`): Slurm; cron disabled as on Delta; accounts
  `bdiu-dtai-gh` and `noalloc`. Partitions bill mostly by GPU (4 GH200 per
  node).
- **Choices**:
  - commands: status, submit, read, shell;
  - account `bdiu-dtai-gh`;
  - queues `ghx4`, `ghx4-interactive`;
  - keepalive **on-use**;
  - directories `~`, `/work/nvme/bdiu/$USER`, `/work/hdd/bdiu/$USER`
    (narrowed from the whole filesystems the same day; `/work` is shared
    with Delta).
- **Setup's last step failed at first**: DeltaAI rejects jobs that request
  no GPU, and the profile's test job asked for 1 CPU only. Fixed in the
  profile (`--gpus-per-node=1` on `ghx4-interactive`). Re-run: test job
  3292775 completed, and its output carried the run's token.
- **Result**: `doctor` passes on every check; `gcx setup deltaai --dry-run`
  reports nothing to change.
- **Limits**: as for Delta (on-use restart needs `gcx login deltaai`). There
  is no `gcx status`, because no Transfer collection exposes DeltaAI's home.

### Bug found while narrowing (Delta, 2026-10-02)

`gcx allowlist --apply` always asked the keepalive to restart the endpoint.
On a cluster with no cron keepalive (on-use, none) nothing honoured that, so
the call timed out. The endpoint kept the old allowlist while the laptop held
the new function IDs, and calls would have been refused.

`allowlist --apply` now restarts the endpoint directly over SSH when there is
no cron keepalive, and clears the stale request. Re-applied on Delta, then
used for DeltaAI.
