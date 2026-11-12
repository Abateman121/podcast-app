"""Learning from ads the user tags by hand.

A tag of kind 'ad' is a missed ad. It helps in three ways:
1. It is skipped at once in that episode, and stays skipped after a re-scan.
2. Phrases from its transcript that appear nowhere else in the show become "anchors".
   When an anchor shows up in a later transcript, the ad is placed around it using
   the same lead and tail as the tagged ad.
3. Its audio is fingerprinted. A worker looks for the same audio in every episode,
   which catches ads that are reused word for word, with or without a transcript.

A tag of kind 'not_ad' is a range that was skipped by mistake. It cuts that range
out of the skipped ranges of that episode.
"""
import json
import os
import re
import time

import adscan
import db
import fingerprint as fp
import markers

AD_AUDIO_CONFIDENCE = float(os.environ.get("AD_AUDIO_CONFIDENCE", "0.5"))
BANK_KEY = "ad_bank_ver"
KINDS = ("ad", "not_ad")
MIN_AD_SEC = 5.0
MAX_AD_SEC = 300.0
MIN_PIECE_SEC = 2.0
MAX_ANCHORS_PER_TAG = 4
BACKGROUND_EPISODES = 40

STOP = set(
    "the a an and or of to in on for with is are was were it its this that these those you your we our i my me be as at by "
    "from so if can will just have has had not but they them their what when who how there here then than about into out "
    "up down over very really also all any some more most one two get got going go like know now".split()
)


# ---------- ranges ----------

def subtract_ranges(ranges, cuts):
    out = []
    for start, end in ranges:
        pieces = [[start, end]]
        for cut_start, cut_end in cuts:
            nxt = []
            for p_start, p_end in pieces:
                if cut_end <= p_start or cut_start >= p_end:
                    nxt.append([p_start, p_end])
                    continue
                if cut_start > p_start:
                    nxt.append([p_start, cut_start])
                if cut_end < p_end:
                    nxt.append([cut_end, p_end])
            pieces = nxt
        out.extend(pieces)
    return out


def effective_ads(detected, audio, tags):
    """Everything the player should skip: detected, audio matches and missed-ad tags, minus 'not ad' tags."""
    ranges = [list(r) for r in detected] + [list(r) for r in audio]
    ranges += [[t["start"], t["end"]] for t in tags if t["kind"] == "ad"]
    if not ranges:
        return []
    merged = adscan.merge_ranges(ranges)
    cuts = [[t["start"], t["end"]] for t in tags if t["kind"] == "not_ad"]
    if cuts:
        merged = [[round(s, 1), round(e, 1)] for s, e in subtract_ranges(merged, cuts) if e - s >= MIN_PIECE_SEC]
    return merged


# ---------- tags in the database ----------

def tag_dict(row):
    return {
        "id": row["id"], "episode_id": row["episode_id"], "kind": row["kind"],
        "start": row["start_sec"], "end": row["end_sec"],
        "has_audio": bool(row["fp"]) if "fp" in row.keys() else None,
        "phrases": len(json.loads(row["anchors_json"] or "[]")) if "anchors_json" in row.keys() else 0,
    }


def tags_by_episode(conn, episode_ids):
    if not episode_ids:
        return {}
    marks = ",".join("?" for _ in episode_ids)
    rows = conn.execute(
        f"SELECT id, episode_id, kind, start_sec, end_sec FROM ad_tags WHERE episode_id IN ({marks}) ORDER BY start_sec",
        list(episode_ids),
    ).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["episode_id"], []).append(tag_dict(r))
    return out


def list_tags(conn, episode_id):
    rows = conn.execute(
        "SELECT id, episode_id, kind, start_sec, end_sec, fp, anchors_json FROM ad_tags WHERE episode_id = ? ORDER BY start_sec",
        (episode_id,),
    ).fetchall()
    return [tag_dict(r) for r in rows]


def current_ads(conn, episode_id):
    row = conn.execute("SELECT ads_json, ads_audio_json FROM episodes WHERE id = ?", (episode_id,)).fetchone()
    if not row:
        return []
    tags = tags_by_episode(conn, [episode_id]).get(episode_id, [])
    return effective_ads(json.loads(row["ads_json"] or "[]"), json.loads(row["ads_audio_json"] or "[]"), tags)


# ---------- text anchors ----------

