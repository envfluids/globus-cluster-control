# Setting up gcx on your own clusters

This is the path from nothing to `gcx <cluster> jobs` working on every cluster
you use. It takes roughly 10–20 minutes per cluster, most of it waiting for
installs and a test job. You need to be at the keyboard for MFA prompts, one
Globus login per cluster, and the command menu.

## What you need

- A laptop running macOS or Linux (on Windows, use WSL).
- [uv](https://docs.astral.sh/uv/getting-started/installation/) and `git`.
- Optional: the [Globus CLI](https://docs.globus.org/cli/)
  (`uv tool install globus-cli`), which `gcx status` uses on clusters that
  have a home-directory collection.
- An account on each cluster, and access to this repository (ask the CeTD
  group; it is private under `envfluids`).

Nothing is installed system-wide on the clusters. Everything lives in
`~/.gcx` in your cluster home, about 60 MB.

## 1. Install

```bash
git clone git@github.com:envfluids/globus-cluster-control.git
cd globus-cluster-control
uv tool install -e .     # puts `gcx` on your PATH; -e: `git pull` updates it
gcx --help
```

## 2. SSH aliases and one login per cluster

```bash
gcx ssh-config delta polaris --user <your-cluster-username>          # preview
gcx ssh-config delta polaris --user <your-cluster-username> --apply  # write
gcx login delta polaris        # you answer MFA per cluster
```

- **`ssh-config`** adds aliases with a shared connection per cluster to your
  SSH config, between `# >>> gcx >>>` markers. A backup is kept, aliases you
  already have are left alone, and running it again for another cluster
  keeps the existing ones. Use `--user` per cluster if your usernames
  differ.
- **`login`** opens those connections; afterwards ssh never prompts. Run it
  once a day, or after your network changes (`gcx login --refresh`).
- **Polaris costs a single-use MFA token per login**, so its connection stays
  open until closed, and `gcx login --refresh` asks before closing it.

## 3. Set up each cluster

```bash
gcx setup delta --dry-run   # optional: see what it would do
gcx setup delta
```

Setup probes the cluster and installs the endpoint, then asks you to:

1. **Log in to Globus for the endpoint.** It prints a URL; sign in (choose
   your institution, or the site's identity: "Argonne LCF" for Polaris,
   "NCAR" for Derecho, ACCESS for Delta), then paste the code back.
2. **Choose which commands gcx may run**:
   - **job status** is always on;
   - **submit/cancel** takes scripts from directories you choose, only to the
     accounts and queues you list;
   - **read files** works in the directories you choose;
   - **arbitrary shell** is off unless you confirm it twice, because it makes
     the other restrictions advisory.

   For directories, paths you type are **added** to the suggestions; `-PATH`
   removes one; you confirm the final list. For accounts and queues, what you
   type **replaces** the suggestion.
3. **Choose how the endpoint is kept alive.** Setup offers what the cluster
   allows: failover between two named login nodes (which needs one more MFA
   login, to the backup node), one node with cron, or, where cron is
   blocked, a restart the next time you use gcx.
4. **Confirm a test job.** It is 1 task for about a minute, using the
   resources in the cluster's profile (on Polaris, a whole GPU node in
   `debug`).

It finishes with `gcx doctor`, the test job, and an updated Claude Code skill
(below). Running `gcx setup <cluster>` again is safe: finished steps are
skipped.

**To change your answers later**: `gcx setup <cluster> --menu`. It re-asks the
menu with your current answers as defaults, then applies them.

## 4. The watcher (setup offers it)

At the end of your first `gcx setup`, you are asked whether to install a
background watcher on your laptop. Say yes, especially if any of your clusters
block cron: Delta, DeltaAI, Derecho and Polaris do. On those, nothing on the
cluster restarts the endpoint when a login node reboots.

Every 30 minutes the watcher checks each endpoint's status with Globus (no
jobs, no charges):

- **"restarted it over SSH"**: it fixed an offline endpoint itself.
- **"Run: gcx login <cluster>"**: an endpoint is offline and your SSH
  connection has expired. Log in; the next check restarts it.
- **"offline … although cron keeps it alive"**: something is wrong with the
  cluster-side keepalive. Run `gcx doctor <cluster>`.

There is one watcher for all your clusters; clusters you set up later are
covered automatically. `gcx watch status` shows it, `gcx watch install` adds
it later, and `gcx watch uninstall` removes it. On macOS it is a launchd
agent; on Linux, a systemd user timer, or crontab where systemd isn't
available.

## 5. Use it

```bash
gcx delta jobs
gcx delta submit ~/runs/train.sbatch -A <account> -p <queue> -t 02:00:00
gcx delta tail ~/runs/slurm-123456.out -n 100
gcx delta cancel 123456
gcx doctor                  # health check of every cluster you set up
gcx doctor delta            # every check for one cluster
```

| Exit code | Meaning |
|---|---|
| 2, "gcx refused" | Outside what you allowed for the cluster. Change it with `gcx setup <c> --menu`. |
| 75 | Network dropped after a `submit`, `cancel` or `sh` may have been sent. Check `jobs` before retrying. |
| 69 | The endpoint is offline and gcx could not restart it. Run `gcx login <c>` and retry. |

## 6. Claude Code and other agents

Setup writes `~/.claude/skills/gcx/SKILL.md`, regenerated from your config,
so every Claude Code session in every repo knows:

- which clusters you have;
- what each one allows;
- the rules: no workarounds for refusals, no retry loops around `submit`,
  never run `gcx login` or `gcx setup` itself.

Run `gcx skill` to rewrite it by hand.

## Per-cluster notes

What each cluster needed is recorded in [clusters.md](clusters.md):
failover vs. restart on use, account spelling (TACC wants `TG-…` in
uppercase), test-job resources (DeltaAI and Polaris need a GPU), and home
quotas. If setup fails on a cluster, the message names the step; fix that and
re-run.

## Security, briefly

- The endpoint on each cluster runs **only** the registered `gcx` functions
  for your policy. Globus itself rejects anything else with HTTP 403, and
  the endpoint refuses pickled payloads.
- Your policy is written into the registered code, so changing it requires
  re-registering, which needs SSH to the cluster.
- With shell off, someone holding your Globus tokens can check and cancel
  your jobs, submit scripts that already exist in your chosen directories,
  and read files there. Nothing more.
