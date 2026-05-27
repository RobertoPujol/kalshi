#!/usr/bin/env bash
# Interactively prompt for Kalshi credentials and write .env.
#
# Safe to run from anywhere — it locates the repo via its own path.
# Existing .env is backed up to .env.bak.<timestamp> before overwrite.
set -euo pipefail

# ── Locate repo root regardless of caller's cwd ──────────────────────────────
SCRIPT_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
REPO_ROOT="$(cd "$(dirname "$SCRIPT_PATH")/.." && pwd)"
ENV_FILE="$REPO_ROOT/.env"
EXAMPLE_FILE="$REPO_ROOT/.env.example"

# ── Pretty output helpers ────────────────────────────────────────────────────
bold()  { printf "\033[1m%s\033[0m" "$*"; }
red()   { printf "\033[31m%s\033[0m" "$*"; }
green() { printf "\033[32m%s\033[0m" "$*"; }
dim()   { printf "\033[2m%s\033[0m" "$*"; }

echo
echo "$(bold "Kalshi .env setup") — repo: $(dim "$REPO_ROOT")"
echo

# ── Prompt helpers ───────────────────────────────────────────────────────────
# ask <var_name> <prompt> [default]
#   reads from /dev/tty so it still works when stdin is piped
ask() {
    local var="$1" prompt="$2" default="${3:-}"
    local hint=""
    [[ -n "$default" ]] && hint=" [$(dim "$default")]"
    local value=""
    printf "%s%s: " "$prompt" "$hint" > /dev/tty
    read -r value < /dev/tty || true
    [[ -z "$value" && -n "$default" ]] && value="$default"
    printf -v "$var" '%s' "$value"
}

# ask_required <var_name> <prompt>
ask_required() {
    local var="$1" prompt="$2"
    while :; do
        ask "$var" "$prompt"
        if [[ -n "${!var}" ]]; then
            return
        fi
        echo "  $(red "required — please enter a value")" > /dev/tty
    done
}

confirm() {
    local prompt="$1" default="${2:-Y}"
    local yn=""
    local hint="[Y/n]"
    [[ "$default" =~ ^[Nn]$ ]] && hint="[y/N]"
    printf "%s %s " "$prompt" "$hint" > /dev/tty
    read -r yn < /dev/tty || true
    yn="${yn:-$default}"
    [[ "$yn" =~ ^[Yy]$ ]]
}

# ── Collect values ───────────────────────────────────────────────────────────
ask_required KALSHI_API_KEY_ID "API Key ID (UUID from Kalshi dashboard)"

# Validate UUID-ish shape (not strict, just a heads-up)
if ! [[ "$KALSHI_API_KEY_ID" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]]; then
    echo "  $(red "warning"): doesn't look like a UUID — continuing anyway"
fi

while :; do
    ask_required KEY_PATH_RAW "Path to RSA private key (.pem)"
    # Expand ~ and resolve to absolute
    KEY_PATH="${KEY_PATH_RAW/#\~/$HOME}"
    if [[ ! -f "$KEY_PATH" ]]; then
        echo "  $(red "not found"): $KEY_PATH"
        confirm "Use it anyway?" N && break
        continue
    fi
    # Basic sanity check: looks like a PEM private key?
    if ! grep -q "BEGIN.*PRIVATE KEY" "$KEY_PATH" 2>/dev/null; then
        echo "  $(red "warning"): file doesn't contain a PEM private-key header"
        confirm "Use it anyway?" N || continue
    fi
    # Permissions check — RSA keys should be 600
    PERMS=$(stat -f '%Lp' "$KEY_PATH" 2>/dev/null || stat -c '%a' "$KEY_PATH" 2>/dev/null || echo "?")
    if [[ "$PERMS" != "600" && "$PERMS" != "400" ]]; then
        echo "  $(dim "current permissions: $PERMS")"
        if confirm "Tighten permissions to 600?" Y; then
            chmod 600 "$KEY_PATH"
            echo "  $(green "chmod 600 $KEY_PATH")"
        fi
    fi
    break
done

ask KALSHI_ENV "Environment (prod|demo)" "prod"
if [[ "$KALSHI_ENV" != "prod" && "$KALSHI_ENV" != "demo" ]]; then
    echo "  $(red "invalid"): must be prod or demo — using prod"
    KALSHI_ENV="prod"
fi

ask DRY_RUN "Dry-run mode? (true|false)" "true"
if [[ "$DRY_RUN" != "true" && "$DRY_RUN" != "false" ]]; then
    DRY_RUN="true"
fi

# ── Show summary ─────────────────────────────────────────────────────────────
echo
echo "$(bold "About to write") $ENV_FILE:"
echo "  KALSHI_API_KEY_ID       = $KALSHI_API_KEY_ID"
echo "  KALSHI_PRIVATE_KEY_PATH = $KEY_PATH"
echo "  KALSHI_ENV              = $KALSHI_ENV"
echo "  DRY_RUN                 = $DRY_RUN"
echo
if [[ "$KALSHI_ENV" == "prod" && "$DRY_RUN" == "false" ]]; then
    echo "  $(red "⚠  LIVE mode against PROD — real orders will be submitted")"
    echo
fi
confirm "Proceed?" Y || { echo "aborted"; exit 1; }

# ── Backup existing .env, then write ─────────────────────────────────────────
if [[ -f "$ENV_FILE" ]]; then
    backup="$ENV_FILE.bak.$(date +%Y%m%d-%H%M%S)"
    cp "$ENV_FILE" "$backup"
    echo "$(dim "backed up existing .env →") $backup"
fi

# Seed risk/strategy/scanner/db blocks from .env.example so the new .env is
# self-contained; overwrite only the credential block with our prompted values.
{
    cat <<EOF
# ── Kalshi credentials ────────────────────────────────────────────────────────
KALSHI_API_KEY_ID=$KALSHI_API_KEY_ID
KALSHI_PRIVATE_KEY_PATH=$KEY_PATH
KALSHI_ENV=$KALSHI_ENV

# ── Safety ────────────────────────────────────────────────────────────────────
DRY_RUN=$DRY_RUN

EOF
    if [[ -f "$EXAMPLE_FILE" ]]; then
        # Skip the credential + safety blocks from the example; keep the rest.
        awk '
            /^# ── Risk limits/        { keep=1 }
            keep                       { print }
        ' "$EXAMPLE_FILE"
    fi
} > "$ENV_FILE"

chmod 600 "$ENV_FILE"

echo "$(green "✓ wrote") $ENV_FILE  $(dim "(chmod 600)")"
echo
echo "Next: $(bold "cd $REPO_ROOT && venv/bin/python main.py")"
