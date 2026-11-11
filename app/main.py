import json
import os
import time
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import adlearn
import adscan
import db
import feeds
import markers
import sources
import workers
from progress import ERRORS, PROGRESS

PLAYABLE_SQL = "(" + ",".join(f"'{s}'" for s in db.PLAYABLE) + ")"
QUEUE_STATUSES = ("queued", "downloading", "downloaded", "scanning", "error", "scan_error")

app = FastAPI(title="Podcast App")


@app.on_event("startup")
def startup():
    db.init()
    if os.environ.get("DISABLE_WORKERS") != "1":
        workers.start()


# ---------- helpers ----------

def episode_dict(row, tags=()):
    d = dict(row)
    detected = json.loads(d.pop("ads_json") or "[]")
    audio = json.loads(d.pop("ads_audio_json") or "[]")
    d["ads"] = adlearn.effective_ads(detected, audio, tags)
    d["markers"] = markers.player_markers(d.pop("markers_json", None))
    d["playable"] = d["status"] in db.PLAYABLE
    d.pop("file_path", None)
    return d


def episodes_payload(conn, rows):
    """Episode dicts with hand-tagged ads folded into the skip list."""
    tags = adlearn.tags_by_episode(conn, [r["id"] for r in rows])
    return [episode_dict(r, tags.get(r["id"], [])) for r in rows]


EP_COLS = (
    "e.id, e.show_id, s.title AS show_title, s.image AS show_image, e.title, e.published, "
    "e.duration, e.status, e.played, e.position, e.ads_json, e.ads_audio_json, e.markers_json, e.file_path, "
    "COALESCE(s.intro_skip, 0) AS intro_skip, COALESCE(s.outro_skip, 0) AS outro_skip"
)


def playlist_query(rules):
    where = [f"e.status IN {PLAYABLE_SQL}"]
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


# ---------- models ----------

class UrlIn(BaseModel):
    url: str


class SkipsIn(BaseModel):
    intro_skip: int = 0
    outro_skip: int = 0


class TemplatesIn(BaseModel):
    episode_id: int
    intro_start: Optional[float] = None
    intro_end: Optional[float] = None
    outro_start: Optional[float] = None
    outro_end: Optional[float] = None


class TagIn(BaseModel):
    start: float
    end: float
    kind: str = "ad"


class ProgressIn(BaseModel):
    position: float
    played: bool = False


class PlaylistIn(BaseModel):
    name: str
    rules: dict


# ---------- shows ----------

