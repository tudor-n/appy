#!/usr/bin/env bash
# Deploy the memory service to the Mac mini and (re)start it under launchd.
# Run from anywhere on the laptop: appy_memory/deploy/deploy.sh
#
# Needs: `ssh macmini` working, ~/appy-memory/.env on the Mac (MEMORY_TOKEN etc.,
# see README), and MEMORY_URL / MEMORY_TOKEN in the laptop's .env for the final check.
set -euo pipefail

ROOT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../.." && pwd)"
HOST="${APPY_MAC_HOST:-macmini}"
cd "$ROOT"

echo "deploy: copying code to $HOST:~/appy-memory"
ssh "$HOST" 'mkdir -p ~/appy-memory/tests'
rsync -a --delete --exclude __pycache__ appy_memory/ "$HOST":appy-memory/appy_memory/
rsync -a tests/__init__.py tests/test_memory_vault.py tests/test_memory_server.py "$HOST":appy-memory/tests/

echo "deploy: venv, deps and tests on $HOST"
ssh "$HOST" 'bash -s' <<'REMOTE'
set -euo pipefail
cd ~/appy-memory
if [[ ! -f .env ]]; then
    echo "deploy: missing ~/appy-memory/.env on the Mac. Create it with:" >&2
    echo "  MEMORY_TOKEN=<token>  MEMORY_DIR=~/appy-data  MEMORY_HOST=0.0.0.0  MEMORY_PORT=8765" >&2
    exit 1
fi
[[ -d .venv ]] || /usr/bin/python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r appy_memory/requirements.txt
PATH=/opt/homebrew/bin:$PATH .venv/bin/python -m unittest tests.test_memory_vault tests.test_memory_server 2>&1 | tail -3
plist=~/Library/LaunchAgents/local.appy.memory.plist
cp appy_memory/deploy/local.appy.memory.plist "$plist"
launchctl bootout "gui/$(id -u)/local.appy.memory" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$plist"
echo "deploy: service (re)started; log at ~/appy-memory/service.log"
REMOTE

url=$(grep -E '^MEMORY_URL=' .env | cut -d= -f2- || true)
token=$(grep -E '^MEMORY_TOKEN=' .env | cut -d= -f2- || true)
if [[ -z "$url" || -z "$token" ]]; then
    echo "deploy: add MEMORY_URL and MEMORY_TOKEN to the laptop .env to check the service" >&2
    exit 1
fi
for _ in $(seq 1 20); do
    if out=$(curl -fsS -H "Authorization: Bearer $token" "$url/context" 2>/dev/null); then
        echo "deploy: OK, $url/context -> $out"
        exit 0
    fi
    sleep 0.5
done
echo "deploy: service didn't answer at $url (see ~/appy-memory/service.log on the Mac)" >&2
exit 1
