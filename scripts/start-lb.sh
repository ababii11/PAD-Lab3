#!/usr/bin/env bash
# Start the load-balancer host: HAProxy and the registrar (service discovery).
#   LB_ALGO=random(2)|roundrobin|leastconn|source   (default random(2) = P2C)
set -e
source "$(dirname "$0")/common.sh"
export LB_ALGO="${LB_ALGO:-random(2)}"   # P2C

if is_running haproxy; then
  echo "haproxy already running"
else
  "$HAPROXY" -c -q -f config/haproxy.cfg
  nohup "$HAPROXY" -f config/haproxy.cfg > logs/haproxy.log 2>&1 &
  echo $! > run/haproxy.pid
  echo "started haproxy (algorithm: $LB_ALGO) -> http://127.0.0.1:8080  stats http://127.0.0.1:8404"
fi

if is_running registrar; then
  echo "registrar already running"
else
  HAPROXY_API=127.0.0.1:9999 nohup "$PYTHON" -m uvicorn registrar.main:app --host 127.0.0.1 --port 9100 \
    > logs/registrar.log 2>&1 &
  echo $! > run/registrar.pid
  wait_http "$REGISTRAR_URL/nodes"
  echo "started registrar -> $REGISTRAR_URL"
fi
