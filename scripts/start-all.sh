#!/usr/bin/env bash
# Start the whole system on one Linux / WSL machine:
#   HAProxy + registrar + N data-warehouse nodes (each with its own MongoDB).
#
#   bash scripts/start-all.sh            # 3 nodes, round robin
#   LB_ALGO=leastconn bash scripts/start-all.sh 4
set -e
source "$(dirname "$0")/common.sh"
N="${1:-3}"

bash scripts/start-lb.sh
for i in $(seq 1 "$N"); do
  bash scripts/add-node.sh "$i"
done

echo
echo "Ready."
echo "  API (through the proxy):  http://127.0.0.1:8080/movies"
echo "  HAProxy stats page:       http://127.0.0.1:8404/"
echo "  Registered nodes:         $REGISTRAR_URL/nodes"
echo "  Demo:                     $PYTHON tests/demo.py"
