"""Smart playlists: which episodes belong to a playlist and in what order.

A playlist's contents are worked out each time, never stored. Ordering lives in order()
and nowhere else, so every client gets the same list.

Current episode: not played and not removed. That is the pool smart playlists draw from.
"""
import json
import time

import db

SORTS = ("oldest", "newest", "oldest_by_show", "newest_by_show")
PRIORITY_MODES = ("by_rank", "by_date")
PRIORITIES = ("high", "normal", "low")
# builtin_type, title, icon, sort
BUILTINS = (
    ("queue", "Up Next", "⏭", "oldest"),
    ("starred", "Starred", "★", "newest"),
    ("downloaded", "Downloaded", "⬇", "newest"),
)
SETUP_KEY = "v16_setup"


# ---------- setup ----------

def setup(conn):
    """Create the built-in playlists and convert playlists saved before v0.0.16. Safe to run on every start."""
    now = int(time.time())
    for position, (builtin, title, icon, sort) in enumerate(BUILTINS):
        if not conn.execute("SELECT 1 FROM playlists WHERE builtin_type = ?", (builtin,)).fetchone():
            conn.execute(
                "INSERT INTO playlists (name, rules_json, kind, builtin_type, icon, sort, position, converted, created, updated) "
                "VALUES (?, '{}', 'builtin', ?, ?, ?, ?, 1, ?, ?)",
                (title, builtin, icon, sort, position, now, now),
            )
    for row in conn.execute("SELECT id, rules_json FROM playlists WHERE converted = 0").fetchall():
        try:
            rules = json.loads(row["rules_json"] or "{}")
        except ValueError:
            rules = {}
        show_ids = [int(s) for s in rules.get("show_ids") or []]
        for show_id in show_ids:
            if conn.execute("SELECT 1 FROM shows WHERE id = ?", (show_id,)).fetchone():
                conn.execute(
                    "INSERT OR IGNORE INTO playlist_shows (playlist_id, show_id) VALUES (?, ?)", (row["id"], show_id)
                )
        conn.execute(
            "UPDATE playlists SET kind = 'smart', include_all_shows = ?, sort = ?, position = ?, converted = 1, "
            "created = ?, updated = ? WHERE id = ?",
            (0 if show_ids else 1, "oldest" if rules.get("sort") == "oldest" else "newest", 100 + row["id"], now, now, row["id"]),
        )
    if not db.meta_get(conn, SETUP_KEY):
        # Before v0.0.16 a keep_last of 0 meant "no limit". It now means "download nothing", so clear it.
        conn.execute("UPDATE shows SET keep_last = NULL WHERE keep_last = 0")
        db.meta_bump(conn, SETUP_KEY)
    conn.commit()


# ---------- reading ----------

def get(conn, playlist_id):
    row = conn.execute("SELECT * FROM playlists WHERE id = ?", (playlist_id,)).fetchone()
    if not row:
        raise LookupError("Playlist not found")
    return row


def summary(row):
    return {
        "id": row["id"],
        "title": row["name"],
        "kind": row["kind"] or "smart",
        "builtin_type": row["builtin_type"],
        "icon": row["icon"] or "",
        "color": row["color"] or "",
        "position": row["position"] or 0,
        "include_all_shows": bool(row["include_all_shows"]),
        "sort": row["sort"] if row["sort"] in SORTS else "oldest",
        "priority_mode": row["priority_mode"] if row["priority_mode"] in PRIORITY_MODES else "by_rank",
        "play_top_next": bool(row["play_top_next"]),
        "continuous": bool(row["continuous"]),
        "source": row["source"],
    }


def show_rows(conn, playlist_id):
    return conn.execute(
        "SELECT ps.show_id, ps.priority, ps.rank, s.title FROM playlist_shows ps JOIN shows s ON s.id = ps.show_id "
        "WHERE ps.playlist_id = ? ORDER BY ps.rank IS NULL, ps.rank, ps.show_id",
        (playlist_id,),
    ).fetchall()


def settings(conn, playlist_id):
    row = get(conn, playlist_id)
    out = summary(row)
    out["shows"] = [dict(r) for r in show_rows(conn, playlist_id)]
    overrides = conn.execute(
        "SELECT o.episode_id, o.action, e.title, s.title AS show_title FROM playlist_overrides o "
        "JOIN episodes e ON e.id = o.episode_id JOIN shows s ON s.id = e.show_id WHERE o.playlist_id = ? ORDER BY e.published DESC",
        (playlist_id,),
    ).fetchall()
    out["include"] = [dict(r) for r in overrides if r["action"] == "include"]
    out["exclude"] = [dict(r) for r in overrides if r["action"] == "exclude"]
    out["manual"] = conn.execute(
        "SELECT COUNT(*) AS n FROM playlist_order WHERE playlist_id = ?", (playlist_id,)
    ).fetchone()["n"]
    return out


