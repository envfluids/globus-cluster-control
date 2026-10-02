# Sent by `gcx setup` as `ssh <alias> bash -s`. Read-only: prints key=value
# lines (lists comma-separated) describing what this login node offers.
# Must work in a minimal bash on any cluster; every probe tolerates failure.
# GCX_ROOT, GCX_EP and GCX_ENV_VARS (profile worker_env) are prepended by the caller.

kv() { printf '%s=%s\n' "$1" "$2"; }
first() { local p; p=$(command -v "$1" 2>/dev/null) && [ -n "$p" ] && echo "$p"; }

kv host "$(hostname -s 2>/dev/null)"
kv fqdn "$(hostname -f 2>/dev/null)"
kv user "$(id -un)"
kv home "$HOME"
kv shell_ulimit_v "$(ulimit -v 2>/dev/null)"
# Site variables that only the login shell sets; the endpoint's worker does not
# start from a login shell, so setup resolves them into literal paths.
for v in WORK SCRATCH PROJECT $GCX_ENV_VARS; do kv "env_$v" "${!v}"; done
kv home_free_kb "$(df -Pk "$HOME" 2>/dev/null | awk 'NR==2{print $4}')"

# Scheduler
if sb=$(first sbatch); then
  kv scheduler slurm
  kv sched_bin "$(dirname "$sb")"
  kv accounts "$(sacctmgr -nP show assoc user="$USER" format=account 2>/dev/null | sort -u | paste -sd, -)"
  kv queues "$(sinfo -h -o %P 2>/dev/null | tr -d '*' | sort -u | paste -sd, -)"
  kv qos "$(sacctmgr -nP show assoc user="$USER" format=qos 2>/dev/null | tr ',' '\n' | sort -u | grep -v '^$' | paste -sd, -)"
elif qs=$(first qsub); then
  kv scheduler pbs
  kv sched_bin "$(dirname "$qs")"
  kv queues "$(qstat -Q 2>/dev/null | awk 'NR>2{print $1}' | sort -u | paste -sd, -)"
else
  kv scheduler none
fi

# Python / uv
kv python3 "$(python3 -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null)"
kv uv "$(first uv || { [ -x "$HOME/.local/bin/uv" ] && echo "$HOME/.local/bin/uv"; })"
kv curl "$(first curl)"

# Outbound reachability of Globus Compute (AMQPS on 5671, or 443 fallback)
tcp() { timeout 5 bash -c "</dev/tcp/$1/$2" 2>/dev/null && echo ok || echo blocked; }
kv net_api_443 "$(tcp compute.api.globus.org 443)"
kv net_amqps_5671 "$(tcp compute.amqps.globus.org 5671)"
kv net_amqps_443 "$(tcp compute.amqps.globus.org 443)"

# Cron
if command -v crontab >/dev/null 2>&1; then
  out=$(crontab -l 2>&1); rc=$?
  if [ $rc -eq 0 ] || echo "$out" | grep -qi "no crontab"; then
    kv cron ok
    kv crontab_gcx "$(echo "$out" | grep -c 'keepalive.sh' 2>/dev/null)"
  else
    kv cron denied
  fi
else
  kv cron missing
fi

# Existing gcx installation (for idempotent setup / adoption)
gce="$GCX_ROOT/venv/bin/globus-compute-endpoint"
kv gce_version "$([ -x "$gce" ] && "$gce" version 2>/dev/null | awk '{print $NF}')"
kv tokens "$([ -f "$HOME/.globus_compute/storage.db" ] && echo yes || echo no)"
epdir="$HOME/.globus_compute/$GCX_EP"
kv ep_configured "$([ -d "$epdir" ] && echo yes || echo no)"
kv ep_id "$(sed -n 's/.*"endpoint_id": *"\([^"]*\)".*/\1/p' "$epdir/endpoint.json" 2>/dev/null)"
kv ep_running "$(pgrep -u "$USER" -f "^Globus Compute Endpoint .*, $GCX_EP\)" >/dev/null && echo yes || echo no)"
kv keepalive "$([ -x "$GCX_ROOT/keepalive.sh" ] && echo yes || echo no)"

# Keepalive state in the shared home: who holds the endpoint, and each node's
# heartbeat age -- a fresh heartbeat proves that node's cron entry works.
st="$GCX_ROOT/state"
now=$(date +%s)
kv owner "$(cat "$st/owner" 2>/dev/null)"
kv owner_age "$([ -e "$st/owner" ] && echo $(( now - $(stat -c %Y "$st/owner") )))"
hb=""
for f in "$st"/heartbeat.*; do
  [ -e "$f" ] || continue
  hb="$hb${hb:+,}${f##*/heartbeat.}:$(( now - $(stat -c %Y "$f") ))"
done
kv heartbeats "$hb"
kv restart_pending "$([ -e "$st/restart-request" ] && echo yes || echo no)"
