import json
import os
import threading
import time

import adlearn
import adscan
import db
import feeds
import markers
import repeats
from progress import ERRORS, PLAYING, PROGRESS

REFRESH_MINUTES = int(os.environ.get("REFRESH_MINUTES", "30"))
CLEANUP_HOURS = float(os.environ.get("CLEANUP_HOURS", "12"))   # played files are deleted after this long. 0 = never
PLAYING_GRACE = 3600   # seconds an episode counts as "in the player" after the page last touched it
wake = threading.Event()


def next_episode(status):
    conn = db.connect()
    row = conn.execute(
        "SELECT id, show_id, file_path FROM episodes WHERE status = ? "
        "ORDER BY dl_priority DESC, published DESC LIMIT 1",
        (status,),
    ).fetchone()   # dl_priority: the episode that is next up in the player goes first
    conn.close()
    return row


def set_status(episode_id, status):
    conn = db.connect()
    conn.execute("UPDATE episodes SET status = ? WHERE id = ?", (status, episode_id))
    conn.commit()
    conn.close()


def refresh_loop():
    while True:
        try:
            print("[1/2] Refreshing feeds")
            feeds.refresh_all()
            conn = db.connect()
            queued = feeds.queue_latest(conn)
            conn.close()
            print(f"[2/2] Queued {queued} new episodes")
        except Exception as exc:
            print(f"refresh error: {exc}")
        wake.wait(timeout=REFRESH_MINUTES * 60)
        wake.clear()


def download_loop():
    while True:
        row = next_episode("queued")
        if row is None:
            time.sleep(3)
            continue
        episode_id = row["id"]
        set_status(episode_id, "downloading")
        conn = db.connect()
        try:
            print(f"[download] episode {episode_id} started")
            feeds.download_episode(conn, episode_id)
            print(f"[download] episode {episode_id} done")
        except Exception as exc:
            print(f"[download] episode {episode_id} failed: {exc}")
            ERRORS[episode_id] = str(exc)[:200]
            set_status(episode_id, "error")
        finally:
            PROGRESS.pop(episode_id, None)
            conn.close()


def scan_loop():
    while True:
        row = next_episode("downloaded")
        if row is None:
            time.sleep(3)
            continue
        episode_id = row["id"]
        set_status(episode_id, "scanning")
        PROGRESS[episode_id] = {"phase": "scanning", "fraction": 0.0}

        def report(fraction, eid=episode_id):
            PROGRESS[eid] = {"phase": "scanning", "fraction": fraction}

        try:
            print(f"[scan] episode {episode_id} started")
            conn = db.connect()
            anchors = adlearn.load_anchors(conn, row["show_id"])
            conn.close()
            ranges, segments = adscan.scan(row["file_path"], report, anchors)
            with open(db.transcript_path(episode_id), "w") as fh:
                json.dump(segments, fh)
            conn = db.connect()
            conn.execute(
                "UPDATE episodes SET ads_json = ?, status = 'ready', dl_priority = 0 WHERE id = ? AND status = 'scanning'",
                (json.dumps(ranges), episode_id),
            )
            conn.commit()
            conn.close()
            print(f"[scan] episode {episode_id} done, {len(ranges)} ads")
        except Exception as exc:
            print(f"[scan] episode {episode_id} failed: {exc}")
            ERRORS[episode_id] = str(exc)[:200]
            set_status(episode_id, "scan_error")
        finally:
            PROGRESS.pop(episode_id, None)


def process_one_marker():
    """Find the intro and outro of the newest episode that has not been analysed yet.

    CPU only, and separate from scan_loop, so playback and Whisper are never held up.
    Returns False when there is nothing to do.
    """
    conn = db.connect()
    try:
        marks = ",".join("?" for _ in db.PLAYABLE)
        row = conn.execute(
            "SELECT id, show_id, file_path, duration, chapters_url FROM episodes "
            f"WHERE file_path IS NOT NULL AND markers_json = '{{}}' AND status IN ({marks}) "
            "ORDER BY published DESC LIMIT 1",
            db.PLAYABLE,
        ).fetchone()
        if row is None:
            return False
        generation = db.meta_get(conn, "marker_gen")
        try:
            result = markers.detect(conn, row)
            print(
                f"[markers] episode {row['id']}: intro {result['intro_source']} "
                f"({result['intro_confidence']}), outro {result['outro_source']} ({result['outro_confidence']})"
            )
        except Exception as exc:
            print(f"[markers] episode {row['id']} failed: {exc}")
            result = markers.empty_markers()
            result["error"] = str(exc)[:200]
        # If a template changed while this ran, drop the result. The episode is picked up again.
        if db.meta_get(conn, "marker_gen") == generation:
            conn.execute("UPDATE episodes SET markers_json = ? WHERE id = ?", (json.dumps(result), row["id"]))
            conn.commit()
        return True
    finally:
        conn.close()


