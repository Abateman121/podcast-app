"""Checks for the Overcast import, run against the small invented export in tests/fixtures.

Run it inside a container where the app listens on port 8099 with an empty DATA_DIR and DISABLE_WORKERS=1.
The script serves the test feeds itself on port 8098. See docs/overcast-import.md, "Testing".
"""
import http.server
import json
import os
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.utils import formatdate

sys.path.insert(0, os.environ.get("APP_DIR", "/srv/app"))
import overcast  # noqa: E402

BASE = "http://127.0.0.1:8099"
HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "fixtures", "overcast-small.opml")
DB_PATH = os.path.join(os.environ.get("DATA_DIR", "/data"), "podcasts.db")
NOW = int(time.time())
passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
    else:
        failed += 1
    print(f"[{passed + failed}] {'ok  ' if ok else 'FAIL'} {name}{'' if ok else '  -> ' + str(detail)}")


def stamp(text):
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp())


def item(guid, title, when, url):
    return (f"<item><guid>{guid}</guid><title>{title}</title><pubDate>{formatdate(when)}</pubDate>"
            f'<enclosure url="{url}" type="audio/mpeg" length="1000"/></item>')


def feed(title, items):
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>{title}</title>{"".join(items)}</channel></rss>'.encode()


FEEDS = {
    "/feed-a.xml": feed("Harbor Light News", [
        item("a7", "Fresh This Week", NOW - 2 * 86400, "http://audio.example.test/harbor/ep7.mp3"),
        item("a4", "Fog Signals", stamp("2024-03-22T11:00:00"), "http://audio.example.test/harbor/ep4.mp3"),
        item("a3", "Tide Tables - a short history!", stamp("2024-03-15T20:00:00"), "http://cdn.example.test/harbor/ep3-new.mp3"),
        item("a2", "The Lighthouse Keeper (Remastered)", stamp("2024-03-08T11:00:00"),
             "https://newtracker.example.test/s/audio.example.test/harbor/ep2.mp3?key=new"),
        item("a1", "Ferry Schedules Explained", stamp("2024-03-01T11:00:00"), "http://audio.example.test/harbor/ep1.mp3?source=feed"),
        item("a6", "Old And Untouched", stamp("2023-06-01T11:00:00"), "http://audio.example.test/harbor/ep6.mp3"),
        # Same title as export episode 2003 but two years away: must not be taken as its match.
        item("a8", "Tide Tables: A Short History", stamp("2022-03-15T11:00:00"), "http://audio.example.test/harbor/ep8.mp3"),
    ]),
    "/feed-b.xml": feed("Quiet Orbit", [
        item("b3", "New Launch", NOW - 86400, "http://audio.example.test/orbit/launch.mp3"),
        item("b2", "Slow Comets", stamp("2024-02-17T12:00:00"), "http://audio.example.test/orbit/comets.mp3"),
        item("b1", "Small Moons", stamp("2024-02-10T12:00:00"), "http://audio.example.test/orbit/moons.mp3"),
    ]),
}


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = FEEDS.get(self.path)
        self.send_response(200 if body else 404)
        self.send_header("Content-Type", "application/rss+xml")
        self.end_headers()
        self.wfile.write(body or b"")

    def log_message(self, *args):
        pass


