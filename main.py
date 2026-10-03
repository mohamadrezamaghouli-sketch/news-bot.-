#!/usr/bin/env python3
"""Telegram news bot - phase 1 (Persian RSS sources, no translation yet).

Each run: read the feeds in sources.json, find items not seen before,
post them to the Telegram channel, and remember them in seen.json.

Secrets (GitHub: Settings > Secrets and variables > Actions):
  TELEGRAM_BOT_TOKEN  - token from @BotFather, like 123456789:AAF...
  TELEGRAM_CHAT_ID    - @channelusername (public) or -100xxxxxxxxxx (private)

Usage:
  python main.py          normal run
  python main.py --test   check token + channel, send one test message, exit
"""
import hashlib
import html
import json
import os
import re
import sys
import time
from calendar import timegm
from datetime import datetime, timedelta, timezone

import feedparser
import requests


def clean_secret(value):
    """Secrets are often pasted with stray spaces, newlines or quotes: remove them."""
    return re.sub(r"\s+", "", value or "").strip("\"'`")


TOKEN = clean_secret(os.environ.get("TELEGRAM_BOT_TOKEN"))
if TOKEN.lower().startswith("bot") and ":" in TOKEN:  # copied from a URL like .../bot123:ABC
    TOKEN = TOKEN[3:]
CHAT_ID = clean_secret(os.environ.get("TELEGRAM_CHAT_ID"))

TOKEN_RE = re.compile(r"^\d{5,15}:[A-Za-z0-9_-]{30,}$")
CHAT_RE = re.compile(r"^(-100\d{6,}|@[A-Za-z][A-Za-z0-9_]{3,})$")

SOURCES_FILE = "sources.json"
SEEN_FILE = "seen.json"

MAX_AGE_HOURS = 24          # items older than this are never posted
MAX_POSTS_PER_RUN = 10      # keeps the channel from being flooded
FIRST_RUN_POSTS = 5         # on the very first run, post only the newest few
SUMMARY_CHARS = 280
TITLE_CHARS = 500
SEEN_LIMIT = 5000
PAUSE_BETWEEN_POSTS = 3     # seconds (Telegram allows ~20 msgs/min to a channel)
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; NewsBot/1.0)"}

# Titles containing these words are skipped (we want politics/economy, not sport/culture).
# Edit freely.
EXCLUDE_WORDS = ["فوتبال", "لیگ برتر", "سینما", "بازیگر", "کنسرت", "المپیک"]


# ----------------------------------------------------------------- helpers

def safe(value, limit=200):
    """Outside text made safe for the Actions log: one line, no '::' workflow commands."""
    return re.sub(r"\s+", " ", str(value)).replace("::", ": :")[:limit]


def clean_text(text):
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def shorten(text, limit):
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(" .,،؛:-") + "…"


def make_key(source_name, ident):
    """Short stable fingerprint of an item (keeps seen.json small)."""
    return hashlib.sha1(("%s|%s" % (source_name, ident)).encode("utf-8")).hexdigest()[:16]


def load_sources():
    try:
        with open(SOURCES_FILE, encoding="utf-8") as f:
            sources = json.load(f)
        if not isinstance(sources, list):
            raise ValueError("top level must be a list")
        return sources
    except (OSError, ValueError) as exc:
        sys.exit("sources.json is missing or invalid: %s" % exc)


