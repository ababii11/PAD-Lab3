#!/usr/bin/env bash
# Start data-warehouse node number I (its own MongoDB + the API).
# The node registers itself; HAProxy and the other nodes pick it up automatically.
#
#   bash scripts/add-node.sh 4             # node4: API :8004, MongoDB :27020
#   WEIGHT=200 bash scripts/add-node.sh 5  # gets twice the traffic share
set -e
source "$(dirname "$0")/common.sh"
I="${1:?usage: add-node.sh <number>}"
MP=$(mongo_port "$I"); NP=$(node_port "$I")

if is_running "mongod$I"; then
  echo "mongod$I already running"
else
  mkdir -p "$DATA_DIR/db$I"
  # --replSet: one-member replica set, required for multi-document transactions.
  # This is NOT shared with other nodes: every node has its own independent database.
  nohup "$MONGOD" --replSet "rs-node$I" --port "$MP" --bind_ip 127.0.0.1 \
    --dbpath "$DATA_DIR/db$I" --wiredTigerCacheSizeGB 0.25 \
    > "logs/mongod$I.log" 2>&1 &
  echo $! > "run/mongod$I.pid"
  echo "started mongod$I on port $MP"
fi

if is_running "node$I"; then
  echo "node$I already running"
else
  NODE_NAME="node$I" PORT="$NP" \
  MONGO_URL="mongodb://127.0.0.1:$MP/?directConnection=true" \
  ADVERTISE_URL="http://127.0.0.1:$NP" REGISTRAR_URL="$REGISTRAR_URL" \
  nohup "$PYTHON" -m uvicorn dw_node.main:app --host 127.0.0.1 --port "$NP" \
    > "logs/node$I.log" 2>&1 &
  echo $! > "run/node$I.pid"
  wait_http "http://127.0.0.1:$NP/health" 60
  echo "started node$I on http://127.0.0.1:$NP"
fi
