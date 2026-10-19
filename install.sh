#!/usr/bin/env bash

# ─────────────────────────────────────────────────────────────────────────────
# install.sh — Complete local setup for yutube_api
# System dependencies, local Redis/MongoDB services, Deno JS runtime,
# Python virtual environment, and interactive .env configuration.
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# Color palette
RED="\033[1;31m"
GREEN="\033[1;32m"
YELLOW="\033[1;33m"
BLUE="\033[1;34m"
CYAN="\033[1;36m"
BOLD="\033[1m"
RESET="\033[0m"

INSTALL_LOG="/tmp/yutube_api_install_$(date +%s).log"
WARNINGS=0
NON_INTERACTIVE=false
SKIP_DEPS=false
SKIP_ENV=false
SKIP_VENV=false

# Logging & UI helpers
print_step() {
    echo -e "\n${BLUE}▶ $1${RESET}"
}

print_success() {
    echo -e "${GREEN}✓ $1${RESET}"
}

print_warning() {
    echo -e "${YELLOW}⚠ $1${RESET}"
    WARNINGS=$((WARNINGS + 1))
}

print_info() {
    echo -e "${CYAN}ℹ $1${RESET}"
}

print_error() {
    echo -e "\n${RED}${BOLD}✗ CRITICAL: $1${RESET}"
    echo -e "${RED}${BOLD}Please resolve this error and re-run install.sh.${RESET}\n"
    log_msg "CRITICAL: $1"
    exit 1
}

log_msg() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] $1" >> "$INSTALL_LOG"
}

run_as_root() {
    local desc="$1"
    shift
    log_msg "Executing: $desc ($*)"
    if [[ $EUID -eq 0 ]]; then
        "$@" >> "$INSTALL_LOG" 2>&1
    elif command -v sudo >/dev/null 2>&1; then
        sudo "$@" >> "$INSTALL_LOG" 2>&1
    else
        "$@" >> "$INSTALL_LOG" 2>&1
    fi
}

show_help() {
    cat <<EOF
Usage: $0 [OPTIONS]

Options:
  -h, --help            Show this help message
  -y, --yes, --non-interactive
                        Run non-interactively using defaults / existing .env
  --skip-deps           Skip system package & database service installation
  --skip-venv           Skip Python venv & pip requirements installation
  --skip-env            Skip interactive .env configuration

Examples:
  $0                    # Full interactive installation
  $0 -y                 # Non-interactive installation using defaults
  $0 --skip-deps        # Only setup venv and configure .env
EOF
    exit 0
}

parse_arguments() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -h|--help)
                show_help
                ;;
            -y|--yes|--non-interactive)
                NON_INTERACTIVE=true
                ;;
            --skip-deps)
                SKIP_DEPS=true
                ;;
            --skip-env)
                SKIP_ENV=true
                ;;
            --skip-venv)
                SKIP_VENV=true
                ;;
            *)
                echo -e "${RED}Unknown option: $1${RESET}"
                echo "Use --help for usage instructions."
                exit 1
                ;;
        esac
        shift
    done
}

detect_system() {
    print_step "Detecting operating system..."

    local os arch
    os="$(uname -s)"
    arch="$(uname -m)"

    if [[ "$os" != "Linux" ]]; then
        print_error "Unsupported OS: $os. This installer targets Linux (Debian/Ubuntu)."
    fi

    case "$arch" in
        x86_64|amd64) ARCH_TYPE="amd64" ;;
        aarch64|arm64) ARCH_TYPE="arm64" ;;
        *) print_error "Unsupported CPU architecture: $arch. x86_64 or arm64 is required." ;;
    esac

    DISTRO_ID="ubuntu"
    DISTRO_CODENAME="jammy"
    PRETTY_NAME="Linux"

    if [[ -f /etc/os-release ]]; then
        # shellcheck disable=SC1091
        . /etc/os-release
        DISTRO_ID="${ID:-ubuntu}"
        DISTRO_CODENAME="${VERSION_CODENAME:-jammy}"
    fi

    print_success "System: ${PRETTY_NAME} (${ARCH_TYPE})"
    log_msg "Detected system: $DISTRO_ID ($DISTRO_CODENAME) on $ARCH_TYPE"
}