@app.get("/api/shows")
def list_shows():
    conn = db.connect()
    rows = conn.execute(
        "SELECT id, title, image, feed_url, COALESCE(intro_skip, 0) AS intro_skip, "
        "COALESCE(outro_skip, 0) AS outro_skip, intro_template_len, outro_template_len FROM shows ORDER BY title"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/shows")
def add_show(body: UrlIn):
    try:
        result = feeds.add_show_from_url(body.url)
    except sources.SourceError as exc:
        raise HTTPException(400, str(exc))
    workers.wake.set()
    return result


@app.post("/api/shows/{show_id}/skips")
def set_skips(show_id: int, body: SkipsIn):
    intro = max(0, min(600, body.intro_skip))
    outro = max(0, min(600, body.outro_skip))
    conn = db.connect()
    cur = conn.execute("UPDATE shows SET intro_skip = ?, outro_skip = ? WHERE id = ?", (intro, outro, show_id))
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        raise HTTPException(404, "Show not found")
    return {"intro_skip": intro, "outro_skip": outro}


@app.post("/api/shows/{show_id}/templates")
def set_templates(show_id: int, body: TemplatesIn):
    """Create intro and/or outro templates from marks on one downloaded episode, then re-detect the whole show."""
    conn = db.connect()
    try:
        ep = conn.execute(
            "SELECT file_path FROM episodes WHERE id = ? AND show_id = ?", (body.episode_id, show_id)
        ).fetchone()
        if not ep or not ep["file_path"] or not os.path.exists(ep["file_path"]):
            raise HTTPException(404, "That episode is not downloaded.")
        todo = [
            (kind, start, end)
            for kind, start, end in (
                ("intro", body.intro_start, body.intro_end),
                ("outro", body.outro_start, body.outro_end),
            )
            if start is not None and end is not None
        ]
        if not todo:
            raise HTTPException(400, "Mark a start and an end for the intro, the outro, or both.")
        saved = []
        try:
            for kind, start, end in todo:
                saved.append(markers.create_template(conn, show_id, kind, ep["file_path"], start, end))
        except ValueError as exc:
            if saved:
                markers.queue_redetect(conn, show_id)
                raise HTTPException(400, f"Saved the {saved[0]['kind']} template, but: {exc}")
            raise HTTPException(400, str(exc))
        return {"saved": saved, "queued": markers.queue_redetect(conn, show_id)}
    finally:
        conn.close()


@app.delete("/api/shows/{show_id}/templates/{kind}")
def delete_template(show_id: int, kind: str):
    conn = db.connect()
    try:
        markers.delete_template(conn, show_id, kind)
        queued = markers.queue_redetect(conn, show_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    finally:
        conn.close()
    return {"ok": True, "queued": queued}


@app.post("/api/shows/{show_id}/redetect")
def redetect_show(show_id: int):
    """Redo intro/outro detection for a show. Uses the CPU only, never Whisper."""
    conn = db.connect()
    queued = markers.queue_redetect(conn, show_id)
    conn.close()
    return {"queued": queued}


@app.get("/api/shows/{show_id}/ad-learning")
def show_ad_learning(show_id: int):
    conn = db.connect()
    summary = adlearn.show_summary(conn, show_id)
    conn.close()
    return summary


@app.delete("/api/shows/{show_id}")
def delete_show(show_id: int):
    conn = db.connect()
    removed = feeds.delete_show_files(conn, show_id)
    had_tags = conn.execute("SELECT 1 FROM ad_tags WHERE show_id = ? AND kind = 'ad' LIMIT 1", (show_id,)).fetchone()
    conn.execute("DELETE FROM shows WHERE id = ?", (show_id,))
    if had_tags:
        db.meta_bump(conn, adlearn.BANK_KEY)
    conn.commit()
    conn.close()
    return {"ok": True, "files_removed": removed}


@app.post("/api/import/opml")
async def import_opml(request: Request):
    text = (await request.body()).decode("utf-8", errors="replace")
    try:
        result = feeds.import_opml_text(text)
    except Exception:
        raise HTTPException(400, "That file is not a valid OPML file.")
    if result["total"] == 0:
        raise HTTPException(400, "No podcast feeds were found in that OPML file.")
    workers.wake.set()
    return result


# ---------- episodes and queue ----------

@app.get("/api/episodes")
def list_episodes(show_id: int, limit: int = 100):
    conn = db.connect()
    rows = conn.execute(
        f"SELECT {EP_COLS} FROM episodes e JOIN shows s ON s.id = e.show_id "
        "WHERE e.show_id = ? ORDER BY e.published DESC LIMIT ?",
        (show_id, limit),
    ).fetchall()
    payload = episodes_payload(conn, rows)
    conn.close()
    return payload


@app.get("/api/episodes/{episode_id}")
def get_episode(episode_id: int):
    conn = db.connect()
    row = conn.execute(
        f"SELECT {EP_COLS} FROM episodes e JOIN shows s ON s.id = e.show_id WHERE e.id = ?", (episode_id,)
    ).fetchone()
    payload = episodes_payload(conn, [row]) if row else None
    conn.close()
    if not payload:
        raise HTTPException(404, "Episode not found")
    return payload[0]


def load_transcript(episode_id):
    path = db.transcript_path(episode_id)
    if not os.path.exists(path):
        return None
    with open(path) as fh:
        return json.load(fh)


@app.post("/api/episodes/{episode_id}/rescan")
def rescan_episode(episode_id: int):
    """Re-apply the current ad rules. Uses the saved transcript when there is one, otherwise queues a new scan."""
    segments = load_transcript(episode_id)
    conn = db.connect()
    try:
        if segments is not None:
            show = conn.execute("SELECT show_id FROM episodes WHERE id = ?", (episode_id,)).fetchone()
            anchors = adlearn.load_anchors(conn, show["show_id"]) if show else []
            ranges = adscan.find_ad_ranges(segments, anchors)
            conn.execute(
                "UPDATE episodes SET ads_json = ?, status = 'ready' WHERE id = ?", (json.dumps(ranges), episode_id)
            )
            conn.commit()
            return {"mode": "saved transcript", "ads": len(ranges)}
        conn.execute(
            "UPDATE episodes SET status = 'downloaded' WHERE id = ? AND file_path IS NOT NULL "
            "AND status IN ('ready', 'scan_error')",
            (episode_id,),
        )
        conn.commit()
        return {"mode": "new scan queued"}
    finally:
        conn.close()


@app.get("/api/episodes/{episode_id}/debug")
def debug_episode(episode_id: int):
    segments = load_transcript(episode_id)
    if segments is None:
        raise HTTPException(404, "No saved transcript yet. Use Re-scan ads on the show page.")
    conn = db.connect()
    show = conn.execute("SELECT show_id FROM episodes WHERE id = ?", (episode_id,)).fetchone()
    anchors = adlearn.load_anchors(conn, show["show_id"]) if show else []
    conn.close()
    return {
        "segments": len(segments),
        "ranges": adscan.find_ad_ranges(segments, anchors),
        "hits": adscan.debug_hits(segments),
        "learned_phrase_hits": adscan.anchor_hits(segments, anchors),
    }


@app.get("/api/episodes/{episode_id}/markers")
def debug_markers(episode_id: int):
    """Intro/outro detection result for one episode, and what the player will actually skip."""
    conn = db.connect()
    row = conn.execute(
        "SELECT e.markers_json, e.duration, s.intro_template_len, s.outro_template_len, "
        "COALESCE(s.intro_skip, 0) AS intro_skip, COALESCE(s.outro_skip, 0) AS outro_skip "
        "FROM episodes e JOIN shows s ON s.id = e.show_id WHERE e.id = ?",
        (episode_id,),
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Episode not found")
    found = json.loads(row["markers_json"] or "{}")
    duration = found.get("duration") or row["duration"] or 0
    return {
        "analysed": bool(found),
        "markers": found,
        "player_will_use": markers.effective(found, row["intro_skip"], row["outro_skip"], duration),
        "show_templates": {"intro_seconds": row["intro_template_len"], "outro_seconds": row["outro_template_len"]},
        "confidence_threshold": markers.SKIP_CONFIDENCE,
        "notes": markers.transcript_notes(found, load_transcript(episode_id) or []),
    }


# ---------- hand-tagged ads ----------

@app.get("/api/episodes/{episode_id}/ad-tags")
def get_ad_tags(episode_id: int):
    conn = db.connect()
    tags = adlearn.list_tags(conn, episode_id)
    conn.close()
    return tags


@app.post("/api/episodes/{episode_id}/ad-tags")
def add_ad_tag(episode_id: int, body: TagIn):
    conn = db.connect()
    try:
        result = adlearn.add_tag(conn, episode_id, body.start, body.end, body.kind)
        return {"id": result["id"], "note": result["note"], "ads": adlearn.current_ads(conn, episode_id)}
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    finally:
        conn.close()


@app.delete("/api/ad-tags/{tag_id}")
def remove_ad_tag(tag_id: int):
    conn = db.connect()
    try:
        episode_id = adlearn.delete_tag(conn, tag_id)
        return {"ok": True, "episode_id": episode_id, "ads": adlearn.current_ads(conn, episode_id)}
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    finally:
        conn.close()


@app.post("/api/episodes/{episode_id}/download")
def want_episode(episode_id: int):
    conn = db.connect()
    conn.execute("UPDATE episodes SET status = 'queued' WHERE id = ? AND status IN ('new', 'error')", (episode_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/episodes/{episode_id}/retry")
def retry_episode(episode_id: int):
    ERRORS.pop(episode_id, None)
    conn = db.connect()
    conn.execute("UPDATE episodes SET status = 'queued' WHERE id = ? AND status = 'error'", (episode_id,))
    conn.execute("UPDATE episodes SET status = 'downloaded' WHERE id = ? AND status = 'scan_error'", (episode_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/queue")
def queue():
    marks = ",".join("?" for _ in QUEUE_STATUSES)
    conn = db.connect()
    rows = conn.execute(
        f"SELECT e.id, e.title, s.title AS show_title, e.status FROM episodes e JOIN shows s ON s.id = e.show_id "
        f"WHERE e.status IN ({marks}) "
        "ORDER BY CASE e.status WHEN 'downloading' THEN 0 WHEN 'scanning' THEN 1 WHEN 'queued' THEN 2 "
        "WHEN 'downloaded' THEN 3 ELSE 4 END, e.published DESC LIMIT 200",
        QUEUE_STATUSES,
    ).fetchall()
    conn.close()
    items = []
    for r in rows:
        item = dict(r)
        info = PROGRESS.get(r["id"], {})
        done, total = info.get("done"), info.get("total")
        if "fraction" in info:
            item["fraction"] = info["fraction"]
        elif total:
            item["fraction"] = min(1.0, done / total)
        else:
            item["fraction"] = None
        item["done"] = done
        item["total"] = total
        item["error"] = ERRORS.get(r["id"])
        items.append(item)
    return items


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


# ---------- playlists ----------

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
    payload = episodes_payload(conn, rows)
    conn.close()
    return payload


@app.post("/api/refresh")
def refresh_now():
    workers.wake.set()
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
