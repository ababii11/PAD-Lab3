#!/usr/bin/env bash
# Stop node I (simulates a server failure). Its data stays on disk, so
#   bash scripts/add-node.sh I
# brings it back; it then catches up from the other nodes by itself.
#   bash scripts/stop-node.sh 2           # stop API and its MongoDB
#   bash scripts/stop-node.sh 2 --api     # stop only the API process
source "$(dirname "$0")/common.sh"
I="${1:?usage: stop-node.sh <number> [--api]}"
stop_pid "node$I"
[ "$2" = "--api" ] || stop_pid "mongod$I"