install_system_packages() {
    [[ "$SKIP_DEPS" == true ]] && return 0

    print_step "Installing system packages via apt..."

    if ! command -v apt-get >/dev/null 2>&1; then
        print_error "apt-get not found. This script requires a Debian/Ubuntu-based system."
    fi

    run_as_root "Updating package lists" apt-get update -y

    local pkgs=(
        curl
        git
        ffmpeg
        python3
        python3-venv
        python3-pip
        redis-server
        gnupg
        unzip
    )

    for pkg in "${pkgs[@]}"; do
        print_info "Ensuring $pkg is installed..."
        if ! run_as_root "Installing $pkg" apt-get install -y "$pkg"; then
            print_warning "Failed to install $pkg via apt. Review $INSTALL_LOG"
        fi
    done
    print_success "System base packages installed"
}

setup_mongodb() {
    [[ "$SKIP_DEPS" == true ]] && return 0

    print_step "Configuring MongoDB Community service..."

    if command -v mongod >/dev/null 2>&1 || command -v mongodb >/dev/null 2>&1; then
        local current_ver
        current_ver=$(mongod --version 2>/dev/null | grep -oE "v[0-9]+\.[0-9]+\.[0-9]+" | head -n1 || echo "installed")
        print_success "MongoDB is already installed ($current_ver)"
    else
        # Check whether standard repos carry mongodb
        if apt-cache show mongodb >/dev/null 2>&1; then
            print_info "Installing mongodb from system repositories..."
            run_as_root "Installing mongodb" apt-get install -y mongodb
        else
            # Ubuntu 22.04 (jammy), Ubuntu 24.04 (noble), or Debian 12 (bookworm)
            print_info "Adding official MongoDB Community repository for $DISTRO_CODENAME..."

            local mongo_ver="7.0"
            if [[ "$DISTRO_CODENAME" == "noble" || "$DISTRO_CODENAME" == "bookworm" ]]; then
                mongo_ver="8.0"
            fi

            local gpg_key="/usr/share/keyrings/mongodb-server-${mongo_ver}.gpg"
            local repo_file="/etc/apt/sources.list.d/mongodb-org-${mongo_ver}.list"
            local key_url="https://pgp.mongodb.com/server-${mongo_ver}.asc"

            print_info "Importing MongoDB GPG key ($key_url)..."
            curl -fsSL "$key_url" | run_as_root "Importing MongoDB GPG key" gpg --dearmor --yes -o "$gpg_key"

            local repo_base="https://repo.mongodb.org/apt/${DISTRO_ID}"
            local component="multiverse"
            if [[ "$DISTRO_ID" == "debian" ]]; then
                component="main"
            fi

            echo "deb [ arch=$(dpkg --print-architecture) signed-by=$gpg_key ] $repo_base $DISTRO_CODENAME/mongodb-org/$mongo_ver $component" | run_as_root "Adding MongoDB repo" tee "$repo_file" >/dev/null

            run_as_root "Updating apt index for MongoDB" apt-get update -y
            print_info "Installing mongodb-org package..."
            run_as_root "Installing mongodb-org" apt-get install -y mongodb-org
        fi
        print_success "MongoDB package installed"
    fi

    # Reload systemd and enable/start mongod service
    print_info "Enabling and starting MongoDB service..."
    run_as_root "Reloading systemd daemon" systemctl daemon-reload || true

    if systemctl list-unit-files 2>/dev/null | grep -q "mongod.service"; then
        run_as_root "Enabling and starting mongod" systemctl enable --now mongod
    elif systemctl list-unit-files 2>/dev/null | grep -q "mongodb.service"; then
        run_as_root "Enabling and starting mongodb" systemctl enable --now mongodb
    else
        run_as_root "Starting mongod service" systemctl enable --now mongod || true
    fi

    if systemctl is-active --quiet mongod 2>/dev/null || systemctl is-active --quiet mongodb 2>/dev/null; then
        print_success "MongoDB service is active and running"
    else
        print_warning "MongoDB service did not report active status. Check 'systemctl status mongod'"
    fi
}