def tokens(text):
    return adscan.tokens(text)


def load_transcript(episode_id):
    path = db.transcript_path(episode_id)
    if not os.path.exists(path):
        return None
    with open(path) as fh:
        return json.load(fh)


def _gram_hashes(words, sizes=(3, 4)):
    for n in sizes:
        for i in range(len(words) - n + 1):
            yield hash(" ".join(words[i:i + n]))


def _inside(ranges, start, end):
    return any(s < end and start < e for s, e in ranges)


def background_hashes(conn, show_id, skip_episode_id, skip_range):
    """Hashes of word groups from the show's normal (non-ad) speech."""
    seen = set()
    rows = conn.execute(
        "SELECT id, ads_json FROM episodes WHERE show_id = ? AND status = 'ready' ORDER BY published DESC LIMIT ?",
        (show_id, BACKGROUND_EPISODES + 1),
    ).fetchall()
    tagged = tags_by_episode(conn, [r["id"] for r in rows])
    for row in rows:
        segments = load_transcript(row["id"])
        if not segments:
            continue
        skip = [list(r) for r in json.loads(row["ads_json"] or "[]")]
        skip += [[t["start"], t["end"]] for t in tagged.get(row["id"], []) if t["kind"] == "ad"]
        if row["id"] == skip_episode_id:
            skip.append(list(skip_range))
        for seg in segments:
            if _inside(skip, seg["start"], seg["end"]):
                continue
            seen.update(_gram_hashes(tokens(seg["text"])))
    return seen


def derive_anchors(conn, show_id, episode_id, start, end):
    """Phrases from the tagged ad that never occur in the show's normal speech."""
    segments = load_transcript(episode_id)
    if not segments:
        return []
    background = background_hashes(conn, show_id, episode_id, (start, end))
    anchors = []
    for seg in segments:
        if seg["end"] <= start or seg["start"] >= end or len(anchors) >= MAX_ANCHORS_PER_TAG:
            continue
        words = tokens(seg["text"])
        picked = None
        for n in (4, 3):
            for i in range(len(words) - n + 1):
                gram = words[i:i + n]
                content = [w for w in gram if w not in STOP and len(w) >= 4 and not w.isdigit()]
                if len(content) >= 2 and hash(" ".join(gram)) not in background:
                    picked = gram
                    break
            if picked:
                break
        if picked:
            anchors.append({"phrase": " ".join(picked), "lead": round(seg["start"] - start, 1), "tail": round(end - seg["start"], 1)})
    return anchors


def load_anchors(conn, show_id):
    rows = conn.execute(
        "SELECT anchors_json FROM ad_tags WHERE show_id = ? AND kind = 'ad' AND anchors_json != '[]'", (show_id,)
    ).fetchall()
    anchors, seen = [], set()
    for row in rows:
        for a in json.loads(row["anchors_json"] or "[]"):
            if a["phrase"] not in seen:
                seen.add(a["phrase"])
                anchors.append(a)
    return anchors


def reapply_text(conn, show_id):
    """Re-run text detection on the show's saved transcripts so new anchors take effect. No GPU needed."""
    anchors = load_anchors(conn, show_id)
    changed = 0
    for row in conn.execute("SELECT id FROM episodes WHERE show_id = ? AND status = 'ready'", (show_id,)).fetchall():
        segments = load_transcript(row["id"])
        if segments is None:
            continue
        conn.execute("UPDATE episodes SET ads_json = ? WHERE id = ?", (json.dumps(adscan.find_ad_ranges(segments, anchors)), row["id"]))
        changed += 1
    conn.commit()
    return changed


# ---------- audio bank ----------

def load_bank(conn):
    """Return [(tag_id, Fingerprint, length_seconds)] for every missed ad that has audio."""
    bank = []
    rows = conn.execute("SELECT id, fp, start_sec, end_sec FROM ad_tags WHERE kind = 'ad' AND fp IS NOT NULL").fetchall()
    for row in rows:
        fingerprint = fp.unpack(row["fp"])
        if fingerprint is None:  # algorithm changed: rebuild from the saved audio
            wav = db.ad_sample_path(row["id"])
            if not os.path.exists(wav):
                continue
            fingerprint = fp.fingerprint_file(wav)
            conn.execute("UPDATE ad_tags SET fp = ? WHERE id = ?", (fp.pack(fingerprint), row["id"]))
            conn.commit()
        bank.append((row["id"], fingerprint, row["end_sec"] - row["start_sec"]))
    return bank


