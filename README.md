# podcast-app *AI SLOP*

A self-hosted podcast app for myself. It pulls in shows from almost any link, downloads the latest episodes, transcribes them on the GPU, and automatically skips the ads while you listen.

**Current version: v0.0.16** (shown in the bottom right of the player bar on every screen)

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
- **Keep N downloaded**: the newest N unplayed episodes of each subscribed show are downloaded automatically. N is set per show on the show page. Empty means the default (`AUTO_DOWNLOAD_LATEST`, default 5). 0 means none.
- Any other episode can be downloaded by hand with its **Download** button. An episode that is next up in the player is downloaded automatically, ahead of everything else.
- **Cleanup**: the audio file of a finished episode is deleted `CLEANUP_HOURS` (default 12) after it was finished. Starred episodes keep their file. Unplayed episodes are never deleted. The episode stays in the list and can be downloaded again. Transcripts, ad tags, and learned ads are kept.
- A show can be **unsubscribed** on its page. It stops refreshing and downloading, its episodes leave the playlists, and its history stays. **Subscribe again** brings it back.
- A **Downloads** tab shows every episode that is downloading, waiting, scanning, or failed, with live progress bars, download sizes, error messages, and a **Retry** button. The tab title shows a count of active items.
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

### Repeated audio across episodes
The app compares the newest downloaded episodes of each show and looks for sections that sound the same in more than one of them. Theme music, reused sponsor reads, promos, and jingles all show up this way. No template or tag is needed first.
- Each show page has a **Repeated audio** list. Every entry shows how long the section is, how many of the compared episodes have it, where it sits, and a guess: intro, outro, or ad.
- **Play** lets you hear it. Then pick **Use as intro**, **Use as outro**, **It is an ad**, or **Ignore**.
- Intro and outro choices become the show's template, the same as **Set intro/outro**. An ad choice becomes a tagged ad, the same as **Tag ad**, so it is also found in other episodes.
- Nothing is skipped until you choose. Ignored sections are not suggested again.
- Sections the player already skips are left out of the list.
- It runs by itself when the newest downloaded episodes of a show change. **Compare again** runs it on demand.
- It is CPU only and reads each compared episode in full, about 10 seconds per hour of audio. `REPEAT_EPISODES` sets how many episodes are compared.
- Limits: it matches the same recording, not the same words. A host reading the same ad copy live each week is not found this way. Tag those by hand so the phrases are learned.

### Smart playlists
- The Playlists tab shows every playlist as a button with its icon, colour, and count of unplayed episodes. An empty playlist is grey.
- A smart playlist is a set of rules, not a stored list. It always shows every unplayed episode of its shows, downloaded or not. Episodes that are not on disk are grey and have a **Download** button.
- Playlist settings:
  - Title, icon, and colour
  - Shows: all shows, or a picked list. Each picked show is **High**, **Normal**, or **Low** priority. High shows always come first and Low shows last.
  - Order: oldest first, newest first, or grouped by show (oldest or newest)
  - Priority shows: keep each High show together in the order you ranked them, or mix the High shows by date
  - **Play the top episode next**: when an episode ends, play whatever is at the top of the playlist, so a new High episode jumps the line
  - **Continuous play**: keep going when an episode ends
- Single episodes can be added to a playlist or hidden from it (the ⋯ button on an episode). Both are listed in the playlist settings with an Undo button.
- Episodes can be reordered by dragging, or with the ↑ and ↓ buttons. Your order stays on top, and new episodes land after it. **Clear my order** goes back to the rule order. Changing the order rule clears it too.
- Three built-in playlists:
  - **Up Next**: your own queue. **Play Next** and **Play Last** on any episode add to it.
  - **Starred**: episodes you starred. Starring also protects the file from cleanup.
  - **Downloaded**: everything on disk.
- Per show, **Only from the last N days** (on the show page) still limits what that show puts in playlists.
- When an episode ends, the server works out the next one. If it is not downloaded yet, the player moves on to the next downloaded episode and says so.
- An episode can be **removed from all playlists** without marking it played. It can be put back from the show page.

### Player
- Fixed player bar on every screen with seek bar, elapsed time, and time remaining (adjusted for playback speed).
- A coloured strip above the seek bar shows what was detected in the episode: red for ads, blue for the intro and outro. It updates while an episode is still being scanned and when you tag an ad.
- Back 15 seconds and forward 30 seconds buttons.
- **Playback speed from 0.5x to 3.0x in 0.05x steps.** Use the `−` and `+` buttons around the speed label. Tap the speed label to reset to 1.0x. The chosen speed carries over to the next episode.
- Progress is saved every few seconds, so episodes resume where you left off. Finished episodes are marked as played and greyed out.
- Media Session support: lock screen and headphone controls for play, pause, seek, and next track, plus episode artwork.
- Layout follows your system light or dark theme and is safe-area aware for phones.

