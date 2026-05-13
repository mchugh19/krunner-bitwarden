#!/usr/bin/env python3
"""KRunner plugin for Bitwarden — searches the bw serve API and copies credentials."""

import configparser
import json
import os
import signal
import subprocess
import time
import threading
import urllib.error
import urllib.parse
import urllib.request

import dbus
import dbus.service
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

# ── DBus identifiers ─────────────────────────────────────────────────────────
BUS_NAME = "org.kde.krunner.bitwarden"
OBJ_PATH = "/runner"
IFACE = "org.kde.krunner1"

# ── Match types (mirrors Plasma::QueryType) ───────────────────────────────────
# categoryRelevance values — mirror KRunner::QueryMatch::CategoryRelevance enum
CATEGORY_LOWEST = 0
CATEGORY_LOW = 30
CATEGORY_MODERATE = 50
CATEGORY_HIGH = 70
CATEGORY_HIGHEST = 100

# ── Action IDs ────────────────────────────────────────────────────────────────
ACTION_COPY_PASSWORD = ""          # primary / default
ACTION_COPY_USERNAME = "copy_username"
ACTION_COPY_TOTP = "copy_totp"
ACTION_OPEN_URL = "open_url"

# ── Config defaults ───────────────────────────────────────────────────────────
CONFIG_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
    "krunner-bitwarden",
)
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.ini")

DEFAULTS = {
    "bitwarden": {
        "credential_retrieval": "dynamic",
        "bitwarden_api": "http://localhost:8087",
        "bw_executable_path": "bw",
        "auto_start_server": "true",
        "unlock_method": "prompt",
        "unlock_password_file": "",
        "unlock_password_env": "BW_MASTER_PASS",
    }
}

ICON = "password-vault"


# ─────────────────────────────────────────────────────────────────────────────

