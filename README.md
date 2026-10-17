# krunner-bitwarden (rbw backend)

A [KRunner](https://userbase.kde.org/Plasma/Krunner) plugin that lets you search your [Bitwarden](https://bitwarden.com/) vault, copy credentials to the clipboard, open URLs, and manage the vault, all without leaving your keyboard.

This fork uses [rbw](https://github.com/doy/rbw), the unofficial Bitwarden CLI, instead of the official `bw` CLI. The official CLI is not required and `bw serve` is not used. All vault operations are short-lived `rbw` subprocess calls, and the background `rbw-agent` holds the vault keys in memory, the same way `ssh-agent` works.

![KRunner showing Bitwarden results](docs/screenshot.png)

---

## Features

- **Search credentials** by name, username, or URL as you type
- **Multi-token narrowing**: type `paypal ccarpo` to filter by both name and username
- **Copy password** (default action on Enter), fetched from rbw at action time
- **Copy username** (action button)
- **Copy TOTP** code (action button)
- **Open URL** in the default browser (action button)
- **Unlock vault** through rbw's pinentry prompt. The plugin never sees or stores your master password
- **Lock vault** and **sync vault** from KRunner, and stop `rbw-agent`
- **Generate passwords** with configurable presets (password, passphrase, PIN)
- Works on **Plasma 5 and Plasma 6**
- No passwords are kept in the plugin's match cache, and there is no local HTTP API

---

## Requirements

| Dependency | Notes |
|---|---|
| rbw | Fedora: `sudo dnf install rbw`. Arch: `sudo pacman -S rbw`. Other: `cargo install --locked rbw` |
| A GUI pinentry for rbw | e.g. `pinentry-qt`. Set it with `rbw config set pinentry pinentry-qt`. A terminal pinentry will not work: the runner is a systemd user service with no TTY |
| Python 3.8+ | `python3` |
| python-dbus | Fedora: `python3-dbus`. Debian/Ubuntu: `python3-dbus` |
| python-gobject | Fedora: `python3-gobject`. Debian/Ubuntu: `python3-gi` |
| A clipboard tool | `xclip`, `xsel`, or `wl-clipboard` |

Set rbw up first, if you have not already:

```bash
rbw config set email you@example.com
rbw config set pinentry pinentry-qt
rbw register   # only for the official bitwarden.com server
rbw login
rbw sync
```

---

## Installation

```bash
git clone <this repo>
cd krunner-bitwarden
bash install.sh
```

The script will:
1. Copy plugin files to `~/.local/share/krunner-bitwarden/`
2. Install the `.desktop` runner metadata
3. Install and enable the systemd user service
4. Write a default config (if none exists)
5. Enable the plugin in KRunner and rebuild the cache

Open KRunner (`Alt+Space`) and type any credential name. On first use, the vault will be locked: search `unlock bitwarden` and press Enter, then enter your master password in the pinentry prompt.

---

## Usage

| What you type | What happens |
|---|---|
| `paypal` | Shows all Bitwarden entries matching "paypal" |
| `paypal ccarpo` | Filters to entries matching "paypal" **and** "ccarpo" (name, username, or URL) |
| `bitwarden` | Shows vault status, lock, unlock, and sync |
| `unlock bitwarden` | Unlocks the vault (`rbw unlock`, pinentry prompt) |
| `lock bitwarden` | Locks the vault immediately (`rbw lock`) |
| `sync bitwarden` | Syncs the local vault copy (`rbw sync`) |
| `stop agent` | Stops `rbw-agent`, which also locks the vault |
| `generate password` | Shows password generator presets |
| `generate passphrase` | Shows passphrase generator preset |

### Actions on credential entries

After a credential appears, use the action buttons (shown on hover or Tab):

| Action | Result |
|---|---|
| **Enter** (default) | Copies the **password** to clipboard |
| Copy Username | Copies the **username** to clipboard |
| Copy TOTP | Copies the current **TOTP code** to clipboard |
| Open URL | Opens the entry's **URL** in the default browser |

---

## Configuration

The config file is created at `~/.config/krunner-bitwarden/config.ini` on first run.

```ini
[bitwarden]
# Path to the rbw executable (leave empty to use rbw from PATH)
rbw_executable_path =

# rbw profile (leave empty for the default profile; sets RBW_PROFILE)
rbw_profile =
```

There are deliberately no unlock or password settings. Unlocking, lock timing, and sync interval belong to rbw:

```bash
rbw config set lock_timeout 3600    # seconds the agent keeps keys in memory
rbw config set sync_interval 3600   # seconds between automatic syncs, 0 disables
rbw config set pinentry pinentry-qt
```

### Password generator presets

Add as many `[generator.<name>]` sections as you like:

```ini
[generator.default]
type = password
length = 16
uppercase = true
lowercase = true
numbers = true
special = false

[generator.passphrase]
type = passphrase
words = 4
separator = -
capitalize = true
include_number = true
```

rbw's generator is simpler than these presets, so some fields are approximated or ignored:

- Passphrase presets become `rbw generate <words> --diceware`. `separator`, `capitalize`, and `include_number` are ignored.
- A preset with numbers only (no uppercase, lowercase, or special) becomes `rbw generate <length> --only-numbers`, which is how the PIN preset works.
- `special = false` becomes `--no-symbols`; `special = true` includes symbols.
- The `uppercase` / `lowercase` toggles have no rbw equivalent and are ignored.
- Generated passwords are printed by rbw and copied to the clipboard. They are **not** saved to your vault, because the runner calls `rbw generate` with no entry name.

After editing, restart the service:

```bash
systemctl --user restart krunner-bitwarden
```

---

## Service management

```bash
# Status
systemctl --user status krunner-bitwarden

# Restart after config changes
systemctl --user restart krunner-bitwarden

# Stop
systemctl --user stop krunner-bitwarden

# Disable autostart
systemctl --user disable krunner-bitwarden
```

This service is only the KRunner plugin. `rbw-agent` is a separate process, started by rbw itself on first use.

---

## Uninstallation

```bash
bash uninstall.sh
```

Your config at `~/.config/krunner-bitwarden/` is preserved. Remove it manually if desired:

```bash
rm -rf ~/.config/krunner-bitwarden
```

---

## Troubleshooting

### Plugin not appearing in KRunner

1. Make sure the service is running:
   ```bash
   systemctl --user status krunner-bitwarden
   ```
2. Verify the DBus service responds:
   ```bash
   qdbus6 --literal org.kde.krunner.bitwarden /runner org.kde.krunner1.Match "test"
   ```
3. Rebuild the KRunner cache and restart:
   ```bash
   kbuildsycoca6 --noincremental
   kquitapp6 krunner
   ```
4. Ensure the plugin is enabled in **System Settings → Search → Plasma Search**.

### Only one result shown

KRunner collapses lower-ranked categories to a single entry. Bitwarden results use the highest category relevance, so they should expand fully. If they appear near the bottom, it means another runner produced a higher-scoring result for that query.

### No results, vault looks locked

Check rbw itself first, in a terminal:

```bash
rbw unlocked && echo unlocked || echo locked
rbw list | head
```

### Unlock prompt never appears

The runner has no TTY, so rbw must be configured with a GUI pinentry:

```bash
rbw config set pinentry pinentry-qt
rbw stop-agent   # so the next rbw call starts an agent with the new setting
```

### `rbw` not found

Set the full path in config:

```ini
rbw_executable_path = /home/youruser/.cargo/bin/rbw
```

---

## Architecture

```
krunner-bitwarden/
├── bitwarden_runner.py            # Main DBus service / KRunner plugin (rbw subprocess calls)
├── plasma-runner-bitwarden.desktop  # KRunner plugin metadata
├── krunner-bitwarden.service      # systemd user service (the plugin only)
├── config.ini                     # Default configuration template
├── install.sh                     # Installation script
└── uninstall.sh                   # Uninstallation script
```

The plugin exposes the `org.kde.krunner1` DBus interface on `org.kde.krunner.bitwarden /runner`. KRunner discovers it via the `.desktop` file in `~/.local/share/krunner/dbusplugins/` (Plasma 6) or `~/.local/share/kservices5/` (Plasma 5).

Per query, the plugin runs `rbw list --raw` (which returns entry names, usernames, URLs, and types, but no passwords) and filters locally. Passwords and TOTP codes are fetched with `rbw get` / `rbw code` only when you run an action.

---

## License

MIT, see [LICENSE](LICENSE)
