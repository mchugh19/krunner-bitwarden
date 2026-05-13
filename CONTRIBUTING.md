# Contributing

Contributions are welcome! Here's how to get started.

## Development setup

```bash
git clone https://github.com/ccarpo/krunner-bitwarden.git
cd krunner-bitwarden

# Install dependencies (Arch/CachyOS)
sudo pacman -S python-dbus python-gobject xclip

# Run the plugin directly (without systemd)
python3 bitwarden_runner.py
```

In a second terminal, test the DBus interface:

```bash
# Check actions
qdbus6 --literal org.kde.krunner.bitwarden /runner org.kde.krunner1.Actions

# Test a search query
qdbus6 --literal org.kde.krunner.bitwarden /runner org.kde.krunner1.Match "github"
```

## Project structure

| File | Purpose |
|---|---|
| `bitwarden_runner.py` | Main plugin — DBus service, KRunner interface, all logic |
| `bitwarden_unlock_helper.py` | Standalone terminal helper for vault unlock |
| `plasma-runner-bitwarden.desktop` | KRunner plugin metadata (DBus runner descriptor) |
| `krunner-bitwarden.service` | systemd user service unit |
| `config.ini` | Default configuration template |
| `install.sh` | User-local installation script |
| `uninstall.sh` | Clean removal script |

## DBus interface

The plugin implements `org.kde.krunner1` at `org.kde.krunner.bitwarden /runner`:

| Method | Signature | Description |
|---|---|---|
| `Actions()` | `→ a(sss)` | Returns static list of actions |
| `Match(query)` | `s → a(sssida{sv})` | Returns matching credentials and commands |
| `Run(matchId, actionId)` | `ss →` | Executes the selected action |

Match tuple: `(id, text, iconName, categoryRelevance, relevance, properties{sv})`

## Submitting changes

1. Fork the repository
2. Create a branch: `git checkout -b feature/my-improvement`
3. Make your changes, test with the DBus commands above
4. Update `CHANGELOG.md`
5. Open a pull request
