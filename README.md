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
policy, and the registered function IDs (`gcx register <cluster>` after
changing the policy). The pilot's `~/.config/gc-endpoints.json` and the
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
| `endpoint/midway3/` | endpoint config, keepalive script, crontabs, `deploy.sh` |
| `tests/` | unit tests incl. a fake scheduler (`uv run pytest`), live outage test (`tests/outage_test.sh`) |
| `docs/design.md` | architecture, failover design, measured results, open items |

## Changing the Midway3 endpoint

Edit files in `endpoint/midway3/`, then `endpoint/midway3/deploy.sh` to see the
diff against what is deployed, and `--apply` to push it. Deploying uses SSH
(this is setup, not day-to-day use). Crontabs are per login node and installed
by hand. See `docs/design.md`.

The SDK version in `pyproject.toml` must match `globus-compute-endpoint` in
`~/gc-endpoint/venv` on the cluster (currently 4.17.1).
