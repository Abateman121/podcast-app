"""Shorten Silence listening test. A throwaway tool, not part of the app.

It downloads one episode, finds the pauses with ffmpeg silencedetect at a few thresholds,
and serves a page that speeds a plain <audio> element up inside those pauses.
The point is to hear whether this sounds clean enough before the real feature is built.

Run (from the repo folder, with the app image):
  docker run -d --name podcast-silence-test -p 8097:8097 -v <repo>/tools/silence_test:/srv/silence:ro \
    -e DATA_DIR=/data <image> sh -c "cd /srv/silence && exec python3 -m uvicorn server:app --host 0.0.0.0 --port 8097"
"""
import json
import os
import re
import subprocess
import threading

import feedparser
import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

DATA_DIR = os.environ.get("DATA_DIR", "/data")
INDEX_PATH = os.path.join(DATA_DIR, "silence_test.json")
HERE = os.path.dirname(os.path.abspath(__file__))
UA = {"User-Agent": "podcast-app silence test"}
THRESHOLDS = (-30, -35, -40, -45, -50)   # dBFS. The spec starts at -35.
MIN_SILENCE = 0.25                        # seconds, spec 6.3.1 step 6
LOCK = threading.Lock()

app = FastAPI(title="Shorten Silence listening test")


class AddIn(BaseModel):
    url: str


def load_index():
    if not os.path.exists(INDEX_PATH):
        return []
    with open(INDEX_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_index(items):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(INDEX_PATH, "w", encoding="utf-8") as fh:
        json.dump(items, fh)


def resolve(url):
    """An audio address and a title. A feed address gives its newest episode."""
    head = requests.get(url, headers=UA, timeout=30, stream=True)
    head.raise_for_status()
    kind = (head.headers.get("Content-Type") or "").lower()
    if kind.startswith("audio/") or re.search(r"\.(mp3|m4a|aac|ogg|opus|wav)$", url.split("?")[0], re.I):
        head.close()
        return url, os.path.basename(url.split("?")[0])
    parsed = feedparser.parse(head.content)
    for entry in parsed.entries:
        for link in entry.get("enclosures", []):
            if link.get("href"):
                return link["href"], f"{parsed.feed.get('title', 'Show')}: {entry.get('title', 'Episode')}"
    raise ValueError("That link is not an audio file and not a feed with episodes.")


def download(url, path):
    with requests.get(url, headers=UA, stream=True, timeout=60) as resp:
        resp.raise_for_status()
        with open(path, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=256 * 1024):
                fh.write(chunk)


def duration_of(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", path],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip() or 0)


def detect(path, noise_db, total):
    """Silence intervals [[start, end], ...] in seconds of media time."""
    out = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-vn",
         "-af", f"aformat=channel_layouts=mono,aresample=16000,silencedetect=noise={noise_db}dB:d={MIN_SILENCE}",
         "-f", "null", "-"],
        capture_output=True, text=True,
    )
    intervals, start = [], None
    for line in out.stderr.splitlines():
        m = re.search(r"silence_start: (-?[\d.]+)", line)
        if m:
            start = max(0.0, float(m.group(1)))
        m = re.search(r"silence_end: (-?[\d.]+)", line)
        if m and start is not None:
            intervals.append([round(start, 3), round(float(m.group(1)), 3)])
            start = None
    if start is not None and total > start:
        intervals.append([round(start, 3), round(total, 3)])
    return intervals


def reencode(source, target):
    """An AAC copy with exact seeking. Variable bit rate MP3 files can drift in the browser after a jump."""
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-y", "-i", source, "-vn", "-c:a", "aac", "-b:a", "96k",
         "-movflags", "+faststart", target],
        capture_output=True, check=True,
    )


@app.get("/")
def page():
    return FileResponse(os.path.join(HERE, "page.html"), headers={"Cache-Control": "no-store"})


@app.get("/api/items")
def items():
    return load_index()


@app.post("/api/items")
def add(body: AddIn):
    with LOCK:
        index = load_index()
        item_id = max([i["id"] for i in index], default=0) + 1
        try:
            print(f"1. Resolving {body.url}")
            audio_url, title = resolve(body.url.strip())
            ext = os.path.splitext(audio_url.split("?")[0])[1].lower() or ".mp3"
            if len(ext) > 5:
                ext = ".mp3"
            original = os.path.join(DATA_DIR, f"silence_{item_id}{ext}")
            os.makedirs(DATA_DIR, exist_ok=True)
            print(f"2. Downloading {title}")
            download(audio_url, original)
            total = duration_of(original)
            maps = {}
            for n, noise in enumerate(THRESHOLDS, 1):
                print(f"3. Finding silence ({n}/{len(THRESHOLDS)}) at {noise} dB")
                maps[str(noise)] = detect(original, noise, total)
            print("4. Making the AAC copy")
            copy = os.path.join(DATA_DIR, f"silence_{item_id}_copy.m4a")
            reencode(original, copy)
        except (requests.RequestException, ValueError, subprocess.CalledProcessError) as exc:
            raise HTTPException(400, f"Could not prepare that episode ({exc}).")
        with open(os.path.join(DATA_DIR, f"silence_{item_id}.json"), "w", encoding="utf-8") as fh:
            json.dump(maps, fh)
        item = {
            "id": item_id, "title": title, "duration": total, "original": original, "copy": copy,
            "stats": {k: {"count": len(v), "seconds": round(sum(e - s for s, e in v), 1)} for k, v in maps.items()},
        }
        index.append(item)
        save_index(index)
        print(f"5. Done: {title}")
        return item


def find(item_id):
    for item in load_index():
        if item["id"] == item_id:
            return item
    raise HTTPException(404, "No such episode.")


@app.get("/api/items/{item_id}/maps")
def maps(item_id: int):
    find(item_id)
    with open(os.path.join(DATA_DIR, f"silence_{item_id}.json"), "r", encoding="utf-8") as fh:
        return json.load(fh)


@app.get("/audio/{item_id}/{which}")
def audio(item_id: int, which: str):
    item = find(item_id)
    return FileResponse(item["copy"] if which == "copy" else item["original"])
