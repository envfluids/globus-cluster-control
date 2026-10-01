# globus-cluster-control — agent brief

Run commands and submit jobs on clusters through Globus Compute rather than SSH.
Read `README.md` for usage and `docs/design.md` before changing anything on
the cluster side.

- **Use `gcx <cluster> <verb>` for job control**: `jobs`, `history`, `queues`,
  `submit`, `cancel`, `ls`, `tail`, `head`, `du`, and `sh` only where enabled
  (`gcx --help`). It survives network drops; SSH sockets do not. Quote `$VARS`
  so they expand on the cluster.
- **Exit code 2 with "gcx refused"** means the cluster's policy forbids it. Do
  not work around it with `sh`; ask the human whether the policy should change.
- **Exit code 75 = submission state unknown.** Check with `squeue` (or the
  equivalent) before resubmitting. Never wrap `gcx submit` in your own retry
  loop; that would undo its at-most-once guarantee.
- **Never run `globus-compute-endpoint login`** or the Mac-side login. They open
  a browser; ask the human.
- **The endpoint runs on exactly one Midway3 login node** (primary login4,
  backup login3; see `docs/design.md`). Do not `start` it by hand on another
  node, and do not delete files in `~/gc-endpoint/state/`. Use
  `gcx --status midway3`.
- **The endpoint only runs registered gcx functions.** A raw Globus Compute
  `ShellFunction` gets 403 by design. Policy changes are
  `gcx register <c>` + `gcx allowlist <c> --apply`, and only with the human's
  OK. `gcx doctor <c>` is the health check.
- **Changing what is installed on a cluster**: edit the profile or templates
  under `src/gcx/data/`, then `gcx setup <c> --dry-run`, then `gcx setup <c>`.
  Do not edit files on the cluster directly. Setup asks questions and needs
  the human for MFA and Globus logins, so run it only when asked.
- **Commit and push only when asked.**
