#!/usr/bin/env bash
# Stop everything started by start-all.sh / add-node.sh. The databases are kept.
#   bash scripts/stop-all.sh           # stop
#   bash scripts/stop-all.sh --wipe    # stop and delete all databases and logs
source "$(dirname "$0")/common.sh"
for f in run/node*.pid; do [ -e "$f" ] && stop_pid "$(basename "$f" .pid)"; done
stop_pid registrar
stop_pid haproxy
for f in run/mongod*.pid; do [ -e "$f" ] && stop_pid "$(basename "$f" .pid)"; done
if [ "$1" = "--wipe" ]; then rm -rf "$DATA_DIR" logs run; echo "deleted $DATA_DIR, logs/, run/"; fi
