# globus-cluster-control

Run commands and submit jobs on HPC clusters through
[Globus Compute](https://globus-compute.readthedocs.io/) instead of SSH.

SSH ControlMaster sockets die when the Mac's network blips, and rebuilding them
costs an MFA round trip per cluster. Here a small daemon (a Globus Compute
*endpoint*) runs on a cluster login node and holds an **outbound** connection to
Globus's cloud service. The Mac talks only to that service, over stateless HTTPS:
a dropped network costs a retry, not a login, and a submitted task waits in the
cloud until its result is collected.

Status: **pilot, Midway3 only** (2026-10-01). Design, failover and test record:
[docs/design.md](docs/design.md).

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
functions. After changing a policy, run `gcx register <cluster>`, then
`gcx allowlist <cluster> --apply`; calls are refused until both are done.
`gcx doctor <cluster>` checks that everything works and is locked down. The pilot's `~/.config/gc-endpoints.json` and the
`gcx <cluster> '<cmd>'` form still work; the old `gc` command is deprecated.

## Authentication

Both logins open a browser and are for the human to do, not an agent:

- **Mac**: the first `gcx` call with no stored tokens prints a login URL.
- **Cluster**: `ssh -t midway3 '~/gc-endpoint/venv/bin/globus-compute-endpoint login'`.

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
`~/gc-endpoint/venv` on the cluster (currently 4.17.1).
