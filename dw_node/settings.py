"""Configuration for one data-warehouse (DW) node, read from environment variables.

Every node knows only three things: its own name, its own MongoDB, and the
registrar (which lives on the load-balancer host). It learns about the other
nodes from the registrar, never from a hard-coded list.
"""
import os


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


NODE_NAME = os.environ.get("NODE_NAME", "node1")
PORT = int(os.environ.get("PORT", "8001"))

# Each node has its OWN MongoDB, started as a single-node replica set
# (needed for multi-document transactions).
MONGO_URL = os.environ.get("MONGO_URL", "mongodb://127.0.0.1:27017/?directConnection=true")
MONGO_DB = os.environ.get("MONGO_DB", "dw")

# Where other nodes and the registrar can reach this node.
ADVERTISE_URL = os.environ.get("ADVERTISE_URL", f"http://127.0.0.1:{PORT}")
REGISTRAR_URL = os.environ.get("REGISTRAR_URL", "http://127.0.0.1:9100")
WEIGHT = int(os.environ.get("WEIGHT", "100"))  # HAProxy weight (traffic share)

HEARTBEAT_INTERVAL = _env_float("HEARTBEAT_INTERVAL", 3)    # seconds
PULL_INTERVAL = _env_float("PULL_INTERVAL", 2)              # change-feed polling
RECONCILE_INTERVAL = _env_float("RECONCILE_INTERVAL", 60)   # full reconciliation
PULL_BATCH = int(os.environ.get("PULL_BATCH", "500"))

# Housekeeping. Tombstones must live much longer than any node can stay offline,
# otherwise reconciliation could resurrect a deleted record.
CHANGE_RETENTION_HOURS = _env_float("CHANGE_RETENTION_HOURS", 24)
TOMBSTONE_RETENTION_HOURS = _env_float("TOMBSTONE_RETENTION_HOURS", 24 * 7)
