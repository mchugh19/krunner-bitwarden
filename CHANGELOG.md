# Changelog

All notable changes to krunner-bitwarden are documented here.

## [1.1.0] — 2026-05-13

### Added
- **Multi-token search narrowing**: type `paypal ccarpo` to filter results by name AND username simultaneously. The first token is sent to the Bitwarden API; additional tokens are filtered client-side.
- **In-memory match cache**: credential data is cached per `Match()` call so `Run()` actions (copy username, copy TOTP, open URL) work instantly without a second API round-trip.
- **`category` property** on all matches: KRunner now groups all Bitwarden results under a "Bitwarden" category header, displaying all results instead of collapsing to one.
- **Unique relevance scores** per result position so KRunner preserves ordering within the category.
- **`kwriteconfig6/5`** call in `install.sh` to explicitly enable the plugin in `krunnerrc`.
- `uninstall.sh` now removes desktop files from both Plasma 5 and Plasma 6 paths and cleans up `krunnerrc`.

### Fixed
- Plasma 6 desktop file path corrected from `krunner/dbusrunners` to `krunner/dbusplugins`.
- `X-Plasma-Request-Actions-Once=true` set in desktop file to prevent redundant `Actions()` DBus calls on every query.
- `install.sh` KRunner restart now tries `kstart6` before `kstart5`.
- Removed stale blank lines from desktop file.

### Changed
- Match ID is now the Bitwarden item UUID instead of a full JSON blob — cleaner, more stable, no size concerns.
- `categoryRelevance` for credential results raised to `Highest (100)` so Bitwarden appears near the top of results.

## [1.0.0] — 2026-05-13

### Added
- Initial release ported from [keypirinha-bitwarden](https://github.com/ccarpo/keypirinha-bitwarden).
- KRunner DBus plugin exposing `org.kde.krunner1` interface.
- Credential search via `bw serve` REST API.
- Actions: copy password (default), copy username, copy TOTP, open URL.
- Vault management: status, lock, unlock (terminal prompt), start/stop server.
- Password generator with configurable presets (password, passphrase, PIN).
- Clipboard support: `xclip`, `xsel`, `wl-copy` (auto-detected).
- systemd user service for automatic startup.
- `install.sh` / `uninstall.sh` scripts.
- Plasma 5 and Plasma 6 support.
