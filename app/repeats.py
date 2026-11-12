"""Find audio that repeats across episodes of one show. CPU only, no templates or tags needed.

How it works:
1. The newest downloaded episodes of a show are fingerprinted in full.
2. Every pair of episodes is compared. Hashes found in both vote for a time offset,
   and a run of seconds that keeps voting for the same offset is a shared section.
3. Shared sections are grouped across episodes. A group seen in enough episodes is
   saved as a suggestion, with a guess of intro, outro or ad based on where it sits.

Nothing here changes what the player skips. A suggestion only takes effect when the
user accepts it. It then becomes a normal intro/outro template or a tagged ad.
"""
import json
import math
import os
import time

import numpy as np

import adlearn
import db
import fingerprint as fp
import markers

REPEAT_EPISODES = max(2, int(os.environ.get("REPEAT_EPISODES", "5")))
REPEAT_MIN_EPISODES = max(2, int(os.environ.get("REPEAT_MIN_EPISODES", "2")))
MIN_SEC = float(os.environ.get("REPEAT_MIN_SECONDS", "5"))
MAX_SEC = 300.0
SIG_VERSION = "v1"
MAX_HASH_COUNT = 30       # a hash that occurs more often than this in one episode says nothing
MIN_CANDIDATE_VOTES = 40  # votes one time offset needs before it is looked at
MAX_CANDIDATES = 40
MIN_VOTES_PER_SEC = 3     # votes a second needs to count as matching
MAX_GAP_SEC = 2           # matching seconds this far apart still belong to one section
MIN_DENSITY = 0.6         # share of a section's seconds that must match
TAIL_SEC = 0.5            # a hash is timed at its first peak, so a section runs a little past the last one
INTRO_SHARE = 0.6         # an intro or outro must be in this share of the compared episodes

RUNNING = {"show_id": None}


# ---------- comparing two episodes ----------

def prepare(fingerprint):
    """Drop hashes that are very common inside one episode. They only add noise and cost memory."""
    if len(fingerprint) == 0:
        return fingerprint
    _values, inverse, counts = np.unique(fingerprint.hashes, return_inverse=True, return_counts=True)
    keep = counts[inverse] <= MAX_HASH_COUNT
    return fp.Fingerprint(fingerprint.hashes[keep], fingerprint.times[keep])


def _pairs(a, b):
    """Frame times (in a, in b) of every hash the two fingerprints share."""
    empty = np.zeros(0, np.int64)
    if len(a) == 0 or len(b) == 0:
        return empty, empty
    left = np.searchsorted(a._sh, b.hashes, side="left")
    right = np.searchsorted(a._sh, b.hashes, side="right")
    counts = right - left
    total = int(counts.sum())
    if total == 0:
        return empty, empty
    b_idx = np.repeat(np.arange(len(b)), counts)
    base = np.repeat(np.cumsum(counts) - counts, counts)
    a_idx = np.repeat(left, counts) + (np.arange(total) - base)
    return a._st[a_idx].astype(np.int64), b.times[b_idx].astype(np.int64)


def _runs(seconds_with_votes):
    """Group matching seconds into runs. Returns [(first_second, last_second)]."""
    runs = []
    for sec in seconds_with_votes:
        if runs and sec - runs[-1][1] <= MAX_GAP_SEC + 1:
            runs[-1][1] = sec
        else:
            runs.append([sec, sec])
    return runs


def common_segments(a, b):
    """Sections that sound the same in both. Returns [(start_in_a, start_in_b, seconds)]."""
    ta, tb = _pairs(a, b)
    if len(ta) < MIN_CANDIDATE_VOTES:
        return []
    offsets = tb - ta
    lo = int(offsets.min())
    hist = np.bincount(offsets - lo)
    smooth = np.convolve(hist, np.ones(3, dtype=np.int64), mode="same")
    found = []
    for _ in range(MAX_CANDIDATES):
        pos = int(smooth.argmax())
        if smooth[pos] < MIN_CANDIDATE_VOTES:
            break
        smooth[max(0, pos - 8): pos + 9] = 0
        offset = pos + lo
        frames = np.sort(ta[np.abs(offsets - offset) <= 1])
        secs = (frames * fp.FRAME_SEC).astype(np.int64)
        per_sec = np.bincount(secs)
        good = np.nonzero(per_sec >= MIN_VOTES_PER_SEC)[0]
        for first, last in _runs(good.tolist()):
            matched = int((per_sec[first:last + 1] >= MIN_VOTES_PER_SEC).sum())
            if matched / (last - first + 1) < MIN_DENSITY:
                continue
            inside = frames[(secs >= first) & (secs <= last)]
            start = float(inside.min() * fp.FRAME_SEC)
            end = float(inside.max() * fp.FRAME_SEC) + TAIL_SEC
            if end - start >= MIN_SEC:
                found.append((round(start, 2), round(start + offset * fp.FRAME_SEC, 2), round(end - start, 2)))
    return found


