import calendar
import os
import re

import feedparser
import requests

import db
import sources
from progress import PROGRESS

# How many unplayed episodes of each show are kept downloaded when the show has no number of its own.
AUTO_DOWNLOAD_LATEST = int(os.environ.get("AUTO_DOWNLOAD_LATEST", "5"))


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


def fetch_feed(feed_url):
    resp = requests.get(feed_url, headers=sources.UA, timeout=30)
    resp.raise_for_status()
    return feedparser.parse(resp.content)


def is_youtube_url(url):
    return "youtube.com/watch" in (url or "") or "youtu.be/" in (url or "")


def feed_entries(parsed, feed_url):
    """The episodes of a parsed feed as plain dicts. Nothing is saved."""
    youtube_feed = "youtube.com/feeds" in feed_url
    out = []
    for entry in parsed.entries:
        audio_url = None
        for link in entry.get("enclosures", []):
            audio_url = link.get("href")
            break
        if not audio_url and youtube_feed:
            audio_url = entry.get("link")
        if not audio_url:
            continue
        guid = entry.get("id") or audio_url
        published = 0
        if entry.get("published_parsed"):
            published = calendar.timegm(entry.published_parsed)
        duration = parse_duration(entry.get("itunes_duration"))
        chapters = entry.get("podcast_chapters")
        chapters_url = chapters.get("url") if isinstance(chapters, dict) else None
        out.append({
            "guid": guid, "title": entry.get("title", "Untitled"), "published": published,
            "duration": duration, "audio_url": audio_url, "chapters_url": chapters_url,
        })
    return out


def store_entries(conn, show_id, entries):
    """Insert feed episodes that are not in the database yet. Returns how many were new. The caller commits."""
    added = 0
    for e in entries:
        cur = conn.execute(
            "INSERT OR IGNORE INTO episodes (show_id, guid, title, published, duration, audio_url, chapters_url) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (show_id, e["guid"], e["title"], e["published"], e["duration"], e["audio_url"], e["chapters_url"]),
        )
        added += cur.rowcount
        if e["chapters_url"] and not cur.rowcount:
            conn.execute(
                "UPDATE episodes SET chapters_url = ? WHERE show_id = ? AND guid = ? AND chapters_url IS NULL",
                (e["chapters_url"], show_id, e["guid"]),
            )
    return added


def refresh_feed(conn, show_id, feed_url):
    parsed = fetch_feed(feed_url)
    title = parsed.feed.get("title")
    image = (parsed.feed.get("image") or {}).get("href")
    if title:
        conn.execute(
            "UPDATE shows SET title = ?, image = COALESCE(?, image) WHERE id = ?", (title, image, show_id)
        )
    added = store_entries(conn, show_id, feed_entries(parsed, feed_url))
    conn.commit()
    return added


def insert_show(conn, feed_url, title=None, auto_latest=None):
    cur = conn.execute(
        "INSERT OR IGNORE INTO shows (feed_url, title, auto_latest) VALUES (?, ?, ?)",
        (feed_url, title, auto_latest),
    )
    conn.commit()
    show_id = conn.execute("SELECT id FROM shows WHERE feed_url = ?", (feed_url,)).fetchone()["id"]
    return show_id, cur.rowcount == 1


def add_show_from_url(url):
    feed_url, note = sources.resolve(url)
    conn = db.connect()
    try:
        show_id, created = insert_show(conn, feed_url)
        if not created:
            return {"id": show_id, "note": "That show is already added."}
        try:
            refresh_feed(conn, show_id, feed_url)
        except Exception as exc:
            conn.execute("DELETE FROM shows WHERE id = ?", (show_id,))
            conn.commit()
            raise sources.SourceError(f"Could not read a podcast feed from that link ({exc}).")
        count = conn.execute("SELECT COUNT(*) AS n FROM episodes WHERE show_id = ?", (show_id,)).fetchone()["n"]
        if count == 0:
            conn.execute("DELETE FROM shows WHERE id = ?", (show_id,))
            conn.commit()
            raise sources.SourceError("That link did not contain any podcast episodes.")
        title = conn.execute("SELECT title FROM shows WHERE id = ?", (show_id,)).fetchone()["title"]
        return {"id": show_id, "title": title, "note": note}
    finally:
        conn.close()


