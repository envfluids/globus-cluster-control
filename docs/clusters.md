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
| stampede3 | set up, locked down | Slurm | failover login4 → login1 | yes | 2026-10-02 |
| derecho | set up, locked down | PBS | on-use (no cron) | yes, but no keepalive state to show | 2026-10-02 |
| polaris | set up, locked down | PBS | on-use (no cron) | no | 2026-10-02 |

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

## stampede3 (2026-10-02)

Fresh install, run by the user. It took four rounds to get through: a menu
bug, then three TACC-specific facts that only a real submission revealed.

- **Probe** (`login4`):
  - Slurm; cron **allowed**. Login nodes `login1`–`login5` have public names
    (the round-robin covers 1–4), so failover works.
  - Python 3.12.9 and uv present; outbound 443 and 5671 open.
  - 8 GB per-process virtual memory cap (`ulimit -v`). The throttled uv
    install took 84 s and the endpoint runs under the cap.
  - **`$HOME` quota is 14 GB, 85% used**, not the 25 GB the profile first
    said. uv's cache is pointed at `$SCRATCH` (`UV_CACHE_DIR`); the venv
    takes 59 MB.
- **Choices**:
  - commands: status, submit, read, shell;
  - directories `~`, `$WORK` and `$SCRATCH`, resolved to
    `/work2/09979/awikner/stampede3` and `/scratch/09979/awikner`;
  - queues `skx`, `skx-dev`, `spr`, `h100`, `pvc`, `icx`;
  - account `TG-ATM170020`;
  - keepalive failover, primary login4 and backup login1, with cron
    installed on both; `gcx status` shows both heartbeats.
- **What went wrong, in order**:
  1. **`y` saved as a script directory.** It was typed at "Add directories"
     in answer to the "Use this?" that follows, and only failed at
     registration, with a traceback. Fixed: entries are checked as typed,
     y/n are explained, invalid saved answers send setup back to the menu,
     and QoS `none` means empty.
  2. **The `default` account.** sacctmgr lists a pseudo-account `default`
     that cannot be used. It was removed, and setup no longer suggests it.
  3. **Account case.** Slurm reports `tg-atm170020`, but TACC's submit filter
     rejects anything but `TG-ATM170020` ("Unknown project"). The profile
     sets `accounts_upper`, so setup suggests the uppercase name. On the
     laptop, `gcx submit -A` maps any case to the policy's spelling.
  4. **Login-shell variables.** TACC's filter aborts without them ("Unable to
     query WORK2 environment variable"). The profile's `worker_env` names
     the variables to copy from the login shell into the endpoint's
     `user_environment.yaml`: WORK, WORK2, SCRATCH, STOCKYARD, STOCKYARD2,
     ARCHIVE, TACC_SYSTEM, TACC_DOMAIN.
  5. **Banner before the job ID.** TACC's `sbatch --parsable` prints a
     welcome banner, an env dump and checks to stdout before the job ID, so
     the runtime's `job_id` was all of that text. Setup then followed a job
     whose ID it could not parse until its 30-minute limit. Fixed on the
     laptop side (`cli.job_id_of`: the last line that looks like a job ID),
     and setup now fails at once if history refuses the ID. The runtime fix
     is deferred to the next change that re-registers functions anyway.
- **Result**:
  - `doctor` passes on every check.
  - Test jobs 3557959 and 3558018 on `spr` completed with the run's token
    (the first one finished, but setup could not follow it, per item 5).
  - A CLI `submit` with the lowercase account printed job 3558038.
  - `gcx setup stampede3 --dry-run` reports nothing to change.
- **Failover test** (same method as on Midway3):

  | Time | Event |
  |---|---|
  | 11:18:54 | login4's cron entry commented out and its endpoint stopped |
  | 11:24:17 | login1 starts the endpoint (primary heartbeat 360 s old) |
  | 11:24 | `doctor` passes against login1, allowlist included; `status` flags login4 STALE; the worker on login1 has `$WORK2` |
  | 11:24:47 | login4's cron entry restored |
  | 11:28:11 | login1 sees login4's fresh heartbeat and stops |
  | 11:30:14 | login4 starts the endpoint again |

  - Takeover took 5.5 minutes. Hand-back took 5.5 minutes, including
    2 minutes with no endpoint.
  - Afterwards one endpoint process runs on login4, and the dry run reports
    nothing to change.