# ---------- grouping across episodes ----------

def _overlap(s1, e1, s2, e2):
    return max(0.0, min(e1, e2) - max(s1, s2))


def _same_place(n1, n2):
    """Two sections of one episode that cover mostly the same audio."""
    if n1[0] != n2[0]:
        return False
    both = _overlap(n1[1], n1[2], n2[1], n2[2])
    union = max(n1[2], n2[2]) - min(n1[1], n2[1])
    return union > 0 and both / union >= 0.5


def cluster(pair_segments):
    """Group pairwise matches into sections.

    pair_segments is [(episode_a, start_a, episode_b, start_b, seconds)].
    Returns a list of sections, each a list of occurrences {episode_id, start, end}.
    """
    nodes = []
    parent = []

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        parent[find(i)] = find(j)

    for ep_a, start_a, ep_b, start_b, seconds in pair_segments:
        for ep, start in ((ep_a, start_a), (ep_b, start_b)):
            nodes.append((ep, max(0.0, start), max(0.0, start) + seconds))
            parent.append(len(parent))
        union(len(nodes) - 2, len(nodes) - 1)
    for i in range(len(nodes)):
        for j in range(i + 1, len(nodes)):
            if _same_place(nodes[i], nodes[j]):
                union(i, j)

    groups = {}
    for i, node in enumerate(nodes):
        groups.setdefault(find(i), []).append(node)

    sections = []
    for members in groups.values():
        occurrences = []
        for ep in sorted({m[0] for m in members}):
            mine = sorted(m for m in members if m[0] == ep)
            # one episode can hold the section more than once, so split on gaps
            piles = [[mine[0]]]
            for m in mine[1:]:
                if _overlap(m[1], m[2], piles[-1][-1][1], piles[-1][-1][2]) > 0:
                    piles[-1].append(m)
                else:
                    piles.append([m])
            for pile in piles:
                start = float(np.median([m[1] for m in pile]))
                end = float(np.median([m[2] for m in pile]))
                occurrences.append({"episode_id": ep, "start": round(start, 1), "end": round(end, 1)})
        sections.append(occurrences)
    return sections


def _clash(occ_a, occ_b):
    """True when two sections cover mostly the same audio in an episode they share."""
    for x in occ_a:
        for y in occ_b:
            if x["episode_id"] != y["episode_id"]:
                continue
            shorter = min(x["end"] - x["start"], y["end"] - y["start"])
            if shorter > 0 and _overlap(x["start"], x["end"], y["start"], y["end"]) / shorter >= 0.5:
                return True
    return False


def guess_kind(occurrences, durations, compared):
    """Intro, outro or ad, from where the section sits and how many episodes have it."""
    episodes = len({o["episode_id"] for o in occurrences})
    seconds = float(np.median([o["end"] - o["start"] for o in occurrences]))
    common = episodes >= max(2, math.ceil(compared * INTRO_SHARE)) and seconds <= markers.MAX_TEMPLATE_SEC
    if not common:
        return "ad"

    def after(o):
        """Seconds between the end of the section and the end of the episode."""
        return max(0.0, (durations.get(o["episode_id"]) or 0.0) - o["end"])

    # Short episodes fit inside both windows, so the nearer edge of the episode decides.
    if all(o["end"] <= markers.INTRO_WINDOW and o["start"] <= after(o) for o in occurrences):
        return "intro"
    if all(durations.get(o["episode_id"]) and o["start"] >= durations[o["episode_id"]] - markers.OUTRO_WINDOW
           and after(o) < o["start"] for o in occurrences):
        return "outro"
    return "ad"


def summarize(sections, durations, compared):
    """Keep the sections worth showing. Returns dicts ready to store, best first."""
    items = []
    for occurrences in sections:
        episodes = len({o["episode_id"] for o in occurrences})
        seconds = float(np.median([o["end"] - o["start"] for o in occurrences]))
        if episodes < REPEAT_MIN_EPISODES or not (MIN_SEC <= seconds <= MAX_SEC):
            continue
        items.append({
            "occurrences": occurrences, "episodes": episodes, "seconds": round(seconds, 1),
            "guess": guess_kind(occurrences, durations, compared),
        })
    items.sort(key=lambda i: (-i["episodes"], -i["seconds"]))
    kept = []
    for item in items:
        if not any(_clash(item["occurrences"], k["occurrences"]) for k in kept):
            kept.append(item)
    return kept