setup_redis() {
    [[ "$SKIP_DEPS" == true ]] && return 0

    print_step "Configuring Redis service..."

    if systemctl list-unit-files 2>/dev/null | grep -q "redis-server.service"; then
        run_as_root "Enabling and starting redis-server" systemctl enable --now redis-server
    elif systemctl list-unit-files 2>/dev/null | grep -q "redis.service"; then
        run_as_root "Enabling and starting redis" systemctl enable --now redis
    else
        run_as_root "Starting redis-server" systemctl enable --now redis-server || true
    fi

    if systemctl is-active --quiet redis-server 2>/dev/null || systemctl is-active --quiet redis 2>/dev/null; then
        print_success "Redis service is active and running"
    else
        print_warning "Redis service did not report active status. Check 'systemctl status redis-server'"
    fi
}

setup_deno() {
    [[ "$SKIP_DEPS" == true ]] && return 0

    print_step "Checking Deno JS runtime (required for yt-dlp EJS challenges)..."

    # Check if already in PATH
    if command -v deno >/dev/null 2>&1; then
        local deno_ver
        deno_ver=$(deno --version 2>/dev/null | head -n1 | awk '{print $2}')
        print_success "Deno already available on PATH ($deno_ver)"
        return 0
    fi

    # Check ~/.deno/bin
    if [[ -x "$HOME/.deno/bin/deno" ]]; then
        export PATH="$HOME/.deno/bin:$PATH"
        print_success "Found Deno in ~/.deno/bin ($(deno --version | head -n1 | awk '{print $2}'))"
    else
        print_info "Installing Deno via official install script..."
        if [[ $EUID -eq 0 ]]; then
            curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh
        else
            curl -fsSL https://deno.land/install.sh | sh
            export PATH="$HOME/.deno/bin:$PATH"
        fi
    fi

    # If installed in ~/.deno/bin and we have root, link to /usr/local/bin for global accessibility
    if [[ -x "$HOME/.deno/bin/deno" && ! -f /usr/local/bin/deno ]]; then
        run_as_root "Symlinking deno to /usr/local/bin" ln -sf "$HOME/.deno/bin/deno" /usr/local/bin/deno 2>/dev/null || true
    fi

    # Ensure PATH is persisted to user profile
    for profile in "$HOME/.bashrc" "$HOME/.profile"; do
        if [[ -f "$profile" ]] && ! grep -qF "deno" "$profile" 2>/dev/null; then
            echo 'export PATH="$HOME/.deno/bin:/usr/local/bin:$PATH"' >> "$profile"
            print_info "Added Deno to PATH in $profile"
        fi
    done

    if command -v deno >/dev/null 2>&1; then
        print_success "Deno installed and discoverable on PATH ($(deno --version | head -n1 | awk '{print $2}'))"
    else
        print_warning "Deno installed but may require 'source ~/.bashrc' to appear in current interactive session"
    fi
}

setup_python_venv() {
    [[ "$SKIP_VENV" == true ]] && return 0

    print_step "Setting up Python virtual environment..."

    local script_dir
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    local venv_dir="$script_dir/.venv"

    if [[ -d "$venv_dir" && -f "$venv_dir/bin/python" ]]; then
        print_success "Using existing virtual environment (.venv)"
    else
        print_info "Creating virtual environment in $venv_dir..."
        python3 -m venv "$venv_dir"
        print_success "Virtual environment created"
    fi

    print_info "Installing Python dependencies from requirements.txt..."
    "$venv_dir/bin/pip" install --upgrade pip --quiet || true
    if "$venv_dir/bin/pip" install -r "$script_dir/requirements.txt"; then
        print_success "Python dependencies installed successfully"
    else
        print_error "Failed to install dependencies from requirements.txt. Review $INSTALL_LOG"
    fi
}

