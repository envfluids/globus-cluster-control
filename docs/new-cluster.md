# Adding a cluster

gcx knows a cluster through its **profile**: a short TOML file of facts about
the site, in `src/gcx/data/profiles/<name>.toml`. Setup probes everything
else (scheduler path, cron, Python, network, your accounts) when it runs.
Your own choices never go in the profile; they stay in
`~/.config/gcx/clusters/<name>.json` on your laptop.

So adding a cluster is: gather a few facts, write the profile, then the usual
`ssh-config`, `login` and `setup` from [colleague-setup.md](colleague-setup.md).

## 1. Facts to gather

Most come from the site's user guide; check the rest by logging in once.

| Fact | Where it goes | How to find it |
|---|---|---|
| Login hostname | `ssh_host` | The site's "connecting" page. |
| Scheduler | `scheduler` | `slurm` (`squeue` exists) or `pbs` (`qstat`). |
| Named login nodes reachable from outside | `login_nodes` | `hostname -f` on a few logins, then `ssh <user>@<that name>` from your laptop. Leave it empty if the nodes are behind a load balancer or refuse direct SSH. |
| Is MFA single-use (each login costs a token)? | `mfa_single_use` | ALCF-style token MFA is; Duo pushes are not. |
| How long the site lets a connection live | `ssh_persist` | `24h` unless the site says otherwise; single-use MFA sites use `yes` (until closed). |
| A partition and resources for a 1-minute test job | `[test_job]` | The site's job examples. Some sites require a GPU or node/task counts. |
| A Globus collection that exposes your home | `transfer_collection`, `transfer_home` | The site's Globus page. Optional: only `gcx status` uses it. |
| Site quirks | the fields below | The user guide, and what goes wrong in step 4. |

You do not need to know whether cron is allowed or whether the login node can
reach Globus: setup probes both and says so.

## 2. Write the profile

Start from the closest existing profile (`midway3.toml` for a Slurm site with
Duo, `derecho.toml` for PBS) and change the facts. A minimal profile:

```toml
# Example University Cluster. Facts about the site, not about any user.
name = "example"
description = "Example University Cluster"
ssh_host = "login.example.edu"
scheduler = "slurm"

login_nodes = [            # optional: enables login-node failover
  "login1.example.edu",
  "login2.example.edu",
]
read_roots = ["~", "/scratch/$USER"]     # suggested in the menu
script_roots = ["~", "/scratch/$USER"]

[test_job]
queue = "debug"
resources = ["--ntasks=1", "--cpus-per-task=1", "--mem=1G", "--time=00:02:00"]
```

`name`, `ssh_host` and `scheduler` are required; everything else has a
default (`src/gcx/profiles.py`):

| Field | Default | What it is for |
|---|---|---|
| `description` | | Shown in the agent skill. |
| `ssh_persist` | `24h` | How long a `gcx login` connection lives. |
| `mfa_single_use` | `false` | `gcx login --refresh` asks before spending a token. |
| `login_nodes` | `[]` | Full names of nodes reachable by name; two or more, plus cron, offer failover. |
| `read_roots`, `script_roots` | `["~"]` | Directory suggestions in the menu. `$USER` and `$HOME` always work; other `$VARS` are replaced with their probed value. |
| `submit_extra` | `[]` | Flags added to every submission (Polaris: `-l filesystems=home:eagle`). |
| `accounts_upper` | `false` | Upper-case account names (TACC's submit filter, NCAR projects). |
| `accounts_from_groups` | `""` | Regex: suggest matching Unix groups as accounts (NCAR, where projects are groups). |
| `worker_env` | `[]` | Login-shell variables the site's tools need ($WORK2 on TACC), copied into the endpoint's environment. |
| `uv_env` | `{}` | Environment for installing the endpoint (Stampede3 throttles uv and moves its cache off a small home). |
| `max_workers` | `4` | Endpoint worker processes. |
| `transfer_collection`, `transfer_home` | `""`, `/~/` | For `gcx status`; `gcx login globus` grants the Globus CLI access to it. |
| `notes` | `""` | One line of site notes, shown to agents in the skill. |
| `verified` | `""` | Date of the last full setup plus test job; fill it in after step 4. |
| `[test_job]` | | `queue` and `resources` (scheduler directives) for setup's test job. |

Because gcx is installed with `uv tool install -e .`, the new file is picked
up immediately; there is nothing to reinstall.

## 3. SSH

```bash
gcx ssh-config example --user <username>           # preview: check the Host blocks
gcx ssh-config example --user <username> --apply
gcx login example                                  # you answer MFA
ssh -O check example                               # "Master running" = good
```

If the site needs more than a hostname and user (a jump host, a specific key),
add those lines to your own `Host example` block in `~/.ssh/config` instead:
ssh-config leaves an existing alias alone. gcx only needs `ssh example` to
work without a prompt once `gcx login` has run.

## 4. Set up and fix what it finds

```bash
gcx setup example --dry-run   # probes and reports; changes nothing
gcx setup example
```

The dry run stops at the first step that needs something:

- **"expected slurm, found …"**: fix `scheduler`.
- **"login node cannot reach Globus Compute"**: outbound HTTPS (443) and
  AMQPS (5671, or 443) to `compute.api.globus.org` and
  `compute.amqps.globus.org` are blocked. gcx cannot run there; ask the site.
- **cron "denied"**: fine. Setup offers "restart on use", and the laptop
  watcher covers reboots.
- **Install failures** (memory caps, small home quotas): see `uv_env`, and the
  Stampede3 entry in [clusters.md](clusters.md).
- **The test job fails**: usually `[test_job]` resources, a required account,
  or a submit filter (see `accounts_upper`, `worker_env`, `submit_extra`).

Then follow [colleague-setup.md](colleague-setup.md), section 4.

## 5. Record it

- Set `verified` in the profile to today's date.
- Add a section to [clusters.md](clusters.md): what setup found, what needed
  changing, and anything still open. The existing sections show the format.
- Open a pull request with the profile and the notes, so the next person
  starts from your facts.
