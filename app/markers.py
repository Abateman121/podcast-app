"""Automatic intro and outro detection for one downloaded episode. CPU only.

Order of attempts for each of intro and outro:
1. Audio fingerprint of the show's template (confidence must reach SKIP_CONFIDENCE).
2. Chapters, from the audio file's own tags or from the feed's podcast:chapters.
3. Nothing. The player then uses the show's fixed intro/outro seconds.

Results are cached in episodes.markers_json, so each episode is analysed once.
"""
import json
import os
import re
import subprocess
import time

import requests

import db
import fingerprint as fp
import sources

SKIP_CONFIDENCE = float(os.environ.get("SKIP_CONFIDENCE", "0.6"))
INTRO_WINDOW = float(os.environ.get("INTRO_WINDOW_SECONDS", "240"))
OUTRO_WINDOW = float(os.environ.get("OUTRO_WINDOW_SECONDS", "240"))
MARKERS_VERSION = 1
KINDS = ("intro", "outro")
MIN_TEMPLATE_SEC = 3.0
MAX_TEMPLATE_SEC = 120.0
CHAPTER_CONFIDENCE = 0.9

INTRO_TITLE = re.compile(
    r"^\W*(intro(duction)?|intro (music|theme)|opening( theme| credits| music)?|theme( music| song)?|title sequence)\W*$",
    re.IGNORECASE,
)
OUTRO_TITLE = re.compile(
    r"^\W*(outro|outro (music|theme)|closing( theme| credits| music)?|end credits|credits|theme (outro|out))\W*$",
    re.IGNORECASE,
)
THEME_ONLY = re.compile(r"^\W*theme( music| song)?\W*$", re.IGNORECASE)


# ---------- ffmpeg helpers ----------

def probe_duration(path):
    try:
        res = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True, timeout=60,
        )
        return float(res.stdout.strip() or 0)
    except Exception:
        return 0.0


def file_chapters(path):
    """Chapters stored in the audio file (ID3 CHAP frames, MP4 chapters)."""
    try:
        res = subprocess.run(
            ["ffprobe", "-v", "error", "-show_chapters", "-of", "json", path],
            capture_output=True, text=True, timeout=60,
        )
        data = json.loads(res.stdout or "{}")
    except Exception:
        return []
    chapters = []
    for c in data.get("chapters", []):
        try:
            chapters.append(
                {"start": float(c["start_time"]), "end": float(c["end_time"]), "title": (c.get("tags") or {}).get("title", "")}
            )
        except (KeyError, ValueError):
            continue
    return chapters


def feed_chapters(url, duration=0.0):
    """Chapters from a podcast:chapters JSON file. A chapter with no end runs to the next one, or to the end of the episode."""
    resp = requests.get(url, headers=sources.UA, timeout=20)
    resp.raise_for_status()
    raw = resp.json().get("chapters", [])
    items = []
    for c in raw:
        try:
            items.append({"start": float(c["startTime"]), "end": c.get("endTime"), "title": c.get("title", "")})
        except (KeyError, TypeError, ValueError):
            continue
    items.sort(key=lambda c: c["start"])
    for i, c in enumerate(items):
        if c["end"] is None:
            c["end"] = items[i + 1]["start"] if i + 1 < len(items) else (duration or None)
        else:
            c["end"] = float(c["end"])
    return [c for c in items if c["end"] is not None]


def pick_from_chapters(chapters, duration):
    """Return {"intro": (start, end) or None, "outro": (start, end) or None}."""
    found = {"intro": None, "outro": None}
    for c in sorted(chapters, key=lambda c: c["start"]):
        title = c.get("title") or ""
        start, end = c["start"], c["end"]
        if not (0 <= start < end) or end - start > 300:
            continue
        is_intro = bool(INTRO_TITLE.match(title))
        is_outro = bool(OUTRO_TITLE.match(title))
        if THEME_ONLY.match(title) and duration:
            is_intro, is_outro = start < duration * 0.4, start >= duration * 0.4
        if is_intro and found["intro"] is None:
            found["intro"] = (start, end)
        if is_outro:
            found["outro"] = (start, end)
    return found


# ---------- templates ----------

def extract_wav(path, start, length, out_path):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    tmp = out_path + ".part.wav"
    cmd = ["ffmpeg", "-y", "-v", "error", "-nostdin", "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", path,
           "-vn", "-ac", "1", "-ar", str(fp.SR), tmp]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if res.returncode != 0 or not os.path.exists(tmp):
        raise ValueError("Could not cut that section out of the audio file.")
    os.replace(tmp, out_path)


