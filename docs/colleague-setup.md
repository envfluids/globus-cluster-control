# Setting up gcx

This is the path from a fresh computer to `gcx <cluster> jobs` working, with
Midway3 as the worked example. Allow about 10 minutes for the laptop and
20 minutes per cluster, most of it waiting for installs and a test job. You
need to be at the keyboard throughout: there are MFA prompts, two Globus
logins (a third if you want the optional `gcx status`) and a few questions.

For a cluster gcx has no profile for yet, see [new-cluster.md](new-cluster.md)
first.

## What you need

- A laptop running macOS or Linux. On Windows, use WSL (Ubuntu); everything
  below runs inside it.
- An account on the cluster, with its MFA working. On Midway3 that is your
  CNetID, password and Duo.
- The name of the allocation your jobs are charged to. On Midway3 that is
  your PI's account, `pi-<name>`; `rcchelp` or your PI can tell you.
- A Globus identity. On Midway3 your UChicago login works: when Globus asks,
  choose "University of Chicago".
- Optional: the [Globus CLI](https://docs.globus.org/cli/), for
  `gcx status <cluster>`, which shows which login node holds the endpoint.
  It is useful on the clusters kept alive by cron whose Globus collection
  exposes your home: Midway3 and Stampede3. Install it with
  `uv tool install globus-cli`, then run `gcx login globus` once: it logs the
  Globus CLI in and grants it access to each of your clusters' collections
  (a URL and a code for each). `gcx login globus --check` shows what is
  missing, and setup offers it at the end. Nothing else in gcx needs the
  Globus CLI, and `gcx doctor` works without it.

Nothing is installed system-wide on the cluster: everything lives in `~/.gcx`
in your cluster home, about 60 MB.

## 1. Laptop tools

gcx needs `git` and [uv](https://docs.astral.sh/uv/). Skip what you already
have (`git --version`, `uv --version`).

```bash
# macOS: git comes with Apple's command-line tools (a few minutes)
xcode-select --install

# Linux or WSL (Ubuntu)
sudo apt update && sudo apt install -y git curl

# uv, on any of them
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Open a new terminal afterwards, so `uv` is on your PATH.

## 2. Install gcx

```bash
git clone https://github.com/envfluids/globus-cluster-control.git
cd globus-cluster-control
uv tool install -e .     # puts `gcx` on your PATH; -e: `git pull` updates it
gcx --help
```

If your shell says `gcx: command not found`, run `uv tool update-shell` and
open a new terminal.

## 3. SSH aliases and one login per cluster

```bash
gcx ssh-config midway3 --user <CNetID>           # preview
gcx ssh-config midway3 --user <CNetID> --apply   # write
gcx login midway3                                # password + Duo
```

- **`ssh-config`** adds SSH aliases to `~/.ssh/config`, between
  `# >>> gcx >>>` markers, each with a shared connection (a ControlMaster).
  For Midway3 that is `midway3`, plus `midway3-login1`, `-login3` ... for
  the named login nodes that failover uses. It keeps a backup
  (`~/.ssh/config.bak.<date>`), leaves aliases you already have alone, and
  keeps the existing ones when you run it again for another cluster. Pass
  `--user`: the default is your laptop username, which is usually not your
  cluster username.
- **`login`** opens those connections. Afterwards ssh never prompts, for
  24 hours on Midway3. Run it once a day, or after your network changes
  (`gcx login --refresh`).
- **Polaris costs a single-use MFA token per login**, so its connection stays
  open until closed, and `gcx login --refresh` asks before closing it.

## 4. Set up the cluster

```bash
gcx setup midway3 --dry-run   # optional: see what it would do
gcx setup midway3
```

Setup prints one line per step. Here is what happens, in order, and what you
type:

1. **Probe and install.** It checks the login node (scheduler, cron, Python,
   network) and installs the Globus Compute endpoint in `~/.gcx/venv`. This
   takes a few minutes; nothing to type.
2. **The endpoint's Globus login.** It prints a URL. Open it, sign in
   ("University of Chicago" on Midway3; "Argonne LCF" for Polaris, "NCAR"
   for Derecho, ACCESS for Delta), and paste the code back. This gives the
   endpoint on the cluster its own credentials.
3. **Choose what gcx may do on this cluster**:
   - **job status** is always on;
   - **submit and cancel** takes scripts from directories you choose;
   - **run scripts with arguments** (Slurm only; off by default) lets
     `gcx run` submit scripts from chosen repos (see [Use it](#7-use-it));
   - **read files** works in the directories you choose;
   - **arbitrary shell** is off unless you confirm it twice, because it makes
     the other restrictions advisory.

   For directories, paths you type are **added** to the suggestions, `-PATH`
   removes one, and you confirm the final list. For accounts and queues, what
   you type **replaces** the suggestion. On Midway3, sensible answers are:

   | Question | Midway3 |
   |---|---|
   | Directories to read / submit from | the defaults (`~`, `/scratch/midway3/$USER`), plus `/project/<pi>` if you use it |
   | Accounts | your `pi-<name>` |
   | Queues | `caslake` (CPU), plus any GPU partition you use |
   | QoS | blank, unless your group has one |
   | Launchers (if you turned on run) | a repo root such as `~/repos/myproject`; Enter for the command (`cat`) and timeout |

4. **The laptop's Globus login.** The first time this computer registers
   anything, it prints `Please authenticate with Globus here:` and a URL, then
   `Enter the resulting Authorization Code here:`. Sign in with the same
   identity as in step 2 and paste the code. It is stored in
   `~/.globus_compute/storage.db` and not asked again.
5. **How the endpoint is kept alive.** Setup offers what the cluster allows.
   On Midway3, answer yes to "Primary midway3-login4, backup midway3-login1?"
   (your node names may differ) and give an email for cron alerts. Setup then
   logs in to the backup node to install its cron entry: **one more Duo
   prompt**. Clusters without cron (Delta, DeltaAI, Derecho, Polaris) offer
   "restart on use" instead.
6. **Lock-down and checks.** It restarts the endpoint so Globus enforces the
   allowlist, writes the agent skills (below), and runs `gcx doctor`.
7. **The test job.** It shows the account and queue it will use; press Enter
   to accept, then confirm. It is one task for about a minute (on Polaris, a
   whole GPU node in `debug`).
8. **The watcher.** It asks whether to install it on this laptop; see
   section 5.
9. **The Globus CLI (optional; Midway3 and Stampede3).** If you installed it
   (see [What you need](#what-you-need)), setup offers `gcx login globus` so
   that `gcx status` works; the default is no, and you can run it any time.
   If you did not, it prints a one-line hint instead.

Running `gcx setup midway3` again is safe: finished steps are skipped. If it
stops with "blocked at `<step>`", fix what that line says and re-run it.

## 5. The watcher

Say yes when setup offers it, especially if any of your clusters block cron:
Delta, DeltaAI, Derecho and Polaris do. On those, nothing on the cluster
restarts the endpoint when a login node reboots.

Every 30 minutes the watcher checks each endpoint's status with Globus (no
jobs, no charges):

- **"restarted it over SSH"**: it fixed an offline endpoint itself.
- **"Run: gcx login <cluster>"**: an endpoint is offline and your SSH
  connection has expired. Log in; the next check restarts it.
- **"offline … although cron keeps it alive"**: something is wrong with the
  cluster-side keepalive. Run `gcx doctor <cluster>`.

There is one watcher for all your clusters, and clusters you set up later are
covered automatically. `gcx watch status` shows it, `gcx watch install` adds
it later, and `gcx watch uninstall` removes it. On macOS it is a launchd
agent. On Linux it is a systemd user timer, or a crontab entry where systemd
isn't available. On WSL, systemd is often off (turn it on with
`[boot]` / `systemd=true` in `/etc/wsl.conf`, then `wsl --shutdown` from
Windows), the cron service may not be running (`sudo service cron start`),
and there are no desktop notifications, so check `gcx watch status` now and
then.

## 6. A second computer

Do **not** run `gcx setup` for the same cluster from a second computer. On
the same endpoint, it would register a new set of functions and replace the
allowlist, so the first computer's calls would be refused. Under another
endpoint name or directory, it would start a second endpoint beside the
first. Setup now stops at its `other-computers` step and says so. Instead:

1. Do sections 1–3 on the new computer.
2. Copy `~/.config/gcx/` from the first computer.
3. Run any gcx command, such as `gcx midway3 jobs`. The first one asks for
   this computer's Globus login (as in step 4 above).
4. Run `gcx skill` to write the agent skills here too.
5. If you use `gcx status`, run `gcx login globus` here too: Globus CLI
   logins and consents belong to each computer.

Keep the watcher on one computer only.

If the first computer's `~/.config/gcx/` is gone (a lost or wiped laptop),
run `gcx setup midway3 --take-over` on the new one. It sets up as if it were
the first computer. If the old endpoint had a different name or directory
from the one setup uses, it is left running; remove it as in section 10.

## 7. Use it

```bash
gcx midway3 jobs
gcx midway3 submit ~/runs/train.sbatch -A pi-<name> -p caslake -t 02:00:00
gcx midway3 tail ~/runs/slurm-123456.out -n 100
gcx midway3 cancel 123456
gcx doctor                  # health check of every cluster you set up
gcx doctor midway3          # every check for one cluster
gcx status midway3          # which login node holds the endpoint (needs the Globus CLI)
```

- **`submit`** needs an account and a queue, from the flags or from the
  script's own `#SBATCH -A`/`-p` (`#PBS -A`/`-q`) lines, and both must be ones
  you allowed. It refuses otherwise and says which is missing.
- **`run`** submits a script with arguments:

  ```bash
  gcx midway3 run --dry-run ~/repos/myproject/jobs/train.sh --epochs 10   # check first
  gcx midway3 run ~/repos/myproject/jobs/train.sh --epochs 10
  ```

  The script must be under a launcher root, and its header must set the
  account and partition (or pass `-A`/`-p` before the script). gcx options go
  before the script; everything after it is the script's `"$@"`. `--dry-run`
  prints the script and the `sbatch` command without submitting anything.
- **Add a launcher** for another repo without re-answering the menu
  (it restarts the endpoint for about a minute):

  ```bash
  gcx launcher add midway3 ~/repos/other                 # scripts as written
  gcx launcher add midway3 ~/repos/other --dry-run       # check only
  gcx launcher list midway3
  gcx launcher remove midway3 ~/repos/other
  ```

| Exit code | Meaning |
|---|---|
| 2, "gcx refused" | Outside what you allowed for the cluster. Change it with `gcx setup <c> --menu` (or `gcx launcher add` for `run`). |
| 75 | Network dropped after a `submit`, `run`, `cancel` or `sh` may have been sent. Check `jobs` before retrying. |
| 69 | The endpoint is offline and gcx could not restart it. Run `gcx login <c>` and retry. |

**To change your answers later**: `gcx setup <cluster> --menu` re-asks the
menu with your current answers as defaults, then applies them.

## 8. Claude Code and other agents

Setup writes two skills under `~/.claude/skills/`, regenerated from your
config, so every Claude Code session in every repo knows:

- `gcx`: which clusters you have, what each one allows, and the rules: no
  workarounds for refusals, no retry loops around `submit`, never run
  `gcx login`, `gcx setup` or `gcx launcher add` itself;
- `gcx-run-author`: how to make a script runnable with `gcx run`, ending
  with the `gcx launcher add` command for you to run.

Run `gcx skill` to rewrite them by hand.

## 9. When something goes wrong

| What you see | What to do |
|---|---|
| `gcx: command not found` | `uv tool update-shell`, then open a new terminal. |
| ssh asks for a password or Duo during setup | The connection expired: `gcx login <c>`, then re-run setup. |
| Duo push timed out | Re-run the command; finished steps are skipped. |
| Setup: "blocked at `ssh`" | `gcx login <c>` (or `gcx ssh-config <c> --apply` if the alias is new). |
| Setup: "blocked at `probe`" | The login node cannot reach Globus Compute; the line says which. Ask the site. |
| Setup: "blocked at `other-computers`" | Another computer set up this cluster: copy its `~/.config/gcx/` (section 6). |
| Doctor: "functions registered for current policy" FAIL | gcx itself was updated: run `gcx setup <c>`. |
| Exit 69 | `gcx login <c>`; the next call restarts the endpoint. |
| "gcx refused" | Read the reason: a path, account or queue you did not allow. `gcx setup <c> --menu`. |
| `run` refused: "not under a launcher root" | `gcx launcher list <c>`; add one with `gcx launcher add`. |
| `gcx status` says to install the Globus CLI or run `gcx login globus` | Do that (see [What you need](#what-you-need)); `gcx login globus --check` confirms. |
| The Globus login URL does not open (WSL) | Copy it into your Windows browser. |

## 10. Removing gcx

**One cluster.** The commands below use the default endpoint name `gcx` and
directory `~/.gcx`. Yours are `endpoint_name` and `remote_root` in
`~/.config/gcx/clusters/<cluster>.json`.

Remove the cron entries first. Otherwise the keepalive restarts the
endpoint within 2 minutes of you stopping it. Cron entries are per login
node; with failover there are two, on `keepalive.primary` and
`keepalive.backup` in that file. Start with the backup, so that it cannot
take over while the primary's entry is still there:

```bash
ssh midway3-login1          # the backup node: a login of its own (MFA)
crontab -l | grep -v '/\.gcx/keepalive\.sh ' | crontab -
crontab -l                  # no line runs .gcx/keepalive.sh any more
exit
```

The `grep` matches gcx's line by the script it runs, which works for entries
written by any gcx version and leaves every other line alone. Repeat it on
the primary node. Then, on the node that holds the endpoint
(`cat ~/.gcx/state/owner` names it):

```bash
~/.gcx/venv/bin/globus-compute-endpoint stop gcx
~/.gcx/venv/bin/globus-compute-endpoint delete gcx --yes   # unregisters it with Globus too
rm -rf ~/.gcx ~/.globus_compute/uep.<endpoint-id>.*
```

The `uep.` directories are the endpoint's per-session workers, named after
its ID (`endpoint` in your config). Then, on the laptop:

```bash
rm ~/.config/gcx/clusters/midway3.json
gcx skill                 # rewrite the agent skills without it
```

and delete that cluster's `Host` blocks between the `# >>> gcx >>>` markers
in `~/.ssh/config`.

**Everything**, after removing each cluster:

```bash
gcx watch uninstall
uv tool uninstall globus-cluster-control
rm -rf ~/.config/gcx ~/.claude/skills/gcx ~/.claude/skills/gcx-run-author
rm ~/.globus_compute/storage.db   # the laptop's Globus login for gcx
```

If you installed the Globus CLI only for `gcx status`, also run
`globus logout` and `uv tool uninstall globus-cli`.

## Per-cluster notes

What each cluster needed is recorded in [clusters.md](clusters.md):
failover vs. restart on use, account spelling (TACC wants `TG-…` in
uppercase), test-job resources (DeltaAI and Polaris need a GPU), and home
quotas.

## Security, briefly

- The endpoint on each cluster runs **only** the registered `gcx` functions
  for your policy. Globus itself rejects anything else with HTTP 403, and
  the endpoint refuses pickled payloads.
- Your policy is written into the registered code, so changing it requires
  re-registering, which needs SSH to the cluster.
- With shell off, someone holding your Globus tokens can check and cancel
  your jobs, submit scripts that already exist in your chosen directories
  (to your allowed accounts and queues), and read files there. Nothing more.
