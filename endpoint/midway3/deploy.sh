#!/bin/bash
# Push this directory's endpoint files to Midway3 over the `midway3` SSH alias.
#
#   endpoint/midway3/deploy.sh          # show what would change
#   endpoint/midway3/deploy.sh --apply  # copy the changed files
#
# Does not touch crontabs (per node; see crontab.primary / crontab.backup) and
# does not restart the endpoint: keepalive.sh is re-read on the next cron tick,
# and template changes apply the next time the endpoint starts its worker.
# Written for macOS's bash 3.2 (no associative arrays).
set -eu
here=$(cd "$(dirname "$0")" && pwd)
apply=${1:-}
ep=.globus_compute/midway3-login
files="keepalive.sh:gc-endpoint/keepalive.sh
user_config_template.yaml.j2:$ep/user_config_template.yaml.j2
user_config_schema.json:$ep/user_config_schema.json
user_environment.yaml:$ep/user_environment.yaml"

ssh -o BatchMode=yes midway3 true || { echo "midway3 unreachable over SSH"; exit 1; }
changed=""
for pair in $files; do
  f=${pair%%:*} d=${pair#*:}
  remote=$(ssh -o BatchMode=yes midway3 "cat ~/$d 2>/dev/null || true")
  if ! diff -u --label "midway3:$d" <(printf '%s\n' "$remote") --label "$f" "$here/$f"; then
    changed="$changed $pair"
  fi
done
[ -z "$changed" ] && { echo "midway3 is up to date"; exit 0; }
[ "$apply" = --apply ] || { echo "changed:$changed (rerun with --apply)"; exit 0; }
for pair in $changed; do
  f=${pair%%:*} d=${pair#*:}
  scp -q "$here/$f" "midway3:$d"
  echo "copied $f -> $d"
done
ssh -o BatchMode=yes midway3 'chmod 700 ~/gc-endpoint/keepalive.sh'
