"""Checks for Shorten Silence on the server: detection, the stored map, the routes, and the stats.

Run it inside a container where the app listens on port 8099 with an empty DATA_DIR and DISABLE_WORKERS=1.
The test calls the worker step itself, so nothing runs in the background. Audio is made with ffmpeg.

    docker exec podcast-test python3 /tests/check_silence.py
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.environ.get("APP_DIR", "/srv/app"))
import silence  # noqa: E402
import workers  # noqa: E402

BASE = "http://127.0.0.1:8099"
DATA_DIR = os.environ.get("DATA_DIR", "/data")
DB_PATH = os.path.join(DATA_DIR, "podcasts.db")
WORK = "/tmp/silence-check"
GAPS = [0.1, 0.3, 0.6, 1.5]   # spec section 9, silence test 1
BURST = 1.0                   # seconds of tone between the gaps
passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
    else:
        failed += 1
    print(f"[{passed + failed}] {'ok  ' if ok else 'FAIL'} {name}{'' if ok else '  -> ' + str(detail)}")


def call(method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, None


def ffmpeg(args, out):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args, out], check=True)


def make_tone_file(path):
    """Tone, gap, tone, gap ... ending with tone. Returns the gaps as [start, end] in seconds."""
    parts, gaps, t = [], [], 0.0
    for i, g in enumerate(GAPS + [None]):
        tone = os.path.join(WORK, f"tone{i}.wav")
        ffmpeg(["-f", "lavfi", "-i", f"sine=frequency=1000:sample_rate=44100:duration={BURST}", "-af", "volume=0.5"], tone)
        parts.append(tone)
        t += BURST
        if g is None:
            break
        gap = os.path.join(WORK, f"gap{i}.wav")
        ffmpeg(["-f", "lavfi", "-i", f"anullsrc=r=44100:cl=mono", "-t", str(g)], gap)
        parts.append(gap)
        gaps.append([t, t + g])
        t += g
    listing = os.path.join(WORK, "list.txt")
    with open(listing, "w") as fh:
        fh.write("".join(f"file '{p}'\n" for p in parts))
    ffmpeg(["-f", "concat", "-safe", "0", "-i", listing, "-c:a", "libmp3lame", "-b:a", "128k"], path)
    return gaps


def make_music_file(path):
    """Continuous sound with no real pause: two tones that swell and fade, over pink noise."""
    ffmpeg(["-f", "lavfi", "-i", "sine=frequency=220:duration=20", "-f", "lavfi", "-i", "sine=frequency=330:duration=20",
            "-f", "lavfi", "-i", "anoisesrc=color=pink:amplitude=0.05:duration=20",
            "-filter_complex",
            "[0]volume='0.3+0.15*sin(2*PI*t/3)':eval=frame[a];[1]volume=0.2[b];[a][b][2]amix=inputs=3:normalize=0",
            "-c:a", "libmp3lame", "-b:a", "128k"], path)


def add_episode(conn, show_id, guid, path):
    cur = conn.execute(
        "INSERT INTO episodes (show_id, guid, title, published, duration, file_path, status) VALUES (?, ?, ?, ?, 10, ?, 'ready')",
        (show_id, guid, guid, 1000 + len(guid), path),
    )
    conn.commit()
    return cur.lastrowid


def run_worker():
    done = 0
    while workers.process_one_silence():
        done += 1
    return done


def main():
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK)
    print("1. Making test audio")
    tone_path = os.path.join(WORK, "tones.mp3")
    gaps = make_tone_file(tone_path)
    music_path = os.path.join(WORK, "music.mp3")
    make_music_file(music_path)

    print("2. Detection")
    found = silence.detect(tone_path)
    check("100 ms gap is not a pause (shorter than 250 ms)", not any(s < gaps[0][1] and e > gaps[0][0] for s, e in found), found)
    check("three pauses found", len(found) == 3, found)
    for (gs, ge), iv in zip(gaps[1:], found):
        check(f"pause of {ge - gs:.1f} s found within 20 ms at both edges",
              abs(iv[0] - gs) <= 0.02 and abs(iv[1] - ge) <= 0.02, (iv, [gs, ge]))
    bursts = [[0, gaps[0][0]]] + [[gaps[i][1], gaps[i + 1][0] if i + 1 < len(gaps) else gaps[i][1] + BURST]
                                  for i in range(len(gaps))]
    keep = 0.08
    check("no boosted part touches a tone burst (80 ms kept at each edge)",
          all(not (s + keep < b1 and e - keep > b0) for s, e in found for b0, b1 in bursts), (found, bursts))
    check("music-only file gives no pauses", silence.detect(music_path) == [], silence.detect(music_path))

    print("3. Worker and stored map")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    show_id = conn.execute("INSERT INTO shows (feed_url, title) VALUES ('http://example.invalid/sil.xml', 'Silence Test')").lastrowid
    conn.commit()
    ep_tone = add_episode(conn, show_id, "tones", tone_path)
    ep_music = add_episode(conn, show_id, "music", music_path)
    played_path = os.path.join(WORK, "played.mp3")
    shutil.copy(tone_path, played_path)
    ep_played = add_episode(conn, show_id, "played", played_path)
    conn.execute("UPDATE episodes SET played = 1 WHERE id = ?", (ep_played,))
    conn.commit()
    status, _ = call("GET", f"/api/episodes/{ep_tone}/silence")
    check("map is 404 before analysis", status == 404, status)
    check("worker analyses the two unplayed episodes", run_worker() == 2)
    status, body = call("GET", f"/api/episodes/{ep_tone}/silence")
    check("map is served after analysis", status == 200 and len(body["intervals"]) == 3, (status, body))
    check("map carries the analyzer version", body and body["analyzer_version"] == silence.ANALYZER_VERSION, body)
    with open(silence.map_path(ep_tone)) as fh:
        stored = json.load(fh)
    check("stored map has file hash and size",
          stored["file_sha256"] == silence.file_hash(tone_path) and stored["file_size"] == os.path.getsize(tone_path), stored)
    status, body = call("GET", f"/api/episodes/{ep_music}/silence")
    check("music episode has an empty map", status == 200 and body["intervals"] == [], (status, body))
    status, _ = call("GET", f"/api/episodes/{ep_played}/silence")
    check("played episode is not analysed", status == 404, status)
    status, _ = call("GET", "/api/episodes/999999/silence")
    check("unknown episode is 404", status == 404, status)

    print("4. A changed file invalidates the map")
    make_music_file(tone_path)   # a new download replaced the file
    status, _ = call("GET", f"/api/episodes/{ep_tone}/silence")
    check("map is 404 after the file changed", status == 404, status)
    ver = conn.execute("SELECT silence_ver FROM episodes WHERE id = ?", (ep_tone,)).fetchone()[0]
    check("episode is due for analysis again", ver == 0, ver)
    check("worker analyses it again", run_worker() == 1)
    status, body = call("GET", f"/api/episodes/{ep_tone}/silence")
    check("new map fits the new file", status == 200 and body["intervals"] == [], (status, body))
    status, _ = call("POST", f"/api/episodes/{ep_music}/analyze")
    check("analyze route queues a new analysis", status == 200 and run_worker() == 1, status)
    status, _ = call("POST", "/api/episodes/999999/analyze")
    check("analyze of unknown episode is 404", status == 404, status)

    print("5. Settings")
    status, body = call("GET", "/api/settings/silence")
    check("default is off", status == 200 and body["on"] is False, body)
    call("POST", "/api/settings/silence", {"on": True})
    status, body = call("GET", "/api/settings/silence")
    check("default can be turned on", body["on"] is True, body)
    show = next(s for s in call("GET", "/api/shows")[1] if s["id"] == show_id)
    check("show uses the default", show["shorten_silence"] is None and show["silence_default"] is True, show)
    status, body = call("POST", f"/api/shows/{show_id}/silence", {"value": False})
    check("show override off", status == 200 and body == {"shorten_silence": 0, "effective": False}, body)
    status, body = call("GET", f"/api/episodes/{ep_tone}")
    check("episode carries the show override", body["show_silence"] == 0, body.get("show_silence"))
    status, body = call("POST", f"/api/shows/{show_id}/silence", {"value": None})
    check("show back to the default", body == {"shorten_silence": None, "effective": True}, body)
    status, _ = call("POST", "/api/shows/999999/silence", {"value": True})
    check("override of unknown show is 404", status == 404, status)

    print("6. Time saved")
    status, body = call("GET", "/api/stats/time-saved")
    check("stats start at zero", body["total"] == 0 and body["shows"] == [], body)
    call("POST", "/api/stats/time-saved", {"episode_id": ep_tone, "speed": 10, "silence": 4.5, "ads": 30})
    call("POST", "/api/stats/time-saved", {"episode_id": ep_tone, "speed": 2, "silence": 0.5, "ads": 0})
    call("POST", "/api/stats/time-saved", {"episode_id": ep_music, "speed": 1, "silence": -5, "ads": 9999})
    status, body = call("GET", "/api/stats/time-saved")
    check("totals by source", (body["speed"], body["silence"], body["ads"]) == (13, 5, 630), body)
    check("total is the sum", body["total"] == 648, body)
    check("rolled up per show", len(body["shows"]) == 1 and body["shows"][0]["ads"] == 630, body["shows"])
    status, body = call("GET", f"/api/stats/time-saved?episode_id={ep_tone}")
    check("per episode", (body["speed"], body["silence"], body["ads"]) == (12, 5, 30), body)
    status, _ = call("POST", "/api/stats/time-saved", {"episode_id": 999999, "speed": 1})
    check("report for unknown episode is 404", status == 404, status)

    print("7. Cleanup resets the map flag")
    conn.execute("UPDATE episodes SET played = 1, played_at = 1 WHERE id = ?", (ep_music,))
    conn.commit()
    workers.cleanup_played(conn)
    row = conn.execute("SELECT status, silence_ver FROM episodes WHERE id = ?", (ep_music,)).fetchone()
    check("cleaned episode is due for analysis after a new download", row["status"] == "new" and row["silence_ver"] == 0, dict(row))
    conn.close()

    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
