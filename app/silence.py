"""Shorten Silence: find the pauses in an episode once, so the player can speed through them.

The map is a list of [start, end] pairs in media seconds. It is stored as JSON in
DATA_DIR/silence/<episode id>.json together with the size and sha256 of the audio file and the
analyzer version. A map whose file or version no longer matches is treated as missing.
"""
import hashlib
import json
import os
import re
import subprocess

import db

ANALYZER_VERSION = 1
THRESHOLD_DB = -35      # quieter than this counts as silence
MIN_SILENCE = 0.25      # seconds. Shorter gaps are left alone (spec 6.3.1 step 6)
SILENCE_DIR = os.path.join(db.DATA_DIR, "silence")
SETTING_KEY = "shorten_silence"   # meta key for the global default (1 = on)

_hash_cache = {}   # path -> (size, mtime, sha256), so the same file is not hashed on every request


def map_path(episode_id):
    return os.path.join(SILENCE_DIR, f"{episode_id}.json")


def file_hash(path):
    st = os.stat(path)
    cached = _hash_cache.get(path)
    if cached and cached[0] == st.st_size and cached[1] == st.st_mtime:
        return cached[2]
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    digest = h.hexdigest()
    _hash_cache[path] = (st.st_size, st.st_mtime, digest)
    return digest


def probe_duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", path],
        capture_output=True, text=True, timeout=120,
    ).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return 0.0


def detect(path):
    """Silence intervals of a file as [[start, end], ...] in seconds. One ffmpeg pass, CPU only."""
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-vn", "-af",
         f"aformat=channel_layouts=mono,aresample=16000,silencedetect=noise={THRESHOLD_DB}dB:d={MIN_SILENCE}",
         "-f", "null", "-"],
        capture_output=True, text=True, timeout=3600,
    )
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg failed: " + proc.stderr.strip()[-300:])
    intervals, start = [], None
    for line in proc.stderr.splitlines():
        m = re.search(r"silence_start: (-?[\d.]+)", line)
        if m:
            start = max(0.0, float(m.group(1)))
            continue
        m = re.search(r"silence_end: ([\d.]+)", line)
        if m and start is not None:
            intervals.append([start, float(m.group(1))])
            start = None
    if start is not None:   # the file ends in silence
        intervals.append([start, probe_duration(path)])
    # ffmpeg can report one pause as two that touch. Join them, then drop what is too short.
    merged = []
    for s, e in sorted(intervals):
        if merged and s - merged[-1][1] <= 0.01:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [[round(s, 3), round(e, 3)] for s, e in merged if e - s >= MIN_SILENCE]


def analyze(conn, episode_id, file_path):
    """Detect, store the map, and mark the episode done. Returns the stored map. The caller commits."""
    intervals = detect(file_path)
    data = {
        "episode_id": episode_id,
        "file_size": os.path.getsize(file_path),
        "file_sha256": file_hash(file_path),
        "analyzer_version": ANALYZER_VERSION,
        "threshold_db": THRESHOLD_DB,
        "min_silence": MIN_SILENCE,
        "intervals": intervals,
    }
    os.makedirs(SILENCE_DIR, exist_ok=True)
    tmp = map_path(episode_id) + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh)
    os.replace(tmp, map_path(episode_id))
    conn.execute("UPDATE episodes SET silence_ver = ? WHERE id = ?", (ANALYZER_VERSION, episode_id))
    return data


def load_map(episode_id, file_path):
    """The stored map, or None when there is none or it no longer fits the file or the analyzer."""
    if not file_path or not os.path.exists(file_path):
        return None
    try:
        with open(map_path(episode_id)) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if data.get("analyzer_version") != ANALYZER_VERSION:
        return None
    if data.get("file_size") != os.path.getsize(file_path):
        return None
    if data.get("file_sha256") != file_hash(file_path):
        return None
    return data


def forget(conn, episode_id):
    """Drop the map so the worker analyses the episode again. The caller commits."""
    try:
        os.remove(map_path(episode_id))
    except OSError:
        pass
    conn.execute("UPDATE episodes SET silence_ver = 0 WHERE id = ?", (episode_id,))


def global_on(conn):
    return bool(db.meta_get(conn, SETTING_KEY))


def effective(conn, show_override):
    """Is Shorten Silence on for a show? show_override: None = use the default, 1 = on, 0 = off."""
    if show_override is None:
        return global_on(conn)
    return bool(show_override)


# ---- time saved ----

def add_saved(conn, episode_id, speed, silence, ads):
    """Add seconds saved while playing one episode. Each part is clamped to 0..600 per report. The caller commits."""
    parts = [max(0.0, min(600.0, float(x or 0))) for x in (speed, silence, ads)]
    row = conn.execute("SELECT show_id FROM episodes WHERE id = ?", (episode_id,)).fetchone()
    if row is None:
        raise LookupError("Episode not found")
    conn.execute(
        "INSERT INTO time_saved (episode_id, show_id, speed, silence, ads) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(episode_id) DO UPDATE SET speed = speed + excluded.speed, "
        "silence = silence + excluded.silence, ads = ads + excluded.ads",
        (episode_id, row["show_id"], *parts),
    )
    return parts


def totals(conn, show_id=None, episode_id=None):
    where, args = "", []
    if episode_id is not None:
        where, args = "WHERE episode_id = ?", [episode_id]
    elif show_id is not None:
        where, args = "WHERE show_id = ?", [show_id]
    r = conn.execute(
        f"SELECT COALESCE(SUM(speed), 0) AS speed, COALESCE(SUM(silence), 0) AS silence, "
        f"COALESCE(SUM(ads), 0) AS ads FROM time_saved {where}", args
    ).fetchone()
    out = {k: round(r[k], 1) for k in ("speed", "silence", "ads")}
    out["total"] = round(out["speed"] + out["silence"] + out["ads"], 1)
    return out


def by_show(conn):
    rows = conn.execute(
        "SELECT t.show_id, s.title, SUM(t.speed) AS speed, SUM(t.silence) AS silence, SUM(t.ads) AS ads "
        "FROM time_saved t JOIN shows s ON s.id = t.show_id GROUP BY t.show_id ORDER BY s.title"
    ).fetchall()
    return [{"show_id": r["show_id"], "title": r["title"], "speed": round(r["speed"], 1),
             "silence": round(r["silence"], 1), "ads": round(r["ads"], 1)} for r in rows]
