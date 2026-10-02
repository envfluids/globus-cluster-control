# globus-cluster-control

Run commands and submit jobs on HPC clusters through
[Globus Compute](https://globus-compute.readthedocs.io/) instead of SSH.

SSH ControlMaster sockets die when the Mac's network blips, and rebuilding them
costs an MFA round trip per cluster. Here a small daemon (a Globus Compute
*endpoint*) runs on a cluster login node and holds an **outbound** connection to
Globus's cloud service. The Mac talks only to that service, over stateless HTTPS:
a dropped network costs a retry, not a login, and a submitted task waits in the
cloud until its result is collected.

Status: **all seven clusters set up and locked down** (2026-10-02); per-cluster record in [docs/clusters.md](docs/clusters.md). Design, failover and test record:
[docs/design.md](docs/design.md).

## Getting started

Colleagues: the full walk-through is [docs/colleague-setup.md](docs/colleague-setup.md).

From your laptop (macOS or Linux), with [uv](https://docs.astral.sh/uv/) and
the [Globus CLI](https://docs.globus.org/cli/) installed:

```bash
git clone git@github.com:envfluids/globus-cluster-control.git
cd globus-cluster-control && uv tool install -e .   # puts gcx on PATH

gcx ssh-config midway3 --user <cluster-username>    # preview the SSH aliases
gcx ssh-config midway3 --user <cluster-username> --apply
gcx login midway3                                   # you answer MFA, once a day
gcx setup midway3                                   # asks questions, installs, tests
```

Known clusters: delta, deltaai, derecho, dsi, midway3, polaris, stampede3
(`src/gcx/data/profiles/`). All have been set up and verified once; see
[docs/clusters.md](docs/clusters.md) for what each needs (failover or restart
on use, account spelling, test-job resources).

- **`gcx ssh-config`** writes SSH aliases with a shared connection per cluster
  into your SSH config, between `# >>> gcx >>>` markers. It also adds one
  alias per named login node on failover clusters. It never changes an alias
  you already have, and it puts its block above any `Host *` so that block
  can't override the username.
- **`gcx login`** opens those connections, so you answer MFA once a day.
  `--refresh` rebuilds them after a network change. On a single-use-MFA
  cluster (Polaris) it asks before closing a connection, because each login
  costs a token.

## Use

```bash
uv tool install -e .                         # once, from the repo; puts gcx on PATH
gcx midway3 jobs                             # your queued/running jobs
gcx midway3 history --since 2026-10-01       # finished jobs
gcx midway3 submit ~/runs/job.sbatch -A pi-dfreedman -p caslake -t 00:30:00
gcx midway3 cancel 59848685
gcx midway3 tail ~/runs/slurm-59848684.out -n 50
gcx midway3 ls '$SCRATCH'                    # also: head, du
gcx midway3 sh 'any shell command'           # only if shell is enabled for the cluster
gcx status midway3                           # which login node holds the endpoint
```

`gcx --help` lists everything. Add `--json` for machine-readable output, or
`--no-wait` to get a task id and collect it later with `gcx result <id>`.

- **What a cluster allows is fixed when its functions are registered.** Each
  command is a small Python function with the cluster's policy baked into its
  code: which directories can be read, which scripts can be submitted, which
  accounts, queues and QoS. Requests outside the policy are refused (exit 2)
  on the cluster. See [docs/design.md](docs/design.md#restricted-commands).
- Paths are relative to your cluster home unless absolute; `~` and `$VARS`
  expand on the cluster.
- Results are capped at 1 MB. For large files, use Globus Transfer.
- **Exit code 75** (`submit`, `cancel`, `sh` only) means the connection dropped
  after the request may have reached Globus, so it may or may not have run.
  Check with `jobs` before retrying. `gcx` never resubmits on its own in that
  case.
- `status` reads the keepalive state through Globus *Transfer*, so it still
  works when the compute endpoint is down. It exits 1 if no node holds a fresh
  lease.

Configuration lives in `~/.config/gcx/clusters/<cluster>.json`: endpoint ID,
policy and the registered function IDs. The endpoint accepts **only** those
functions. To change what a cluster allows, run `gcx setup <cluster> --menu`
(it re-asks the menu with your current answers as defaults, then registers and
enforces the result). `gcx doctor <cluster>` checks that everything works and
is locked down.

`gcx skill` (also run by every `gcx setup`) writes
`~/.claude/skills/gcx/SKILL.md` from your config, so Claude Code agents in
any repo know your clusters, what each allows, and the rules.

## Authentication

Both logins open a browser and are for the human to do, not an agent:

- **Laptop**: the first `gcx` call with no stored tokens prints a login URL.
- **Each cluster's endpoint**: `gcx setup <cluster>` runs the endpoint's login
  for you, over `ssh -t`, and you paste the code back.

Tokens are long-lived refresh tokens. There is no daily re-login.

## Layout

| Path | What |
|---|---|
| `src/gcx/` | the `gcx` command (`cli.py`), network-safety logic (`transport.py`), personal config (`config.py`), function registration (`registry.py`) |
| `src/gcx/capabilities/` | `runtime.py`, the code that runs on the cluster; `build.py`, which bakes a policy into it |
| `src/gcx/setup/` | `gcx setup`: probe, command menu, idempotent install steps |
| `src/gcx/data/` | cluster profiles (`profiles/*.toml`), endpoint templates, the keepalive script |
| `tests/` | unit tests incl. a fake scheduler (`uv run pytest`), live outage test (`tests/outage_test.sh`) |
| `docs/design.md` | architecture, failover design, measured results, open items |

## Setting up a cluster

```bash
gcx setup <cluster> --dry-run     # what would change; changes nothing
gcx setup <cluster>               # install / update, then `doctor` and a test job
```

`gcx setup` works from a profile of facts about the cluster
(`src/gcx/data/profiles/<cluster>.toml`) and your answers, stored in
`~/.config/gcx/clusters/<cluster>.json`. It goes step by step, and each step
checks before acting, so re-running it on a finished cluster changes nothing.
The steps:

1. **Check** that SSH works.
2. **Probe** the login node in one round trip: scheduler, cron, Python, outbound
   access to Globus, your accounts.
3. **Install** the endpoint into `~/.gcx/venv`.
4. **Log in to Globus** for the endpoint (you paste a code).
5. **Ask** which commands to allow (the command menu).
6. **Register** the functions and **write the allowlist** before the endpoint
   first starts.
7. **Set up the keepalive.** Failover if the cluster has named login nodes and
   cron, otherwise one node with cron, otherwise none.
8. **Test**: run `doctor`, then submit, follow and read back a test job.

Setup uses SSH (this is setup, not day-to-day use). It never logs in for you;
you do the MFA and Globus prompts.

The SDK version in `pyproject.toml` must match `globus-compute-endpoint` in
each cluster's `~/.gcx/venv` (currently 4.17.1). After bumping it, `gcx setup
<cluster>` reinstalls the endpoint to match. (Midway3's pilot install lives in
`~/gc-endpoint` and is managed in place.)
