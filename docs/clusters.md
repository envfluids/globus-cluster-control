# Clusters: setup record

What `gcx setup` found and did on each cluster, and what is still open.
Profiles are in `src/gcx/data/profiles/`; a profile's `verified` date is the
last full setup plus test job.

| Cluster | Status | Scheduler | Keepalive | `gcx status` | Verified |
|---|---|---|---|---|---|
| midway3 | set up, locked down | Slurm | failover login4 → login3 | yes | 2026-10-01 |
| dsi | set up, locked down | Slurm | single (fe02) | no (no collection exposes home) | 2026-10-01 |
| delta | profiled | Slurm | expected single | yes | — |
| deltaai | profiled | Slurm | expected single | no | — |
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
