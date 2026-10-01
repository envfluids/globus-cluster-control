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
uv sync                                   # once
.venv/bin/gc midway3 'squeue -u $USER'    # run, wait, print; exits with the command's rc
.venv/bin/gc midway3 --submit 'sbatch job.sbatch'   # print a task id and return
.venv/bin/gc --result <task-id>           # collect it later
.venv/bin/gc --status midway3             # which login node holds the endpoint
```

- Commands run as you, in your home directory, on a Midway3 login node, under
  bash, with a 600 s default walltime (`--walltime`). About 2–7 s per call.
- Output is capped at a few MB per result. For large files, use Globus Transfer.
- **Exit code 75** means the connection dropped after the request may have
  reached Globus. The command may or may not be running. Check (for example
  with `squeue`) before resubmitting. `gc` never resubmits on its own in that
  case, so a retried `sbatch` cannot run twice.
- `--status` reads the keepalive state through Globus *Transfer*, so it still
  works when the compute endpoint is down. It exits 1 if no node holds a fresh
  lease.

Configuration lives in `~/.config/gc-endpoints.json`
(format: [endpoints.example.json](endpoints.example.json)).

## Authentication

Both logins open a browser and are for the human to do, not an agent:

- **Mac**: the first `gc` call with no stored tokens prints a login URL.
- **Cluster**: `ssh -t midway3 '~/gc-endpoint/venv/bin/globus-compute-endpoint login'`.

Tokens are long-lived refresh tokens. There is no daily re-login.

## Layout

| Path | What |
|---|---|
| `src/globus_cluster_control/cli.py` | the `gc` command |
| `endpoint/midway3/` | endpoint config, keepalive script, crontabs, `deploy.sh` |
| `tests/` | unit tests (`uv run pytest`), live outage test (`tests/outage_test.sh`) |
| `docs/design.md` | architecture, failover design, measured results, open items |

## Changing the Midway3 endpoint

Edit files in `endpoint/midway3/`, then `endpoint/midway3/deploy.sh` to see the
diff against what is deployed, and `--apply` to push it. Deploying uses SSH
(this is setup, not day-to-day use). Crontabs are per login node and installed
by hand. See `docs/design.md`.

The SDK version in `pyproject.toml` must match `globus-compute-endpoint` in
`~/gc-endpoint/venv` on the cluster (currently 4.17.1).