def load_seen():
    """Returns (list_of_seen_keys, first_run)."""
    if not os.path.exists(SEEN_FILE):
        return [], True
    try:
        with open(SEEN_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data, False
    except (OSError, ValueError):
        pass
    print("WARNING: seen.json is unreadable - starting fresh.")
    return [], True


def save_seen(seen):
    tmp = SEEN_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(seen[-SEEN_LIMIT:], f, ensure_ascii=False, indent=1)
    os.replace(tmp, SEEN_FILE)


def entry_time(entry):
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return datetime.fromtimestamp(timegm(t), tz=timezone.utc)
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------- feeds

def fetch_feed(source):
    resp = requests.get(source["url"], headers=HEADERS, timeout=25)
    resp.raise_for_status()
    parsed = feedparser.parse(resp.content)
    if not parsed.entries:
        ctype = re.sub(r"[^\w/;=.+-]", "", resp.headers.get("Content-Type", ""))[:60]
        raise ValueError(
            "no feed entries found (HTTP %s, content-type %s) - the site may be "
            "blocking this server, or the URL changed" % (resp.status_code, ctype or "unknown"))
    items = []
    for e in parsed.entries:
        link = (e.get("link") or "").strip()
        title = clean_text(e.get("title"))
        if not link or not title:
            continue
        ident = e.get("id") or link
        summary = clean_text(e.get("summary") or e.get("description"))
        items.append({
            "id": ident,
            "key": make_key(source["name"], ident),
            "title": title,
            "summary": shorten(summary, SUMMARY_CHARS),
            "link": link,
            "ts": entry_time(e),
            "name": source["name"],
            "label": source.get("label", ""),
            "priority": source.get("priority", 3),
        })
    return items


def format_post(item):
    title = shorten(item["title"], TITLE_CHARS)
    parts = ["<b>%s</b>" % html.escape(title)]
    summary = item["summary"]
    # Many feeds repeat the title as the summary - skip it in that case.
    if summary and not summary.startswith(title[:30]):
        parts.append(html.escape(summary))
    source_line = item["name"]
    if item["label"]:
        source_line += " · " + item["label"]
    parts.append("📰 " + html.escape(source_line))
    parts.append('<a href="%s">لینک خبر اصلی</a>' % html.escape(item["link"], quote=True))
    return "\n\n".join(parts)


# ---------------------------------------------------------------- telegram

def tg(method, payload=None):
    """Calls the Bot API. Returns (http_status_or_None, json_dict)."""
    url = "https://api.telegram.org/bot%s/%s" % (TOKEN, method)
    try:
        r = requests.post(url, json=payload or {}, timeout=25)
    except requests.RequestException as exc:
        # Never print the exception text: it contains the URL (and the token).
        print("Telegram request failed: %s" % type(exc).__name__)
        return None, {}
    try:
        data = r.json()
        if not isinstance(data, dict):
            data = {}
    except ValueError:
        data = {}
    return r.status_code, data


def error_text(status, data):
    return "%s %s" % (status, safe(data.get("description", "")))


def send_message(text):
    """Returns 'ok', 'skip' (this one message is bad), 'fatal' (token/channel
    problem) or 'retry' (temporary problem - try again next run)."""
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
    }
    for _ in range(3):
        status, data = tg("sendMessage", payload)
        if status == 200:
            return "ok"
        if status is None:
            return "retry"
        if status == 429:
            params = data.get("parameters")
            wait = params.get("retry_after", 5) if isinstance(params, dict) else 5
            time.sleep(min(int(wait), 60) + 1)
            continue
        print("Telegram error %s" % error_text(status, data))
        if status in (401, 403, 404):
            return "fatal"
        desc = str(data.get("description", "")).lower()
        if status == 400 and ("chat not found" in desc or "chat_id" in desc or "peer_id" in desc):
            return "fatal"
        if status == 400:
            return "skip"
        return "retry"
    return "retry"


def config_problems():
    """Offline sanity check of the two secrets (nothing secret is printed)."""
    problems = []
    if not TOKEN:
        problems.append("secret TELEGRAM_BOT_TOKEN is empty or missing (check its exact name).")
    elif not TOKEN_RE.match(TOKEN):
        hint = ""
        if re.fullmatch(r"-?\d+|@\w+", TOKEN):
            hint = " It looks like a chat id - the two secrets may be swapped."
        problems.append("secret TELEGRAM_BOT_TOKEN has a wrong format (length %d; expected "
                        "like 123456789:AAF...).%s" % (len(TOKEN), hint))
    if not CHAT_ID:
        problems.append("secret TELEGRAM_CHAT_ID is empty or missing (check its exact name).")
    elif not CHAT_RE.match(CHAT_ID):
        hint = ""
        if TOKEN_RE.match(CHAT_ID):
            hint = " It looks like a bot token - the two secrets may be swapped."
        elif re.fullmatch(r"-?\d+", CHAT_ID):
            hint = " A channel id must start with -100."
        problems.append("secret TELEGRAM_CHAT_ID has a wrong format (length %d; a private channel "
                        "looks like -1001234567890, a public one like @name).%s" % (len(CHAT_ID), hint))
    return problems