def members(conn, row, force_id=None):
    """Rows (id, show_id, published, played, status) of the episodes in a playlist, in no particular order.

    force_id keeps one episode in the result even if it no longer qualifies. The player uses it to
    find what comes after an episode that was just finished.
    """
    cols = "e.id, e.show_id, e.published, e.played, e.status"
    pid = row["id"]
    force = " OR e.id = ?" if force_id else ""
    extra = [force_id] if force_id else []
    if row["kind"] == "builtin":
        if row["builtin_type"] == "queue":
            sql = (
                f"SELECT {cols} FROM episodes e JOIN playlist_order o ON o.episode_id = e.id AND o.playlist_id = ? "
                f"WHERE e.played = 0{force}"
            )
            return conn.execute(sql, [pid] + extra).fetchall()
        if row["builtin_type"] == "starred":
            return conn.execute(f"SELECT {cols} FROM episodes e WHERE e.starred = 1{force}", extra).fetchall()
        marks = ",".join("?" for _ in db.PLAYABLE)
        return conn.execute(
            f"SELECT {cols} FROM episodes e WHERE e.status IN ({marks}){force}", list(db.PLAYABLE) + extra
        ).fetchall()
    params = [int(time.time())]
    from_show = "s.subscribed = 1 AND (COALESCE(s.newer_than_days, 0) <= 0 OR e.published >= ? - s.newer_than_days * 86400)"
    if not row["include_all_shows"]:
        from_show += " AND e.show_id IN (SELECT show_id FROM playlist_shows WHERE playlist_id = ?)"
        params.append(pid)
    sql = (
        f"SELECT {cols} FROM episodes e JOIN shows s ON s.id = e.show_id "
        f"WHERE (e.played = 0 AND e.removed = 0 AND (({from_show}) "
        "OR e.id IN (SELECT episode_id FROM playlist_overrides WHERE playlist_id = ? AND action = 'include')) "
        f"AND e.id NOT IN (SELECT episode_id FROM playlist_overrides WHERE playlist_id = ? AND action = 'exclude')){force}"
    )
    return conn.execute(sql, params + [pid, pid] + extra).fetchall()


# ---------- ordering ----------

def order(episodes, sort="oldest", priority_mode="by_rank", show_priority=None, manual=None):
    """Put playlist members in playing order. Pure: no database, same input always gives the same output.

    episodes: rows or dicts with id, show_id and published.
    show_priority: {show_id: (priority, rank)}. Shows not listed are normal.
    manual: {episode_id: position}. Those episodes come first, in that order.
    """
    show_priority = show_priority or {}
    manual = manual or {}
    newest = sort.startswith("newest")
    by_show = sort.endswith("_by_show")

    def date_key(e):
        return (-e["published"], -e["id"]) if newest else (e["published"], e["id"])

    def clustered(items):
        groups = {}
        for e in items:
            groups.setdefault(e["show_id"], []).append(e)
        for group in groups.values():
            group.sort(key=date_key)
        # Each cluster is placed by its first episode: the oldest one, or the newest when sorting newest first.
        ordered = sorted(groups.items(), key=lambda kv: (date_key(kv[1][0])[0], kv[0]))
        return [e for _show, group in ordered for e in group]

    def plain(items):
        return clustered(items) if by_show else sorted(items, key=date_key)

    fixed = sorted((e for e in episodes if e["id"] in manual), key=lambda e: (manual[e["id"]], e["id"]))
    tiers = {"high": [], "normal": [], "low": []}
    for e in episodes:
        if e["id"] in manual:
            continue
        priority = show_priority.get(e["show_id"], ("normal", None))[0]
        tiers[priority if priority in tiers else "normal"].append(e)

    if priority_mode == "by_date":
        high = plain(tiers["high"])
    else:
        def rank_key(e):
            rank = show_priority[e["show_id"]][1]
            return (rank is None, rank or 0, e["show_id"]) + date_key(e)
        high = sorted(tiers["high"], key=rank_key)
    return fixed + high + plain(tiers["normal"]) + plain(tiers["low"])


def ordered(conn, row, force_id=None):
    """The playlist's members in playing order."""
    rows = members(conn, row, force_id)
    show_priority = {}
    if row["kind"] != "builtin":
        show_priority = {r["show_id"]: (r["priority"], r["rank"]) for r in show_rows(conn, row["id"])}
    manual = {
        r["episode_id"]: r["position"]
        for r in conn.execute("SELECT episode_id, position FROM playlist_order WHERE playlist_id = ?", (row["id"],))
    }
    sort = row["sort"] if row["sort"] in SORTS else "oldest"
    mode = row["priority_mode"] if row["priority_mode"] in PRIORITY_MODES else "by_rank"
    return order(rows, sort, mode, show_priority, manual)


