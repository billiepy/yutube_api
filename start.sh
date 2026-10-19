#!/usr/bin/env bash

# ─────────────────────────────────────────────────────────────────────────────
# yutube_api — start.sh
# Validates environment, verifies service health, and starts the FastAPI server.
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ANSI Color formatting
RED="\033[1;31m"
GREEN="\033[1;32m"
YELLOW="\033[1;33m"
CYAN="\033[1;36m"
BOLD="\033[1m"
RESET="\033[0m"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

print_ok()   { echo -e "${GREEN}✓ $1${RESET}"; }
print_warn() { echo -e "${YELLOW}⚠ $1${RESET}"; }
print_err()  { echo -e "${RED}${BOLD}✗ $1${RESET}"; }
print_info() { echo -e "${CYAN}ℹ $1${RESET}"; }

# ── 1. Check .env existence & load ──────────────────────────────────────────

if [[ ! -f ".env" ]]; then
    print_err ".env configuration file not found in $SCRIPT_DIR."
    print_warn "Run the installer to configure your environment:"
    echo -e "  ${CYAN}./install.sh${RESET}"
    exit 1
fi

# Load variables safely from .env without executing arbitrary code
while IFS='=' read -r raw_key raw_val || [[ -n "$raw_key" ]]; do
    key=$(echo "$raw_key" | tr -d ' ' | tr -d '\r')
    [[ -z "$key" || "$key" =~ ^# ]] && continue
    val=$(echo "$raw_val" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//")
    if [[ -z "${!key:-}" ]]; then
        export "$key"="$val"
    fi
done < ".env"

# ── 2. Validate required variables ───────────────────────────────────────────

MISSING=()

[[ -z "${API_ID:-}" ]]           && MISSING+=("API_ID (Telegram API ID)")
[[ -z "${API_HASH:-}" ]]         && MISSING+=("API_HASH (Telegram API Hash)")
[[ -z "${BOT_TOKEN:-}" ]]        && MISSING+=("BOT_TOKEN (Telegram Bot Token from @BotFather)")
[[ -z "${CACHE_CHANNEL_ID:-}" ]] && MISSING+=("CACHE_CHANNEL_ID (Private Telegram channel chat ID)")

if [[ ${#MISSING[@]} -gt 0 ]]; then
    print_err "Missing required configuration in .env:"
    for var in "${MISSING[@]}"; do
        echo -e "  ${RED}• $var${RESET}"
    done
    echo
    print_warn "Edit .env or run install.sh to supply required credentials:"
    echo -e "  ${CYAN}./install.sh --skip-deps --skip-venv${RESET}"
    exit 1
fi

print_ok "Configuration validated"

# ── 3. Validate database services ────────────────────────────────────────────

REDIS_RUNNING=false
if systemctl is-active --quiet redis-server 2>/dev/null || systemctl is-active --quiet redis 2>/dev/null; then
    REDIS_RUNNING=true
fi

MONGO_RUNNING=false
if systemctl is-active --quiet mongod 2>/dev/null || systemctl is-active --quiet mongodb 2>/dev/null; then
    MONGO_RUNNING=true
fi

SERVICE_ERRORS=0

# If Redis is targeted at localhost, ensure local service is running
if [[ "${REDIS_HOST:-localhost}" =~ ^(localhost|127\.0\.0\.1|::1)$ ]]; then
    if [[ "$REDIS_RUNNING" == false ]]; then
        print_err "Local Redis service is not active (configured for REDIS_HOST=$REDIS_HOST)."
        print_warn "Start the service or run ./install.sh:"
        echo -e "  ${CYAN}sudo systemctl start redis-server${RESET}"
        SERVICE_ERRORS=$((SERVICE_ERRORS + 1))
    else
        print_ok "Redis service is active"
    fi
else
    print_info "Redis configured with remote host: ${REDIS_HOST}"
fi

# If MongoDB is targeted at localhost, ensure local service is running
if [[ "${MONGO_URI:-}" =~ localhost|127\.0\.0\.1 ]]; then
    if [[ "$MONGO_RUNNING" == false ]]; then
        print_err "Local MongoDB service is not active (configured for MONGO_URI=$MONGO_URI)."
        print_warn "Start the service or run ./install.sh:"
        echo -e "  ${CYAN}sudo systemctl start mongod${RESET}"
        SERVICE_ERRORS=$((SERVICE_ERRORS + 1))
    else
        print_ok "MongoDB service is active"
    fi
else
    print_info "MongoDB configured with external URI"
fi

if [[ $SERVICE_ERRORS -gt 0 ]]; then
    exit 1
fi

# ── 4. Validate Python virtualenv & dependencies ─────────────────────────────

VENV_PYTHON="$SCRIPT_DIR/.venv/bin/python"
VENV_UVICORN="$SCRIPT_DIR/.venv/bin/uvicorn"

if [[ ! -x "$VENV_PYTHON" || ! -x "$VENV_UVICORN" ]]; then
    print_err "Python virtual environment (.venv) not found or incomplete."
    print_warn "Create the venv and install dependencies via install.sh:"
    echo -e "  ${CYAN}./install.sh --skip-deps --skip-env${RESET}"
    exit 1
fi

# Spot-check critical Python packages
if ! "$VENV_PYTHON" -c "import fastapi, uvicorn, redis, motor, yt_dlp" >/dev/null 2>&1; then
    print_err "Python dependencies in .venv are missing or broken."
    print_warn "Reinstall dependencies from requirements.txt:"
    echo -e "  ${CYAN}.venv/bin/pip install -r requirements.txt${RESET}"
    exit 1
fi

print_ok "Python virtual environment & dependencies verified"

# ── 5. Validate Deno JS runtime ──────────────────────────────────────────────

if ! command -v deno >/dev/null 2>&1; then
    if [[ -x "$HOME/.deno/bin/deno" ]]; then
        export PATH="$HOME/.deno/bin:$PATH"
    elif [[ -x "/usr/local/bin/deno" ]]; then
        export PATH="/usr/local/bin:$PATH"
    else
        print_warn "deno not found on PATH — YouTube JS/EJS challenges may fail."
        print_warn "Run: curl -fsSL https://deno.land/install.sh | sh"
    fi
fi

if command -v deno >/dev/null 2>&1; then
    print_ok "Deno runtime available ($(deno --version 2>/dev/null | head -n1 | awk '{print $2}'))"
fi

# ── 6. Start API server ──────────────────────────────────────────────────────

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"
WORKERS="${WEB_CONCURRENCY:-1}"

echo
echo -e "${GREEN}${BOLD}▶ Starting yutube_api server...${RESET}"
echo -e "${CYAN}  Host:     ${HOST}:${PORT}${RESET}"
echo -e "${CYAN}  Workers:  ${WORKERS}${RESET}"
echo -e "${CYAN}  Mode:     Foreground (Press Ctrl+C to stop)${RESET}"
echo

exec "$VENV_UVICORN" main:app --host "$HOST" --port "$PORT" --workers "$WORKERS"