def import_opml_text(text):
    entries = sources.parse_opml(text)
    conn = db.connect()
    added = 0
    for feed_url, title in entries:
        _show_id, created = insert_show(conn, feed_url, title)
        added += 1 if created else 0
    conn.close()
    return {"total": len(entries), "added": added, "skipped": len(entries) - added}


def refresh_all():
    conn = db.connect()
    shows = conn.execute("SELECT id, feed_url FROM shows WHERE COALESCE(subscribed, 1) = 1").fetchall()
    for i, show in enumerate(shows, 1):
        try:
            print(f"    ({i}/{len(shows)}) {show['feed_url']}")
            refresh_feed(conn, show["id"], show["feed_url"])
        except Exception as exc:
            print(f"    feed error {show['feed_url']}: {exc}")
    conn.close()


def queue_latest(conn):
    """Keep N downloaded: queue the newest N unplayed episodes of every subscribed show."""
    queued = 0
    for show in conn.execute("SELECT id, keep_last FROM shows WHERE COALESCE(subscribed, 1) = 1").fetchall():
        limit = AUTO_DOWNLOAD_LATEST if show["keep_last"] is None else show["keep_last"]
        if limit <= 0:
            continue
        rows = conn.execute(
            "SELECT id, status FROM episodes WHERE show_id = ? AND played = 0 AND removed = 0 "
            "ORDER BY published DESC, id DESC LIMIT ?",
            (show["id"], limit),
        ).fetchall()
        for r in rows:
            if r["status"] == "new":
                conn.execute("UPDATE episodes SET status = 'queued' WHERE id = ?", (r["id"],))
                queued += 1
    conn.commit()
    return queued


def download_http(episode_id, url, path):
    tmp = path + ".part"
    with requests.get(url, headers=sources.UA, stream=True, timeout=60) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        with open(tmp, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=256 * 1024):
                fh.write(chunk)
                done += len(chunk)
                PROGRESS[episode_id] = {"phase": "downloading", "done": done, "total": total}
    os.replace(tmp, path)


def download_youtube(episode_id, url):
    import yt_dlp  # imported here so the app still starts if yt-dlp is missing

    def hook(d):
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            PROGRESS[episode_id] = {"phase": "downloading", "done": d.get("downloaded_bytes", 0), "total": total}

    options = {
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "outtmpl": os.path.join(db.AUDIO_DIR, f"{episode_id}.%(ext)s"),
        "progress_hooks": [hook],
        "noplaylist": True,
        "quiet": True,
        "noprogress": True,
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
        return ydl.prepare_filename(info), int(info.get("duration") or 0)


def download_episode(conn, episode_id):
    ep = conn.execute("SELECT audio_url FROM episodes WHERE id = ?", (episode_id,)).fetchone()
    duration = 0
    if is_youtube_url(ep["audio_url"]):
        path, duration = download_youtube(episode_id, ep["audio_url"])
    else:
        ext = os.path.splitext(re.sub(r"\?.*$", "", ep["audio_url"]))[1] or ".mp3"
        if len(ext) > 5:
            ext = ".mp3"
        path = os.path.join(db.AUDIO_DIR, f"{episode_id}{ext}")
        download_http(episode_id, ep["audio_url"], path)
    conn.execute(
        "UPDATE episodes SET file_path = ?, status = 'downloaded', duration = CASE WHEN ? > 0 THEN ? ELSE duration END "
        "WHERE id = ?",
        (path, duration, duration, episode_id),
    )
    conn.commit()


def delete_show_files(conn, show_id):
    rows = conn.execute("SELECT file_path FROM episodes WHERE show_id = ?", (show_id,)).fetchall()
    removed = 0
    for r in rows:
        if r["file_path"] and os.path.exists(r["file_path"]):
            os.remove(r["file_path"])
            removed += 1
    # Intro/outro template snippets and the audio of tagged ads go with the show.
    for kind in ("intro", "outro"):
        wav = db.template_path(show_id, kind)
        if os.path.exists(wav):
            os.remove(wav)
    for tag in conn.execute("SELECT id FROM ad_tags WHERE show_id = ?", (show_id,)).fetchall():
        wav = db.ad_sample_path(tag["id"])
        if os.path.exists(wav):
            os.remove(wav)
    return removed
