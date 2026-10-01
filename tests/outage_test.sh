#!/bin/bash
# Live outage test against a real endpoint: routes gcx through tests/flaky_proxy.py
# and cuts the "network" at two points.
#
#   tests/outage_test.sh [cluster]     # default midway3; takes ~3 minutes
#
#   A. outage while waiting for a result  -> gcx retries and prints the result
#   B. outage at submit time              -> gcx retries; the command runs exactly once
set -u
cluster=${1:-midway3}
here=$(cd "$(dirname "$0")" && pwd)
gc="$here/../.venv/bin/gcx"
port=18080
flag=$(mktemp -u "${TMPDIR:-/tmp}/gc-net-down.XXXX")
fail=0

"$here/../.venv/bin/python" "$here/flaky_proxy.py" $port "$flag" &
proxy=$!
trap 'kill $proxy 2>/dev/null; wait $proxy 2>/dev/null; rm -f "$flag"' EXIT
export HTTPS_PROXY=http://127.0.0.1:$port
sleep 1

outage() {  # outage <start-after-s> <duration-s>
  ( sleep "$1"; touch "$flag"; echo "$(date +%T) >>> network down"
    sleep "$2"; rm -f "$flag"; echo "$(date +%T) >>> network up" ) &
}

echo "== A: 90s outage while a 30s remote command runs"
outage 8 90; toggler=$!
out=$("$gc" "$cluster" sh 'sleep 30; echo "A-ok on $(hostname -s)"' 2> >(sed 's/^/  /' >&2))
wait $toggler
echo "  stdout: $out"
[[ $out == A-ok* ]] || { echo "A FAILED"; fail=1; }

echo "== B: network already down at submit, up after 40s"
token=$(date +%s)$$
touch "$flag"; echo "$(date +%T) >>> network down"
( sleep 40; rm -f "$flag"; echo "$(date +%T) >>> network up" ) & toggler=$!
out=$("$gc" "$cluster" sh "mkdir -p ~/gc-endpoint/test; echo x >> ~/gc-endpoint/test/once.$token; wc -l < ~/gc-endpoint/test/once.$token; rm ~/gc-endpoint/test/once.$token" 2> >(sed 's/^/  /' >&2))
wait $toggler
echo "  times the command ran: $out"
[[ $out == 1 ]] || { echo "B FAILED"; fail=1; }

[ $fail = 0 ] && echo "PASS" || echo "FAIL"
exit $fail