configure_env() {
    [[ "$SKIP_ENV" == true ]] && return 0

    print_step "Setting up environment configuration (.env)..."

    local script_dir
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    local env_file="$script_dir/.env"

    # Default values reflecting config.py
    local val_api_id="2040"
    local val_api_hash="b18441a1ff607e10a989891a5462e627"
    local val_bot_token=""
    local val_cache_channel_id=""
    local val_tg_group="nub_coder_s"
    local val_tg_channel="nub_coders"
    local val_admin_ids=""
    local val_start_bot="true"
    local val_enable_bot="true"
    local val_redis_host="localhost"
    local val_redis_port="6379"
    local val_redis_username="default"
    local val_redis_password=""
    local val_mongo_uri="mongodb://localhost:27017"
    local val_cache_db_name="ytube_cache"
    local val_upload_concurrency="4"
    local val_daily_limit="1000"
    local val_admin_limit="10000"
    local val_cookies_file="cookies.txt"
    local val_cookies_browser="firefox"
    local val_cookies_bootstrap_url="https://www.youtube.com/watch?v=BaW_jenozKc"
    local val_cookies_refresh_hours="6"
    local val_api_host="api.nubcoders.com"
    local val_base_url="https://api.nubcoders.com"
    local val_youtube_api_keys=""
    local val_ytdlp_max_procs="4"
    local val_web_concurrency="1"

    # Pre-populate from existing .env if present
    if [[ -f "$env_file" ]]; then
        print_info "Existing .env detected. Pre-filling current values..."
        while IFS='=' read -r raw_key raw_val || [[ -n "$raw_key" ]]; do
            local key val
            key=$(echo "$raw_key" | tr -d ' ' | tr -d '\r')
            [[ -z "$key" || "$key" =~ ^# ]] && continue
            val=$(echo "$raw_val" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//")
            case "$key" in
                API_ID) val_api_id="$val" ;;
                API_HASH) val_api_hash="$val" ;;
                BOT_TOKEN) val_bot_token="$val" ;;
                CACHE_CHANNEL_ID) val_cache_channel_id="$val" ;;
                TG_GROUP) val_tg_group="$val" ;;
                TG_CHANNEL) val_tg_channel="$val" ;;
                ADMIN_IDS) val_admin_ids="$val" ;;
                START_BOT) val_start_bot="$val" ;;
                ENABLE_BOT) val_enable_bot="$val" ;;
                REDIS_HOST) val_redis_host="$val" ;;
                REDIS_PORT) val_redis_port="$val" ;;
                REDIS_USERNAME) val_redis_username="$val" ;;
                REDIS_PASSWORD) val_redis_password="$val" ;;
                MONGO_URI) val_mongo_uri="$val" ;;
                CACHE_DB_NAME) val_cache_db_name="$val" ;;
                UPLOAD_CONCURRENCY) val_upload_concurrency="$val" ;;
                DAILY_LIMIT) val_daily_limit="$val" ;;
                ADMIN_LIMIT) val_admin_limit="$val" ;;
                COOKIES_FILE) val_cookies_file="$val" ;;
                COOKIES_BROWSER) val_cookies_browser="$val" ;;
                COOKIES_BOOTSTRAP_URL) val_cookies_bootstrap_url="$val" ;;
                COOKIES_REFRESH_HOURS) val_cookies_refresh_hours="$val" ;;
                API_HOST) val_api_host="$val" ;;
                BASE_URL) val_base_url="$val" ;;
                YOUTUBE_API_KEYS) val_youtube_api_keys="$val" ;;
                YTDLP_MAX_PROCS) val_ytdlp_max_procs="$val" ;;
                WEB_CONCURRENCY) val_web_concurrency="$val" ;;
            esac
        done < "$env_file"
    fi

    if [[ "$NON_INTERACTIVE" == true || ! -t 0 ]]; then
        print_info "Running in non-interactive mode; preserving configured values."
    else
        echo -e "\n${BOLD}${CYAN}─────────────────────────────────────────────────────────────────${RESET}"
        echo -e "${BOLD}${CYAN}                INTERACTIVE CONFIGURATION SETUP                  ${RESET}"
        echo -e "${BOLD}${CYAN}─────────────────────────────────────────────────────────────────${RESET}"
        echo -e "${CYAN}Please answer the prompts below to configure your deployment secrets.${RESET}"
        echo -e "${CYAN}Press [Enter] to keep the shown default value in brackets.${RESET}\n"

        # 1. API_ID
        while true; do
            read -r -p "$(echo -e "${BLUE}▶ Telegram API_ID${RESET} [default: ${val_api_id}]: ")" input_api_id
            input_api_id="${input_api_id:-$val_api_id}"
            if [[ "$input_api_id" =~ ^[0-9]+$ ]]; then
                val_api_id="$input_api_id"
                break
            else
                echo -e "${RED}API_ID must be a numeric integer. Please try again.${RESET}"
            fi
        done

        # 2. API_HASH
        read -r -p "$(echo -e "${BLUE}▶ Telegram API_HASH${RESET} [default: ${val_api_hash}]: ")" input_api_hash
        val_api_hash="${input_api_hash:-$val_api_hash}"

        # 3. BOT_TOKEN
        while true; do
            local prompt_hint="required"
            [[ -n "$val_bot_token" ]] && prompt_hint="default: ${val_bot_token}"
            read -r -p "$(echo -e "${BLUE}▶ Telegram BOT_TOKEN${RESET} (from @BotFather) [${prompt_hint}]: ")" input_bot_token
            input_bot_token="${input_bot_token:-$val_bot_token}"
            if [[ -n "$input_bot_token" ]]; then
                val_bot_token="$input_bot_token"
                break
            else
                echo -e "${RED}BOT_TOKEN is required. Please provide a valid token from @BotFather.${RESET}"
            fi
        done

        # 4. CACHE_CHANNEL_ID
        echo -e "\n${YELLOW}ℹ CACHE_CHANNEL_ID must be the numeric ID of a private Telegram channel${RESET}"
        echo -e "${YELLOW}  where your bot is added as an administrator (e.g., -1001234567890).${RESET}"
        echo -e "${YELLOW}  Media files will be stored in this channel for caching & fast streaming.${RESET}"
        while true; do
            local prompt_hint="required"
            [[ -n "$val_cache_channel_id" ]] && prompt_hint="default: ${val_cache_channel_id}"
            read -r -p "$(echo -e "${BLUE}▶ Telegram CACHE_CHANNEL_ID${RESET} [${prompt_hint}]: ")" input_cache_channel_id
            input_cache_channel_id="${input_cache_channel_id:-$val_cache_channel_id}"
            if [[ "$input_cache_channel_id" =~ ^-?[0-9]+$ ]]; then
                val_cache_channel_id="$input_cache_channel_id"
                break
            else
                echo -e "${RED}CACHE_CHANNEL_ID must be a numeric chat ID. Please try again.${RESET}"
            fi
        done

        # 5. Local Database Services vs Custom
        echo -e "\n${BOLD}${CYAN}─────────────────────────────────────────────────────────────────${RESET}"
        echo -e "${BOLD}${CYAN}                DATABASE & CACHE CONNECTIONS                     ${RESET}"
        echo -e "${BOLD}${CYAN}─────────────────────────────────────────────────────────────────${RESET}"
        read -r -p "$(echo -e "${BLUE}▶ Use local services defaults (Redis: localhost:6379, MongoDB: localhost:27017)?${RESET} [Y/n]: ")" use_local
        use_local="${use_local:-Y}"

        if [[ "$use_local" =~ ^[Yy]$ ]]; then
            val_redis_host="localhost"
            val_redis_port="6379"
            val_redis_username="default"
            val_redis_password=""
            val_mongo_uri="mongodb://localhost:27017"
            val_cache_db_name="ytube_cache"
            print_success "Configured for local Redis and local MongoDB"
        else
            read -r -p "$(echo -e "${BLUE}▶ Enter MONGO_URI${RESET} [default: ${val_mongo_uri}]: ")" input_mongo_uri
            val_mongo_uri="${input_mongo_uri:-$val_mongo_uri}"

            read -r -p "$(echo -e "${BLUE}▶ Enter CACHE_DB_NAME${RESET} [default: ${val_cache_db_name}]: ")" input_cache_db_name
            val_cache_db_name="${input_cache_db_name:-$val_cache_db_name}"

            read -r -p "$(echo -e "${BLUE}▶ Enter REDIS_HOST${RESET} [default: ${val_redis_host}]: ")" input_redis_host
            val_redis_host="${input_redis_host:-$val_redis_host}"

            read -r -p "$(echo -e "${BLUE}▶ Enter REDIS_PORT${RESET} [default: ${val_redis_port}]: ")" input_redis_port
            val_redis_port="${input_redis_port:-$val_redis_port}"

            read -r -p "$(echo -e "${BLUE}▶ Enter REDIS_USERNAME${RESET} [default: ${val_redis_username}]: ")" input_redis_username
            val_redis_username="${input_redis_username:-$val_redis_username}"

            read -r -p "$(echo -e "${BLUE}▶ Enter REDIS_PASSWORD (leave blank if none)${RESET}: ")" input_redis_password
            val_redis_password="${input_redis_password:-$val_redis_password}"
        fi

        # 6. Optional Admin & Tuning Settings
        read -r -p "$(echo -e "\n${BLUE}▶ Do you want to customize optional settings (Admin IDs, Public Host, etc.)?${RESET} [y/N]: ")" customize_optional
        customize_optional="${customize_optional:-N}"

        if [[ "$customize_optional" =~ ^[Yy]$ ]]; then
            read -r -p "$(echo -e "${BLUE}▶ Space-separated ADMIN_IDS${RESET} [default: \"${val_admin_ids}\"]: ")" input_admin_ids
            val_admin_ids="${input_admin_ids:-$val_admin_ids}"

            read -r -p "$(echo -e "${BLUE}▶ Support TG_GROUP${RESET} [default: ${val_tg_group}]: ")" input_tg_group
            val_tg_group="${input_tg_group:-$val_tg_group}"

            read -r -p "$(echo -e "${BLUE}▶ Update TG_CHANNEL${RESET} [default: ${val_tg_channel}]: ")" input_tg_channel
            val_tg_channel="${input_tg_channel:-$val_tg_channel}"

            read -r -p "$(echo -e "${BLUE}▶ API_HOST${RESET} [default: ${val_api_host}]: ")" input_api_host
            val_api_host="${input_api_host:-$val_api_host}"

            read -r -p "$(echo -e "${BLUE}▶ BASE_URL${RESET} [default: https://${val_api_host}]: ")" input_base_url
            val_base_url="${input_base_url:-https://${val_api_host}}"
        fi
    fi

    # Backup existing .env before rewriting
    if [[ -f "$env_file" ]]; then
        local backup_file="${env_file}.backup.$(date +%s)"
        cp "$env_file" "$backup_file"
        print_info "Saved backup of existing .env to $(basename "$backup_file")"
    fi

    # Write formatted .env file mirroring .env.example
    cat <<EOF > "$env_file"
# ── Telegram Bot API Credentials ───────────────────────────────────────────────
API_ID=${val_api_id}
API_HASH=${val_api_hash}

# Generate BOT_TOKEN using @BotFather on Telegram
BOT_TOKEN=${val_bot_token}

# Support group and update channel usernames (without '@')
TG_GROUP=${val_tg_group}
TG_CHANNEL=${val_tg_channel}

# Space-separated list of Telegram User IDs who have administrative privileges
ADMIN_IDS="${val_admin_ids}"

# Set to false to disable starting the Telegram bot (e.g. during testing)
START_BOT=${val_start_bot}
ENABLE_BOT=${val_enable_bot}

# ── Redis Configuration ─────────────────────────────────────────────────────────
# Connection details for Redis caching & rate limiting
REDIS_HOST=${val_redis_host}
REDIS_PORT=${val_redis_port}
REDIS_USERNAME=${val_redis_username}
REDIS_PASSWORD=${val_redis_password}

# Optional Upstash Redis for search service caching
# UPSTASH_REDIS_REST_URL=
# UPSTASH_REDIS_REST_TOKEN=

# ── API Rate Limits ─────────────────────────────────────────────────────────────
# Default daily request counts allowed per user/admin token
DAILY_LIMIT=${val_daily_limit}
ADMIN_LIMIT=${val_admin_limit}

# ── Cookies ──────────────────────────────────────────────────────────────────
# yt-dlp cookies file. Exported at startup from COOKIES_BROWSER, then reused and
# re-exported every COOKIES_REFRESH_HOURS (0 disables refresh).
COOKIES_FILE=${val_cookies_file}
COOKIES_BROWSER=${val_cookies_browser}
COOKIES_BOOTSTRAP_URL=${val_cookies_bootstrap_url}
COOKIES_REFRESH_HOURS=${val_cookies_refresh_hours}

# ── Deployment Host & Public URL ──────────────────────────────────────────────
API_HOST=${val_api_host}
BASE_URL=${val_base_url}

# ── YouTube Data API (Optional) ─────────────────────────────────────────────────
# Comma-separated list of official YouTube Data API keys for search/metadata fast-path
YOUTUBE_API_KEYS=${val_youtube_api_keys}

# ── Process / Concurrency Tuning (Optional) ───────────────────────────────────
YTDLP_MAX_PROCS=${val_ytdlp_max_procs}
WEB_CONCURRENCY=${val_web_concurrency}

# ── MongoDB & Telegram Media Cache ───────────────────────────────────────────
MONGO_URI=${val_mongo_uri}
CACHE_DB_NAME=${val_cache_db_name}
# Numeric ID of private Telegram channel where media files get uploaded
CACHE_CHANNEL_ID=${val_cache_channel_id}
# Concurrency limit for background media downloads and uploads
UPLOAD_CONCURRENCY=${val_upload_concurrency}
EOF

    print_success ".env configuration written successfully"
}

