# podcast-app *AI SLOP*

A self-hosted podcast app for myself. It pulls in shows from almost any link, downloads the latest episodes, transcribes them on the GPU, and automatically skips the ads while you listen.

**Current version: v0.0.14** (shown in the bottom right of the player bar on every screen)

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
- A debug endpoint (`/api/episodes/{id}/debug`) lists the phrase hits for an episode, including hits from phrases learned from your tags.

### Tagging missed ads
When an ad gets through, tap **Tag ad** in the player.
- The dialog is filled in with the last 30 seconds. Use **Now**, the 5 second nudges, and **Play** to set the exact start and end, then save.
- The ad is skipped right away in that episode and stays skipped after a re-scan.
- The app learns from it in two ways:
  - **Audio.** The ad is fingerprinted. A background worker looks for the same audio in every downloaded episode, in any show. This catches ads that are reused word for word, even with different encoding or volume.
  - **Phrases.** Phrases from the transcript that never appear in the show's normal speech are saved. When one shows up in another transcript, an ad is placed around it using the same lead and length as the one you tagged. This works on the saved transcripts, so no GPU run is needed.
- If something was skipped by mistake, open **Tag ad** and tap **Not an ad** on that range. That range plays from then on in that episode.
- Your tags are listed in the same dialog, and **Remove** undoes one. Removing a tag also removes what was learned from it.
- Each show page shows how many tags and phrases the app has learned from.
- Matching tagged audio is CPU only. It reads each episode once, about 10 seconds per hour of audio. It runs again over your library whenever you add or remove a tag.

### Intro and outro skipping
Each episode gets its own intro end time and outro start time. They are found in this order:
1. **Audio fingerprint of the show's theme.** Most shows reuse the same intro and outro music. Mark it once and the app finds it in every other episode by its sound, not by a fixed time, so inserted ads do not throw it off. It handles different encodings, volume changes, and a voice talking over the start of the music.
2. **Chapters.** If the file or the feed (`podcast:chapters`) has chapters titled like Intro, Outro, Theme, or Credits, those are used when there is no theme match.
3. **Fallback seconds.** If neither finds anything confident, the show's fallback intro and outro seconds are used, the same as before. 0 means no skip. Nothing is guessed below the confidence threshold.

Setting up a show:
- Open the show page and tap **Set intro/outro**.
- Pick any downloaded episode, press play, and use **Now** to mark where the intro starts and ends. Do the same for the outro if you want. The 0.1 second nudges and **Play** buttons help fine tune. Intro and outro can each be 3 to 120 seconds.
- Save. The app fingerprints that section as the show's template and re-checks all downloaded episodes of the show.
- To replace a template, mark it again. Use the delete buttons in the same dialog to remove one.
- **Re-detect intro/outro** on the show page redoes detection for the whole show. It uses the CPU only and does not run Whisper again.

Playback:
- The intro is skipped once, when playback reaches where it starts. A cold open before the intro plays normally.
- If you seek back into the intro after it was skipped, it is not skipped again.
- The outro is skipped to the end of the episode, which moves on to the next one.
- The **Intro/outro: on/off** toggle in the player turns all of this on or off, and the choice is remembered on that device.
- Detection runs once per episode after download, on its own worker, so episodes are playable straight away.
- A debug endpoint (`/api/episodes/{id}/markers`) shows the source, confidence, and times for an episode, and what the player will actually skip.

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

## Changes in v0.0.14
- Automatic intro and outro detection for every episode. It uses an audio fingerprint of the show's theme, then chapters, then the fixed seconds as a fallback. The fixed seconds are now called fallback seconds on the show page.
- New **Set intro/outro** tool on each show page to mark the theme once, with **Re-detect intro/outro** to redo a show.
- A cold open before the intro is no longer skipped, and seeking back into a skipped intro does not skip it again.
- New **Tag ad** button in the player for ads that were missed. Tagged ads are skipped at once, and the app learns them by audio and by phrase to catch them in other episodes.
- Added **Not an ad** for ranges that were skipped by mistake.
- New settings `SKIP_CONFIDENCE`, `AD_AUDIO_CONFIDENCE`, `INTRO_WINDOW_SECONDS`, and `OUTRO_WINDOW_SECONDS`.
- Added numpy and scipy. The database upgrades itself on start.

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
| `SKIP_CONFIDENCE` | `0.6` | How sure the intro/outro theme match must be (0 to 1) before it is used |
| `AD_AUDIO_CONFIDENCE` | `0.5` | How sure a match to a tagged ad's audio must be (0 to 1) |
| `INTRO_WINDOW_SECONDS` | `240` | How much of the start of an episode is searched for the intro |
| `OUTRO_WINDOW_SECONDS` | `240` | How much of the end of an episode is searched for the outro |
| `DATA_DIR` | `/data` | Where the database, audio, transcripts, and templates are stored |
| `DISABLE_WORKERS` | unset | Set to `1` to run the web app without background workers |

The `/data` volume holds the SQLite database, downloaded audio, saved transcripts, and the audio snippets behind intro/outro templates and tagged ads (`/data/templates`). The snippets let the fingerprints be rebuilt if the matching method changes.

## How it is built
- **Backend:** Python, FastAPI, SQLite (`app/main.py`, `feeds.py`, `sources.py`, `workers.py`, `adscan.py`, `db.py`, plus `fingerprint.py`, `markers.py`, and `adlearn.py` for intro/outro detection and learning from tagged ads)
- **Frontend:** a single static page, `app/static/index.html`, with no build step
- **Workers:** five background threads for feed refresh, downloading, ad scanning, intro/outro detection, and matching tagged ads. The last two use the CPU only.
- **Image:** Nvidia CUDA runtime base with ffmpeg, built and published by GitHub Actions