def create_template(conn, show_id, kind, episode_path, start, end):
    """Cut [start, end] out of a reference episode and store its fingerprint as the show's template."""
    if kind not in KINDS:
        raise ValueError("kind must be intro or outro")
    length = float(end) - float(start)
    if start < 0 or length < MIN_TEMPLATE_SEC:
        raise ValueError(f"The {kind} section must be at least {MIN_TEMPLATE_SEC:.0f} seconds long.")
    if length > MAX_TEMPLATE_SEC:
        raise ValueError(f"The {kind} section can be at most {MAX_TEMPLATE_SEC:.0f} seconds long.")
    wav = db.template_path(show_id, kind)
    extract_wav(episode_path, start, length, wav)
    fingerprint = fp.fingerprint_file(wav)
    if len(fingerprint) < fp.MIN_TEMPLATE_HASHES:
        os.remove(wav)
        raise ValueError(f"There is too little distinct sound in that {kind} section. Pick a part with music.")
    conn.execute(
        f"UPDATE shows SET {kind}_template = ?, {kind}_template_len = ? WHERE id = ?",
        (fp.pack(fingerprint), length, show_id),
    )
    conn.commit()
    return {"kind": kind, "seconds": round(length, 1), "hashes": len(fingerprint)}


def delete_template(conn, show_id, kind):
    if kind not in KINDS:
        raise ValueError("kind must be intro or outro")
    conn.execute(f"UPDATE shows SET {kind}_template = NULL, {kind}_template_len = NULL WHERE id = ?", (show_id,))
    conn.commit()
    wav = db.template_path(show_id, kind)
    if os.path.exists(wav):
        os.remove(wav)


def load_template(conn, show_id, kind):
    """Return (Fingerprint, length_seconds), or (None, 0). Rebuilds from the saved wav if the algorithm changed."""
    row = conn.execute(
        f"SELECT {kind}_template AS blob, {kind}_template_len AS length FROM shows WHERE id = ?", (show_id,)
    ).fetchone()
    if not row or not row["blob"]:
        return None, 0.0
    fingerprint = fp.unpack(row["blob"])
    if fingerprint is None:
        wav = db.template_path(show_id, kind)
        if not os.path.exists(wav):
            return None, 0.0
        fingerprint = fp.fingerprint_file(wav)
        conn.execute(f"UPDATE shows SET {kind}_template = ? WHERE id = ?", (fp.pack(fingerprint), show_id))
        conn.commit()
    return fingerprint, float(row["length"] or 0)


# ---------- detection ----------

def empty_markers(duration=0.0):
    return {
        "version": MARKERS_VERSION, "detected_at": int(time.time()), "duration": round(duration, 1),
        "intro_start": None, "intro_end": None, "intro_source": "fallback", "intro_confidence": 0.0,
        "outro_start": None, "outro_end": None, "outro_source": "fallback", "outro_confidence": 0.0,
        "source": "fallback", "confidence": 0.0,
    }


def summarize(m):
    """Fill the top-level source and confidence from the intro, else the outro."""
    for side in ("intro", "outro"):
        if m[f"{side}_source"] != "fallback":
            m["source"], m["confidence"] = m[f"{side}_source"], m[f"{side}_confidence"]
            return m
    m["source"], m["confidence"] = "fallback", max(m["intro_confidence"], m["outro_confidence"])
    return m


def detect_intro(conn, ep, path, duration, m):
    template, length = load_template(conn, ep["show_id"], "intro")
    if template is None:
        return
    window = min(INTRO_WINDOW, duration) if duration else INTRO_WINDOW
    hit = fp.best_match(template, fp.fingerprint_file(path, 0.0, window))
    m["intro_confidence"] = round(hit["confidence"], 3)
    if hit["confidence"] < SKIP_CONFIDENCE:
        return
    start, end = max(0.0, hit["offset"]), hit["offset"] + length
    if duration:
        end = min(end, duration)
    if end - start >= 1.0:
        m.update(intro_start=round(start, 2), intro_end=round(end, 2), intro_source="fingerprint")


