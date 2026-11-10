import os
import sqlite3

DATA_DIR = os.environ.get("DATA_DIR", "/data")
DB_PATH = os.path.join(DATA_DIR, "podcasts.db")
AUDIO_DIR = os.path.join(DATA_DIR, "audio")

SCHEMA = """
CREATE TABLE IF NOT EXISTS shows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    feed_url TEXT UNIQUE NOT NULL,
    title TEXT,
    image TEXT
);
CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    show_id INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
    guid TEXT NOT NULL,
    title TEXT,
    published INTEGER DEFAULT 0,
    duration INTEGER DEFAULT 0,
    audio_url TEXT,
    file_path TEXT,
    status TEXT DEFAULT 'new',
    wanted INTEGER DEFAULT 0,
    played INTEGER DEFAULT 0,
    position REAL DEFAULT 0,
    ads_json TEXT DEFAULT '[]',
    UNIQUE(show_id, guid)
);
CREATE TABLE IF NOT EXISTS playlists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    rules_json TEXT NOT NULL
);
"""


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init():
    os.makedirs(AUDIO_DIR, exist_ok=True)
    conn = connect()
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