def call(method, path, body=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "text/xml" if raw is not None else "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"null")


def rows(sql, args=()):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    out = conn.execute(sql, args).fetchall()
    conn.close()
    return out


def one(sql, args=()):
    return rows(sql, args)[0][0]


def run_job(dry, keep_days):
    code, _ = call("POST", "/api/import/overcast/run", {"dry": dry, "keep_days": keep_days})
    assert code == 200, code
    for _ in range(120):
        status = call("GET", "/api/import/overcast/status")[1]
        if status["state"] != "running":
            return status
        time.sleep(0.25)
    raise SystemExit("the import job did not finish")


def by_oc(overcast_id):
    return rows("SELECT * FROM episodes WHERE overcast_id = ?", (overcast_id,))[0]


def by_guid(guid):
    return rows("SELECT * FROM episodes WHERE guid = ?", (guid,))[0]


threading.Thread(target=http.server.ThreadingHTTPServer(("127.0.0.1", 8098), Handler).serve_forever, daemon=True).start()
with open(FIXTURE, "rb") as fh:
    DATA = fh.read()

print("--- 1. pure functions")
parsed = overcast.parse(DATA)
check("parse totals", overcast.totals(parsed) == {"feeds": 3, "subscribed": 2, "episodes": 9, "played": 6, "in_progress": 3, "playlists": 5},
      overcast.totals(parsed))
check("unknown sorting gives one warning", len(parsed["warnings"]) == 1 and "random-shuffle" in parsed["warnings"][0], parsed["warnings"])
check("sortings mapped", [p["sort"] for p in parsed["playlists"]] == ["oldest", "oldest_by_show", "newest", "newest_by_show", "oldest"])
check("dates with an offset", parsed["feeds"][0]["episodes"][0]["published"] == stamp("2024-03-01T11:00:00"))
check("normalize drops tracker and query",
      overcast.normalize_url("https://t.example.test/e/s.example.test/id/audio.example.test/x/Ep.mp3?a=1") == "audio.example.test/x/ep.mp3")
check("normalize leaves a plain address", overcast.normalize_url("http://audio.example.test/x/ep.mp3") == "audio.example.test/x/ep.mp3")
for bad, label in ((b"not xml at all", "not XML"), (b"<opml><body><outline type='rss' xmlUrl='http://x/f'/></body></opml>", "no Overcast ids")):
    try:
        overcast.parse(bad)
        check("parse rejects " + label, False)
    except ValueError:
        check("parse rejects " + label, True)

print("--- 2. upload and guards")
code, body = call("POST", "/api/import/overcast/run", {"dry": True})
check("run without an upload is refused", code == 400, (code, body))
code, body = call("POST", "/api/import/overcast", raw=b"<opml><body/></opml>")
check("a file without Overcast shows is refused", code == 400, (code, body))
code, body = call("POST", "/api/import/overcast", raw=DATA)
check("upload gives the summary", code == 200 and body["feeds"] == 3 and body["episodes"] == 9 and body["played"] == 6
      and body["in_progress"] == 3 and body["playlists"] == 5, (code, body))

print("--- 3. a show that is already here, with local state")
code, body = call("POST", "/api/shows", {"url": "http://127.0.0.1:8098/feed-a.xml"})
check("show A added the normal way", code == 200, (code, body))
conn = sqlite3.connect(DB_PATH)
conn.execute("UPDATE episodes SET position = 99 WHERE guid = 'a4'")
conn.commit()
conn.close()
before = {r["guid"]: r["status"] for r in rows("SELECT guid, status FROM episodes")}

print("--- 4. dry run")
status = run_job(True, 14)
check("dry run finished", status["state"] == "done" and status["result"]["dry"] is True, status)
r = status["result"]
check("dry sum", r["sum"] == {"linked": 0, "exact": 4, "normalized": 1, "title_match": 1, "history": 3,
                              "changed": 11, "keep_current": 3, "mark_removed": 2}, r["sum"])
a, b, c = r["feeds"]
check("show A report", (a["new_show"], a["exact"], a["normalized"], a["title_match"], a["history"], a["mark_removed"], a["keep_current"])
      == (False, 2, 1, 1, 1, 2, 2), a)
check("show B report", (b["new_show"], b["exact"], b["history"], b["mark_removed"], b["keep_current"]) == (True, 2, 0, 0, 1), b)
check("show C report: feed error, history only", bool(c["feed_error"]) and c["subscribed"] is False and c["history"] == 2 and c["keep_current"] == 0, c)
check("warnings: sorting and feed error", len(r["warnings"]) == 2, r["warnings"])
check("rough download count", r["may_download"] == 3, r["may_download"])
check("dry playlists", [(p["title"], p["shows"], p["high"], p["include"], p["manual"], p["manual_missing"]) for p in r["playlists"]]
      == [("Morning", 2, 1, 0, 1, 1), ("Deep Dives", 1, 0, 0, 0, 0), ("Fresh", 2, 0, 0, 0, 0), ("Keepers", 1, 0, 1, 0, 0), ("Odd", 1, 0, 0, 0, 0)],
      r["playlists"])
check("dry run saved nothing", one("SELECT COUNT(*) FROM shows") == 1 and one("SELECT COUNT(*) FROM playlists WHERE source = 'overcast'") == 0
      and one("SELECT COUNT(*) FROM episodes WHERE overcast_id IS NOT NULL OR removed = 1 OR played = 1") == 0
      and one("SELECT COUNT(*) FROM meta WHERE key = 'overcast_first_import'") == 0)
keep_all = run_job(True, None)["result"]
check("empty days keeps every unplayed episode", keep_all["sum"]["mark_removed"] == 0 and keep_all["sum"]["keep_current"] == 5, keep_all["sum"])

print("--- 5. the import")
status = run_job(False, 14)
r = status["result"]
check("import finished", status["state"] == "done" and r["dry"] is False, status)
check("same numbers as the dry run", r["sum"]["changed"] == 11 and r["sum"]["history"] == 3 and r["sum"]["mark_removed"] == 2, r["sum"])
check("acceptance numbers", r["database"] == {"feeds": 3, "episodes": 9, "played": 6, "in_progress": 3, "playlists": 5}, r["database"])
e = by_oc("2001")
check("exact match: played, removed, played_at", (e["guid"], e["played"], e["removed"], e["played_at"]) == ("a1", 1, 1, stamp("2024-03-02T13:15:00")),
      dict(e))
check("a queued download of a played episode is taken back", before["a1"] != "queued" or e["status"] == "new", (before["a1"], e["status"]))
e = by_oc("2002")
check("normalized match: played, not removed", (e["guid"], e["played"], e["removed"]) == ("a2", 1, 0), dict(e))
e = by_oc("2003")
check("title match: progress becomes position", (e["guid"], e["played"], e["removed"], e["position"]) == ("a3", 0, 1, 754), dict(e))
check("same title far away is not matched", by_guid("a8")["overcast_id"] is None)
e = by_oc("2004")
check("a local position is kept", (e["guid"], e["position"], e["removed"]) == ("a4", 99, 0), dict(e))
e = by_oc("2005")
check("history row", (e["guid"], e["title"], e["played"], e["status"], e["file_path"]) == ("overcast:2005", "Pilot Episode", 1, "new", None), dict(e))
e = by_oc("2102")
check("played wins over progress", (e["played"], e["position"]) == (1, 0), dict(e))
e = by_oc("2202")
check("fraction of a second kept or rounded", abs(e["position"] - 45.5) <= 0.5 and e["removed"] == 1, dict(e))
check("old untouched feed episodes are taken off", by_guid("a6")["removed"] == 1 and by_guid("a8")["removed"] == 1)
check("recent feed episodes stay", by_guid("a7")["removed"] == 0 and by_guid("b3")["removed"] == 0)
shows = {s["overcast_id"]: s for s in rows("SELECT * FROM shows")}
check("three shows, C not subscribed", len(shows) == 3 and shows["1003"]["subscribed"] == 0 and shows["1001"]["subscribed"] == 1
      and shows["1002"]["subscribed"] == 1, {k: dict(v) for k, v in shows.items()})
check("titles: from the feed, or from the export when the feed fails", shows["1002"]["title"] == "Quiet Orbit" and shows["1003"]["title"] == "Lantern Archive")
check("nothing was queued by the import", one("SELECT COUNT(*) FROM episodes WHERE show_id != ? AND status != 'new'", (shows["1001"]["id"],)) == 0)
check("first import time stored", abs(one("SELECT value FROM meta WHERE key = 'overcast_first_import'") - NOW) < 600)

print("--- 6. playlists")
pls = {p["name"]: p for p in rows("SELECT * FROM playlists WHERE source = 'overcast'")}
check("five playlists", sorted(pls) == ["Deep Dives", "Fresh", "Keepers", "Morning", "Odd"], sorted(pls))
check("sort settings", [pls[n]["sort"] for n in ("Morning", "Deep Dives", "Fresh", "Keepers", "Odd")]
      == ["oldest", "oldest_by_show", "newest", "newest_by_show", "oldest"])
check("priority by rank, play top next off", all(p["priority_mode"] == "by_rank" and not p["play_top_next"] and not p["include_all_shows"]
                                                 for p in pls.values()))


def shows_of(name):
    return [(r["show_id"], r["priority"]) for r in rows(
        "SELECT show_id, priority FROM playlist_shows WHERE playlist_id = ? ORDER BY show_id", (pls[name]["id"],))]


A, B, C = shows["1001"]["id"], shows["1002"]["id"], shows["1003"]["id"]
check("Morning shows: B high, A normal", shows_of("Morning") == sorted([(A, "normal"), (B, "high")]), shows_of("Morning"))
check("Fresh shows", shows_of("Fresh") == sorted([(B, "normal"), (C, "normal")]), shows_of("Fresh"))
check("unknown show id skipped", shows_of("Odd") == [(A, "normal")], shows_of("Odd"))
check("include override, unknown episode skipped", [(r["episode_id"], r["action"]) for r in rows(
    "SELECT * FROM playlist_overrides WHERE playlist_id = ?", (pls["Keepers"]["id"],))] == [(by_oc("2101")["id"], "include")])
check("manual order, unknown episode skipped", [r["episode_id"] for r in rows(
    "SELECT * FROM playlist_order WHERE playlist_id = ?", (pls["Morning"]["id"],))] == [by_oc("2004")["id"]])
code, body = call("GET", f"/api/playlists/{pls['Morning']['id']}/episodes")
got = [x["id"] for x in body["episodes"]]
check("Morning plays: own order first, then the high show, then the rest",
      got == [by_guid("a4")["id"], by_guid("b3")["id"], by_guid("a7")["id"]], got)

print("--- 7. a second run changes nothing")
counts = (one("SELECT COUNT(*) FROM shows"), one("SELECT COUNT(*) FROM episodes"), one("SELECT COUNT(*) FROM playlists"))
state = [tuple(x) for x in rows("SELECT id, played, removed, position, played_at, overcast_id, status FROM episodes ORDER BY id")]
conn = sqlite3.connect(DB_PATH)
conn.execute("UPDATE episodes SET played = 1, played_at = 5 WHERE guid = 'a7'")   # played here after the import
conn.commit()
conn.close()
state = [tuple(x) for x in rows("SELECT id, played, removed, position, played_at, overcast_id, status FROM episodes ORDER BY id")]
r = run_job(False, 14)["result"]
check("second run: all linked, no changes", r["sum"]["linked"] == 9 and r["sum"]["changed"] == 0 and r["sum"]["history"] == 0
      and r["sum"]["mark_removed"] == 0, r["sum"])
check("second run: no duplicates", counts == (one("SELECT COUNT(*) FROM shows"), one("SELECT COUNT(*) FROM episodes"),
                                               one("SELECT COUNT(*) FROM playlists")))
check("second run: episode state identical", state == [tuple(x) for x in rows(
    "SELECT id, played, removed, position, played_at, overcast_id, status FROM episodes ORDER BY id")])
check("second run: playlists marked as existing", all(p["exists"] for p in r["playlists"]))
check("second run: acceptance numbers unchanged", r["database"] == {"feeds": 3, "episodes": 9, "played": 6, "in_progress": 3, "playlists": 5},
      r["database"])
check("second run: one manual row, one override", one("SELECT COUNT(*) FROM playlist_order WHERE playlist_id = ?", (pls["Morning"]["id"],)) == 1
      and one("SELECT COUNT(*) FROM playlist_overrides WHERE playlist_id = ?", (pls["Keepers"]["id"],)) == 1)

print(f"--- {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