# ---------- one show ----------

def episodes_to_compare(conn, show_id):
    marks = ",".join("?" for _ in db.PLAYABLE)
    rows = conn.execute(
        "SELECT id, file_path, duration, published FROM episodes "
        f"WHERE show_id = ? AND file_path IS NOT NULL AND status IN ({marks}) ORDER BY published DESC, id DESC LIMIT ?",
        [show_id] + list(db.PLAYABLE) + [REPEAT_EPISODES],
    ).fetchall()
    return [r for r in rows if os.path.exists(r["file_path"])]


def signature(rows):
    """Changes whenever the set of episodes to compare changes."""
    return SIG_VERSION + ":" + ",".join(str(r["id"]) for r in rows)


def analyse(rows, log=print):
    """Fingerprint and compare the given episodes. Returns (items, durations)."""
    prints, durations = {}, {}
    for n, row in enumerate(rows, 1):
        log(f"[repeats] {n}/{len(rows)} fingerprinting episode {row['id']}")
        prints[row["id"]] = prepare(fp.fingerprint_file(row["file_path"]))
        durations[row["id"]] = markers.probe_duration(row["file_path"]) or float(row["duration"] or 0)
    ids = [r["id"] for r in rows]
    pair_segments = []
    for i, ep_a in enumerate(ids):
        for ep_b in ids[i + 1:]:
            for start_a, start_b, seconds in common_segments(prints[ep_a], prints[ep_b]):
                pair_segments.append((ep_a, start_a, ep_b, start_b, seconds))
    return summarize(cluster(pair_segments), durations, len(rows)), durations


def store(conn, show_id, items, compared, order):
    """Replace the show's open suggestions. Ignored ones are kept, and stay ignored when found again."""
    rank = {ep: n for n, ep in enumerate(order)}
    ignored = conn.execute("SELECT id, occ_json FROM repeats WHERE show_id = ? AND status = 'ignored'", (show_id,)).fetchall()
    conn.execute("DELETE FROM repeats WHERE show_id = ? AND status = 'new'", (show_id,))
    added = 0
    for item in items:
        occurrences = sorted(item["occurrences"], key=lambda o: (rank.get(o["episode_id"], 999), o["start"]))
        blob = json.dumps(occurrences)
        seen_before = [r for r in ignored if _clash(occurrences, json.loads(r["occ_json"] or "[]"))]
        if seen_before:
            # remember where it is now, so it stays ignored as older episodes drop out
            conn.execute("UPDATE repeats SET occ_json = ? WHERE id = ?", (blob, seen_before[0]["id"]))
            continue
        conn.execute(
            "INSERT INTO repeats (show_id, status, guess, seconds, episodes, compared, occ_json, created) "
            "VALUES (?, 'new', ?, ?, ?, ?, ?, ?)",
            (show_id, item["guess"], item["seconds"], item["episodes"], compared, blob, int(time.time())),
        )
        added += 1
    return added


def next_show(conn):
    """A show whose newest downloaded episodes changed since it was last compared, or None."""
    for show in conn.execute("SELECT id, repeat_sig FROM shows ORDER BY id").fetchall():
        rows = episodes_to_compare(conn, show["id"])
        if len(rows) >= 2 and signature(rows) != show["repeat_sig"]:
            return show["id"], rows
    return None


def process_one(conn, log=print):
    """Compare the episodes of one show that is due. Returns False when nothing is due."""
    due = next_show(conn)
    if due is None:
        return False
    show_id, rows = due
    RUNNING["show_id"] = show_id
    try:
        try:
            items, _durations = analyse(rows, log)
            added = store(conn, show_id, items, len(rows), [r["id"] for r in rows])
            log(f"[repeats] show {show_id}: {len(items)} repeated sections in {len(rows)} episodes, {added} to review")
        except Exception as exc:
            log(f"[repeats] show {show_id} failed: {exc}")
        # Saved even after a failure, so a bad file is not retried in a loop.
        conn.execute("UPDATE shows SET repeat_sig = ? WHERE id = ?", (signature(rows), show_id))
        conn.commit()
    finally:
        RUNNING["show_id"] = None
    return True


# ---------- what the show page uses ----------

