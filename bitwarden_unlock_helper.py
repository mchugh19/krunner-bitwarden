#!/usr/bin/env python3
"""
Interactive terminal helper: prompts for the Bitwarden master password and
POSTs it to the local bw serve API to unlock the vault.

Usage:
    python3 bitwarden_unlock_helper.py [API_URL]

The API_URL defaults to http://localhost:8087.
"""

import getpass
import json
import sys
import urllib.request
import urllib.error

API_URL = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://localhost:8087"

print("=" * 50)
print("  Bitwarden Vault Unlock")
print("=" * 50)

try:
    password = getpass.getpass("Master password: ")
except (KeyboardInterrupt, EOFError):
    print("\nCancelled.")
    sys.exit(0)

if not password:
    print("No password entered. Exiting.")
    sys.exit(0)

payload = json.dumps({"password": password}).encode()
req = urllib.request.Request(f"{API_URL}/unlock", data=payload, method="POST")
req.add_header("Content-Type", "application/json")

try:
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read().decode())
    if result.get("success"):
        print("\nVault unlocked successfully.")
    else:
        print(f"\nFailed to unlock vault: {result.get('message', result)}")
except urllib.error.HTTPError as exc:
    body = exc.read().decode()
    print(f"\nHTTP error {exc.code}: {body}")
except Exception as exc:
    print(f"\nError: {exc}")

input("\nPress Enter to close this window…")
