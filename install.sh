#!/usr/bin/env bash
# polite-fetch — interactive installation wizard.
#
# Run via:
#   bash <(curl -fsSL https://raw.githubusercontent.com/M00C1FER/polite-fetch/main/install.sh)
# Or, locally:
#   ./install.sh
#
# Supports:  Debian / Ubuntu (incl. WSL2) · Fedora / RHEL · Arch · Alpine · openSUSE · macOS

set -euo pipefail

# ── Colors (gracefully degrade on non-tty) ───────────────────────────────────
if [ -t 1 ]; then C_BOLD="$(tput bold 2>/dev/null || true)"; C_DIM="$(tput dim 2>/dev/null || true)"; C_RESET="$(tput sgr0 2>/dev/null || true)"; C_GREEN="$(tput setaf 2 2>/dev/null || true)"; C_YELLOW="$(tput setaf 3 2>/dev/null || true)"; C_RED="$(tput setaf 1 2>/dev/null || true)"; else C_BOLD=""; C_DIM=""; C_RESET=""; C_GREEN=""; C_YELLOW=""; C_RED=""; fi

say()   { printf "%s%s%s\n" "$C_BOLD" "$1" "$C_RESET"; }
info()  { printf "  %s\n" "$1"; }
ok()    { printf "  %s✓%s %s\n" "$C_GREEN" "$C_RESET" "$1"; }
warn()  { printf "  %s!%s %s\n" "$C_YELLOW" "$C_RESET" "$1"; }
fail()  { printf "  %s✗%s %s\n" "$C_RED" "$C_RESET" "$1" >&2; exit 1; }

prompt_yn() {
    local q="$1" def="${2:-y}" ans
    if [ "$def" = "y" ]; then read -r -p "  $q [Y/n]: " ans; ans="${ans:-y}"; else read -r -p "  $q [y/N]: " ans; ans="${ans:-n}"; fi
    [[ "$ans" =~ ^[Yy] ]]
}
prompt_default() {
    local q="$1" def="$2" ans
    read -r -p "  $q [$def]: " ans; echo "${ans:-$def}"
}

# ── Detect OS / package manager ──────────────────────────────────────────────
detect_os() {
    OS_ID="unknown"; OS_LIKE=""; OS_VERSION=""
    if [ -f /etc/os-release ]; then
        # shellcheck disable=SC1091
        . /etc/os-release
        OS_ID="${ID:-unknown}"
        OS_LIKE="${ID_LIKE:-}"
        OS_VERSION="${VERSION_ID:-}"
    elif [ "$(uname)" = "Darwin" ]; then
        OS_ID="macos"
    fi
    if grep -qi microsoft /proc/sys/kernel/osrelease 2>/dev/null; then OS_WSL=1; else OS_WSL=0; fi
}

pkg_install() {
    # $1.. packages
    case "$OS_ID" in
        debian|ubuntu) sudo apt-get update -qq && sudo apt-get install -y "$@";;
        fedora|rhel|centos) sudo dnf install -y "$@";;
        arch|manjaro) sudo pacman -S --noconfirm "$@";;
        alpine) sudo apk add --no-cache "$@";;
        opensuse*|sles) sudo zypper install -y "$@";;
        macos) command -v brew >/dev/null || fail "Homebrew required on macOS"; brew install "$@";;
        *)
            case "$OS_LIKE" in
                *debian*|*ubuntu*) sudo apt-get install -y "$@";;
                *rhel*|*fedora*) sudo dnf install -y "$@";;
                *) warn "unknown OS — install manually: $*"; return 1;;
            esac;;
    esac
}

