#!/bin/bash
# Keep the Globus Compute endpoint alive on exactly one Midway3 login node.
#
#   keepalive.sh primary   # cron on midway3-login4
#   keepalive.sh backup    # cron on midway3-login3
#
# The nodes cannot ssh to each other, so they coordinate through files in the
# shared home ($STATE). Each run touches heartbeat.<node>; the node running the
# endpoint refreshes `owner`. The backup runs the endpoint only while the
# primary's heartbeat is stale, and hands it back once the primary is fresh.
# The primary never starts while the backup holds a fresh lease, so the two
# never run concurrently. Prints only on state changes, so cron MAILTO mails
# exactly the events worth knowing about.
set -u
USER=${USER:-$(id -un)}   # cron may not set it

PRIMARY=midway3-login4
BACKUP=midway3-login3
EP=midway3-login
GCE=$HOME/gc-endpoint/venv/bin/globus-compute-endpoint
EPDIR=$HOME/.globus_compute/$EP
STATE=$HOME/gc-endpoint/state
STALE=360            # seconds: 3 missed 2-minute ticks
LOG=$STATE/keepalive.log

role=${1:?usage: keepalive.sh primary|backup}
me=$(hostname -s)
case $role in
  primary) peer=$BACKUP; [ "$me" = "$PRIMARY" ] || { echo "keepalive: primary role on $me, expected $PRIMARY"; exit 1; } ;;
  backup)  peer=$PRIMARY; [ "$me" = "$BACKUP" ] || { echo "keepalive: backup role on $me, expected $BACKUP"; exit 1; } ;;
  *) echo "keepalive: unknown role $role"; exit 1 ;;
esac

mkdir -p "$STATE"
touch "$STATE/heartbeat.$me"

say() { echo "$(date '+%F %T') $me: $*" | tee -a "$LOG"; }   # stdout -> cron mail
age() { [ -e "$1" ] && echo $(( $(date +%s) - $(stat -c %Y "$1") )) || echo 999999; }
running() { pgrep -u "$USER" -f "^Globus Compute Endpoint .*, $EP\)" >/dev/null; }
owner() { cat "$STATE/owner" 2>/dev/null; }
# owner-is.<node> mirrors `owner` so `gc.py status` can see the holder from a
# Globus Transfer listing, which shows names and mtimes but not contents.
claim() {
  echo "$me" > "$STATE/owner.tmp.$me" && mv -f "$STATE/owner.tmp.$me" "$STATE/owner"
  touch "$STATE/owner-is.$me"; rm -f "$STATE/owner-is.$peer"
}

start() {
  # daemon.pid lives in shared home and may hold a pid from the other node.
  rm -f "$EPDIR/daemon.pid"
  if "$GCE" start --detach "$EP" >>"$LOG" 2>&1 && sleep 10 && running; then
    claim; say "started endpoint ($1)"
  else
    say "FAILED to start endpoint ($1); see $LOG and $EPDIR/endpoint.log"
  fi
}

stop() {
  "$GCE" stop "$EP" >>"$LOG" 2>&1
  sleep 5
  running && pkill -u "$USER" -f "^Globus Compute Endpoint .*, $EP\)"
  [ "$(owner)" = "$me" ] && rm -f "$STATE/owner"
  rm -f "$STATE/owner-is.$me"
  say "stopped endpoint ($1)"
}

peer_fresh() { [ "$(age "$STATE/heartbeat.$peer")" -lt "$STALE" ]; }
lease_fresh() { [ "$(age "$STATE/owner")" -lt "$STALE" ]; }

if [ "$role" = primary ]; then
  if running; then
    [ "$(owner)" = "$me" ] || say "endpoint running; reclaiming lease from '$(owner)'"
    claim
  elif [ "$(owner)" = "$peer" ] && lease_fresh; then
    : # backup holds it; it will hand back once it sees our heartbeat
  else
    start "primary"
  fi
else
  if peer_fresh; then
    running && stop "primary is back"
  elif running; then
    claim
  else
    start "primary heartbeat $(age "$STATE/heartbeat.$peer")s old"
  fi
fi