def _known_ranges(conn, episode_id, cache):
    """What the player already skips in an episode: ads, and the detected intro and outro."""
    if episode_id not in cache:
        ranges = [list(r) for r in adlearn.current_ads(conn, episode_id)]
        row = conn.execute("SELECT markers_json, duration FROM episodes WHERE id = ?", (episode_id,)).fetchone()
        try:
            m = json.loads(row["markers_json"] or "{}") if row else {}
        except ValueError:
            m = {}
        if m.get("intro_end") is not None:
            ranges.append([m.get("intro_start") or 0.0, m["intro_end"]])
        if m.get("outro_start") is not None:
            ranges.append([m["outro_start"], m.get("outro_end") or m.get("duration") or (row["duration"] or 0) or m["outro_start"]])
        cache[episode_id] = ranges
    return cache[episode_id]


def _already_handled(conn, occurrences, cache):
    covered = 0
    for o in occurrences:
        length = o["end"] - o["start"]
        inside = sum(_overlap(o["start"], o["end"], s, e) for s, e in _known_ranges(conn, o["episode_id"], cache))
        if length > 0 and inside / length >= 0.5:
            covered += 1
    return covered * 2 >= len(occurrences)


def show_status(conn, show_id):
    show = conn.execute("SELECT repeat_sig FROM shows WHERE id = ?", (show_id,)).fetchone()
    if not show:
        raise LookupError("Show not found")
    rows = episodes_to_compare(conn, show_id)
    if len(rows) < 2:
        state = "need_more"
    elif RUNNING["show_id"] == show_id:
        state = "running"
    elif signature(rows) != show["repeat_sig"]:
        state = "waiting"
    else:
        state = "done"
    titles = {r["id"]: r["title"] for r in conn.execute("SELECT id, title FROM episodes WHERE show_id = ?", (show_id,)).fetchall()}
    items, cache = [], {}
    for row in conn.execute(
        "SELECT id, guess, seconds, episodes, compared, occ_json FROM repeats WHERE show_id = ? AND status = 'new' "
        "ORDER BY episodes DESC, seconds DESC", (show_id,)
    ).fetchall():
        occurrences = [o for o in json.loads(row["occ_json"] or "[]") if o["episode_id"] in titles]
        if not occurrences or _already_handled(conn, occurrences, cache):
            continue
        for o in occurrences:
            o["title"] = titles[o["episode_id"]]
        items.append({
            "id": row["id"], "guess": row["guess"], "seconds": row["seconds"], "episodes": row["episodes"],
            "compared": row["compared"], "occurrences": occurrences,
        })
    return {"state": state, "downloaded": len(rows), "max_compared": REPEAT_EPISODES, "items": items}


def rescan(conn, show_id):
    conn.execute("UPDATE shows SET repeat_sig = NULL WHERE id = ?", (show_id,))
    conn.commit()


def ignore(conn, repeat_id):
    cur = conn.execute("UPDATE repeats SET status = 'ignored' WHERE id = ?", (repeat_id,))
    conn.commit()
    if not cur.rowcount:
        raise LookupError("Suggestion not found")


def accept(conn, repeat_id, kind):
    """Turn a suggestion into an intro template, an outro template, or a tagged ad."""
    row = conn.execute("SELECT id, show_id, occ_json FROM repeats WHERE id = ?", (repeat_id,)).fetchone()
    if not row:
        raise LookupError("Suggestion not found")
    if kind not in ("intro", "outro", "ad"):
        raise ValueError("kind must be intro, outro or ad")
    chosen = None
    for o in json.loads(row["occ_json"] or "[]"):
        ep = conn.execute("SELECT id, file_path FROM episodes WHERE id = ?", (o["episode_id"],)).fetchone()
        if ep and ep["file_path"] and os.path.exists(ep["file_path"]):
            chosen = (ep, o)
            break
    if chosen is None:
        raise ValueError("None of the episodes with this section are downloaded any more. Scan again.")
    ep, o = chosen
    result = {"kind": kind, "episode_id": ep["id"], "start": o["start"], "end": o["end"], "note": ""}
    if kind == "ad":
        result["note"] = adlearn.add_tag(conn, ep["id"], o["start"], o["end"], "ad")["note"]
    else:
        markers.create_template(conn, row["show_id"], kind, ep["file_path"], o["start"], o["end"])
        result["queued"] = markers.queue_redetect(conn, row["show_id"])
    conn.execute("DELETE FROM repeats WHERE id = ?", (repeat_id,))
    conn.commit()
    return result