# ── Prereqs: Python 3.10+ ────────────────────────────────────────────────────
ensure_python() {
    local pyv
    if command -v python3 >/dev/null 2>&1; then
        pyv="$(python3 -c 'import sys; print("%d.%d"%sys.version_info[:2])')"
        case "$pyv" in
            3.1[0-9]|3.[2-9][0-9]) ok "Python $pyv"; return 0;;
        esac
        warn "Python $pyv detected — polite-fetch needs ≥ 3.10"
    else
        warn "Python 3 not found"
    fi
    if prompt_yn "Install Python 3.10+ via system package manager?"; then
        case "$OS_ID" in
            debian|ubuntu) pkg_install python3 python3-venv python3-pip;;
            fedora|rhel|centos) pkg_install python3 python3-pip;;
            arch|manjaro) pkg_install python python-pip;;
            alpine) pkg_install python3 py3-pip;;
            macos) pkg_install python@3.12;;
            *) fail "install Python 3.10+ manually then re-run install.sh";;
        esac
        ok "Python installed"
    else
        fail "polite-fetch requires Python 3.10+"
    fi
}

ensure_pipx_or_venv() {
    if command -v pipx >/dev/null 2>&1; then ok "pipx available"; return 0; fi
    info "pipx not found — will use a dedicated venv instead"
}

# ── Main wizard ──────────────────────────────────────────────────────────────
main() {
    say "polite-fetch — interactive install wizard"
    detect_os
    info "OS: ${OS_ID}${OS_VERSION:+ $OS_VERSION}${OS_WSL:+ (WSL2)}"
    [ "$OS_WSL" = "1" ] && ok "WSL2 detected — supported"

    say ""
    say "Step 1/4: Python 3.10+"
    ensure_python
    ensure_pipx_or_venv

    say ""
    say "Step 2/4: Install location + optional features"
    local INSTALL_HOME EXTRAS=""
    INSTALL_HOME="$(prompt_default "Install root" "$HOME/.local/share/polite-fetch")"
    if prompt_yn "Enable Tier-2 (curl_cffi browser-impersonation)?" y; then EXTRAS+="tier2,"; fi
    if prompt_yn "Enable Tier-3 (Playwright + browserforge, opt-in only)?" n; then EXTRAS+="tier3,"; fi
    if prompt_yn "Install MCP server wrapper (FastMCP)?" y; then EXTRAS+="mcp,"; fi
    EXTRAS="${EXTRAS%,}"

    say ""
    say "Step 3/4: Fetch source + install"
    mkdir -p "$INSTALL_HOME"
    if [ -d "$INSTALL_HOME/.git" ]; then
        info "Updating existing checkout in $INSTALL_HOME"
        ( cd "$INSTALL_HOME" && git pull -q )
    else
        git clone -q https://github.com/M00C1FER/polite-fetch.git "$INSTALL_HOME"
    fi

    cd "$INSTALL_HOME"
    if command -v pipx >/dev/null 2>&1; then
        if [ -n "$EXTRAS" ]; then
            pipx install --force "$INSTALL_HOME[$EXTRAS]"
        else
            pipx install --force "$INSTALL_HOME"
        fi
    else
        python3 -m venv .venv
        # shellcheck disable=SC1091
        . .venv/bin/activate
        pip install --quiet --upgrade pip
        if [ -n "$EXTRAS" ]; then
            pip install --quiet -e ".[$EXTRAS]"
        else
            pip install --quiet -e .
        fi
        # Drop a launcher into ~/.local/bin
        local BIN="${HOME}/.local/bin"; mkdir -p "$BIN"
        cat > "$BIN/polite-fetch" <<EOF
#!/usr/bin/env bash
exec "$INSTALL_HOME/.venv/bin/polite-fetch" "\$@"
EOF
        cat > "$BIN/polite-fetch-mcp" <<EOF
#!/usr/bin/env bash
exec "$INSTALL_HOME/.venv/bin/polite-fetch-mcp" "\$@"
EOF
        chmod +x "$BIN/polite-fetch" "$BIN/polite-fetch-mcp"
    fi
    ok "polite-fetch installed"

    say ""
    say "Step 4/4: Verify"
    if command -v polite-fetch >/dev/null 2>&1; then
        polite-fetch --json https://example.com >/dev/null && ok "live fetch works"
    else
        warn "polite-fetch not on PATH — make sure $HOME/.local/bin is in \$PATH"
    fi
    say ""
    ok "Done. Try: polite-fetch https://example.com"
}

main "$@"
