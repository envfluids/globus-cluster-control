#!/bin/bash
# Keep a Globus Compute endpoint alive on exactly one login node. Run from cron
# every 2 minutes; installed and configured by `gcx setup`.
#
# Settings come from keepalive.env next to this script:
#   MODE     failover | single
#   PRIMARY  failover: preferred node (hostname -s)    single: the only node
#   BACKUP   failover: node that takes over while PRIMARY's heartbeat is stale
#   EP       endpoint name             GCE  globus-compute-endpoint binary
#   STATE    shared-home state dir     STALE  seconds (default 360 = 3 missed ticks)
#
# Failover: nodes cannot reach each other, so they coordinate through files in
# the shared home. Each run touches heartbeat.<node>; the node running the
# endpoint refreshes `owner`. The backup runs the endpoint only while the
# primary's heartbeat is stale and hands it back once it is fresh; the primary
# never starts while the backup holds a fresh lease, so the two never run
# concurrently. Single: the one node restarts the endpoint if it has died.
#
# Prints only on state changes, so cron MAILTO mails exactly those events.
set -u
USER=${USER:-$(id -un)}   # cron may not set it
here=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=/dev/null
. "$here/keepalive.env" || { echo "keepalive: cannot read $here/keepalive.env"; exit 1; }
STALE=${STALE:-360}
SETTLE=${SETTLE:-10}   # seconds to let the endpoint daemonize before checking it
EPDIR=$HOME/.globus_compute/$EP
LOG=$STATE/keepalive.log

me=$(hostname -s)
case "$MODE:$me" in
  failover:"$PRIMARY") role=primary; peer=$BACKUP ;;
  failover:"$BACKUP")  role=backup;  peer=$PRIMARY ;;
  single:"$PRIMARY")   role=single;  peer= ;;
  *) echo "keepalive: $me has no role (MODE=$MODE PRIMARY=$PRIMARY BACKUP=${BACKUP:-})"; exit 1 ;;
esac
# Crontabs pass the role as a check; a mismatch means the crontab is on the wrong node.
if [ $# -gt 0 ] && [ "$1" != "$role" ]; then
  echo "keepalive: asked for role '$1' but $me is '$role'"; exit 1
fi

mkdir -p "$STATE"
touch "$STATE/heartbeat.$me"

say() { echo "$(date '+%F %T') $me: $*" | tee -a "$LOG"; }   # stdout -> cron mail
mtime() { stat -c %Y "$1" 2>/dev/null || stat -f %m "$1"; }   # GNU, then BSD (tests)
age() { [ -e "$1" ] && echo $(( $(date +%s) - $(mtime "$1") )) || echo 999999; }
running() { pgrep -u "$USER" -f "^Globus Compute Endpoint .*, $EP\)" >/dev/null; }
owner() { cat "$STATE/owner" 2>/dev/null; }
# owner-is.<node> mirrors `owner` so `gcx status` can see the holder from a
# Globus Transfer listing, which shows names and mtimes but not contents.
claim() {
  echo "$me" > "$STATE/owner.tmp.$me" && mv -f "$STATE/owner.tmp.$me" "$STATE/owner"
  touch "$STATE/owner-is.$me"
  [ -n "$peer" ] && rm -f "$STATE/owner-is.$peer"
  return 0
}

start() {
  # daemon.pid lives in shared home and may hold a pid from the other node.
  rm -f "$EPDIR/daemon.pid"
  if "$GCE" start --detach "$EP" >>"$LOG" 2>&1 && sleep "$SETTLE" && running; then
    claim; say "started endpoint ($1)"
  else
    say "FAILED to start endpoint ($1); see $LOG and $EPDIR/endpoint.log"
  fi
}

stop() {
  "$GCE" stop "$EP" >>"$LOG" 2>&1
  sleep $(( SETTLE / 2 ))
  running && pkill -u "$USER" -f "^Globus Compute Endpoint .*, $EP\)"
  [ "$(owner)" = "$me" ] && rm -f "$STATE/owner"
  rm -f "$STATE/owner-is.$me"
  say "stopped endpoint ($1)"
}

peer_fresh() { [ "$(age "$STATE/heartbeat.$peer")" -lt "$STALE" ]; }
lease_fresh() { [ "$(age "$STATE/owner")" -lt "$STALE" ]; }

# `gcx allowlist --apply` touches restart-request after pushing a new config;
# whichever node runs the endpoint restarts it. The lease is kept (claim, not
# stop/start) so the other node never sees a gap in which to start a second one.
if running && [ -e "$STATE/restart-request" ]; then
  rm -f "$STATE/restart-request"
  claim
  "$GCE" stop "$EP" >>"$LOG" 2>&1
  sleep $(( SETTLE / 2 ))
  running && pkill -u "$USER" -f "^Globus Compute Endpoint .*, $EP\)"
  rm -f "$EPDIR/daemon.pid"
  claim
  if "$GCE" start --detach "$EP" >>"$LOG" 2>&1 && sleep "$SETTLE" && running; then
    claim; say "restarted endpoint (requested)"
  else
    say "FAILED to restart endpoint on request; see $LOG and $EPDIR/endpoint.log"
  fi
  exit 0
fi

case $role in
  single)
    if running; then claim; else start "not running"; fi ;;
  primary)
    if running; then
      [ "$(owner)" = "$me" ] || say "endpoint running; reclaiming lease from '$(owner)'"
      claim
    elif [ "$(owner)" = "$peer" ] && lease_fresh; then
      : # backup holds it; it will hand back once it sees our heartbeat
    else
      start "primary"
    fi ;;
  backup)
    if peer_fresh; then
      running && stop "primary is back"
    elif running; then
      claim
    else
      start "primary heartbeat $(age "$STATE/heartbeat.$peer")s old"
    fi ;;
esac
exit 0
