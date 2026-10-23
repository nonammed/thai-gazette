#!/usr/bin/env python3
"""
Публикация поста в Telegram-канал @ratchakitcha через бота @socppostingbot.

Токен/чат берутся из окружения (SOCPOST_BOT_TOKEN, SOCPOST_CHAT_ID) —
это путь для GitHub Actions (repo secrets). Если переменных нет, читаем
env-файл $SOCPOST_ENV_FILE (по умолчанию ~/.hermes/scripts/gazette_post.env).

Usage:
    gazette_post.py <file>          # пост из файла
    cat text.txt | gazette_post.py  # пост из stdin
    gazette_post.py --dry-run <file>
Печатает DONE при успехе.
"""
import os
import sys
import json
import urllib.request
import urllib.parse

ENV_FILE = os.path.expanduser(os.environ.get("SOCPOST_ENV_FILE", "~/.hermes/scripts/gazette_post.env"))
MAX_LEN = 4000


def load_env():
    d = {}
    if not os.path.exists(ENV_FILE):
        return d
    with open(ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            d[k.strip()] = v.strip()
    return d


def credentials():
    file_env = load_env()
    token = os.environ.get("SOCPOST_BOT_TOKEN") or file_env.get("SOCPOST_BOT_TOKEN")
    chat_id = os.environ.get("SOCPOST_CHAT_ID") or file_env.get("SOCPOST_CHAT_ID")
    return token, chat_id


def send(token, chat_id, text):
    url = "https://api.telegram.org/bot%s/sendMessage" % token
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": "true",
    }).encode()
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def split_chunks(text):
    chunks = []
    while len(text) > MAX_LEN:
        cut = text.rfind("\n", 0, MAX_LEN)
        if cut == -1:
            cut = MAX_LEN
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip("\n")
    chunks.append(text)
    return chunks


def main():
    args = [a for a in sys.argv[1:]]
    dry_run = "--dry-run" in args
    args = [a for a in args if a != "--dry-run"]

    if args:
        with open(args[0], encoding="utf-8") as f:
            text = f.read()
    else:
        text = sys.stdin.read()

    text = text.strip()
    if not text:
        print("ERROR: empty message")
        sys.exit(1)

    chunks = split_chunks(text)

    if dry_run:
        for i, c in enumerate(chunks, 1):
            print(f"--- chunk {i}/{len(chunks)} ({len(c)} chars) ---")
            print(c)
        print("DRY_RUN_OK")
        return

    token, chat_id = credentials()
    if not token or not chat_id:
        print("ERROR: SOCPOST_BOT_TOKEN / SOCPOST_CHAT_ID не заданы (env или %s)" % ENV_FILE)
        sys.exit(1)

    for i, chunk in enumerate(chunks, 1):
        resp = send(token, chat_id, chunk)
        if not resp.get("ok"):
            print("ERROR chunk %d: %s" % (i, resp))
            sys.exit(1)
        print("SENT chunk %d/%d msg_id=%s" % (i, len(chunks), resp["result"]["message_id"]))

    print("DONE")


if __name__ == "__main__":
    main()
