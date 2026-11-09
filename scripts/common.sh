# Shared settings for the start/stop scripts (sourced, not run directly).
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# MongoDB data must be on the Linux file system: on /mnt/c (Windows drive seen from
# WSL) it is slow and unreliable. So when the project sits on /mnt/..., data goes
# to ~/pad3-data instead.
case "$ROOT" in
  /mnt/*) DATA_DIR="${DATA_DIR:-$HOME/pad3-data}" ;;
  *)      DATA_DIR="${DATA_DIR:-$ROOT/data}" ;;
esac
mkdir -p "$DATA_DIR" logs run

# Python: the virtual environment made by install-wsl.sh, else the project's .venv.
if [ -x "$HOME/.venvs/pad3/bin/python" ]; then PYTHON="$HOME/.venvs/pad3/bin/python"
elif [ -x "$ROOT/.venv/bin/python" ]; then PYTHON="$ROOT/.venv/bin/python"
else PYTHON="${PYTHON:-python3}"; fi
MONGOD="${MONGOD:-mongod}"
HAPROXY="${HAPROXY:-haproxy}"
REGISTRAR_URL="${REGISTRAR_URL:-http://127.0.0.1:9100}"

# Node i uses MongoDB port 27016+i and HTTP port 8000+i.
mongo_port() { echo $((27016 + $1)); }
node_port()  { echo $((8000 + $1)); }

is_running() { [ -f "run/$1.pid" ] && kill -0 "$(cat "run/$1.pid")" 2>/dev/null; }

stop_pid() {
  if is_running "$1"; then
    kill "$(cat "run/$1.pid")" 2>/dev/null
    for _ in $(seq 1 50); do is_running "$1" || break; sleep 0.2; done
    echo "stopped $1"
  fi
  rm -f "run/$1.pid"
}

wait_http() {  # wait_http URL [seconds]
  for _ in $(seq 1 $(( ${2:-30} * 5 ))); do
    curl -s -o /dev/null "$1" && return 0
    sleep 0.2
  done
  echo "timeout waiting for $1" >&2; return 1
}