def match_episode(path, bank):
    """Find every bank ad inside an episode. Decodes the episode once."""
    if not bank:
        return []
    episode_fp = fp.fingerprint_file(path)
    found = []
    for _tag_id, sample, length in bank:
        for hit in fp.match(sample, episode_fp, max_hits=3, min_confidence=AD_AUDIO_CONFIDENCE):
            start = max(0.0, hit["offset"])
            found.append([start, start + length])
    return adscan.merge_ranges(found) if found else []


# ---------- adding and removing tags ----------

def add_tag(conn, episode_id, start, end, kind):
    if kind not in KINDS:
        raise ValueError("kind must be ad or not_ad")
    ep = conn.execute("SELECT id, show_id, file_path, duration FROM episodes WHERE id = ?", (episode_id,)).fetchone()
    if not ep:
        raise LookupError("Episode not found")
    start, end = max(0.0, float(start)), float(end)
    # The length in the feed is often wrong (ads are inserted after it is written), so measure the file.
    real = markers.probe_duration(ep["file_path"]) if ep["file_path"] and os.path.exists(ep["file_path"]) else 0.0
    if real > 0 and end > real:
        end = real
    length = end - start
    if kind == "ad" and not (MIN_AD_SEC <= length <= MAX_AD_SEC):
        raise ValueError(f"A missed ad must be between {MIN_AD_SEC:.0f} and {MAX_AD_SEC:.0f} seconds long.")
    if kind == "not_ad" and length < 1.0:
        raise ValueError("That range is too short.")
    cur = conn.execute(
        "INSERT INTO ad_tags (episode_id, show_id, kind, start_sec, end_sec, created) VALUES (?, ?, ?, ?, ?, ?)",
        (episode_id, ep["show_id"], kind, start, end, int(time.time())),
    )
    tag_id = cur.lastrowid
    note = ""
    if kind == "ad":
        blob = None
        if ep["file_path"] and os.path.exists(ep["file_path"]):
            try:
                wav = db.ad_sample_path(tag_id)
                markers.extract_wav(ep["file_path"], start, length, wav)
                fingerprint = fp.fingerprint_file(wav)
                if len(fingerprint) >= fp.MIN_TEMPLATE_HASHES:
                    blob = fp.pack(fingerprint)
                else:
                    os.remove(wav)
                    note = "That section has too little sound to recognise by audio. Phrases may still be learned."
            except Exception as exc:
                note = f"Could not fingerprint the audio ({exc})."
        anchors = derive_anchors(conn, ep["show_id"], episode_id, start, end)
        conn.execute("UPDATE ad_tags SET fp = ?, anchors_json = ? WHERE id = ?", (blob, json.dumps(anchors), tag_id))
        db.meta_bump(conn, BANK_KEY)
        conn.commit()
        reapply_text(conn, ep["show_id"])
    conn.commit()
    return {"id": tag_id, "note": note}


def delete_tag(conn, tag_id):
    row = conn.execute("SELECT id, episode_id, show_id, kind FROM ad_tags WHERE id = ?", (tag_id,)).fetchone()
    if not row:
        raise LookupError("Tag not found")
    conn.execute("DELETE FROM ad_tags WHERE id = ?", (tag_id,))
    wav = db.ad_sample_path(tag_id)
    if os.path.exists(wav):
        os.remove(wav)
    if row["kind"] == "ad":
        db.meta_bump(conn, BANK_KEY)
        conn.commit()
        reapply_text(conn, row["show_id"])
    conn.commit()
    return row["episode_id"]


def show_summary(conn, show_id):
    rows = conn.execute(
        "SELECT kind, fp IS NOT NULL AS has_audio, anchors_json FROM ad_tags WHERE show_id = ?", (show_id,)
    ).fetchall()
    phrases = load_anchors(conn, show_id)
    return {
        "missed_ads": sum(1 for r in rows if r["kind"] == "ad"),
        "not_ads": sum(1 for r in rows if r["kind"] == "not_ad"),
        "audio_samples": sum(1 for r in rows if r["kind"] == "ad" and r["has_audio"]),
        "phrases": [a["phrase"] for a in phrases][:20],
    }
