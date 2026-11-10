# podcast-app

A self-hosted podcast app for myself. It pulls in shows from almost any link, downloads the latest episodes, transcribes them on the GPU, and automatically skips the ads while you listen.

**Current version: v0.0.13** (shown in the bottom right of the player bar on every screen)

## Features

### Adding shows
- Paste a link and the app works out the RSS feed for you. Supported sources:
  - Direct RSS/Atom feed URLs, or any web page that advertises a feed
  - Apple Podcasts show links
  - Spotify show links (matched to the public feed through Apple Podcasts)
  - Amazon Music podcast links (matched the same way)
  - YouTube channels and playlists (audio is downloaded with yt-dlp, latest 15 videos)
- Import a whole subscription list from an OPML file. Imports queue only the newest episode per show so a big file does not flood the GPU.
- Delete a show, which also removes its downloaded audio.
- Shows added through Spotify or Amazon tell you which Apple Podcasts show they were matched to, so you can check it is the right one.

### Episodes and downloads
- Feeds refresh automatically (every `REFRESH_MINUTES`, default 30) or on demand with **Check for episodes**.
- The newest `AUTO_DOWNLOAD_LATEST` episodes of each show (default 3) are downloaded automatically.
- Older episodes can be downloaded manually by tapping them on a show page.
- A **Queue** tab shows every episode that is downloading, waiting, scanning, or failed, with live progress bars, download sizes, error messages, and a **Retry** button. The tab title shows a count of active items.
- Anything that was in progress when the container stopped is put back in the queue on restart.

### Ad skipping
- Downloaded episodes are transcribed locally with faster-whisper (Nvidia GPU, default model `small.en`).
- The transcript is scanned for sponsor read phrases ("brought to you by", "promo code", "dot com slash", and so on) to work out where ads start and end.
- Detected ads are skipped automatically during playback, with a short "Skipped ad" notice.
- Transcripts are saved, so **Re-scan ads** on a show page re-applies the current detection rules without running the GPU again.
- The **Ads: on/off** toggle in the player turns skipping on or off, and the choice is remembered on that device.
- A debug endpoint (`/api/episodes/{id}/debug`) lists the phrase hits for an episode.

### Intro and outro skipping
- Set a per-show intro and outro length (0 to 600 seconds) on the show page.
- Playback starts after the intro, and the outro is skipped at the end of the episode.
- The **Intro/outro: on/off** toggle in the player turns this on or off, and the choice is remembered on that device.

### Smart playlists
- Create playlists from rules:
  - Which shows to include (none selected means all)
  - Maximum length in minutes
  - Only episodes from the last N days
  - Newest first or oldest first
  - Unplayed only
- Playlists update automatically as new episodes arrive and are limited to 50 episodes.
- Playing a playlist continues through it episode by episode.

### Player
- Fixed player bar on every screen with seek bar, elapsed time, and time remaining (adjusted for playback speed).
- Back 15 seconds and forward 30 seconds buttons.
- **Playback speed from 0.5x to 3.0x in 0.05x steps.** Use the `−` and `+` buttons around the speed label. Tap the speed label to reset to 1.0x. The chosen speed carries over to the next episode.
- Progress is saved every few seconds, so episodes resume where you left off. Finished episodes are marked as played and greyed out.
- Media Session support: lock screen and headphone controls for play, pause, seek, and next track, plus episode artwork.
- Layout follows your system light or dark theme and is safe-area aware for phones.

## Changes in v0.0.13
- Added the app version in the bottom right of the player bar, so it appears on every screen.
- Playback speed now changes in 0.05x increments instead of cycling through a fixed list (1.0, 1.25, 1.5, 1.75, 2.0, 0.8). Added `−`/`+` buttons and a tap-to-reset on the speed label.
- Rewrote this README to cover all current features.

## Releasing a new version
The version is set in one place: the `<div id="ver">` line in `app/static/index.html`. **Bump it with every update** and add a line to the changes section above.

Pushing to `main` triggers the GitHub Actions workflow, which builds the image and publishes it to `ghcr.io/abateman121/podcast-app` as `latest` (and as the commit SHA).

## Running it
Example `docker-compose.yml` (Unraid with an Nvidia GPU):

```yaml
services:
  podcast-app:
    image: ghcr.io/abateman121/podcast-app:latest
    pull_policy: always
    container_name: podcast-app
    restart: unless-stopped
    runtime: nvidia
    ports:
      - "8099:8099"
    volumes:
      - /mnt/cache/appdata/podcast-app:/data
    environment:
      TZ: "America/Los_Angeles"
      NVIDIA_VISIBLE_DEVICES: "all"
      NVIDIA_DRIVER_CAPABILITIES: "compute,utility"
      WHISPER_MODEL: "small.en"
      WHISPER_DEVICE: "cuda"
      WHISPER_COMPUTE: "int8"
      REFRESH_MINUTES: "30"
      AUTO_DOWNLOAD_LATEST: "3"
```

### Settings
| Variable | Default | What it does |
|---|---|---|
| `WHISPER_MODEL` | `small.en` | Whisper model used for transcription |
| `WHISPER_DEVICE` | `cuda` | `cuda` or `cpu` |
| `WHISPER_COMPUTE` | `int8` | Whisper compute type |
| `REFRESH_MINUTES` | `30` | How often feeds are checked |
| `AUTO_DOWNLOAD_LATEST` | `3` | Newest episodes per show to download automatically |
| `DATA_DIR` | `/data` | Where the database, audio, and transcripts are stored |
| `DISABLE_WORKERS` | unset | Set to `1` to run the web app without background workers |

The `/data` volume holds the SQLite database, downloaded audio, and saved transcripts.

## How it is built
- **Backend:** Python, FastAPI, SQLite (`app/main.py`, `feeds.py`, `sources.py`, `workers.py`, `adscan.py`, `db.py`)
- **Frontend:** a single static page, `app/static/index.html`, with no build step
- **Workers:** three background threads for feed refresh, downloading, and ad scanning
- **Image:** Nvidia CUDA runtime base with ffmpeg, built and published by GitHub Actions
