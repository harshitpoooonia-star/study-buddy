#!/usr/bin/env python3
"""
One-time helper: get a personal Moodle web-service token and save it to .env.

Two ways:
  1) Log in with your LMS username + password (typed here, hidden, sent ONLY to the LMS).
  2) Paste a key you copied from LMS -> Profile -> Preferences -> Security keys
     (use this if you sign in with Microsoft / have no LMS password).

The token is checked against the LMS before saving. Treat it like a password.
Revoke any time: LMS -> Preferences -> Security keys -> Reset.
"""

from __future__ import annotations

import getpass
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_URL = "https://lms.bennett.edu.in"
ENV_PATH = Path(__file__).with_name(".env")
# Optional: save into a profile instead, e.g.  get_token.py alice  ->  profiles/alice/.env
if len(sys.argv) > 1 and sys.argv[1].strip():
    ENV_PATH = Path(__file__).with_name("profiles") / sys.argv[1].strip() / ".env"


def post(url: str, fields: dict) -> dict:
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def token_from_password(base: str) -> str | None:
    username = input("LMS username (your enrolment no., e.g. S24CSEU0000): ").strip()
    password = getpass.getpass("LMS password (hidden, nothing shows while typing): ")
    if not username or not password:
        print("Username and password are required.")
        return None
    try:
        result = post(f"{base}/login/token.php",
                      {"username": username, "password": password, "service": "moodle_mobile_app"})
    except Exception as exc:
        print(f"Could not reach {base}: {exc}")
        return None
    finally:
        password = ""
    if "token" in result:
        return result["token"]
    print(f"\nThe LMS refused: {result.get('error') or result.get('errorcode') or result}")
    print("If you log in to the LMS with your Microsoft/Outlook account, use option 2 instead.")
    return None


def token_from_paste() -> str | None:
    print(
        "\nHow to find your key:\n"
        "  a) Install the 'Moodle' app on your phone, enter https://lms.bennett.edu.in and sign in once.\n"
        "  b) On your PC open the LMS in a browser -> click your profile picture -> Preferences\n"
        "     -> 'Security keys'.\n"
        "  c) Copy the key next to 'Moodle mobile web service'.\n"
    )
    key = input("Paste the key here and press Enter: ").strip()
    return key or None


def verify(base: str, token: str) -> dict | None:
    try:
        info = post(f"{base}/webservice/rest/server.php",
                    {"wstoken": token, "wsfunction": "core_webservice_get_site_info",
                     "moodlewsrestformat": "json"})
    except Exception as exc:
        print(f"Could not verify the token: {exc}")
        return None
    if "exception" in info:
        print(f"Token check failed: {info.get('message')} ({info.get('errorcode')})")
        if info.get("errorcode") in ("servicenotavailable", "enablewsdescription"):
            print("Web services are switched off on this LMS. Ask Bennett IT to enable 'Mobile web services'.")
        return None
    return info


def main() -> int:
    base = (os.environ.get("MOODLE_URL") or DEFAULT_URL).rstrip("/")
    print(f"LMS: {base}\n")
    print("How do you log in to the LMS?")
    print("  1) Username + password")
    print("  2) Microsoft / Outlook sign-in (I'll paste a key)")
    choice = input("Enter 1 or 2: ").strip()

    token = token_from_password(base) if choice == "1" else token_from_paste()
    if not token:
        return 1

    info = verify(base, token)
    if not info:
        return 2
    print(f"\n✅ Working! Logged in as {info.get('fullname')} on {info.get('sitename')}.")

    lines = [l for l in (ENV_PATH.read_text().splitlines() if ENV_PATH.exists() else [])
             if not l.startswith(("MOODLE_TOKEN=", "MOODLE_URL="))]
    lines += [f"MOODLE_URL={base}", f"MOODLE_TOKEN={token}"]
    ENV_PATH.write_text("\n".join(lines) + "\n")
    try:
        os.chmod(ENV_PATH, 0o600)
    except OSError:
        pass
    print(f"Saved to {ENV_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
