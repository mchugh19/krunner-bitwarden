#!/usr/bin/env python3
"""KRunner plugin for Bitwarden. Searches the vault via the rbw CLI and copies credentials.

Backend: rbw (https://github.com/doy/rbw), the unofficial Bitwarden CLI.
rbw has no HTTP API. Its background agent (rbw-agent) holds the vault keys
in memory, and every operation here is a short-lived `rbw` subprocess call:

    rbw list --raw            all entries as JSON (id, name, user, uris, type)
    rbw get <id>              password for an entry (UUID accepted as the name)
    rbw get --field <f> <id>  a single field (username, uris, ...)
    rbw code <id>             current TOTP code for an entry
    rbw generate <len> ...    generate a password (no name = print only, not saved)
    rbw unlock / rbw lock     unlock (prompts via pinentry) / lock the vault
    rbw unlocked              exit status 0 if and only if the vault is unlocked
    rbw sync                  refresh the local vault copy
    rbw stop-agent            stop rbw-agent (also locks the vault)

Unlocking is done by rbw-agent through its configured pinentry program.
This plugin never sees, stores, or passes the master password.
"""

import configparser
import json
import os
import signal
import subprocess
import threading

import dbus
import dbus.service
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

# -- DBus identifiers ---------------------------------------------------------
BUS_NAME = "org.kde.krunner.bitwarden"
OBJ_PATH = "/runner"
IFACE = "org.kde.krunner1"

# -- Match types (mirrors Plasma::QueryType) ----------------------------------
# categoryRelevance values, mirror KRunner::QueryMatch::CategoryRelevance enum
CATEGORY_LOWEST = 0
CATEGORY_LOW = 30
CATEGORY_MODERATE = 50
CATEGORY_HIGH = 70
CATEGORY_HIGHEST = 100

# -- Action IDs ---------------------------------------------------------------
ACTION_COPY_PASSWORD = ""          # primary / default
ACTION_COPY_USERNAME = "copy_username"
ACTION_COPY_TOTP = "copy_totp"
ACTION_OPEN_URL = "open_url"

# -- Config defaults ----------------------------------------------------------
CONFIG_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
    "krunner-bitwarden",
)
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.ini")

DEFAULTS = {
    "bitwarden": {
        # Empty means: use `rbw` from PATH.
        "rbw_executable_path": "",
        # Empty means: rbw's default profile. Sets RBW_PROFILE when given.
        "rbw_profile": "",
    }
}

ICON = "password-vault"

# rbw entry types that are not logins. `rbw list --raw` reports the type as a
# string (older/other builds may omit it); anything not in this set, and a
# missing type, is treated as a login so a spelling difference in rbw's type
# names cannot silently hide every credential.
NON_LOGIN_TYPES = {
    "card", "creditcard", "identity", "note", "securenote", "secure note",
    "sshkey", "ssh key", "ssh",
}


# -----------------------------------------------------------------------------

