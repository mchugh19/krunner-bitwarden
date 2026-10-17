#!/usr/bin/env bash
# install.sh — installs krunner-bitwarden for the current user
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

INSTALL_DIR="$HOME/.local/share/krunner-bitwarden"
SERVICE_DIR="$HOME/.config/systemd/user"
CONFIG_DIR="$HOME/.config/krunner-bitwarden"

# Plasma 6 uses krunner/dbusplugins; Plasma 5 uses kservices5
if plasmashell --version 2>/dev/null | grep -q '^plasmashell 6'; then
    DESKTOP_DIR="$HOME/.local/share/krunner/dbusplugins"
else
    DESKTOP_DIR="$HOME/.local/share/kservices5"
fi

echo "Installing krunner-bitwarden (rbw backend)..."

# ── 0. Check for rbw ──────────────────────────────────────────────────────────
# The plugin shells out to rbw for every vault operation. It still installs
# without it, but cannot do anything until rbw is present and configured
# (rbw config set email <you@example.com>, then rbw login / rbw register).
if ! command -v rbw &>/dev/null; then
    echo "WARNING: rbw not found in PATH. Install rbw first, e.g.:"
    echo "  Fedora:  sudo dnf install rbw"
    echo "  Arch:    sudo pacman -S rbw"
    echo "  Other:   cargo install --locked rbw"
    echo ""
fi
# Unlock prompts come from rbw-agent via pinentry. Under a systemd user
# service there is no TTY, so the pinentry must be a GUI one.
if command -v rbw &>/dev/null && ! rbw config show 2>/dev/null | grep -qi '^pinentry.*qt\|^pinentry.*gtk\|^pinentry.*gnome\|^pinentry.*gui'; then
    echo "Tip: make sure rbw uses a GUI pinentry, e.g.:"
    echo "  rbw config set pinentry pinentry-qt"
    echo ""
fi

# ── 1. Copy plugin files ──────────────────────────────────────────────────────
mkdir -p "$INSTALL_DIR"
cp "$SCRIPT_DIR/bitwarden_runner.py"        "$INSTALL_DIR/"
chmod +x "$INSTALL_DIR/bitwarden_runner.py"
# Remove the unlock helper left by the previous (bw serve) version, if any
rm -f "$INSTALL_DIR/bitwarden_unlock_helper.py"

# ── 2. Install .desktop file ──────────────────────────────────────────────────
mkdir -p "$DESKTOP_DIR"
cp "$SCRIPT_DIR/plasma-runner-bitwarden.desktop" "$DESKTOP_DIR/"
echo "Desktop file installed to $DESKTOP_DIR"

# ── 3. Install systemd service ────────────────────────────────────────────────
mkdir -p "$SERVICE_DIR"

# Rewrite ExecStart to use the actual install dir
sed "s|%h/.local/share/krunner-bitwarden|$INSTALL_DIR|g" \
    "$SCRIPT_DIR/krunner-bitwarden.service" \
    > "$SERVICE_DIR/krunner-bitwarden.service"

echo "Systemd service installed to $SERVICE_DIR"

# ── 4. Install default config (do NOT overwrite existing one) ─────────────────
if [ ! -f "$CONFIG_DIR/config.ini" ]; then
    mkdir -p "$CONFIG_DIR"
    cp "$SCRIPT_DIR/config.ini" "$CONFIG_DIR/config.ini"
    echo "Default config installed to $CONFIG_DIR/config.ini"
else
    echo "Existing config found at $CONFIG_DIR/config.ini — not overwriting"
fi

# ── 5. Enable and start the service ──────────────────────────────────────────
systemctl --user daemon-reload
systemctl --user enable --now krunner-bitwarden.service
echo "Service enabled and started"

# ── 6. Enable plugin in krunnerrc ────────────────────────────────────────────
if command -v kwriteconfig6 &>/dev/null; then
    kwriteconfig6 --file krunnerrc --group Plugins --key bitwardenEnabled true
elif command -v kwriteconfig5 &>/dev/null; then
    kwriteconfig5 --file krunnerrc --group Plugins --key bitwardenEnabled true
fi

# ── 7. Rebuild KRunner service cache and restart KRunner ─────────────────────
if command -v kbuildsycoca6 &>/dev/null; then
    kbuildsycoca6 --noincremental 2>/dev/null || true
elif command -v kbuildsycoca5 &>/dev/null; then
    kbuildsycoca5 --noincremental 2>/dev/null || true
fi

# Restart KRunner so it picks up the new runner
kquitapp6 krunner 2>/dev/null || kquitapp5 krunner 2>/dev/null || true
sleep 1
(kstart6 krunner &>/dev/null || kstart5 krunner &>/dev/null || true) &

echo ""
echo "Installation complete!"
echo "Open KRunner (Alt+Space) and search for 'Bitwarden' or type any credential name."
echo "On first use the vault will be locked: search 'unlock bitwarden' and press Enter,"
echo "then enter your master password in the rbw pinentry prompt."
echo ""
echo "To configure, edit: $CONFIG_DIR/config.ini"
echo "Then restart the service:  systemctl --user restart krunner-bitwarden"