def counts(conn, row):
    rows = members(conn, row)
    return {"total": len(rows), "unplayed": sum(1 for r in rows if not r["played"])}


# ---------- what plays next ----------

def pick_next(candidates):
    """From episodes in playing order, the one that should play next and the first one that can play now.

    Returns (wanted, playable). They differ when the wanted one is not downloaded yet.
    """
    wanted = candidates[0] if candidates else None
    playable = next((e for e in candidates if e["status"] in db.PLAYABLE), None)
    return wanted, playable


def prepare(conn, episode):
    """Make sure the next episode is on its way: queue the download and put it at the front."""
    if episode is None:
        return False
    # Only one episode is next up at a time.
    conn.execute("UPDATE episodes SET dl_priority = 0 WHERE dl_priority = 1 AND id != ?", (episode["id"],))
    if episode["status"] in ("new", "error"):
        conn.execute("UPDATE episodes SET status = 'queued', dl_priority = 1 WHERE id = ?", (episode["id"],))
    elif episode["status"] in ("queued", "downloaded"):
        conn.execute("UPDATE episodes SET dl_priority = 1 WHERE id = ?", (episode["id"],))
    else:
        conn.commit()
        return False   # already on disk and scanned
    conn.commit()
    return True


def next_in_playlist(conn, playlist_id, after_id):
    """Episodes that could follow after_id in this playlist, best first."""
    row = get(conn, playlist_id)
    items = ordered(conn, row, force_id=after_id)
    if row["play_top_next"]:
        return [e for e in items if e["id"] != after_id]
    for i, e in enumerate(items):
        if e["id"] == after_id:
            return items[i + 1:]
    return items


def next_in_show(conn, show_id, after_id):
    """On a show page the list runs newest first, so the next one is the next older current episode."""
    after = conn.execute("SELECT published, id FROM episodes WHERE id = ?", (after_id,)).fetchone()
    if not after:
        return []
    return conn.execute(
        "SELECT id, show_id, published, played, status FROM episodes WHERE show_id = ? AND played = 0 AND removed = 0 "
        "AND id != ? AND (published < ? OR (published = ? AND id < ?)) ORDER BY published DESC, id DESC LIMIT 200",
        (show_id, after_id, after["published"], after["published"], after["id"]),
    ).fetchall()


# ---------- writing ----------

def create(conn, title, fields=None):
    now = int(time.time())
    position = conn.execute("SELECT COALESCE(MAX(position), 0) + 1 AS p FROM playlists").fetchone()["p"]
    cur = conn.execute(
        "INSERT INTO playlists (name, rules_json, kind, position, converted, created, updated) VALUES (?, '{}', 'smart', ?, 1, ?, ?)",
        (title, position, now, now),
    )
    if fields:
        update(conn, cur.lastrowid, fields)
    conn.commit()
    return cur.lastrowid


def update(conn, playlist_id, fields):
    """Change playlist settings. Changing the sort order drops the hand-made order (not for Up Next)."""
    row = get(conn, playlist_id)
    sets, params = [], []
    for key, value in fields.items():
        if value is None:
            continue
        if key == "title":
            if not str(value).strip():
                raise ValueError("A playlist needs a title.")
            sets.append("name = ?")
            params.append(str(value).strip()[:120])
        elif key == "sort":
            if value not in SORTS:
                raise ValueError("Unknown sort order.")
            sets.append("sort = ?")
            params.append(value)
            if value != row["sort"] and row["builtin_type"] != "queue":
                conn.execute("DELETE FROM playlist_order WHERE playlist_id = ?", (playlist_id,))
        elif key == "priority_mode":
            if value not in PRIORITY_MODES:
                raise ValueError("Unknown priority mode.")
            sets.append("priority_mode = ?")
            params.append(value)
        elif key in ("icon", "color", "source"):
            sets.append(f"{key} = ?")
            params.append(str(value)[:40])
        elif key in ("include_all_shows", "play_top_next", "continuous"):
            sets.append(f"{key} = ?")
            params.append(1 if value else 0)
        elif key == "position":
            sets.append("position = ?")
            params.append(int(value))
    if sets:
        sets.append("updated = ?")
        params.append(int(time.time()))
        conn.execute(f"UPDATE playlists SET {', '.join(sets)} WHERE id = ?", params + [playlist_id])
    conn.commit()