class BitwardenRunner(dbus.service.Object):

    def __init__(self):
        DBusGMainLoop(set_as_default=True)
        session_bus = dbus.SessionBus()
        session_bus.request_name(BUS_NAME, dbus.bus.NAME_FLAG_REPLACE_EXISTING)
        bus_name = dbus.service.BusName(BUS_NAME, bus=session_bus)
        dbus.service.Object.__init__(self, bus_name, OBJ_PATH)

        # Runtime state. The match cache deliberately holds no secrets:
        # rbw list returns no passwords, so Run() fetches the password,
        # TOTP code, etc. from rbw at action time.
        self._generator_presets = []
        self._match_cache = {}  # item_id -> {"name", "username", "uris"}
        self._rbw_not_found = False

        self._load_config()
        self._load_generator_presets()
        # No server to start and nothing to unlock here: rbw-agent starts
        # on first use and owns the vault state and lock timeout.

    # -- Config ---------------------------------------------------------------

    def _load_config(self):
        cfg = configparser.ConfigParser()

        # Write defaults if file absent
        if not os.path.exists(CONFIG_FILE):
            os.makedirs(CONFIG_DIR, exist_ok=True)
            cfg.read_dict(DEFAULTS)
            cfg["generator.default"] = {
                "type": "password", "length": "16",
                "uppercase": "true", "lowercase": "true",
                "numbers": "true", "special": "false",
            }
            cfg["generator.strong"] = {
                "type": "password", "length": "24",
                "uppercase": "true", "lowercase": "true",
                "numbers": "true", "special": "true",
            }
            cfg["generator.passphrase"] = {
                "type": "passphrase", "words": "3",
                "separator": "-", "capitalize": "false",
                "include_number": "false",
            }
            cfg["generator.pin"] = {
                "type": "password", "length": "6",
                "uppercase": "false", "lowercase": "false",
                "numbers": "true", "special": "false",
            }
            with open(CONFIG_FILE, "w") as f:
                cfg.write(f)
        else:
            cfg.read(CONFIG_FILE)

        bw = cfg["bitwarden"] if "bitwarden" in cfg else {}

        # Legacy alias from the bw-serve version of this plugin:
        # bw_executable_path was its name for the CLI path. The literal
        # default "bw" from old configs means "unset", not the rbw path.
        legacy_exe = bw.get("bw_executable_path", "")
        if legacy_exe == "bw":
            legacy_exe = ""
        self.rbw_exe = (
            bw.get("rbw_executable_path", "") or legacy_exe or "rbw"
        )
        self.rbw_profile = bw.get("rbw_profile", "")

        self._cfg = cfg  # keep for preset parsing

    def _load_generator_presets(self):
        self._generator_presets = []
        cfg = self._cfg
        for section in cfg.sections():
            if not section.lower().startswith("generator."):
                continue
            label = section[len("generator."):]
            ptype = cfg.get(section, "type", fallback="password").lower()
            if ptype == "passphrase":
                preset = {
                    "label": label, "type": "passphrase",
                    "words": cfg.getint(section, "words", fallback=3),
                    "separator": cfg.get(section, "separator", fallback="-"),
                    "capitalize": cfg.getboolean(section, "capitalize", fallback=False),
                    "include_number": cfg.getboolean(section, "include_number", fallback=False),
                }
            else:
                preset = {
                    "label": label, "type": "password",
                    "length": cfg.getint(section, "length", fallback=16),
                    "uppercase": cfg.getboolean(section, "uppercase", fallback=True),
                    "lowercase": cfg.getboolean(section, "lowercase", fallback=True),
                    "numbers": cfg.getboolean(section, "numbers", fallback=True),
                    "special": cfg.getboolean(section, "special", fallback=False),
                }
            self._generator_presets.append(preset)

    # -- rbw subprocess plumbing ----------------------------------------------

    def _rbw_env(self):
        env = dict(os.environ)
        if self.rbw_profile:
            env["RBW_PROFILE"] = self.rbw_profile
        return env

    def _run_rbw(self, args, timeout=10):
        """Run rbw, return the CompletedProcess, or None on spawn/timeout error."""
        try:
            return subprocess.run(
                [self.rbw_exe] + list(args),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                env=self._rbw_env(),
            )
        except FileNotFoundError:
            self._rbw_not_found = True
            print(f"[bitwarden] rbw executable not found: {self.rbw_exe}", flush=True)
            return None
        except subprocess.TimeoutExpired:
            print(f"[bitwarden] rbw timed out: {' '.join(args)}", flush=True)
            return None
        except Exception as exc:
            print(f"[bitwarden] rbw error ({' '.join(args)}): {exc}", flush=True)
            return None

    def _rbw_output(self, args, timeout=10):
        """Run rbw and return stdout as text, without its trailing newline."""
        proc = self._run_rbw(args, timeout=timeout)
        if proc is None or proc.returncode != 0:
            if proc is not None and proc.stderr:
                first = proc.stderr.decode(errors="replace").strip().splitlines()
                if first:
                    print(f"[bitwarden] rbw {' '.join(args)}: {first[0]}", flush=True)
            return ""
        out = proc.stdout.decode(errors="replace")
        # Strip exactly the trailing newline rbw println adds. Passwords
        # ending in other whitespace must survive untouched.
        if out.endswith("\n"):
            out = out[:-1]
            if out.endswith("\r"):
                out = out[:-1]
        return out

    def _vault_state(self) -> str:
        """One of: "unlocked", "locked", "missing"."""
        if self._rbw_not_found:
            return "missing"
        proc = self._run_rbw(["unlocked"], timeout=5)
        if proc is None:
            return "missing" if self._rbw_not_found else "locked"
        # Current rbw has `rbw unlocked` (exit 0 = unlocked). Very old rbw
        # rejects the subcommand; report locked rather than guess unlocked,
        # because searching a locked vault would fire a pinentry prompt
        # from inside Match(), which runs on every keystroke.
        return "unlocked" if proc.returncode == 0 else "locked"

    def _list_items(self):
        """All vault entries from `rbw list --raw`, normalised to dicts.

        Raw entries look like:
            {"id": "<uuid>", "name": "...", "user": "..." or null,
             "folder": "..." or null, "uris": ["https://..."] or null,
             "type": "login" or null}
        Only call this when the vault is unlocked (see Match); rbw would
        otherwise try to unlock via pinentry from inside the search path.
        """
        proc = self._run_rbw(["list", "--raw"], timeout=10)
        if proc is None or proc.returncode != 0:
            if proc is not None and proc.stderr:
                first = proc.stderr.decode(errors="replace").strip().splitlines()
                if first:
                    print(f"[bitwarden] rbw list: {first[0]}", flush=True)
            return []
        try:
            raw_items = json.loads(proc.stdout.decode(errors="replace"))
        except Exception as exc:
            print(f"[bitwarden] Cannot parse rbw list output: {exc}", flush=True)
            return []

        items = []
        for raw in raw_items if isinstance(raw_items, list) else []:
            if not isinstance(raw, dict) or not raw.get("id"):
                continue
            entry_type = str(raw.get("type") or "").strip().lower()
            if entry_type in NON_LOGIN_TYPES:
                continue
            uris = []
            for uri in raw.get("uris") or []:
                if isinstance(uri, str):
                    uris.append(uri)
                elif isinstance(uri, dict) and uri.get("uri"):
                    uris.append(uri["uri"])
            items.append({
                "id": raw["id"],
                "name": raw.get("name") or "",
                # `rbw list` calls the field "user"; `rbw get --raw` uses
                # "username" inside its data object. Accept both.
                "username": raw.get("user") or raw.get("username") or "",
                "uris": uris,
            })
        return items

    def _get_field(self, item_id: str, field: str) -> str:
        return self._rbw_output(["get", "--field", field, item_id])

    # -- KRunner DBus interface -------------------------------------------------

    @dbus.service.method(IFACE, out_signature="a(sss)")
    def Actions(self):
        return [
            (ACTION_COPY_USERNAME, "Copy Username", "edit-copy"),
            (ACTION_COPY_TOTP,     "Copy TOTP",     "accept_time_event"),
            (ACTION_OPEN_URL,      "Open URL",      "internet-web-browser"),
        ]

    @dbus.service.method(IFACE, in_signature="s", out_signature="a(sssida{sv})")
    def Match(self, query: str):
        query = query.strip()
        matches = []

        state = self._vault_state()
        rbw_available = state != "missing"
        vault_locked = state != "unlocked"

        # -- Management commands ------------------------------------------------
        q_lower = query.lower()

        # Status
        if "bitwarden" in q_lower or "bw status" in q_lower or query == "":
            if not rbw_available:
                desc = "rbw not found (set rbw_executable_path in config.ini)"
            elif vault_locked:
                desc = "Vault LOCKED"
            else:
                desc = "Vault UNLOCKED"
            matches.append((
                "cmd:status", f"Bitwarden Status: {desc}", ICON,
                CATEGORY_LOWEST, 0.5, {"subtext": desc, "category": "Bitwarden"}
            ))

        # Lock
        if rbw_available and not vault_locked and (
                "lock" in q_lower or "bitwarden" in q_lower):
            matches.append((
                "cmd:lock", "Lock Bitwarden", ICON,
                CATEGORY_MODERATE, 0.6, {"subtext": "Lock the vault immediately (rbw lock)", "category": "Bitwarden"}
            ))

        # Unlock. rbw prompts through pinentry, so no terminal helper and
        # no password handling in this plugin.
        if rbw_available and vault_locked and (
                "unlock" in q_lower or "bitwarden" in q_lower or "password" in q_lower):
            matches.append((
                "cmd:unlock", "Unlock Bitwarden", ICON,
                CATEGORY_HIGH, 0.9,
                {"subtext": "Press Enter to unlock (rbw pinentry prompt)", "category": "Bitwarden"}
            ))

        # Sync
        if rbw_available and not vault_locked and (
                "sync" in q_lower or "bitwarden" in q_lower):
            matches.append((
                "cmd:sync", "Sync Bitwarden Vault", ICON,
                CATEGORY_LOWEST, 0.4, {"subtext": "Refresh the local vault copy (rbw sync)", "category": "Bitwarden"}
            ))

        # Stop agent
        if rbw_available and not vault_locked and (
                "stop" in q_lower or "agent" in q_lower):
            matches.append((
                "cmd:stop_agent", "Stop rbw Agent", ICON,
                CATEGORY_LOWEST, 0.4, {"subtext": "Stop rbw-agent, which also locks the vault", "category": "Bitwarden"}
            ))

        # Generate password
        if "generate" in q_lower or "password" in q_lower or "passphrase" in q_lower:
            for preset in self._generator_presets:
                summary = self._preset_summary(preset)
                matches.append((
                    f"cmd:generate:{json.dumps(preset)}",
                    f"Generate Password ({preset['label']})", ICON,
                    CATEGORY_MODERATE, 0.5,
                    {"subtext": summary, "category": "Bitwarden"}
                ))

        # -- Credential search ----------------------------------------------------
        if len(query) >= 3 and rbw_available and not vault_locked:
            matches.extend(self._search_credentials(query.split()))

        return matches

    @dbus.service.method(IFACE, in_signature="ss")
    def Run(self, match_id: str, action_id: str):
        if match_id.startswith("cmd:"):
            self._run_command(match_id[4:], action_id)
            return

        # Credential result. match_id is the Bitwarden item UUID, which rbw
        # accepts directly as the entry name for get/code.
        cached = self._match_cache.get(match_id, {})

        if action_id == ACTION_COPY_USERNAME:
            username = cached.get("username") or self._get_field(match_id, "username")
            self._copy(username)
        elif action_id == ACTION_COPY_TOTP:
            self._fetch_and_copy_totp(match_id)
        elif action_id == ACTION_OPEN_URL:
            uris = cached.get("uris") or self._get_field(match_id, "uris").splitlines()
            if uris and uris[0]:
                subprocess.Popen(["xdg-open", uris[0]])
        else:
            # Default: copy password, fetched from rbw at action time.
            self._copy(self._rbw_output(["get", match_id]))

    # -- Internal helpers ---------------------------------------------------------

    def _run_command(self, cmd: str, _action_id: str):
        if cmd == "status":
            pass  # informational only
        elif cmd == "lock":
            self._lock_vault()
        elif cmd == "unlock":
            self._unlock_vault()
        elif cmd == "sync":
            self._sync_vault()
        elif cmd == "stop_agent":
            self._stop_agent()
        elif cmd.startswith("generate:"):
            preset_json = cmd[len("generate:"):]
            try:
                preset = json.loads(preset_json)
                generated = self._generate_password(preset)
                if generated:
                    self._copy(generated)
            except Exception as exc:
                print(f"[bitwarden] Generate error: {exc}", flush=True)

    def _search_credentials(self, tokens: list):
        matches = []
        if not tokens:
            return matches
        try:
            items = self._list_items()
            # Multi-token narrowing: every token must appear in the entry's
            # name, username, or one of its URIs.
            def matches_all(item):
                haystack = (
                    item["name"] + " " + item["username"] + " " +
                    " ".join(item["uris"])
                ).lower()
                return all(t.lower() in haystack for t in tokens)

            cred_items = [it for it in items if matches_all(it)]
            for i, item in enumerate(cred_items):
                item_id = item["id"]
                username = item["username"]
                # Unique relevance per position so KRunner shows all entries
                relevance = max(0.5, 0.95 - i * 0.02)
                # Cache only what Run() needs besides the secret itself.
                self._match_cache[item_id] = {
                    "name": item["name"],
                    "username": username,
                    "uris": item["uris"],
                }
                matches.append((
                    item_id,
                    item["name"],
                    ICON,
                    CATEGORY_HIGHEST,
                    relevance,
                    {"subtext": username if username else "no username", "category": "Bitwarden"}
                ))
        except Exception as exc:
            print(f"[bitwarden] Search error: {exc}", flush=True)
        return matches

    def _fetch_and_copy_totp(self, item_id: str):
        totp = self._rbw_output(["code", item_id])
        if totp:
            self._copy(totp)

    def _copy(self, text: str):
        if not text:
            return
        # Try xclip first, fall back to xsel, then wl-copy (Wayland)
        for cmd in (["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"], ["wl-copy"]):
            try:
                proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                        stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL)
                proc.communicate(input=text.encode())
                if proc.returncode == 0:
                    return
            except FileNotFoundError:
                continue
        print("[bitwarden] No clipboard tool found (install xclip, xsel, or wl-clipboard)", flush=True)

    def _lock_vault(self):
        proc = self._run_rbw(["lock"], timeout=10)
        if proc is not None and proc.returncode == 0:
            print("[bitwarden] Vault locked", flush=True)

    def _unlock_vault(self):
        # Non-blocking: rbw talks to rbw-agent, which presents the pinentry
        # prompt. Blocking the DBus Run() call on it would freeze KRunner.
        try:
            subprocess.Popen(
                [self.rbw_exe, "unlock"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=self._rbw_env(),
            )
        except FileNotFoundError:
            self._rbw_not_found = True
            print(f"[bitwarden] rbw executable not found: {self.rbw_exe}", flush=True)
        except Exception as exc:
            print(f"[bitwarden] Unlock error: {exc}", flush=True)

    def _sync_vault(self):
        # Sync can take a while and may need the network; keep it off the
        # DBus thread.
        thread = threading.Thread(
            target=lambda: self._run_rbw(["sync"], timeout=60),
            daemon=True,
        )
        thread.start()

    def _stop_agent(self):
        proc = self._run_rbw(["stop-agent"], timeout=10)
        if proc is not None and proc.returncode == 0:
            print("[bitwarden] rbw-agent stopped", flush=True)

    def _generate_password(self, preset: dict) -> str | None:
        """Generate with rbw. No entry name is passed, so rbw prints the
        result instead of saving it to the vault.

        Preset mapping, and its limits (rbw has fewer knobs than bw):
          passphrase preset  -> rbw generate <words> --diceware
                                (separator/capitalize/include_number ignored)
          numbers-only preset -> rbw generate <length> --only-numbers
          special = false     -> --no-symbols
          special = true      -> no flag (symbols included)
          uppercase/lowercase toggles have no rbw equivalent and are ignored.
        """
        try:
            if preset["type"] == "passphrase":
                args = ["generate", str(preset.get("words", 3)), "--diceware"]
            else:
                args = ["generate", str(preset.get("length", 16))]
                numbers_only = (
                    preset.get("numbers", True)
                    and not preset.get("uppercase", True)
                    and not preset.get("lowercase", True)
                    and not preset.get("special", False)
                )
                if numbers_only:
                    args.append("--only-numbers")
                elif not preset.get("special", False):
                    args.append("--no-symbols")
            generated = self._rbw_output(args, timeout=10)
            return generated or None
        except Exception as exc:
            print(f"[bitwarden] Generate error: {exc}", flush=True)
        return None

    @staticmethod
    def _preset_summary(preset: dict) -> str:
        if preset["type"] == "passphrase":
            # rbw diceware: separator/capitalize/include_number unsupported
            return f"{preset['words']} words, diceware"
        parts = [f"{preset['length']} chars"]
        if preset.get("uppercase"):
            parts.append("upper")
        if preset.get("lowercase"):
            parts.append("lower")
        if preset.get("numbers"):
            parts.append("numbers")
        if preset.get("special"):
            parts.append("special")
        return " · ".join(parts)

    # -- Main loop ----------------------------------------------------------------

    def run(self):
        loop = GLib.MainLoop()

        def _sigint(sig, _frame):
            print("\n[bitwarden] Shutting down...", flush=True)
            loop.quit()

        signal.signal(signal.SIGINT, _sigint)
        signal.signal(signal.SIGTERM, _sigint)

        print("[bitwarden] KRunner Bitwarden runner started (rbw backend)", flush=True)
        loop.run()


# -----------------------------------------------------------------------------

if __name__ == "__main__":
    runner = BitwardenRunner()
    runner.run()
