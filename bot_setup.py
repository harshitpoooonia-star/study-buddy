#!/usr/bin/env python3
"""
Interactive setup for the Study Buddy Telegram bot (run on the server via set-bot-secrets).

Asks for the Telegram bot token and (optional) Groq API key with hidden input, checks both,
pairs your Telegram account as the owner, and writes them to .env (chmod 600).
Nothing is echoed, logged, or passed on a command line.
"""

from __future__ import annotations

import getpass
import os
import sys
import time
from pathlib import Path

import httpx

ENV = Path(__file__).resolve().with_name(".env")


def read_env() -> dict[str, str]:
    out: dict[str, str] = {}
    if ENV.exists():
        for line in ENV.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def write_env(updates: dict[str, str]) -> None:
    lines = ENV.read_text().splitlines() if ENV.exists() else []
    lines = [l for l in lines if l.split("=", 1)[0].strip() not in updates]
    lines += [f"{k}={v}" for k, v in updates.items()]
    tmp = ENV.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(ENV)


def tg(token: str, method: str, **params: object) -> dict:
    r = httpx.post(f"https://api.telegram.org/bot{token}/{method}", json=params, timeout=40)
    d = r.json()
    if not d.get("ok"):
        raise RuntimeError(d.get("description", "Telegram error"))
    return d["result"]


def ask_secret(prompt: str) -> str:
    v = getpass.getpass(prompt).strip().strip('"').strip("'")
    return v


def main() -> int:
    env = read_env()
    updates: dict[str, str] = {}

    # ---------------------------------------------------------------- Telegram token
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    print("\n[1/3] Telegram bot token (from @BotFather).")
    new = ask_secret("Paste it (hidden)" + (" - or press Enter to keep the current one" if token else "") + ": ")
    if new:
        token = new
    if not token:
        print("No token given - nothing changed.")
        return 1
    try:
        me = tg(token, "getMe")
    except Exception as e:  # noqa: BLE001
        print(f"Telegram rejected that token ({e}). Copy it again from @BotFather and re-run.")
        return 2
    username = me.get("username", "?")
    print(f"   OK - bot is @{username}")
    updates["TELEGRAM_BOT_TOKEN"] = token

    # ---------------------------------------------------------------- Groq key (optional)
    groq = env.get("GROQ_API_KEY", "")
    print("\n[2/3] Groq API key for plain-English questions (free: console.groq.com/keys).")
    new = ask_secret("Paste it (hidden), or press Enter to " + ("keep the current one" if groq else "skip") + ": ")
    if new:
        try:
            r = httpx.get("https://api.groq.com/openai/v1/models",
                          headers={"Authorization": f"Bearer {new}"}, timeout=30)
            if r.status_code == 401:
                print("   Groq rejected that key - skipping it (bot will use keyword answers).")
                new = ""
            else:
                print("   OK - Groq key works.")
        except httpx.HTTPError:
            print("   Couldn't reach Groq to check the key; saving it anyway.")
        if new:
            updates["GROQ_API_KEY"] = new
    elif not groq:
        print("   Skipped - the bot will answer free text with keyword matching.")

    # ---------------------------------------------------------------- pair the owner
    owner = env.get("OWNER_CHAT_ID", "")
    print("\n[3/3] Link your Telegram account.")
    redo = "y"
    if owner:
        redo = input(f"   Already linked to chat {owner}. Link a different account? [y/N]: ").strip().lower()
    if redo.startswith("y"):
        try:
            tg(token, "deleteWebhook")
            ups = tg(token, "getUpdates", timeout=0)
            offset = (ups[-1]["update_id"] + 1) if ups else 0  # ignore anything older than now
        except Exception as e:  # noqa: BLE001
            print(f"   Telegram error: {e}")
            return 2
        print(f"   Open Telegram, search @{username}, press Start (or send /start).")
        print("   Waiting up to 3 minutes…")
        found = None
        deadline = time.time() + 180
        while time.time() < deadline and not found:
            try:
                ups = tg(token, "getUpdates", offset=offset, timeout=25)
            except Exception:  # noqa: BLE001
                time.sleep(3)
                continue
            for u in ups:
                offset = u["update_id"] + 1
                msg = u.get("message") or {}
                chat = msg.get("chat") or {}
                if chat.get("type") == "private":
                    found = chat
        if not found:
            print("   Didn't see a message. Re-run set-bot-secrets and press Start in the bot.")
            return 3
        tg(token, "getUpdates", offset=offset, timeout=0)  # acknowledge
        name = " ".join(x for x in (found.get("first_name"), found.get("last_name")) if x)
        handle = f" (@{found['username']})" if found.get("username") else ""
        ok = input(f"   Got a message from {name}{handle}. Is that you? [Y/n]: ").strip().lower()
        if ok.startswith("n"):
            print("   Not linked. Re-run when ready.")
            return 3
        updates["OWNER_CHAT_ID"] = str(found["id"])
        tg(token, "sendMessage", chat_id=found["id"],
           text="Linked ✅ - Study Buddy will start in a few seconds.")

    write_env(updates)
    print("\nSaved to .env (only readable by you).")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nCancelled - nothing changed.")
        sys.exit(1)
