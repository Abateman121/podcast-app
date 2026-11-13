import os
import sqlite3

DATA_DIR = os.environ.get("DATA_DIR", "/data")
DB_PATH = os.path.join(DATA_DIR, "podcasts.db")
AUDIO_DIR = os.path.join(DATA_DIR, "audio")
TRANSCRIPT_DIR = os.path.join(DATA_DIR, "transcripts")
TEMPLATE_DIR = os.path.join(DATA_DIR, "templates")
AD_SAMPLE_DIR = os.path.join(TEMPLATE_DIR, "ads")


def transcript_path(episode_id):
    return os.path.join(TRANSCRIPT_DIR, f"{episode_id}.json")


def template_path(show_id, kind):
    """Audio snippet a show template was built from, so fingerprints can be rebuilt."""
    return os.path.join(TEMPLATE_DIR, f"{show_id}_{kind}.wav")


def ad_sample_path(tag_id):
    return os.path.join(AD_SAMPLE_DIR, f"{tag_id}.wav")

# Episode status flow:
# new -> queued -> downloading -> downloaded -> scanning -> ready
# error (download failed), scan_error (downloaded but ad scan failed)
PLAYABLE = ("downloaded", "scanning", "ready", "scan_error")

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
-- Shows a smart playlist takes episodes from. priority is high, normal or low. rank orders the high shows.
CREATE TABLE IF NOT EXISTS playlist_shows (
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    show_id INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
    priority TEXT NOT NULL DEFAULT 'normal',
    rank INTEGER,
    PRIMARY KEY (playlist_id, show_id)
);
-- Single episodes added to (include) or hidden from (exclude) a playlist by hand.
CREATE TABLE IF NOT EXISTS playlist_overrides (
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    episode_id INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    PRIMARY KEY (playlist_id, episode_id)
);
-- Hand-made order. For the Up Next playlist these rows are also its contents.
CREATE TABLE IF NOT EXISTS playlist_order (
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    episode_id INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    PRIMARY KEY (playlist_id, episode_id)
);
-- Ads the user tagged by hand. kind 'ad' is a missed ad, 'not_ad' is a range that was skipped by mistake.
-- fp holds the audio fingerprint of a missed ad, anchors_json the phrases learned from its transcript.
CREATE TABLE IF NOT EXISTS ad_tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    show_id INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
    kind TEXT NOT NULL DEFAULT 'ad',
    start_sec REAL NOT NULL,
    end_sec REAL NOT NULL,
    fp BLOB,
    anchors_json TEXT DEFAULT '[]',
    created INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ad_tags_episode ON ad_tags(episode_id);
CREATE INDEX IF NOT EXISTS ad_tags_show ON ad_tags(show_id);
-- Sections of audio found in more than one episode of a show. status 'new' waits for the user,
-- 'ignored' is kept so the same section is not suggested again.
CREATE TABLE IF NOT EXISTS repeats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    show_id INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'new',
    guess TEXT NOT NULL DEFAULT 'ad',
    seconds REAL NOT NULL DEFAULT 0,
    episodes INTEGER NOT NULL DEFAULT 0,
    compared INTEGER NOT NULL DEFAULT 0,
    occ_json TEXT NOT NULL DEFAULT '[]',
    created INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS repeats_show ON repeats(show_id);
-- Small counters shared by the web app and the workers.
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);
"""

SHOW_COLUMNS = {
    "auto_latest": "INTEGER",
    "intro_skip": "INTEGER",
    "outro_skip": "INTEGER",
    "intro_template": "BLOB",
    "intro_template_len": "REAL",
    "outro_template": "BLOB",
    "outro_template_len": "REAL",
    "repeat_sig": "TEXT",           # which episodes were last compared for repeated audio
    "newer_than_days": "INTEGER",   # playlists only take episodes this recent (0 or NULL = no limit)
    "keep_last": "INTEGER",         # keep the newest N unplayed episodes downloaded (NULL = the default, 0 = none)
    "subscribed": "INTEGER DEFAULT 1",   # 0 = history only: no refresh, no downloads, not in playlists
    "overcast_id": "TEXT",          # set by the Overcast import, so a second import finds the same show
}
EPISODE_COLUMNS = {
    "markers_json": "TEXT DEFAULT '{}'",
    "chapters_url": "TEXT",
    "ads_audio_json": "TEXT DEFAULT '[]'",
    "ads_audio_ver": "INTEGER DEFAULT 0",
    "starred": "INTEGER DEFAULT 0",
    "removed": "INTEGER DEFAULT 0",       # taken off the lists by the user
    "played_at": "INTEGER DEFAULT 0",     # when it was finished, for the cleanup of played files
    "dl_priority": "INTEGER DEFAULT 0",   # 1 = next up in the player, so download and scan it first
    "overcast_id": "TEXT",                # set by the Overcast import
}
PLAYLIST_COLUMNS = {
    "kind": "TEXT DEFAULT 'smart'",       # smart or builtin
    "builtin_type": "TEXT",               # queue, starred or downloaded
    "icon": "TEXT",
    "color": "TEXT",
    "position": "INTEGER DEFAULT 0",
    "include_all_shows": "INTEGER DEFAULT 0",
    "sort": "TEXT DEFAULT 'oldest'",
    "priority_mode": "TEXT DEFAULT 'by_rank'",
    "play_top_next": "INTEGER DEFAULT 0",
    "continuous": "INTEGER DEFAULT 1",
    "source": "TEXT",
    "created": "INTEGER DEFAULT 0",
    "updated": "INTEGER DEFAULT 0",
    "converted": "INTEGER DEFAULT 0",     # 1 once the old rules_json has been turned into the columns above
}


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def meta_get(conn, key):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else 0


def meta_bump(conn, key):
    """Add one to a counter and return the new value. The caller commits."""
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, 1) ON CONFLICT(key) DO UPDATE SET value = value + 1", (key,)
    )
    return meta_get(conn, key)


def meta_set(conn, key, value):
    """Store a number. The caller commits."""
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, int(value))
    )


def add_columns(conn, table, columns):
    existing = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    for column, decl in columns.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def init():
    os.makedirs(AUDIO_DIR, exist_ok=True)
    os.makedirs(TRANSCRIPT_DIR, exist_ok=True)
    os.makedirs(AD_SAMPLE_DIR, exist_ok=True)
    conn = connect()
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    add_columns(conn, "shows", SHOW_COLUMNS)
    add_columns(conn, "episodes", EPISODE_COLUMNS)
    add_columns(conn, "playlists", PLAYLIST_COLUMNS)
    conn.execute("CREATE INDEX IF NOT EXISTS episodes_overcast ON episodes(overcast_id)")
    # Anything mid-flight when the container stopped goes back a step.
    conn.execute("UPDATE episodes SET status = 'queued' WHERE status = 'downloading'")
    conn.execute("UPDATE episodes SET status = 'downloaded' WHERE status = 'scanning'")
    conn.commit()
    conn.close()
