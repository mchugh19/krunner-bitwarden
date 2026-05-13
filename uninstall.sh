#!/usr/bin/env bash
# uninstall.sh — removes krunner-bitwarden for the current user
set -euo pipefail

echo "Uninstalling krunner-bitwarden…"

# ── 1. Stop and disable the service ──────────────────────────────────────────
systemctl --user disable --now krunner-bitwarden.service 2>/dev/null || true
rm -f "$HOME/.config/systemd/user/krunner-bitwarden.service"
systemctl --user daemon-reload

# ── 2. Remove desktop file (both Plasma 5 and 6 paths) ───────────────────────
rm -f "$HOME/.local/share/krunner/dbusplugins/plasma-runner-bitwarden.desktop"
rm -f "$HOME/.local/share/kservices5/plasma-runner-bitwarden.desktop"

# ── 3. Remove plugin files ────────────────────────────────────────────────────
rm -rf "$HOME/.local/share/krunner-bitwarden"

# ── 4. Remove plugin entry from krunnerrc ────────────────────────────────────
if command -v kwriteconfig6 &>/dev/null; then
    kwriteconfig6 --file krunnerrc --group Plugins --key bitwardenEnabled false
elif command -v kwriteconfig5 &>/dev/null; then
    kwriteconfig5 --file krunnerrc --group Plugins --key bitwardenEnabled false
fi

# ── 5. Rebuild KRunner cache ──────────────────────────────────────────────────
if command -v kbuildsycoca6 &>/dev/null; then
    kbuildsycoca6 --noincremental 2>/dev/null || true
elif command -v kbuildsycoca5 &>/dev/null; then
    kbuildsycoca5 --noincremental 2>/dev/null || true
fi

echo ""
echo "Uninstall complete. Config at ~/.config/krunner-bitwarden/ was left intact."
echo "Remove it manually with:  rm -rf ~/.config/krunner-bitwarden"
