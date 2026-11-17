"""Import of an Overcast "All data" OPML export: shows, play history and playlists.

The format, its quirks and the mapping are described in docs/overcast-import.md.

Rules that make a second run safe:
- Shows and episodes carry the Overcast id, so nothing is created twice.
- State is only ever added: an episode played here is never set back to unplayed by the export.
- Nothing is downloaded or scanned by the import.
"""
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime

import db
import feeds
import playlists

IMPORT_PATH = os.path.join(db.DATA_DIR, "import", "overcast.opml")
FIRST_KEY = "overcast_first_import"   # time of the first real import, so later runs use the same cut-off
SOURCE = "overcast"
SORTING = {
    "chronological": "oldest",
    "chronological-by-podcast": "oldest_by_show",
    "reverse-chronological": "newest",
    "reverse-chronological-by-podcast": "newest_by_show",
}
PLAYLIST_ATTRS = {
    "type", "text", "title", "smart", "sorting",
    "includePodcastIds", "priorityPodcastIds", "includeEpisodeIds", "sortedEpisodeIds",
}
TITLE_MATCH_SECONDS = 2 * 86400   # a title match also needs the dates to be this close
HOST_PIECE = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]{2,}$", re.I)

JOB = {"state": "idle"}
LOCK = threading.Lock()


# ---------- reading the file ----------

def iso_time(value):
    """Episode dates: ISO 8601 with an offset, like 2021-09-15T23:58:13-04:00."""
    if not value:
        return 0
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


def rfc_time(value):
    """Feed dates: RFC 822, like Tue, 14 Apr 2020 03:12:55 +0000."""
    if not value:
        return 0
    try:
        return int(parsedate_to_datetime(value).timestamp())
    except (TypeError, ValueError):
        return 0


def id_list(value):
    return [piece.strip() for piece in (value or "").split(",") if piece.strip()]


