# globus-cluster-control

`gcx` runs job-control commands on HPC clusters through
[Globus Compute](https://globus-compute.readthedocs.io/) instead of SSH. It
checks jobs and queues, submits and cancels batch jobs, and reads logs and
files.

## Why

Interactive SSH to HPC clusters is fragile. Multiplexed connections die when
the network changes, and rebuilding them costs an MFA prompt per cluster,
which scripts and AI agents cannot answer. With `gcx`, a small Globus Compute
*endpoint* runs on each cluster's login node and holds an **outbound**
connection to the Globus service. Your laptop talks only to that service, over
stateless HTTPS:

- **A dropped network costs a retry, not a login.** Tokens are long-lived;
  there is no daily MFA for gcx itself.
- **Tasks wait in the cloud** until you collect their results, so a
  submission survives the laptop sleeping.
- **Submission is at-most-once.** If the connection drops after a `submit`
  may have been delivered, gcx stops and says so rather than risk running it
  twice.

## How it is locked down

Each cluster runs only what you allowed for it when you set it up:

- **Commands are small registered functions**, not shell: job status (always
  on), submit/cancel, read files, and optionally arbitrary shell.
- **Your choices are written into the registered code**: which directories
  may be read, which scripts may be submitted, and which accounts, queues and
  QoS. A request outside them is refused on the cluster.
- **The endpoint accepts nothing else.** Its function allowlist means Globus
  rejects any other code with HTTP 403, and it refuses pickled payloads.
- **Only you can use it.** It is a single-user endpoint, owned by your Globus
  identity.

Details: [docs/design.md](docs/design.md).

## Install

You need a laptop running macOS or Linux (WSL works),
[uv](https://docs.astral.sh/uv/) and git. The
[Globus CLI](https://docs.globus.org/cli/) is optional: `gcx status` uses it
where the cluster's Globus collection exposes your home directory.

```bash
git clone git@github.com:envfluids/globus-cluster-control.git
cd globus-cluster-control
uv tool install -e .          # puts `gcx` on PATH; `git pull` updates it
```

## Set up a cluster

```bash
gcx ssh-config <cluster> --user <username>           # preview SSH aliases
gcx ssh-config <cluster> --user <username> --apply   # write them
gcx login <cluster>                                  # you answer MFA
gcx setup <cluster>                                  # install, choose, test
```

`gcx setup` probes the login node and installs the endpoint under `~/.gcx`
in your cluster home (about 60 MB, nothing system-wide). It then has you:

- log in to Globus for the endpoint;
- choose which commands, directories, accounts and queues gcx may use;
- pick how the endpoint is kept alive. The options depend on the cluster:
  failover between two login nodes, cron on one node, or a restart the next
  time you use gcx.

It finishes with a health check and a short test job. Re-running it is safe,
since finished steps are skipped. `--dry-run` shows what it would do, and
`--menu` changes your choices later.

The full walk-through is in [docs/colleague-setup.md](docs/colleague-setup.md).

## Use

```bash
gcx <cluster> jobs                         # your queued and running jobs
gcx <cluster> history --since 2026-10-01   # finished jobs
gcx <cluster> queues
gcx <cluster> submit ~/runs/job.sbatch -A <account> -p <queue> -t 01:00:00
gcx <cluster> cancel <job-id>
gcx <cluster> tail ~/runs/job.out -n 100   # also: head, ls, du
gcx <cluster> sh '<command>'               # only where shell is enabled
gcx doctor <cluster>                       # health check
gcx status <cluster>                       # which login node holds the endpoint
```

- Paths are relative to your cluster home unless absolute. Quote `~` and
  `$VARS` so they expand on the cluster.
- `--json` gives machine-readable output. `--no-wait` returns a task ID to
  collect later with `gcx result <id>`.
- Results are capped at 1 MB; use Globus Transfer for large files.

| Exit code | Meaning |
|---|---|
| 2, "gcx refused" | Outside what you allowed for this cluster; change it with `gcx setup <cluster> --menu`. |
| 75 | The network dropped after a `submit`, `cancel` or `sh` may have been sent. Check `jobs` before retrying. |
| 69 | The endpoint is offline and could not be restarted. Run `gcx login <cluster>` and retry. |

## Supported clusters

Profiles of site facts live in `src/gcx/data/profiles/`: delta, deltaai,
derecho, dsi, midway3, polaris, stampede3. Each one records the login host,
scheduler (Slurm or PBS), named login nodes, test-job resources and site
quirks. Your own choices stay in `~/.config/gcx/` on your laptop.
[docs/clusters.md](docs/clusters.md) records what setting up each cluster
involved.

Adding a cluster means adding a profile. Setup learns everything else by
probing the cluster.

## AI agents

`gcx skill`, also run by every `gcx setup`, writes a
[Claude Code](https://claude.com/claude-code) skill to
`~/.claude/skills/gcx/SKILL.md` from your configuration. Agents in any repo
then know:

- which clusters you have;
- what each one allows;
- the rules: respect refusals, never retry-loop `submit`, and leave `gcx
  login` and `gcx setup` to the human.

## Layout

| Path | What |
|---|---|
| `src/gcx/cli.py` | the `gcx` command |
| `src/gcx/transport.py` | retries and at-most-once submission over an unreliable network |
| `src/gcx/capabilities/` | the code that runs on the cluster (`runtime.py`) and the step that writes a policy into it (`build.py`) |
| `src/gcx/setup/` | `gcx setup`: probe, command menu, idempotent install steps |
| `src/gcx/data/` | cluster profiles, endpoint templates, the keepalive script, the bundled `gcx login` script |
| `docs/` | design, per-cluster record, setup guide |
| `tests/` | unit tests (`uv run pytest`), plus a live outage test (`tests/outage_test.sh`) |

## Development

```bash
uv sync
uv run pytest
```

The tests cover the following, and CI runs them on Linux and macOS:

- the network-safety logic, against real socket failures;
- the cluster-side code, against a fake scheduler, and under Python 3.9
  and 3.13;
- failover, with two simulated login nodes;
- the setup decisions;
- the generated SSH config, checked with OpenSSH.

`globus-compute-sdk` in `pyproject.toml` must match the
`globus-compute-endpoint` version installed on the clusters. After changing
it, `gcx setup <cluster>` reinstalls the endpoint to match.
