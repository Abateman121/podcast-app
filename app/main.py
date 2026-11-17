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
import overcast
import playlists
import repeats
import sources
import workers
from progress import ERRORS, PLAYING, PROGRESS

QUEUE_STATUSES = ("queued", "downloading", "downloaded", "scanning", "error", "scan_error")

app = FastAPI(title="Podcast App")


@app.on_event("startup")
def startup():
    db.init()
    conn = db.connect()
    playlists.setup(conn)
    conn.close()
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
    "COALESCE(e.starred, 0) AS starred, COALESCE(e.removed, 0) AS removed, "
    "COALESCE(s.intro_skip, 0) AS intro_skip, COALESCE(s.outro_skip, 0) AS outro_skip"
)


def episodes_by_id(conn, ids):
    """Full episode dicts for these ids, in the order given."""
    ids = list(ids)
    if not ids:
        return []
    rows = []
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        marks = ",".join("?" for _ in chunk)
        rows.extend(conn.execute(
            f"SELECT {EP_COLS} FROM episodes e JOIN shows s ON s.id = e.show_id WHERE e.id IN ({marks})", chunk
        ).fetchall())
    by_id = {d["id"]: d for d in episodes_payload(conn, rows)}
    return [by_id[i] for i in ids if i in by_id]


def next_payload(conn, candidates, continuous=True):
    """What the player should do after the current episode.

    next: the first candidate that can play now. pending: the one that should have been next but is
    still on its way. Asking also puts that one at the front of the download and scan queues.
    """
    wanted, playable = playlists.pick_next(candidates)
    playlists.prepare(conn, wanted)
    pending = None
    if wanted is not None and (playable is None or playable["id"] != wanted["id"]):
        found = episodes_by_id(conn, [wanted["id"]])
        if found:
            pending = {"id": found[0]["id"], "title": found[0]["title"], "status": found[0]["status"]}
    found = episodes_by_id(conn, [playable["id"]]) if playable is not None else []
    return {"next": found[0] if found else None, "pending": pending, "continuous": bool(continuous)}


def run(fn, *args):
    """Call a playlists function with a fresh connection and turn its errors into HTTP errors."""
    conn = db.connect()
    try:
        return fn(conn, *args)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    finally:
        conn.close()


# ---------- models ----------

class UrlIn(BaseModel):
    url: str


class SkipsIn(BaseModel):
    intro_skip: int = 0
    outro_skip: int = 0


class LimitsIn(BaseModel):
    newer_than_days: int = 0
    keep_last: Optional[int] = None   # None = use the default


class FlagIn(BaseModel):
    on: bool = True


class PlaylistBody(BaseModel):
    title: Optional[str] = None
    icon: Optional[str] = None
    color: Optional[str] = None
    sort: Optional[str] = None
    priority_mode: Optional[str] = None
    include_all_shows: Optional[bool] = None
    play_top_next: Optional[bool] = None
    continuous: Optional[bool] = None
    position: Optional[int] = None


class PlaylistShowIn(BaseModel):
    show_id: int
    priority: str = "normal"
    rank: Optional[int] = None


class PlaylistShowsIn(BaseModel):
    shows: list[PlaylistShowIn]


class OrderIn(BaseModel):
    episode_ids: list[int]


class OverrideIn(BaseModel):
    episode_id: int
    action: str


class AcceptIn(BaseModel):
    kind: str


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


class AdjustIn(BaseModel):
    old_start: float
    old_end: float
    start: float
    end: float


class ProgressIn(BaseModel):
    position: float
    played: bool = False


# ---------- shows ----------

