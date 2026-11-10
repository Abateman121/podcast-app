import calendar
import os
import re

import feedparser
import requests

import db


def parse_duration(raw):
    if not raw:
        return 0
    parts = str(raw).split(":")
    try:
        nums = [int(float(p)) for p in parts]
    except ValueError:
        return 0
    seconds = 0
    for n in nums:
        seconds = seconds * 60 + n
    return seconds


def refresh_feed(conn, show_id, feed_url):
    parsed = feedparser.parse(feed_url)
    title = parsed.feed.get("title", feed_url)
    image = (parsed.feed.get("image") or {}).get("href")
    conn.execute("UPDATE shows SET title = ?, image = ? WHERE id = ?", (title, image, show_id))
    added = 0
    for entry in parsed.entries:
        audio_url = None
        for link in entry.get("enclosures", []):
            audio_url = link.get("href")
            break
        if not audio_url:
            continue
        guid = entry.get("id") or audio_url
        published = 0
        if entry.get("published_parsed"):
            published = calendar.timegm(entry.published_parsed)
        duration = parse_duration(entry.get("itunes_duration"))
        cur = conn.execute(
            "INSERT OR IGNORE INTO episodes (show_id, guid, title, published, duration, audio_url) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (show_id, guid, entry.get("title", "Untitled"), published, duration, audio_url),
        )
        added += cur.rowcount
    conn.commit()
    return added


def add_feed(feed_url):
    conn = db.connect()
    conn.execute("INSERT OR IGNORE INTO shows (feed_url) VALUES (?)", (feed_url,))
    conn.commit()
    show_id = conn.execute("SELECT id FROM shows WHERE feed_url = ?", (feed_url,)).fetchone()["id"]
    refresh_feed(conn, show_id, feed_url)
    conn.close()
    return show_id


def refresh_all():
    conn = db.connect()
    for show in conn.execute("SELECT id, feed_url FROM shows").fetchall():
        try:
            refresh_feed(conn, show["id"], show["feed_url"])
        except Exception as exc:
            print(f"    feed error {show['feed_url']}: {exc}")
    conn.close()


def episodes_to_download(conn, latest_per_show):
    ids = set()
    for show in conn.execute("SELECT id FROM shows").fetchall():
        rows = conn.execute(
            "SELECT id, status FROM episodes WHERE show_id = ? ORDER BY published DESC LIMIT ?",
            (show["id"], latest_per_show),
        ).fetchall()
        ids.update(r["id"] for r in rows if r["status"] == "new")
    wanted = conn.execute("SELECT id FROM episodes WHERE wanted = 1 AND status = 'new'").fetchall()
    ids.update(r["id"] for r in wanted)
    return sorted(ids)


def download_episode(conn, episode_id):
    ep = conn.execute("SELECT audio_url FROM episodes WHERE id = ?", (episode_id,)).fetchone()
    ext = os.path.splitext(re.sub(r"\?.*$", "", ep["audio_url"]))[1] or ".mp3"
    if len(ext) > 5:
        ext = ".mp3"
    path = os.path.join(db.AUDIO_DIR, f"{episode_id}{ext}")
    tmp = path + ".part"
    with requests.get(ep["audio_url"], stream=True, timeout=60) as resp:
        resp.raise_for_status()
        with open(tmp, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=1024 * 256):
                fh.write(chunk)
    os.replace(tmp, path)
    conn.execute(
        "UPDATE episodes SET file_path = ?, status = 'downloaded' WHERE id = ?", (path, episode_id)
    )
    conn.commit()