## Changes in v0.0.16
- Playlists were rebuilt as smart playlists: show priorities (High, Normal, Low), four orders, manual reordering, per-episode add and hide, icons and colours, continuous play, and "play the top episode next". Existing playlists are converted. Their minutes and count limits are dropped.
- New built-in playlists: **Up Next** (with Play Next and Play Last on every episode), **Starred**, and **Downloaded**.
- Playlists now list every unplayed episode, not only downloaded ones. The next episode is downloaded automatically when it is next up.
- Per show **Keep N downloaded** replaces "Keep last N episodes". The default is 5 (`AUTO_DOWNLOAD_LATEST`). It no longer hides episodes from playlists.
- Played files are deleted 12 hours after the episode was finished (`CLEANUP_HOURS`, 0 turns it off). Starred episodes are kept. Files of episodes played before this version are deleted on the first pass.
- Episodes can be starred, and removed from all playlists. Shows can be unsubscribed without losing their history.
- The Queue tab is now called **Downloads**, so it is not confused with Up Next.

## Changes in v0.0.15
- New **Repeated audio** list on each show page. The app compares the newest downloaded episodes of a show and flags sections found in more than one, to confirm as intro, outro, or ad.
- The playback bar now shows detected ads in red and the intro and outro in blue.
- "Only from the last N days" moved from the playlist to the show, and a new per-show **Keep last N episodes** limit was added. Playlists saved earlier with a days rule keep working.
- New **Edit** button on each skipped range in the Tag ad dialog, to correct a start or end that is off by a few seconds. The corrected range is learned in place of the old one.
- Fixed: tagging an ad failed with a length error when the feed listed the episode as shorter than the file.
- New settings `REPEAT_EPISODES`, `REPEAT_MIN_EPISODES`, and `REPEAT_MIN_SECONDS`.
- New `app/repeats.py` and a sixth worker. The database upgrades itself on start.

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
      AUTO_DOWNLOAD_LATEST: "5"
      CLEANUP_HOURS: "12"
```

### Settings
| Variable | Default | What it does |
|---|---|---|
| `WHISPER_MODEL` | `small.en` | Whisper model used for transcription |
| `WHISPER_DEVICE` | `cuda` | `cuda` or `cpu` |
| `WHISPER_COMPUTE` | `int8` | Whisper compute type |
| `REFRESH_MINUTES` | `30` | How often feeds are checked |
| `AUTO_DOWNLOAD_LATEST` | `5` | Default for **Keep N downloaded**: newest unplayed episodes per show kept on disk |
| `CLEANUP_HOURS` | `12` | Hours after an episode is finished before its file is deleted. `0` turns cleanup off |
| `SKIP_CONFIDENCE` | `0.6` | How sure the intro/outro theme match must be (0 to 1) before it is used |
| `AD_AUDIO_CONFIDENCE` | `0.5` | How sure a match to a tagged ad's audio must be (0 to 1) |
| `INTRO_WINDOW_SECONDS` | `240` | How much of the start of an episode is searched for the intro |
| `OUTRO_WINDOW_SECONDS` | `240` | How much of the end of an episode is searched for the outro |
| `REPEAT_EPISODES` | `5` | Newest downloaded episodes per show compared for repeated audio |
| `REPEAT_MIN_EPISODES` | `2` | Episodes a section must appear in before it is suggested |
| `REPEAT_MIN_SECONDS` | `5` | Shortest repeated section that is suggested |
| `DATA_DIR` | `/data` | Where the database, audio, transcripts, and templates are stored |
| `DISABLE_WORKERS` | unset | Set to `1` to run the web app without background workers |

The `/data` volume holds the SQLite database, downloaded audio, saved transcripts, and the audio snippets behind intro/outro templates and tagged ads (`/data/templates`). The snippets let the fingerprints be rebuilt if the matching method changes.

## How it is built
- **Backend:** Python, FastAPI, SQLite (`app/main.py`, `feeds.py`, `sources.py`, `workers.py`, `adscan.py`, `db.py`, plus `fingerprint.py`, `markers.py`, `adlearn.py`, and `repeats.py` for intro/outro detection, learning from tagged ads, and finding repeated audio)
- **Frontend:** a single static page, `app/static/index.html`, with no build step
- **Workers:** six background threads for feed refresh, downloading, ad scanning, intro/outro detection, matching tagged ads, and finding repeated audio. The last three use the CPU only.
- **Image:** Nvidia CUDA runtime base with ffmpeg, built and published by GitHub Actions