@app.get("/api/shows")
def list_shows():
    conn = db.connect()
    rows = conn.execute(
        "SELECT id, title, image, feed_url, COALESCE(intro_skip, 0) AS intro_skip, "
        "COALESCE(outro_skip, 0) AS outro_skip, intro_template_len, outro_template_len, "
        "COALESCE(newer_than_days, 0) AS newer_than_days, keep_last, COALESCE(subscribed, 1) AS subscribed "
        "FROM shows ORDER BY title"
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["keep_default"] = feeds.AUTO_DOWNLOAD_LATEST
        out.append(d)
    return out


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


@app.post("/api/shows/{show_id}/limits")
def set_limits(show_id: int, body: LimitsIn):
    """newer_than_days: playlists only take episodes this recent (0 = no limit).
    keep_last: how many unplayed episodes are kept downloaded (empty = the default, 0 = none)."""
    days = max(0, min(3650, body.newer_than_days))
    keep = None if body.keep_last is None else max(0, min(1000, body.keep_last))
    conn = db.connect()
    cur = conn.execute("UPDATE shows SET newer_than_days = ?, keep_last = ? WHERE id = ?", (days, keep, show_id))
    conn.commit()
    queued = feeds.queue_latest(conn) if cur.rowcount else 0
    conn.close()
    if cur.rowcount == 0:
        raise HTTPException(404, "Show not found")
    return {"newer_than_days": days, "keep_last": keep, "queued": queued}


@app.post("/api/shows/{show_id}/subscribe")
def subscribe_show(show_id: int):
    return set_subscribed(show_id, 1)


@app.post("/api/shows/{show_id}/unsubscribe")
def unsubscribe_show(show_id: int):
    """Stop following a show. Its episodes and history stay, but it leaves the playlists and gets no new downloads."""
    return set_subscribed(show_id, 0)


def set_subscribed(show_id, value):
    conn = db.connect()
    cur = conn.execute("UPDATE shows SET subscribed = ? WHERE id = ?", (value, show_id))
    if not value:
        # Downloads that were waiting for this show are no longer wanted.
        conn.execute("UPDATE episodes SET status = 'new', dl_priority = 0 WHERE show_id = ? AND status = 'queued'", (show_id,))
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        raise HTTPException(404, "Show not found")
    if value:
        workers.wake.set()
    return {"subscribed": bool(value)}


@app.get("/api/shows/{show_id}/playlists")
def show_playlists(show_id: int):
    """Every smart playlist, with whether this show is in it."""
    conn = db.connect()
    rows = conn.execute(
        "SELECT p.id, p.name AS title, p.include_all_shows, "
        "EXISTS(SELECT 1 FROM playlist_shows ps WHERE ps.playlist_id = p.id AND ps.show_id = ?) AS member "
        "FROM playlists p WHERE p.kind = 'smart' ORDER BY p.position, p.id",
        (show_id,),
    ).fetchall()
    conn.close()
    return [
        {"id": r["id"], "title": r["title"], "all_shows": bool(r["include_all_shows"]), "member": bool(r["member"])}
        for r in rows
    ]


@app.post("/api/shows/{show_id}/playlists/{playlist_id}")
def show_playlist_toggle(show_id: int, playlist_id: int, body: FlagIn):
    run(playlists.set_show_member, playlist_id, show_id, body.on)
    return {"member": body.on}


@app.get("/api/shows/{show_id}/next")
def show_next(show_id: int, after: int):
    conn = db.connect()
    try:
        return next_payload(conn, playlists.next_in_show(conn, show_id, after))
    finally:
        conn.close()


@app.get("/api/shows/{show_id}/repeats")
def show_repeats(show_id: int):
    """Sections of audio found in more than one episode, waiting for the user to say what they are."""
    conn = db.connect()
    try:
        return repeats.show_status(conn, show_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    finally:
        conn.close()


@app.post("/api/shows/{show_id}/repeats/scan")
def scan_repeats(show_id: int):
    conn = db.connect()
    repeats.rescan(conn, show_id)
    conn.close()
    return {"ok": True}


@app.post("/api/repeats/{repeat_id}/accept")
def accept_repeat(repeat_id: int, body: AcceptIn):
    conn = db.connect()
    try:
        return repeats.accept(conn, repeat_id, body.kind)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    finally:
        conn.close()


@app.post("/api/repeats/{repeat_id}/ignore")
def ignore_repeat(repeat_id: int):
    conn = db.connect()
    try:
        repeats.ignore(conn, repeat_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    finally:
        conn.close()
    return {"ok": True}


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


class OvercastRunIn(BaseModel):
    dry: bool = True
    keep_days: Optional[int] = 14   # None keeps every feed episode on the lists


@app.post("/api/import/overcast")
async def upload_overcast(request: Request):
    """Step 1: upload the Overcast "All data" export. It is checked and kept for the job. Nothing is imported yet."""
    data = await request.body()
    try:
        return overcast.save_upload(data)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/import/overcast/run")
def run_overcast(body: OvercastRunIn):
    """Step 2: start the background job. dry = compare with the feeds and report, save nothing."""
    keep_days = None if body.keep_days is None else max(0, min(3650, body.keep_days))
    try:
        overcast.start(body.dry, keep_days)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return overcast.status()


@app.get("/api/import/overcast/status")
def overcast_status():
    return overcast.status()


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


@app.post("/api/episodes/{episode_id}/ad-ranges/adjust")
def adjust_ad_range(episode_id: int, body: AdjustIn):
    """Move the start and end of a range that is being skipped."""
    conn = db.connect()
    try:
        result = adlearn.adjust_range(conn, episode_id, body.old_start, body.old_end, body.start, body.end)
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


def mark_played(conn, episode_id, played, position=None):
    """Set played, and remember when it was finished so the cleanup knows when the file may go."""
    now = int(time.time())
    if position is None:
        conn.execute(
            "UPDATE episodes SET played_at = CASE WHEN ? = 1 AND played = 0 THEN ? ELSE played_at END, played = ? WHERE id = ?",
            (played, now, played, episode_id),
        )
    else:
        conn.execute(
            "UPDATE episodes SET played_at = CASE WHEN ? = 1 AND played = 0 THEN ? ELSE played_at END, played = ?, "
            "position = ? WHERE id = ?",
            (played, now, played, position, episode_id),
        )
    conn.commit()


@app.post("/api/episodes/{episode_id}/progress")
def save_progress(episode_id: int, body: ProgressIn):
    PLAYING[episode_id] = time.time()
    conn = db.connect()
    mark_played(conn, episode_id, 1 if body.played else 0, body.position)
    conn.close()
    return {"ok": True}


@app.post("/api/episodes/{episode_id}/played")
def set_played(episode_id: int, body: FlagIn):
    """Mark played or unplayed from a list, without touching the position."""
    conn = db.connect()
    mark_played(conn, episode_id, 1 if body.on else 0)
    conn.close()
    return {"played": body.on}


@app.post("/api/episodes/{episode_id}/star")
def star_episode(episode_id: int, body: FlagIn):
    conn = db.connect()
    cur = conn.execute("UPDATE episodes SET starred = ? WHERE id = ?", (1 if body.on else 0, episode_id))
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        raise HTTPException(404, "Episode not found")
    return {"starred": body.on}


@app.post("/api/episodes/{episode_id}/remove")
def remove_episode(episode_id: int, body: FlagIn):
    """Take an episode off the playlists (or put it back). Its file goes with the normal cleanup."""
    conn = db.connect()
    now = int(time.time())
    cur = conn.execute(
        "UPDATE episodes SET removed = ?, played_at = CASE WHEN ? = 1 AND played = 0 THEN ? ELSE played_at END, "
        "status = CASE WHEN ? = 1 AND status = 'queued' THEN 'new' ELSE status END WHERE id = ?",
        (1 if body.on else 0, 1 if body.on else 0, now, 1 if body.on else 0, episode_id),
    )
    conn.commit()
    if cur.rowcount and body.on:
        playlists.queue_remove(conn, episode_id)
    conn.close()
    if cur.rowcount == 0:
        raise HTTPException(404, "Episode not found")
    return {"removed": body.on}


@app.post("/api/episodes/{episode_id}/play-next")
def play_next(episode_id: int):
    run(playlists.queue_add, episode_id, True)
    return {"ok": True}


@app.post("/api/episodes/{episode_id}/play-last")
def play_last(episode_id: int):
    run(playlists.queue_add, episode_id, False)
    return {"ok": True}


@app.post("/api/episodes/{episode_id}/unqueue")
def unqueue(episode_id: int):
    run(playlists.queue_remove, episode_id)
    return {"ok": True}


# ---------- playlists ----------

def body_fields(body):
    return {k: v for k, v in body.dict().items() if v is not None}


@app.get("/api/playlists")
def list_playlists():
    conn = db.connect()
    out = []
    for row in conn.execute("SELECT * FROM playlists ORDER BY position, id").fetchall():
        item = playlists.summary(row)
        item.update(playlists.counts(conn, row))
        out.append(item)
    conn.close()
    return out


@app.post("/api/playlists")
def create_playlist(body: PlaylistBody):
    title = (body.title or "").strip()
    if not title:
        raise HTTPException(400, "A playlist needs a title.")
    fields = body_fields(body)
    fields.pop("position", None)
    return {"id": run(playlists.create, title, fields)}


@app.get("/api/playlists/{playlist_id}")
def get_playlist(playlist_id: int):
    return run(playlists.settings, playlist_id)


@app.patch("/api/playlists/{playlist_id}")
def patch_playlist(playlist_id: int, body: PlaylistBody):
    run(playlists.update, playlist_id, body_fields(body))
    return run(playlists.settings, playlist_id)


@app.delete("/api/playlists/{playlist_id}")
def delete_playlist(playlist_id: int):
    run(playlists.delete, playlist_id)
    return {"ok": True}


@app.get("/api/playlists/{playlist_id}/episodes")
def playlist_episodes(playlist_id: int, offset: int = 0, limit: int = 200):
    """One page of the playlist in playing order."""
    offset = max(0, offset)
    limit = max(1, min(1000, limit))
    conn = db.connect()
    try:
        row = playlists.get(conn, playlist_id)
        items = playlists.ordered(conn, row)
        manual = {
            r["episode_id"]
            for r in conn.execute("SELECT episode_id FROM playlist_order WHERE playlist_id = ?", (playlist_id,))
        }
        page = episodes_by_id(conn, [e["id"] for e in items[offset:offset + limit]])
        for ep in page:
            ep["manual"] = ep["id"] in manual
        return {"total": len(items), "offset": offset, "episodes": page}
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    finally:
        conn.close()


@app.put("/api/playlists/{playlist_id}/shows")
def put_playlist_shows(playlist_id: int, body: PlaylistShowsIn):
    run(playlists.set_shows, playlist_id, [s.dict() for s in body.shows])
    return run(playlists.settings, playlist_id)


@app.put("/api/playlists/{playlist_id}/order")
def put_playlist_order(playlist_id: int, body: OrderIn):
    run(playlists.set_order, playlist_id, body.episode_ids)
    return {"ok": True}


@app.post("/api/playlists/{playlist_id}/overrides")
def post_playlist_override(playlist_id: int, body: OverrideIn):
    run(playlists.set_override, playlist_id, body.episode_id, body.action)
    return {"ok": True}


@app.get("/api/playlists/{playlist_id}/next")
def playlist_next(playlist_id: int, after: int):
    conn = db.connect()
    try:
        row = playlists.get(conn, playlist_id)
        return next_payload(conn, playlists.next_in_playlist(conn, playlist_id, after), row["continuous"])
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    finally:
        conn.close()


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
    PLAYING[episode_id] = time.time()
    return FileResponse(row["file_path"])


app.mount("/", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static"), html=True))