def delete(conn, playlist_id):
    row = get(conn, playlist_id)
    if row["kind"] == "builtin":
        raise ValueError("Built-in playlists cannot be deleted.")
    conn.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))
    conn.commit()


def set_shows(conn, playlist_id, shows):
    """Replace the shows of a playlist. shows: [{show_id, priority, rank}]. High shows are ranked in list order."""
    get(conn, playlist_id)
    conn.execute("DELETE FROM playlist_shows WHERE playlist_id = ?", (playlist_id,))
    rank = 0
    for item in shows:
        priority = item.get("priority") or "normal"
        if priority not in PRIORITIES:
            raise ValueError("Priority must be high, normal or low.")
        if not conn.execute("SELECT 1 FROM shows WHERE id = ?", (item["show_id"],)).fetchone():
            continue
        this_rank = None
        if priority == "high":
            rank += 1
            this_rank = item.get("rank") if item.get("rank") is not None else rank
        conn.execute(
            "INSERT OR REPLACE INTO playlist_shows (playlist_id, show_id, priority, rank) VALUES (?, ?, ?, ?)",
            (playlist_id, item["show_id"], priority, this_rank),
        )
    conn.commit()


def set_show_member(conn, playlist_id, show_id, member):
    get(conn, playlist_id)
    if member:
        conn.execute("INSERT OR IGNORE INTO playlist_shows (playlist_id, show_id) VALUES (?, ?)", (playlist_id, show_id))
    else:
        conn.execute("DELETE FROM playlist_shows WHERE playlist_id = ? AND show_id = ?", (playlist_id, show_id))
    conn.commit()


def set_order(conn, playlist_id, episode_ids):
    """Replace the hand-made order. An empty list clears it. For Up Next this is also its contents."""
    get(conn, playlist_id)
    conn.execute("DELETE FROM playlist_order WHERE playlist_id = ?", (playlist_id,))
    seen = set()
    position = 0
    for episode_id in episode_ids:
        if episode_id in seen or not conn.execute("SELECT 1 FROM episodes WHERE id = ?", (episode_id,)).fetchone():
            continue   # ids that point nowhere are skipped quietly
        seen.add(episode_id)
        position += 1
        conn.execute(
            "INSERT INTO playlist_order (playlist_id, episode_id, position) VALUES (?, ?, ?)",
            (playlist_id, episode_id, position),
        )
    conn.commit()


def set_override(conn, playlist_id, episode_id, action):
    row = get(conn, playlist_id)
    if row["kind"] == "builtin":
        raise ValueError("Built-in playlists have fixed contents.")
    if action == "clear":
        conn.execute("DELETE FROM playlist_overrides WHERE playlist_id = ? AND episode_id = ?", (playlist_id, episode_id))
    elif action in ("include", "exclude"):
        if not conn.execute("SELECT 1 FROM episodes WHERE id = ?", (episode_id,)).fetchone():
            raise LookupError("Episode not found")
        conn.execute(
            "INSERT OR REPLACE INTO playlist_overrides (playlist_id, episode_id, action) VALUES (?, ?, ?)",
            (playlist_id, episode_id, action),
        )
    else:
        raise ValueError("Action must be include, exclude or clear.")
    conn.commit()


# ---------- Up Next ----------

def queue_id(conn):
    return conn.execute("SELECT id FROM playlists WHERE builtin_type = 'queue'").fetchone()["id"]


def queue_add(conn, episode_id, first):
    """Play Next (first) or Play Last on an episode. Moves it if it is already in Up Next."""
    if not conn.execute("SELECT 1 FROM episodes WHERE id = ?", (episode_id,)).fetchone():
        raise LookupError("Episode not found")
    pid = queue_id(conn)
    # Finished episodes drop out of Up Next here, so the list does not collect stale rows.
    conn.execute(
        "DELETE FROM playlist_order WHERE playlist_id = ? AND (episode_id = ? OR episode_id IN "
        "(SELECT id FROM episodes WHERE played = 1))",
        (pid, episode_id),
    )
    edge = conn.execute(
        "SELECT COALESCE(MIN(position), 1) AS lo, COALESCE(MAX(position), 0) AS hi FROM playlist_order WHERE playlist_id = ?",
        (pid,),
    ).fetchone()
    position = edge["lo"] - 1 if first else edge["hi"] + 1
    conn.execute(
        "INSERT INTO playlist_order (playlist_id, episode_id, position) VALUES (?, ?, ?)", (pid, episode_id, position)
    )
    conn.commit()


def queue_remove(conn, episode_id):
    conn.execute("DELETE FROM playlist_order WHERE playlist_id = ? AND episode_id = ?", (queue_id(conn), episode_id))
    conn.commit()