def detect_outro(conn, ep, path, duration, m):
    template, length = load_template(conn, ep["show_id"], "outro")
    if template is None or not duration:
        return
    base = max(0.0, duration - OUTRO_WINDOW)
    hit = fp.best_match(template, fp.fingerprint_file(path, base, duration - base))
    m["outro_confidence"] = round(hit["confidence"], 3)
    if hit["confidence"] < SKIP_CONFIDENCE:
        return
    start, end = max(0.0, base + hit["offset"]), min(duration, base + hit["offset"] + length)
    if end - start >= 1.0:
        m.update(outro_start=round(start, 2), outro_end=round(end, 2), outro_source="fingerprint")


def detect_from_chapters(ep, path, duration, m):
    wanted = [s for s in KINDS if m[f"{s}_source"] == "fallback"]
    if not wanted:
        return
    sources_to_try = [("file", lambda: file_chapters(path))]
    if ep["chapters_url"]:
        sources_to_try.append(("feed", lambda: feed_chapters(ep["chapters_url"], duration)))
    for _name, load in sources_to_try:
        try:
            picks = pick_from_chapters(load(), duration)
        except Exception:
            continue
        for side in wanted:
            if picks[side] and m[f"{side}_source"] == "fallback":
                start, end = picks[side]
                m[f"{side}_start"], m[f"{side}_end"] = round(start, 2), round(end, 2)
                m[f"{side}_source"], m[f"{side}_confidence"] = "chapter", CHAPTER_CONFIDENCE
        wanted = [s for s in KINDS if m[f"{s}_source"] == "fallback"]
        if not wanted:
            return


def detect(conn, ep):
    """Analyse one episode and return its markers dict. ep needs id, show_id, file_path, duration, chapters_url."""
    path = ep["file_path"]
    if not path or not os.path.exists(path):
        raise FileNotFoundError("the audio file is missing")
    duration = probe_duration(path) or float(ep["duration"] or 0)
    m = empty_markers(duration)
    detect_intro(conn, ep, path, duration, m)
    detect_outro(conn, ep, path, duration, m)
    detect_from_chapters(ep, path, duration, m)
    return summarize(m)


def queue_redetect(conn, show_id):
    """Clear the cached markers of a show's downloaded episodes. The marker worker then redoes them."""
    cur = conn.execute("UPDATE episodes SET markers_json = '{}' WHERE show_id = ? AND file_path IS NOT NULL", (show_id,))
    db.meta_bump(conn, "marker_gen")
    conn.commit()
    return cur.rowcount


# ---------- what the player and the debug view use ----------

def player_markers(markers_json):
    """The small dict sent with each episode. Empty when nothing was detected, so the player falls back."""
    try:
        m = json.loads(markers_json or "{}")
    except ValueError:
        return {}
    out = {}
    for key in ("intro_start", "intro_end", "outro_start", "outro_end"):
        if isinstance(m.get(key), (int, float)):
            out[key] = m[key]
    if out:
        out["source"] = m.get("source", "fallback")
    return out


def effective(markers, intro_skip, outro_skip, duration):
    """What the player will actually skip, including the fixed-seconds fallback."""
    intro = outro = None
    if markers.get("intro_end") is not None:
        intro = {"start": markers.get("intro_start") or 0.0, "end": markers["intro_end"], "source": markers.get("intro_source")}
    elif intro_skip and intro_skip > 0:
        intro = {"start": 0.0, "end": float(intro_skip), "source": "fixed seconds"}
    if markers.get("outro_start") is not None:
        outro = {"start": markers["outro_start"], "source": markers.get("outro_source")}
    elif outro_skip and outro_skip > 0 and duration:
        outro = {"start": max(0.0, duration - outro_skip), "source": "fixed seconds"}
    return {"intro": intro, "outro": outro}


def transcript_notes(markers, segments):
    """Optional sanity check against the saved transcript. Reports only, never changes the markers."""
    notes = []
    if not segments or markers.get("intro_end") is None:
        return notes
    start, end = markers.get("intro_start") or 0.0, markers["intro_end"]
    first_speech = min(s["start"] for s in segments)
    spoken = sum(max(0.0, min(s["end"], end) - max(s["start"], start)) for s in segments)
    if end - start > 20 and spoken / (end - start) > 0.9:
        notes.append("The transcript has speech across almost all of the detected intro. Check this is not a false match.")
    if start > 5 and first_speech < start:
        notes.append(
            f"Speech starts at {first_speech:.0f}s, before the intro at {start:.0f}s (cold open). "
            "The player will not skip anything before the intro starts."
        )
    return notes