class BitwardenRunner(dbus.service.Object):

    def __init__(self):
        DBusGMainLoop(set_as_default=True)
        session_bus = dbus.SessionBus()
        session_bus.request_name(BUS_NAME, dbus.bus.NAME_FLAG_REPLACE_EXISTING)
        bus_name = dbus.service.BusName(BUS_NAME, bus=session_bus)
        dbus.service.Object.__init__(self, bus_name, OBJ_PATH)

        # runtime state
        self._server_process = None
        self._generator_presets = []
        self._match_cache = {}  # item_id -> payload dict, populated by last Match()

        self._load_config()
        self._load_generator_presets()

        if self.auto_start_server:
            if not self._is_server_running():
                self._start_server()
            if self.unlock_method not in ("none", "prompt"):
                self._auto_unlock()

    # ── Config ────────────────────────────────────────────────────────────────

    def _load_config(self):
        cfg = configparser.ConfigParser()

        # write defaults if file absent
        if not os.path.exists(CONFIG_FILE):
            os.makedirs(CONFIG_DIR, exist_ok=True)
            cfg.read_dict(DEFAULTS)
            # write generator presets
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

        self.credential_retrieval = bw.get("credential_retrieval", "dynamic")
        self.api_url = bw.get("bitwarden_api", "http://localhost:8087").rstrip("/")
        self.bw_exe = bw.get("bw_executable_path", "bw") or "bw"
        self.auto_start_server = bw.get("auto_start_server", "true").lower() == "true"
        self.unlock_method = bw.get("unlock_method", "prompt")
        self.unlock_password_file = bw.get("unlock_password_file", "")
        self.unlock_password_env = bw.get("unlock_password_env", "BW_MASTER_PASS")

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

    # ── KRunner DBus interface ────────────────────────────────────────────────

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

        server_running = self._is_server_running()
        vault_locked = self._is_vault_locked() if server_running else False

        # ── Management commands ───────────────────────────────────────────────
        q_lower = query.lower()

        # Status
        if "bitwarden" in q_lower or "bw status" in q_lower or query == "":
            if server_running:
                vault_status = "LOCKED" if vault_locked else "UNLOCKED"
                desc = f"Server running — vault {vault_status}"
            else:
                desc = "Server not running"
            matches.append((
                "cmd:status", f"Bitwarden Status: {desc}", ICON,
                CATEGORY_LOWEST, 0.5, {"subtext": desc, "category": "Bitwarden"}
            ))

        # Lock
        if server_running and not vault_locked and (
                "lock" in q_lower or "bitwarden" in q_lower):
            matches.append((
                "cmd:lock", "Lock Bitwarden", ICON,
                CATEGORY_MODERATE, 0.6, {"subtext": "Lock the vault immediately", "category": "Bitwarden"}
            ))

        # Unlock
        if server_running and vault_locked and (
                "unlock" in q_lower or "bitwarden" in q_lower or "password" in q_lower):
            matches.append((
                "cmd:unlock_prompt", "Unlock Bitwarden", ICON,
                CATEGORY_HIGH, 0.9,
                {"subtext": "Press Enter to open unlock dialog", "category": "Bitwarden"}
            ))

        # Start / Stop server
        if not server_running and (
                "start" in q_lower or "bitwarden" in q_lower or "server" in q_lower):
            matches.append((
                "cmd:start_server", "Start Bitwarden Server", ICON,
                CATEGORY_MODERATE, 0.6, {"subtext": "Start bw serve API server", "category": "Bitwarden"}
            ))
        if server_running and (
                "stop" in q_lower or "bitwarden" in q_lower or "server" in q_lower):
            matches.append((
                "cmd:stop_server", "Stop Bitwarden Server", ICON,
                CATEGORY_LOWEST, 0.4, {"subtext": "Stop bw serve API server", "category": "Bitwarden"}
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

        # ── Credential search ─────────────────────────────────────────────────
        if len(query) >= 3 and server_running and not vault_locked:
            tokens = query.split()
            api_term = tokens[0]          # search API with first token
            filter_terms = tokens[1:]     # filter results client-side with the rest
            matches.extend(self._search_credentials(api_term, filter_terms))

        return matches

    @dbus.service.method(IFACE, in_signature="ss")
    def Run(self, match_id: str, action_id: str):
        if match_id.startswith("cmd:"):
            self._run_command(match_id[4:], action_id)
            return

        # credential result — match_id is a Bitwarden item UUID
        item = self._match_cache.get(match_id)
        if not item:
            # fallback: fetch directly from API
            try:
                url = f"{self.api_url}/object/item/{match_id}"
                with urllib.request.urlopen(url, timeout=10) as resp:
                    data = json.loads(resp.read().decode())
                item = data.get("data", {})
            except Exception as exc:
                print(f"[bitwarden] Run fetch error: {exc}", flush=True)
                return

        login = item.get("login", {})

        if action_id == ACTION_COPY_USERNAME:
            self._copy(login.get("username", ""))
        elif action_id == ACTION_COPY_TOTP:
            self._fetch_and_copy_totp(item.get("id", match_id))
        elif action_id == ACTION_OPEN_URL:
            uris = login.get("uris", [])
            if uris:
                url = uris[0].get("uri", "")
                if url:
                    subprocess.Popen(["xdg-open", url])
        else:
            # default: copy password
            self._copy(login.get("password", ""))

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _run_command(self, cmd: str, _action_id: str):
        if cmd == "status":
            pass  # informational only
        elif cmd == "lock":
            self._lock_vault()
        elif cmd == "unlock_prompt":
            # Launch a small terminal-based unlock helper in background
            self._launch_unlock_dialog()
        elif cmd == "start_server":
            self._start_server()
        elif cmd == "stop_server":
            self._stop_server()
        elif cmd.startswith("generate:"):
            preset_json = cmd[len("generate:"):]
            try:
                preset = json.loads(preset_json)
                generated = self._generate_password(preset)
                if generated:
                    self._copy(generated)
            except Exception as exc:
                print(f"[bitwarden] Generate error: {exc}", flush=True)

    def _search_credentials(self, query: str, filter_terms: list = None):
        matches = []
        sanitized = urllib.parse.quote(query)
        try:
            url = f"{self.api_url}/list/object/items?search={sanitized}"
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            items = data.get("data", {}).get("data", [])
            cred_items = [it for it in items if it.get("type") == 1]
            # Client-side filter: all extra tokens must match name or username
            if filter_terms:
                def matches_all(item):
                    haystack = (
                        (item.get("name") or "") + " " +
                        (item.get("login", {}).get("username") or "")
                    ).lower()
                    return all(t.lower() in haystack for t in filter_terms)
                cred_items = [it for it in cred_items if matches_all(it)]
            for i, item in enumerate(cred_items):
                login = item.get("login", {})
                username = login.get("username") or ""
                item_id = item.get("id", "")
                # Unique relevance per position so KRunner shows all entries
                relevance = max(0.5, 0.95 - i * 0.02)
                # Cache payload so Run() can act without re-fetching
                parsed_payload = {
                    "id": item_id,
                    "name": item.get("name", ""),
                    "login": {
                        "username": login.get("username", ""),
                        "password": login.get("password", ""),
                        "uris": login.get("uris", []),
                    }
                }
                self._match_cache[item_id] = parsed_payload
                matches.append((
                    item_id,
                    item.get("name", ""),
                    ICON,
                    CATEGORY_HIGHEST,
                    relevance,
                    {"subtext": username if username else "no username", "category": "Bitwarden"}
                ))
        except urllib.error.HTTPError as exc:
            if exc.code not in (400, 401, 403):
                print(f"[bitwarden] Search HTTP error: {exc}", flush=True)
        except Exception as exc:
            print(f"[bitwarden] Search error: {exc}", flush=True)
        return matches

    def _fetch_and_copy_totp(self, item_id: str):
        try:
            url = f"{self.api_url}/object/totp/{item_id}"
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            totp = data.get("data", {}).get("data", "")
            if totp:
                self._copy(totp)
        except Exception as exc:
            print(f"[bitwarden] TOTP error: {exc}", flush=True)

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

    def _is_server_running(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.api_url}/status", timeout=3):
                return True
        except Exception:
            return False

    def _get_vault_status(self) -> str:
        try:
            with urllib.request.urlopen(f"{self.api_url}/status", timeout=3) as resp:
                data = json.loads(resp.read().decode())
            status = data.get("data", {}).get("template", {}).get("status", "").lower()
            if status in ("locked", "unlocked"):
                return status
        except Exception:
            pass
        return "unknown"

    def _is_vault_locked(self) -> bool:
        status = self._get_vault_status()
        if status == "locked":
            return True
        if status == "unlocked":
            return False
        try:
            with urllib.request.urlopen(f"{self.api_url}/list/object/items", timeout=3):
                return False
        except urllib.error.HTTPError as exc:
            return exc.code in (400, 401, 403)
        except Exception:
            return True

    def _lock_vault(self):
        try:
            req = urllib.request.Request(f"{self.api_url}/lock",
                                         data=b"", method="POST")
            with urllib.request.urlopen(req, timeout=10):
                pass
            print("[bitwarden] Vault locked", flush=True)
        except Exception as exc:
            print(f"[bitwarden] Lock error: {exc}", flush=True)

    def _unlock_vault(self, password: str):
        try:
            payload = json.dumps({"password": password}).encode()
            req = urllib.request.Request(f"{self.api_url}/unlock",
                                         data=payload, method="POST")
            req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode())
            if result.get("success"):
                time.sleep(0.5)
                self._sync_vault()
                print("[bitwarden] Vault unlocked successfully", flush=True)
            else:
                print(f"[bitwarden] Unlock failed: {result}", flush=True)
        except Exception as exc:
            print(f"[bitwarden] Unlock error: {exc}", flush=True)

    def _sync_vault(self):
        try:
            req = urllib.request.Request(f"{self.api_url}/sync",
                                         data=b"", method="POST")
            with urllib.request.urlopen(req, timeout=30):
                pass
        except Exception:
            pass

    def _auto_unlock(self):
        password = None
        if self.unlock_method == "password_file" and self.unlock_password_file:
            try:
                with open(self.unlock_password_file) as f:
                    password = f.read().strip()
            except Exception as exc:
                print(f"[bitwarden] Cannot read password file: {exc}", flush=True)
                return
        elif self.unlock_method == "password_env":
            password = os.environ.get(self.unlock_password_env)
            if not password:
                print(f"[bitwarden] Env var {self.unlock_password_env} not set", flush=True)
                return
        if password:
            self._unlock_vault(password)

    def _launch_unlock_dialog(self):
        """
        Open a small terminal window so the user can type their master password.
        The helper script reads stdin and POSTs it to the bw serve API.
        """
        helper = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "bitwarden_unlock_helper.py")
        if not os.path.exists(helper):
            print("[bitwarden] Unlock helper not found", flush=True)
            return
        for term in (
            ["konsole", "--hide-menubar", "--hide-tabbar", "-e"],
            ["xterm", "-e"],
            ["gnome-terminal", "--", "bash", "-c"],
        ):
            try:
                subprocess.Popen(term + [f"python3 {helper} {self.api_url}"],
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
                return
            except FileNotFoundError:
                continue
        print("[bitwarden] No terminal emulator found for unlock dialog", flush=True)

    def _start_server(self):
        try:
            self._server_process = subprocess.Popen(
                [self.bw_exe, "serve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(2)
            if self._is_server_running():
                print("[bitwarden] Server started", flush=True)
            else:
                print("[bitwarden] Server may not have started", flush=True)
        except Exception as exc:
            print(f"[bitwarden] Failed to start server: {exc}", flush=True)

    def _stop_server(self):
        if self._server_process and self._server_process.poll() is None:
            self._server_process.terminate()
            self._server_process = None
            print("[bitwarden] Server stopped", flush=True)
            return
        try:
            subprocess.run(["pkill", "-f", "bw serve"],
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
            print("[bitwarden] Server stopped via pkill", flush=True)
        except Exception as exc:
            print(f"[bitwarden] Failed to stop server: {exc}", flush=True)

    def _generate_password(self, preset: dict) -> str | None:
        try:
            params: dict = {}
            if preset["type"] == "passphrase":
                params["passphrase"] = "true"
                params["words"] = str(preset.get("words", 3))
                params["separator"] = preset.get("separator", "-")
                if preset.get("capitalize"):
                    params["capitalize"] = "true"
                if preset.get("include_number"):
                    params["includeNumber"] = "true"
            else:
                params["length"] = str(preset.get("length", 16))
                if preset.get("uppercase", True):
                    params["uppercase"] = "true"
                if preset.get("lowercase", True):
                    params["lowercase"] = "true"
                if preset.get("numbers", True):
                    params["number"] = "true"
                if preset.get("special", False):
                    params["special"] = "true"
            query_string = urllib.parse.urlencode(params)
            url = f"{self.api_url}/generate?{query_string}"
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            if data.get("success"):
                return data.get("data", {}).get("data", "")
            print(f"[bitwarden] Generate failed: {data}", flush=True)
        except Exception as exc:
            print(f"[bitwarden] Generate error: {exc}", flush=True)
        return None

    @staticmethod
    def _preset_summary(preset: dict) -> str:
        if preset["type"] == "passphrase":
            parts = [f"{preset['words']} words", f"sep '{preset['separator']}'"]
            if preset.get("capitalize"):
                parts.append("capitalized")
            if preset.get("include_number"):
                parts.append("with number")
        else:
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

    # ── Main loop ─────────────────────────────────────────────────────────────

    def run(self):
        loop = GLib.MainLoop()

        def _sigint(sig, _frame):
            print("\n[bitwarden] Shutting down…", flush=True)
            loop.quit()

        signal.signal(signal.SIGINT, _sigint)
        signal.signal(signal.SIGTERM, _sigint)

        print("[bitwarden] KRunner Bitwarden runner started", flush=True)
        loop.run()


# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    runner = BitwardenRunner()
    runner.run()