def parse(data):
    """Turn the export into plain dicts. Raises ValueError when it is not an Overcast export."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        raise ValueError("That file is not valid XML.")
    out = {"playlists": [], "feeds": [], "warnings": []}
    for node in root.iter("outline"):
        kind = node.get("type")
        if kind == "podcast-playlist":
            sorting = node.get("sorting") or "chronological"
            if sorting not in SORTING:
                out["warnings"].append(f"Playlist {node.get('title')!r}: unknown sorting {sorting!r}, using oldest first.")
            extra = sorted(set(node.attrib) - PLAYLIST_ATTRS)
            if extra:
                out["warnings"].append(f"Playlist {node.get('title')!r}: skipped unknown settings {', '.join(extra)}.")
            out["playlists"].append({
                "title": (node.get("title") or node.get("text") or "Playlist").strip(),
                "smart": node.get("smart") == "1",
                "sort": SORTING.get(sorting, "oldest"),
                "show_ids": id_list(node.get("includePodcastIds")),
                "high_ids": id_list(node.get("priorityPodcastIds")),
                "include_ids": id_list(node.get("includeEpisodeIds")),
                "order_ids": id_list(node.get("sortedEpisodeIds")),
            })
        elif kind == "rss" and node.get("overcastId") and node.get("xmlUrl"):
            feed = {
                "overcast_id": node.get("overcastId"),
                "title": (node.get("title") or node.get("text") or "").strip(),
                "feed_url": node.get("xmlUrl").strip(),
                "subscribed": node.get("subscribed") == "1",
                "added": rfc_time(node.get("overcastAddedDate")),
                "episodes": [],
            }
            for child in node:
                if child.get("type") != "podcast-episode" or not child.get("overcastId"):
                    continue
                progress = 0.0
                try:
                    progress = float(child.get("progress") or 0)
                except ValueError:
                    pass
                feed["episodes"].append({
                    "overcast_id": child.get("overcastId"),
                    "title": (child.get("title") or "Untitled").strip(),
                    "published": iso_time(child.get("pubDate")),
                    "audio_url": (child.get("enclosureUrl") or "").strip(),
                    "played": child.get("played") == "1",
                    "progress": progress,
                    "removed": child.get("userDeleted") == "1",
                    "updated": iso_time(child.get("userUpdatedDate")),
                })
            out["feeds"].append(feed)
    if not out["feeds"]:
        raise ValueError("No Overcast shows were found. Use the \"All data\" export from overcast.fm/account.")
    return out


def totals(parsed):
    episodes = [e for f in parsed["feeds"] for e in f["episodes"]]
    return {
        "feeds": len(parsed["feeds"]),
        "subscribed": sum(1 for f in parsed["feeds"] if f["subscribed"]),
        "episodes": len(episodes),
        "played": sum(1 for e in episodes if e["played"]),
        "in_progress": sum(1 for e in episodes if e["progress"] > 0 and not e["played"]),
        "playlists": len(parsed["playlists"]),
    }


def save_upload(data):
    """Check the file, keep it for the job, and return the quick summary."""
    parsed = parse(data)
    os.makedirs(os.path.dirname(IMPORT_PATH), exist_ok=True)
    with open(IMPORT_PATH, "wb") as fh:
        fh.write(data)
    result = totals(parsed)
    result["warnings"] = parsed["warnings"]
    return result


# ---------- matching export episodes to feed episodes ----------

def normalize_url(url):
    """The audio address without the parts that change: tracking prefixes, the query string, http or https.

    Trackers wrap the real address like prefix.example/e/tracker.example/id/real.host/file.mp3,
    so everything before the last piece that looks like a host name is dropped.
    """
    text = (url or "").split("?")[0].split("#")[0].strip()
    text = re.sub(r"^https?://", "", text, flags=re.I)
    pieces = [p for p in text.split("/") if p]
    start = 0
    for i, piece in enumerate(pieces[:-1]):
        if HOST_PIECE.match(piece):
            start = i
    return "/".join(pieces[start:]).lower()


def normalize_title(title):
    return re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()


def match_episodes(exported, candidates):
    """Pair export episodes with episodes this app knows (from its database or the feed).

    candidates: dicts with audio_url, title, published and overcast_id (or None).
    Order of trust: already linked, exact address, normalized address, then title plus date.
    Each candidate is used once. Returns {overcast_id: (candidate index, how)}.
    """
    linked, exact, normal, by_title = {}, {}, {}, {}
    for i, c in enumerate(candidates):
        if c.get("overcast_id"):
            linked.setdefault(c["overcast_id"], i)
        if c.get("audio_url"):
            exact.setdefault(c["audio_url"], i)
            normal.setdefault(normalize_url(c["audio_url"]), i)
        by_title.setdefault(normalize_title(c.get("title")), []).append(i)
    found, used = {}, set()

    def take(episode, index, how):
        if index is None or index in used or episode["overcast_id"] in found:
            return
        used.add(index)
        found[episode["overcast_id"]] = (index, how)

    for e in exported:
        take(e, linked.get(e["overcast_id"]), "linked")
    for e in exported:
        take(e, exact.get(e["audio_url"]), "exact")
    for e in exported:
        if e["audio_url"]:
            take(e, normal.get(normalize_url(e["audio_url"])), "normalized")
    for e in exported:
        if e["overcast_id"] in found:
            continue
        best, best_gap = None, None
        for i in by_title.get(normalize_title(e["title"]), []):
            gap = abs((candidates[i].get("published") or 0) - e["published"])
            if i not in used and gap <= TITLE_MATCH_SECONDS and (best is None or gap < best_gap):
                best, best_gap = i, gap
        take(e, best, "title")
    return found


# ---------- the job ----------

def feed_key(url):
    text = re.sub(r"^https?://", "", (url or "").strip(), flags=re.I).rstrip("/")
    host, _, path = text.partition("/")
    return host.lower() + "/" + path


def find_show(conn, feed):
    row = conn.execute("SELECT * FROM shows WHERE overcast_id = ?", (feed["overcast_id"],)).fetchone()
    if row:
        return row
    wanted = feed_key(feed["feed_url"])
    for row in conn.execute("SELECT * FROM shows").fetchall():
        if feed_key(row["feed_url"]) == wanted:
            return row
    return None


EPISODE_FIELDS = (
    "id, guid, title, published, audio_url, overcast_id, played, position, status, file_path, "
    "COALESCE(starred, 0) AS starred, COALESCE(removed, 0) AS removed"
)


def untouched(row):
    """An episode nobody has done anything with yet, here or in Overcast."""
    return (
        not row["played"] and not row["removed"] and not row["starred"] and not row["position"]
        and row["status"] == "new" and not row["file_path"] and not row["overcast_id"]
    )


def import_feed(conn, feed, cutoff, dry):
    """One show: read its feed, pair the export episodes, and (unless dry) save. Returns the numbers for the report."""
    report = {
        "title": feed["title"], "subscribed": feed["subscribed"], "export_episodes": len(feed["episodes"]),
        "new_show": False, "feed_error": None, "feed_episodes": 0,
        "linked": 0, "exact": 0, "normalized": 0, "title_match": 0, "history": 0,
        "changed": 0, "keep_current": 0, "mark_removed": 0,
    }
    show = find_show(conn, feed)
    report["new_show"] = show is None
    entries, feed_title, feed_image = [], None, None
    try:
        fetched = feeds.fetch_feed(feed["feed_url"])
        entries = feeds.feed_entries(fetched, feed["feed_url"])
        feed_title = fetched.feed.get("title")
        feed_image = (fetched.feed.get("image") or {}).get("href")
    except Exception as exc:
        report["feed_error"] = str(exc)[:200]
    report["feed_episodes"] = len(entries)

    show_id = show["id"] if show else None
    if not dry:
        if show is None:
            cur = conn.execute(
                "INSERT INTO shows (feed_url, title, image, subscribed, overcast_id) VALUES (?, ?, ?, ?, ?)",
                (feed["feed_url"], feed_title or feed["title"], feed_image, 1 if feed["subscribed"] else 0, feed["overcast_id"]),
            )
            show_id = cur.lastrowid
        else:
            conn.execute(
                "UPDATE shows SET overcast_id = ?, title = COALESCE(title, ?), image = COALESCE(image, ?) WHERE id = ?",
                (feed["overcast_id"], feed_title or feed["title"], feed_image, show_id),
            )
        feeds.store_entries(conn, show_id, entries)

    # Everything this app knows about the show: database rows first, then feed episodes not saved yet (dry run only).
    rows = []
    if show_id is not None:
        rows = [dict(r) for r in conn.execute(f"SELECT {EPISODE_FIELDS} FROM episodes WHERE show_id = ?", (show_id,)).fetchall()]
    known_guids = {r["guid"] for r in rows}
    candidates = rows[:]
    for e in entries:
        if e["guid"] not in known_guids:
            known_guids.add(e["guid"])
            candidates.append({
                "id": None, "guid": e["guid"], "title": e["title"], "published": e["published"], "audio_url": e["audio_url"],
                "overcast_id": None, "played": 0, "position": 0, "status": "new", "file_path": None, "starred": 0, "removed": 0,
            })
    found = match_episodes(feed["episodes"], candidates)
    matched = {index for index, _how in found.values()}

    for e in feed["episodes"]:
        pair = found.get(e["overcast_id"])
        if pair is None:
            report["history"] += 1
            row = {"id": None, "overcast_id": None, "played": 0, "position": 0, "status": "new", "removed": 0}
            if not dry:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO episodes (show_id, guid, title, published, duration, audio_url) VALUES (?, ?, ?, ?, 0, ?)",
                    (show_id, "overcast:" + e["overcast_id"], e["title"], e["published"], e["audio_url"]),
                )
                row["id"] = cur.lastrowid if cur.rowcount else conn.execute(
                    "SELECT id FROM episodes WHERE show_id = ? AND guid = ?", (show_id, "overcast:" + e["overcast_id"])
                ).fetchone()["id"]
        else:
            row = candidates[pair[0]]
            report["title_match" if pair[1] == "title" else pair[1]] += 1
        # State is only added, never taken away.
        played = 1 if (row["played"] or e["played"]) else 0
        removed = 1 if (row["removed"] or e["removed"]) else 0
        position = row["position"] or 0
        if not position and not played and e["progress"] > 0:
            position = e["progress"]
        changed = (
            played != (row["played"] or 0) or removed != (row["removed"] or 0)
            or position != (row["position"] or 0) or row["overcast_id"] != e["overcast_id"]
        )
        if changed:
            report["changed"] += 1
        if not played and not removed:
            report["keep_current"] += 1
        if changed and not dry:
            conn.execute(
                "UPDATE episodes SET overcast_id = ?, played = ?, removed = ?, position = ?, "
                "played_at = CASE WHEN ? = 1 AND played = 0 THEN ? ELSE played_at END, "
                "status = CASE WHEN status = 'queued' AND (? = 1 OR ? = 1) THEN 'new' ELSE status END "
                "WHERE id = ?",
                (e["overcast_id"], played, removed, position, played, e["updated"], played, removed, row["id"]),
            )

    # Feed episodes that are not in the export. The export only holds episodes that were played or started,
    # so it cannot say which of these are still wanted. Old untouched ones are taken off the lists.
    for index, c in enumerate(candidates):
        if index in matched:
            continue
        if cutoff is not None and untouched(c) and (c["published"] or 0) < cutoff:
            report["mark_removed"] += 1
            report["changed"] += 1
            if not dry:
                conn.execute("UPDATE episodes SET removed = 1 WHERE show_id = ? AND guid = ?", (show_id, c["guid"]))
        elif not c["played"] and not c["removed"]:
            report["keep_current"] += 1
    if not dry:
        conn.commit()
    return report


def import_playlists(conn, parsed, dry):
    """Create or update the playlists. Overcast ids are translated; ids that point nowhere are skipped quietly."""
    export_shows = {f["overcast_id"] for f in parsed["feeds"]}
    export_episodes = {e["overcast_id"] for f in parsed["feeds"] for e in f["episodes"]}
    show_map, episode_map = {}, {}
    if not dry:
        show_map = {r["overcast_id"]: r["id"] for r in conn.execute("SELECT id, overcast_id FROM shows WHERE overcast_id IS NOT NULL")}
        episode_map = {r["overcast_id"]: r["id"] for r in conn.execute("SELECT id, overcast_id FROM episodes WHERE overcast_id IS NOT NULL")}
    reports = []
    for p in parsed["playlists"]:
        ordered_shows = p["high_ids"] + [s for s in p["show_ids"] if s not in p["high_ids"]]
        existing = conn.execute("SELECT id FROM playlists WHERE source = ? AND name = ?", (SOURCE, p["title"])).fetchone()
        reports.append({
            "title": p["title"], "sort": p["sort"], "exists": existing is not None,
            "shows": sum(1 for s in ordered_shows if s in export_shows),
            "high": sum(1 for s in p["high_ids"] if s in export_shows),
            "include": sum(1 for x in p["include_ids"] if x in export_episodes),
            "manual": sum(1 for x in p["order_ids"] if x in export_episodes),
            "manual_missing": sum(1 for x in p["order_ids"] if x not in export_episodes),
        })
        if dry:
            continue
        fields = {"sort": p["sort"], "priority_mode": "by_rank", "play_top_next": False, "include_all_shows": False, "source": SOURCE}
        if existing:
            playlist_id = existing["id"]
            playlists.update(conn, playlist_id, fields)
        else:
            playlist_id = playlists.create(conn, p["title"], fields)
        playlists.set_shows(conn, playlist_id, [
            {"show_id": show_map[s], "priority": "high" if s in p["high_ids"] else "normal"}
            for s in ordered_shows if s in show_map
        ])
        for x in p["include_ids"]:
            if x in episode_map:
                playlists.set_override(conn, playlist_id, episode_map[x], "include")
        playlists.set_order(conn, playlist_id, [episode_map[x] for x in p["order_ids"] if x in episode_map])
    return reports


def database_totals(conn):
    one = lambda sql: conn.execute(sql).fetchone()[0]   # noqa: E731
    return {
        "feeds": one("SELECT COUNT(*) FROM shows WHERE overcast_id IS NOT NULL"),
        "episodes": one("SELECT COUNT(*) FROM episodes WHERE overcast_id IS NOT NULL"),
        "played": one("SELECT COUNT(*) FROM episodes WHERE overcast_id IS NOT NULL AND played = 1"),
        "in_progress": one("SELECT COUNT(*) FROM episodes WHERE overcast_id IS NOT NULL AND played = 0 AND position > 0"),
        "playlists": one(f"SELECT COUNT(*) FROM playlists WHERE source = '{SOURCE}'"),
    }


def work(dry, keep_days):
    """The whole import. dry: read and compare only, save nothing.

    keep_days: feed episodes that are not in the export stay on the lists when they are this recent.
    None keeps all of them.
    """
    with open(IMPORT_PATH, "rb") as fh:
        parsed = parse(fh.read())
    conn = db.connect()
    try:
        first = db.meta_get(conn, FIRST_KEY) or int(time.time())
        cutoff = None if keep_days is None else first - keep_days * 86400
        total = len(parsed["feeds"])
        reports = []
        for i, feed in enumerate(parsed["feeds"], 1):
            JOB.update(step=i, total=total, label=feed["title"])
            print(f"[overcast] ({i}/{total}) {feed['title']}")
            reports.append(import_feed(conn, feed, cutoff, dry))
        JOB.update(label="Playlists")
        playlist_reports = import_playlists(conn, parsed, dry)
        keep_default = feeds.AUTO_DOWNLOAD_LATEST
        result = {
            "dry": dry, "keep_days": keep_days, "export": totals(parsed), "feeds": reports, "playlists": playlist_reports,
            "warnings": parsed["warnings"] + [f"{r['title']}: the feed could not be read ({r['feed_error']})." for r in reports if r["feed_error"]],
            "sum": {key: sum(r[key] for r in reports) for key in (
                "linked", "exact", "normalized", "title_match", "history", "changed", "keep_current", "mark_removed")},
            # A rough number: the keep N rule takes at most the default from every subscribed show.
            "may_download": sum(min(keep_default, r["keep_current"]) for r in reports if r["subscribed"]),
        }
        if not dry:
            if not db.meta_get(conn, FIRST_KEY):
                db.meta_set(conn, FIRST_KEY, first)
                conn.commit()
            result["database"] = database_totals(conn)
        return result
    finally:
        conn.close()


def run(dry, keep_days):
    try:
        result = work(dry, keep_days)
        JOB.update(state="done", result=result, label="")
        print(f"[overcast] {'dry run' if dry else 'import'} done: {result['sum']}")
    except Exception as exc:
        JOB.update(state="error", error=str(exc)[:300], label="")
        print(f"[overcast] failed: {exc}")


def start(dry, keep_days):
    """Start the job in the background. Raises ValueError when it cannot start."""
    if not os.path.exists(IMPORT_PATH):
        raise ValueError("Upload the Overcast file first.")
    with LOCK:
        if JOB.get("state") == "running":
            raise ValueError("An import is already running.")
        JOB.clear()
        JOB.update(state="running", dry=dry, step=0, total=0, label="Reading the file")
    threading.Thread(target=run, args=(dry, keep_days), daemon=True).start()


def status():
    return dict(JOB)
