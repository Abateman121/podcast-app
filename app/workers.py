import json
import os
import threading
import time

import adscan
import db
import feeds
from progress import ERRORS, PROGRESS

REFRESH_MINUTES = int(os.environ.get("REFRESH_MINUTES", "30"))
wake = threading.Event()


def next_episode(status):
    conn = db.connect()
    row = conn.execute(
        "SELECT id, file_path FROM episodes WHERE status = ? ORDER BY published DESC LIMIT 1", (status,)
    ).fetchone()
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
            ranges = adscan.scan(row["file_path"], report)
            conn = db.connect()
            conn.execute(
                "UPDATE episodes SET ads_json = ?, status = 'ready' WHERE id = ?",
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


def start():
    for target in (refresh_loop, download_loop, scan_loop):
        threading.Thread(target=target, daemon=True).start()