def marker_loop():
    while True:
        try:
            if not process_one_marker():
                time.sleep(3)
        except Exception as exc:
            print(f"[markers] loop error: {exc}")
            time.sleep(3)


def process_one_ad_audio():
    """Look for hand-tagged ads (by audio) in the newest episode not yet checked against the current set.

    CPU only. A new or removed tag raises the bank version, which makes every episode due again.
    Returns False when there is nothing to do.
    """
    conn = db.connect()
    try:
        version = db.meta_get(conn, adlearn.BANK_KEY)
        marks = ",".join("?" for _ in db.PLAYABLE)
        row = conn.execute(
            "SELECT id, file_path FROM episodes "
            f"WHERE file_path IS NOT NULL AND status IN ({marks}) AND ads_audio_ver < ? "
            "ORDER BY published DESC LIMIT 1",
            list(db.PLAYABLE) + [version],
        ).fetchone()
        if row is None:
            return False
        ranges = []
        try:
            if os.path.exists(row["file_path"]):
                ranges = adlearn.match_episode(row["file_path"], adlearn.load_bank(conn))
            print(f"[ad-audio] episode {row['id']}: {len(ranges)} tagged ads found by audio")
        except Exception as exc:
            print(f"[ad-audio] episode {row['id']} failed: {exc}")
        conn.execute(
            "UPDATE episodes SET ads_audio_json = ?, ads_audio_ver = ? WHERE id = ?",
            (json.dumps(ranges), version, row["id"]),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def ad_audio_loop():
    while True:
        try:
            if not process_one_ad_audio():
                time.sleep(3)
        except Exception as exc:
            print(f"[ad-audio] loop error: {exc}")
            time.sleep(3)


def repeat_loop():
    """Compare the newest episodes of each show for repeated audio. CPU only, and never in a hurry."""
    time.sleep(20)
    while True:
        try:
            conn = db.connect()
            try:
                busy = repeats.process_one(conn)
            finally:
                conn.close()
            time.sleep(2 if busy else 30)
        except Exception as exc:
            print(f"[repeats] loop error: {exc}")
            time.sleep(30)


def cleanup_played(conn, now=None):
    """Delete the audio of episodes that were finished (or removed) more than CLEANUP_HOURS ago.

    Starred episodes and unplayed episodes are never touched. The row, the transcript, the tags and
    the learned ad samples stay, so the episode can be downloaded again. Returns the ids cleaned up.
    """
    if CLEANUP_HOURS <= 0:
        return []
    now = now or time.time()
    rows = conn.execute(
        "SELECT id, file_path FROM episodes WHERE (played = 1 OR removed = 1) AND starred = 0 "
        "AND file_path IS NOT NULL AND status IN ('downloaded', 'ready', 'scan_error') AND COALESCE(played_at, 0) <= ?",
        (int(now - CLEANUP_HOURS * 3600),),
    ).fetchall()
    cleaned = []
    for row in rows:
        if now - PLAYING.get(row["id"], 0) < PLAYING_GRACE:
            continue   # loaded in the player right now
        cur = conn.execute(
            "UPDATE episodes SET status = 'new', file_path = NULL, dl_priority = 0 WHERE id = ? "
            "AND (played = 1 OR removed = 1) AND starred = 0 AND status IN ('downloaded', 'ready', 'scan_error')",
            (row["id"],),
        )
        conn.commit()
        if not cur.rowcount:
            continue
        try:
            if os.path.exists(row["file_path"]):
                os.remove(row["file_path"])
            print(f"[cleanup] episode {row['id']}: deleted {row['file_path']}")
        except OSError as exc:
            print(f"[cleanup] episode {row['id']}: could not delete {row['file_path']}: {exc}")
        cleaned.append(row["id"])
    return cleaned


def cleanup_loop():
    time.sleep(60)
    while True:
        try:
            conn = db.connect()
            try:
                cleanup_played(conn)
            finally:
                conn.close()
        except Exception as exc:
            print(f"[cleanup] loop error: {exc}")
        time.sleep(300)


def start():
    for target in (refresh_loop, download_loop, scan_loop, marker_loop, ad_audio_loop, repeat_loop, cleanup_loop):
        threading.Thread(target=target, daemon=True).start()