print_summary() {
    local script_dir
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

    echo -e "\n${BLUE}${BOLD}==================================================${RESET}"
    echo -e "${BLUE}${BOLD}             INSTALLATION SUMMARY               ${RESET}"
    echo -e "${BLUE}${BOLD}==================================================${RESET}"
    printf "${CYAN}${BOLD}%-14s | %-12s | %-16s${RESET}\n" "Component" "Status" "Version"
    echo -e "--------------------------------------------------"

    _print_row() {
        local comp="$1" status="$2" ver="$3" ok="$4"
        local color=$GREEN
        [[ "$ok" -ne 0 ]] && color=$RED
        printf "%-14s | ${color}%-12s${RESET} | %-16s\n" "$comp" "$status" "$ver"
    }

    # Python
    local py_ver
    py_ver=$(python3 --version 2>&1 | awk '{print $2}')
    _print_row "Python" "Installed" "$py_ver" 0

    # pip
    local pip_ver
    pip_ver=$(pip3 --version 2>&1 | awk '{print $2}' || echo "-")
    _print_row "pip" "Installed" "$pip_ver" 0

    # FFmpeg
    if command -v ffmpeg >/dev/null 2>&1; then
        local ffmpeg_ver
        ffmpeg_ver=$(ffmpeg -version 2>/dev/null | head -n1 | awk '{print $3}' || echo "installed")
        _print_row "FFmpeg" "Installed" "$ffmpeg_ver" 0
    else
        _print_row "FFmpeg" "Missing" "-" 1
    fi

    # Redis service
    if systemctl is-active --quiet redis-server 2>/dev/null || systemctl is-active --quiet redis 2>/dev/null; then
        local redis_ver
        redis_ver=$(redis-server --version 2>/dev/null | grep -oE "v=[0-9.]+" | cut -d= -f2 || echo "active")
        _print_row "Redis" "Active" "$redis_ver" 0
    else
        _print_row "Redis" "Inactive" "-" 1
    fi

    # MongoDB service
    if systemctl is-active --quiet mongod 2>/dev/null || systemctl is-active --quiet mongodb 2>/dev/null; then
        local mongo_ver
        mongo_ver=$(mongod --version 2>/dev/null | grep -oE "v[0-9.]+" | head -n1 || echo "active")
        _print_row "MongoDB" "Active" "$mongo_ver" 0
    else
        _print_row "MongoDB" "Inactive" "-" 1
    fi

    # Deno runtime
    if command -v deno >/dev/null 2>&1 || [[ -x "$HOME/.deno/bin/deno" ]]; then
        local deno_ver
        deno_ver=$(deno --version 2>/dev/null | head -n1 | awk '{print $2}' || echo "installed")
        _print_row "Deno" "Installed" "$deno_ver" 0
    else
        _print_row "Deno" "Missing" "-" 1
    fi

    # Python venv
    if [[ -d "$script_dir/.venv" && -f "$script_dir/.venv/bin/python" ]]; then
        _print_row "Python venv" "Ready" ".venv" 0
    else
        _print_row "Python venv" "Missing" "-" 1
    fi

    # Dependencies
    if [[ -x "$script_dir/.venv/bin/uvicorn" ]]; then
        _print_row "Dependencies" "Installed" "requirements.txt" 0
    else
        _print_row "Dependencies" "Incomplete" "-" 1
    fi

    # .env Config
    if [[ -f "$script_dir/.env" ]]; then
        _print_row ".env Config" "Configured" ".env" 0
    else
        _print_row ".env Config" "Missing" "-" 1
    fi

    echo -e "--------------------------------------------------"

    echo -e "\n${GREEN}${BOLD}What to run next:${RESET}"
    echo -e "  1. Activate the Python virtual environment:"
    echo -e "     ${CYAN}source .venv/bin/activate${RESET}"
    echo -e "  2. Start the FastAPI API server:"
    echo -e "     ${CYAN}uvicorn main:app --host 0.0.0.0 --port 8000${RESET}"
    echo -e "  3. Start the Telegram Bot daemon:"
    echo -e "     ${CYAN}python3 bot.py${RESET}"
    echo -e "\n${CYAN}Installation log saved to: ${INSTALL_LOG}${RESET}\n"
}

main() {
    parse_arguments "$@"

    log_msg "Starting install.sh with arguments: $*"

    detect_system
    install_system_packages
    setup_mongodb
    setup_redis
    setup_deno
    setup_python_venv
    configure_env
    print_summary

    if [[ $WARNINGS -eq 0 ]]; then
        echo -e "${GREEN}${BOLD}✓ System setup and environment configuration completed successfully!${RESET}"
        exit 0
    else
        echo -e "${YELLOW}${BOLD}⚠ Setup completed with $WARNINGS warning(s). Review $INSTALL_LOG for details.${RESET}"
        exit 0
    fi
}

main "$@"
