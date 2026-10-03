#!/usr/bin/env python3
"""Telegram news bot - phase 1 (Persian RSS sources, no translation yet).

Each run: read the feeds in sources.json, find items not seen before,
post them to the Telegram channel, and remember them in seen.json.

Secrets (set in GitHub: Settings > Secrets and variables > Actions):
  TELEGRAM_BOT_TOKEN  - token from @BotFather
  TELEGRAM_CHAT_ID    - @channelusername (public) or -100xxxxxxxxxx (private)

Usage:
  python main.py          normal run
  python main.py --test   send a single test message and exit
"""
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

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

SOURCES_FILE = "sources.json"
SEEN_FILE = "seen.json"

MAX_AGE_HOURS = 24          # items older than this are never posted
MAX_POSTS_PER_RUN = 10      # keeps the channel from being flooded
FIRST_RUN_POSTS = 5         # on the very first run, post only the newest few
SUMMARY_CHARS = 280
SEEN_LIMIT = 5000
PAUSE_BETWEEN_POSTS = 3     # seconds (Telegram allows ~20 msgs/min to a channel)
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; NewsBot/1.0)"}

# Titles containing these words are skipped (we want politics/economy, not sport/culture).
# Edit freely.
EXCLUDE_WORDS = ["فوتبال", "لیگ برتر", "سینما", "بازیگر", "کنسرت", "المپیک"]


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def clean_text(text):
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def shorten(text, limit):
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(" .,،؛:-") + "…"


def entry_time(entry):
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return datetime.fromtimestamp(timegm(t), tz=timezone.utc)
    return datetime.now(timezone.utc)


def fetch_feed(source):
    resp = requests.get(source["url"], headers=HEADERS, timeout=25)
    resp.raise_for_status()
    parsed = feedparser.parse(resp.content)
    if not parsed.entries:
        raise ValueError("no entries found in feed")
    items = []
    for e in parsed.entries:
        link = (e.get("link") or "").strip()
        title = clean_text(e.get("title"))
        if not link or not title:
            continue
        summary = clean_text(e.get("summary") or e.get("description"))
        items.append({
            "id": e.get("id") or link,
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
    title = item["title"]
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


def send_message(text):
    url = "https://api.telegram.org/bot%s/sendMessage" % TOKEN
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    for _ in range(3):
        try:
            r = requests.post(url, json=payload, timeout=25)
        except requests.RequestException as exc:
            # Do not print the exception text: it contains the URL (and the token).
            print("Telegram request failed: %s" % type(exc).__name__)
            return False
        if r.status_code == 200:
            return True
        if r.status_code == 429:
            wait = r.json().get("parameters", {}).get("retry_after", 5)
            time.sleep(wait + 1)
            continue
        print("Telegram error %s: %s" % (r.status_code, r.text[:200]))
        return False
    return False


def main():
    if not TOKEN or not CHAT_ID:
        sys.exit("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")

    if "--test" in sys.argv:
        ok = send_message("✅ ربات خبر فعال است و به کانال وصل شد.")
        sys.exit(0 if ok else 1)

    sources = load_json(SOURCES_FILE, [])
    first_run = not os.path.exists(SEEN_FILE)
    seen = load_json(SEEN_FILE, [])
    seen_set = set(seen)

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=MAX_AGE_HOURS)

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
            print("FAIL %s: %s" % (source["name"], exc))

    new_items = [i for i in all_items if i["id"] not in seen_set]
    # Mark old or filtered items as seen without posting them.
    fresh = []
    for i in new_items:
        if i["ts"] < cutoff or any(w in i["title"] for w in EXCLUDE_WORDS):
            seen.append(i["id"])
        else:
            fresh.append(i)

    if first_run:
        fresh.sort(key=lambda i: i["ts"], reverse=True)
        to_post = list(reversed(fresh[:FIRST_RUN_POSTS]))
        # everything not being posted now is considered old news; posted items
        # are added to "seen" only after Telegram accepts them (see below)
        posting_ids = {i["id"] for i in to_post}
        seen.extend(i["id"] for i in fresh if i["id"] not in posting_ids)
    else:
        fresh.sort(key=lambda i: (i["priority"], i["ts"]))
        to_post = fresh[:MAX_POSTS_PER_RUN]

    posted = 0
    for item in to_post:
        if send_message(format_post(item)):
            posted += 1
            if item["id"] not in seen:
                seen.append(item["id"])
            time.sleep(PAUSE_BETWEEN_POSTS)
        else:
            print("Stopping: could not post to Telegram.")
            break

    # Had news to send but nothing reached Telegram: do NOT save state,
    # otherwise these items would be remembered as "seen" and lost.
    if to_post and posted == 0:
        print("Could not post anything to Telegram - state not saved.")
        sys.exit(1)

    save_json(SEEN_FILE, seen[-SEEN_LIMIT:])
    print("Done. posted=%d, new=%d, failed feeds=%s" % (posted, len(new_items), failed or "none"))

    # If every feed failed, exit with an error so GitHub shows a red X (and emails you).
    if active and len(failed) == active:
        sys.exit(1)


if __name__ == "__main__":
    main()