## derecho (2026-10-02)

Fresh install, run by the user. **This was the first live use of the PBS
paths** (`qsub`, `qstat`, `qdel`).

- **Probe** (`derecho6`):
  - PBS Pro; cron **denied** (PAM), as on Delta. `derecho1`–`8` have public
    names, but without cron there is no failover.
  - System Python 3.11; the endpoint's 3.12 comes from uv.
  - Home quota 100 GB (69% used).
  - Project groups `uchi0014`, `uchi0018` and `uric0009`.
- **Found before running, by reading the PBS paths**:
  - Setup's test job looked for `" F "`, but `qstat -x -f` prints
    `job_state = F` at the end of a line, so setup would never have seen a
    PBS job finish.
  - Setup only knew Slurm's output file name; PBS writes `<name>.o<number>`.
  - PBS has no account list to probe. On NCAR, projects are Unix groups, so
    the profile's `accounts_from_groups` plus `accounts_upper` make setup
    suggest `UCHI0014`, `UCHI0018` and `URIC0009`.
- **Choices**:
  - commands: status, submit, read, shell;
  - read `~`, `/glade/work/$USER`, `/glade/derecho/scratch/$USER`,
    `/glade/campaign/univ/uchi0018`;
  - submit from the first three;
  - accounts `URIC0009`, `UCHI0014`, `UCHI0018`;
  - keepalive on-use.
  - Queues were first entered as `cpu`, `gpu`, `cpudev` and `gpudev`, which
    are where jobs land after routing. They were corrected to the routing
    queues users submit to, `main` and `develop`.
- **Result**:
  - `doctor` passes on every check.
  - Test job 7686534.desched1 in `develop` (routed to `cpudev`) completed;
    its `.o` file was read back with the run's token.
  - **Cancel tested live**: a 2-minute sleep job was running (`R`), and
    `gcx derecho cancel` ended it (`job_state = F`, `Exit_status = 271`,
    PBS's code for a qdel kill).
  - Refused: queue `cpu`; account `NCAR0001`.
- **Limits**:
  - Restart on use needs `gcx login derecho`.
  - The cluster is being retired, with data moving to Delta.

## polaris (2026-10-02)

Fresh install, run by the user over the existing SSH master, so no extra
single-use MFA token was spent.

- **Probe** (`polaris-login-04`):
  - PBS in `/opt/pbs/bin`; cron **denied**. System Python 3.6; the
    endpoint's 3.12 comes from uv. Outbound 443 and 5671 open.
  - Projects are Unix groups (`lighthouse-uchicago`, `MDClimateSim`,
    `MDClimSim`, `AI-S2S`). The profile's `accounts_from_groups` skips
    `users` and the hardware `*_users` groups.
  - **`/home` quota 76.1 GB, 95% used (72.3 GB).** uv's cache goes to
    node-local `/tmp`; the venv takes 21 MB.
- **Choices**:
  - commands: status, submit, read, shell;
  - read and submit under `~` and `/eagle/lighthouse-uchicago`;
  - account `lighthouse-uchicago`;
  - queues `debug`, `debug-scaling`, `prod`, `preemptable`, `capacity`;
  - keepalive on-use.
  - `-l filesystems=home:eagle` is added to every submission (profile
    `submit_extra`).
  - The endpoint runs **2 workers**, per ALCF's "with caution" for user
    endpoints on login nodes.
- **Result**:
  - `doctor` passes on every check; the dry run reports nothing to change.
  - Test job 7707542 in `debug` ran on one GPU node (`x3111c0s7b0n0`), exited
    0, and its `.o` file carried the run's token.
- **Bug found**: `~` was saved twice in the read roots. A typed `~/` was
  compared with `~` before the trailing slash was removed. Duplicates are now
  compared and removed after normalising; the policy was cleaned and
  re-applied.
- **Limits**:
  - On-use restarts need the SSH master. On Polaris it persists until closed
    (`ControlPersist yes`), so it is normally up, and `gcx login` asks before
    spending a token on it.
  - No `gcx status`, since the Polaris collection is rooted at `/eagle`, not
    home.
