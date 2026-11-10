import json
import os
import threading
import time

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import adscan
import db
import feeds

REFRESH_MINUTES = int(os.environ.get("REFRESH_MINUTES", "30"))
AUTO_DOWNLOAD_LATEST = int(os.environ.get("AUTO_DOWNLOAD_LATEST", "3"))
PLAYABLE = "('downloaded', 'ready')"

app = FastAPI(title="Podcast App")
wake = threading.Event()


# ---------- background worker ----------

def run_cycle():
    print("[1/3] Refreshing feeds")
    feeds.refresh_all()
    conn = db.connect()
    todo = feeds.episodes_to_download(conn, AUTO_DOWNLOAD_LATEST)
    print(f"[2/3] Downloading {len(todo)} episodes")
    for i, episode_id in enumerate(todo, 1):
        try:
            print(f"    ({i}/{len(todo)}) episode {episode_id}")
            feeds.download_episode(conn, episode_id)
        except Exception as exc:
            print(f"    download error {episode_id}: {exc}")
            conn.execute("UPDATE episodes SET status = 'error' WHERE id = ?", (episode_id,))
            conn.commit()
    pending = conn.execute("SELECT id, file_path FROM episodes WHERE status = 'downloaded'").fetchall()
    print(f"[3/3] Scanning {len(pending)} episodes for ads")
    for i, row in enumerate(pending, 1):
        try:
            print(f"    ({i}/{len(pending)}) episode {row['id']}")
            ranges = adscan.scan(row["file_path"])
            conn.execute(
                "UPDATE episodes SET ads_json = ?, status = 'ready' WHERE id = ?",
                (json.dumps(ranges), row["id"]),
            )
            conn.commit()
        except Exception as exc:
            print(f"    scan error {row['id']}: {exc}")
            conn.execute("UPDATE episodes SET status = 'ready' WHERE id = ?", (row["id"],))
            conn.commit()
    conn.close()


def worker_loop():
    while True:
        try:
            run_cycle()
        except Exception as exc:
            print(f"cycle error: {exc}")
        wake.wait(timeout=REFRESH_MINUTES * 60)
        wake.clear()


@app.on_event("startup")
def startup():
    db.init()
    threading.Thread(target=worker_loop, daemon=True).start()


# ---------- helpers ----------

def episode_dict(row):
    d = dict(row)
    d["ads"] = json.loads(d.pop("ads_json") or "[]")
    d["playable"] = d["status"] in ("downloaded", "ready")
    d.pop("file_path", None)
    return d


EP_COLS = (
    "e.id, e.show_id, s.title AS show_title, s.image AS show_image, e.title, e.published, "
    "e.duration, e.status, e.played, e.position, e.ads_json, e.file_path"
)


def playlist_query(rules):
    where = [f"e.status IN {PLAYABLE}"]
    params = []
    if rules.get("show_ids"):
        marks = ",".join("?" for _ in rules["show_ids"])
        where.append(f"e.show_id IN ({marks})")
        params.extend(rules["show_ids"])
    if rules.get("unplayed_only", True):
        where.append("e.played = 0")
    if rules.get("max_minutes"):
        where.append("e.duration > 0 AND e.duration <= ?")
        params.append(int(rules["max_minutes"]) * 60)
    if rules.get("min_minutes"):
        where.append("e.duration >= ?")
        params.append(int(rules["min_minutes"]) * 60)
    if rules.get("newer_than_days"):
        where.append("e.published >= ?")
        params.append(int(time.time()) - int(rules["newer_than_days"]) * 86400)
    order = "ASC" if rules.get("sort") == "oldest" else "DESC"
    limit = int(rules.get("limit", 50))
    sql = (
        f"SELECT {EP_COLS} FROM episodes e JOIN shows s ON s.id = e.show_id "
        f"WHERE {' AND '.join(where)} ORDER BY e.published {order} LIMIT ?"
    )
    params.append(limit)
    return sql, params


# ---------- API ----------

class FeedIn(BaseModel):
    feed_url: str


class ProgressIn(BaseModel):
    position: float
    played: bool = False


class PlaylistIn(BaseModel):
    name: str
    rules: dict


@app.get("/api/shows")
def list_shows():
    conn = db.connect()
    rows = conn.execute("SELECT id, title, image, feed_url FROM shows ORDER BY title").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/shows")
def add_show(body: FeedIn):
    show_id = feeds.add_feed(body.feed_url.strip())
    wake.set()
    return {"id": show_id}


@app.delete("/api/shows/{show_id}")
def delete_show(show_id: int):
    conn = db.connect()
    conn.execute("DELETE FROM shows WHERE id = ?", (show_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/episodes")
def list_episodes(show_id: int, limit: int = 100):
    conn = db.connect()
    rows = conn.execute(
        f"SELECT {EP_COLS} FROM episodes e JOIN shows s ON s.id = e.show_id "
        "WHERE e.show_id = ? ORDER BY e.published DESC LIMIT ?",
        (show_id, limit),
    ).fetchall()
    conn.close()
    return [episode_dict(r) for r in rows]


@app.post("/api/episodes/{episode_id}/download")
def want_episode(episode_id: int):
    conn = db.connect()
    conn.execute("UPDATE episodes SET wanted = 1 WHERE id = ?", (episode_id,))
    conn.commit()
    conn.close()
    wake.set()
    return {"ok": True}


@app.post("/api/episodes/{episode_id}/progress")
def save_progress(episode_id: int, body: ProgressIn):
    conn = db.connect()
    conn.execute(
        "UPDATE episodes SET position = ?, played = ? WHERE id = ?",
        (body.position, 1 if body.played else 0, episode_id),
    )
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/playlists")
def list_playlists():
    conn = db.connect()
    rows = conn.execute("SELECT id, name, rules_json FROM playlists ORDER BY id").fetchall()
    conn.close()
    return [{"id": r["id"], "name": r["name"], "rules": json.loads(r["rules_json"])} for r in rows]


@app.post("/api/playlists")
def create_playlist(body: PlaylistIn):
    conn = db.connect()
    cur = conn.execute(
        "INSERT INTO playlists (name, rules_json) VALUES (?, ?)", (body.name, json.dumps(body.rules))
    )
    conn.commit()
    conn.close()
    return {"id": cur.lastrowid}


@app.delete("/api/playlists/{playlist_id}")
def delete_playlist(playlist_id: int):
    conn = db.connect()
    conn.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/playlists/{playlist_id}/episodes")
def playlist_episodes(playlist_id: int):
    conn = db.connect()
    pl = conn.execute("SELECT rules_json FROM playlists WHERE id = ?", (playlist_id,)).fetchone()
    if not pl:
        raise HTTPException(404, "Playlist not found")
    sql, params = playlist_query(json.loads(pl["rules_json"]))
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [episode_dict(r) for r in rows]


@app.post("/api/refresh")
def refresh_now():
    wake.set()
    return {"ok": True}


@app.get("/audio/{episode_id}")
def audio(episode_id: int):
    conn = db.connect()
    row = conn.execute("SELECT file_path FROM episodes WHERE id = ?", (episode_id,)).fetchone()
    conn.close()
    if not row or not row["file_path"] or not os.path.exists(row["file_path"]):
        raise HTTPException(404, "Audio not downloaded yet")
    return FileResponse(row["file_path"])


app.mount("/", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static"), html=True))