def self_check():
    """Verifies token, channel and admin rights; prints a plain diagnosis."""
    status, data = tg("getMe")
    if status is None:
        print("CHECK: cannot reach api.telegram.org from this server.")
        return False
    if status == 401:
        print("CHECK token: REJECTED (401). The value in secret TELEGRAM_BOT_TOKEN is wrong or was "
              "revoked. Copy it again from @BotFather (/mybots > your bot > API Token) and update the secret.")
        return False
    if status != 200 or not data.get("ok"):
        print("CHECK token: unexpected answer: %s" % error_text(status, data))
        return False
    bot = data.get("result") or {}
    print("CHECK token: OK (bot @%s)" % safe(bot.get("username", "?")))

    status, data = tg("getChat", {"chat_id": CHAT_ID})
    if status != 200 or not data.get("ok"):
        print("CHECK channel: FAILED (%s). Wrong TELEGRAM_CHAT_ID, or the bot was never added "
              "to the channel as an administrator." % error_text(status, data))
        return False
    print("CHECK channel: OK (type=%s)" % safe((data.get("result") or {}).get("type", "?")))

    status, data = tg("getChatMember", {"chat_id": CHAT_ID, "user_id": bot.get("id")})
    if status == 200 and data.get("ok"):
        member = data.get("result") or {}
        if member.get("status") not in ("administrator", "creator"):
            print("CHECK admin: the bot is NOT an administrator of the channel (status=%s)."
                  % safe(member.get("status", "?")))
            return False
        if member.get("can_post_messages") is False:
            print("CHECK admin: the bot is an admin but lacks the 'Post messages' permission.")
            return False
        print("CHECK admin: OK")
    else:
        print("CHECK admin: could not verify (%s)" % error_text(status, data))
    return True


# -------------------------------------------------------------------- main

def main():
    problems = config_problems()
    if problems:
        for p in problems:
            print("CONFIG PROBLEM: " + p)
        sys.exit(1)

    if "--test" in sys.argv:
        if not self_check():
            sys.exit(1)
        result = send_message("✅ ربات خبر فعال است و به کانال وصل شد.")
        print("Test message: %s" % result)
        sys.exit(0 if result == "ok" else 1)

    sources = load_sources()
    seen, first_run = load_seen()
    seen_set = set(seen)

    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)

    all_items, failed, active = [], [], 0
    for source in sources:
        if not source.get("enabled", True):
            continue
        active += 1
        try:
            items = fetch_feed(source)
            all_items.extend(items)
            print("OK   %s: %d items" % (source["name"], len(items)))
        except Exception as exc:  # keep going if one feed breaks
            failed.append(source["name"])
            print("FAIL %s: %s" % (source["name"], safe(exc)))

    if active == 0:
        sys.exit("No enabled sources in sources.json.")
    if len(failed) == active:
        print("All %d feeds failed - nothing processed, state not saved." % active)
        sys.exit(1)  # red X in GitHub (and an e-mail to you)

    # One entry per item; seen.json from older versions stored the raw id, so accept both.
    unique = {}
    for i in all_items:
        unique.setdefault(i["key"], i)
    new_items = [i for i in unique.values()
                 if i["key"] not in seen_set and i["id"] not in seen_set]

    # Old or filtered-out items are marked as seen without posting them.
    fresh = []
    for i in new_items:
        if i["ts"] < cutoff or any(w in i["title"] for w in EXCLUDE_WORDS):
            seen.append(i["key"])
        else:
            fresh.append(i)

    if first_run:
        fresh.sort(key=lambda i: i["ts"], reverse=True)
        to_post = list(reversed(fresh[:FIRST_RUN_POSTS]))
        # Everything not posted now counts as old news; posted items are added
        # to "seen" only after Telegram accepts them (see below).
        posting = {i["key"] for i in to_post}
        seen.extend(i["key"] for i in fresh if i["key"] not in posting)
    else:
        fresh.sort(key=lambda i: (i["priority"], i["ts"]))
        to_post = fresh[:MAX_POSTS_PER_RUN]

    posted = skipped = 0
    stop_reason = None
    for item in to_post:
        result = send_message(format_post(item))
        if result == "ok":
            posted += 1
            seen.append(item["key"])
            time.sleep(PAUSE_BETWEEN_POSTS)
        elif result == "skip":
            skipped += 1
            seen.append(item["key"])  # a message Telegram refuses must not block the queue
            print("Skipped one item from %s: Telegram rejected the message." % item["name"])
        else:
            stop_reason = result
            print("Stopping this run (%s)." % result)
            break

    # Nothing reached Telegram: do NOT save state, or these items would be lost.
    if stop_reason and posted == 0 and skipped == 0:
        print("Nothing could be posted - state not saved, the items will be retried.")
        if stop_reason == "fatal":
            self_check()  # explains what is wrong
        sys.exit(1)

    save_seen(seen)
    print("Done. posted=%d, skipped=%d, new=%d, failed feeds=%s"
          % (posted, skipped, len(new_items), failed or "none"))
    if stop_reason == "fatal":
        sys.exit(1)


if __name__ == "__main__":
    main()
