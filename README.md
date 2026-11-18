# SkipJack *AI SLOP*

SkipJack is a self-hosted podcast app for myself. The repo and the Docker image are still named `podcast-app`. It pulls in shows from almost any link, downloads the latest episodes, transcribes them on the GPU, and automatically skips the ads while you listen.

**Current version: v0.0.22** (shown in the bottom right of the player bar on every screen)

## Features

### Adding shows
- Paste a link and the app works out the RSS feed for you. Supported sources:
  - Direct RSS/Atom feed URLs, or any web page that advertises a feed
  - Apple Podcasts show links
  - Spotify show links (matched to the public feed through Apple Podcasts)
  - Amazon Music podcast links (matched the same way)
  - YouTube channels and playlists (audio is downloaded with yt-dlp, latest 15 videos)
- Import a whole subscription list from an OPML file. Imports queue only the newest episode per show so a big file does not flood the GPU.
- **Import from Overcast**: bring over shows, play history, and playlists from an Overcast "All data" export. See "Import from Overcast" below.
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

### Import from Overcast
- On overcast.fm/account, use **Export Your Data > All data**. On the **Shows** tab here, press **Import from Overcast** and pick that file.
- The app first shows what the file holds. **Check against the feeds (dry run)** reads every feed and reports, per show, what would happen. Nothing is saved. **Import now** does the import.
- What comes over: shows (ones you no longer follow come in as not subscribed), played and in-progress episodes with their positions, episodes you deleted in Overcast (they stay off the playlists), and your playlists with their shows, priority shows, order, and hand-picked episodes.
- Episodes are matched to the feed by audio address, then by title and date. Episodes the feed no longer carries come in as history only: title, date, and state, with no download.
- The export only lists episodes you played or started. So the app cannot tell which older unplayed episodes you still want. **Keep unplayed episodes from the last N days** (default 14) decides it: older episodes that you never touched are taken off the lists. They can be put back from the show page. Leave the field empty to keep them all.
- Nothing you have here is deleted or set back. An episode played here stays played. The import can be run again with a newer export without making duplicates.
- The import downloads nothing by itself. Afterwards the normal Keep N downloaded rule applies.
- Details are in [docs/overcast-import.md](docs/overcast-import.md).

### Shorten Silence
- Speeds through the pauses in speech. No speech is cut and no pause is removed: the first and last 80 ms of every pause play normally, and the middle plays at 3x your speed (never above 4.0x). Every pause still lasts at least 180 ms.
- Each downloaded, unplayed episode is checked once for pauses (quieter than -35 dB for at least 250 ms). This is a short ffmpeg pass on the CPU, separate from the ad scan. Music and steady sound are left alone. A new download of the same episode is checked again.
- Turn it on with the **Silence** button in the player, or with **Shorten Silence by default** on the Shows tab. Each show can override the default on its show page: Default, On, or Off. The player button changes the show setting when the show has one, otherwise the default. A change takes effect at once.
- While it is on, the button shows the effective speed, and the time remaining is in real time. Positions, ads, and intro/outro markers stay in episode time.
- Pauses inside a skipped ad, intro, or outro are ignored. The skip comes first.
- Until an episode has been checked, the button reads "Silence: on (not ready)" and the episode plays normally.
- The Shows tab shows the time saved in total, split into speed, shortened silence, and skipped ads.
- The page sets the speed from a timer. When the browser slows that timer down (for example a hidden tab, or a phone with the screen locked), playback stays at your normal speed until the timer runs smoothly again. That way a late timer can never leave speech sped up.

### Player
- Fixed player bar on every screen with seek bar, elapsed time, and time remaining (adjusted for playback speed and shortened silence).
- A coloured strip above the seek bar shows what was detected in the episode: red for ads, blue for the intro and outro. It updates while an episode is still being scanned and when you tag an ad.
- Back 15 seconds and forward 30 seconds buttons.
- **Playback speed from 0.5x to 3.0x in 0.05x steps.** Use the `−` and `+` buttons around the speed label. Tap the speed label to reset to 1.0x. The chosen speed carries over to the next episode.
- Progress is saved every few seconds, so episodes resume where you left off. Finished episodes are marked as played and greyed out.
- Media Session support: lock screen and headphone controls for play, pause, seek, and next track, plus episode artwork.
- Layout follows your system light or dark theme and is safe-area aware for phones.

## Version history
Everything that has been added or changed, newest first. Each version is a git tag and, from v0.0.17 on, a pinned image `ghcr.io/abateman121/podcast-app:<version>`.

| Version | Date | Highlights |
|---|---|---|
| v0.0.22 | 2026-11-18 | New app icon: a skipjack inside a pair of headphones |
| v0.0.21 | 2026-11-18 | Renamed to SkipJack, Set intro/outro opens with the current times, progress bar for Repeated audio |
| v0.0.20 | 2026-11-18 | Asks before a shorter section replaces an intro or outro template |
| v0.0.19 | 2026-11-17 | Play from here, playback controls in Tag ad, new app icon |
| v0.0.18 | 2026-11-17 | Shorten Silence, time-saved stats |
| v0.0.17 | 2026-11-13 | Import from Overcast; version-tagged images |
| v0.0.16 | 2026-11-13 | Smart playlists, Up Next, Starred, Keep N downloaded, cleanup of played files |
| v0.0.15 | 2026-11-12 | Repeated audio suggestions, coloured playback bar, edit a skipped ad range |
| v0.0.14 | 2026-11-11 | Intro/outro detection by audio fingerprint, Tag ad, Not an ad |
| v0.0.13 | 2026-11-10 | Version shown in the player, 0.05x speed steps |
| v0.0.12 | 2026-11-10 | Image built by GitHub Actions and published to ghcr.io |
| v0.0.11 | 2026-11-10 | Better ad detection rules, saved transcripts, Re-scan ads |
| v0.0.1 | 2026-11-10 | Downloads queue, OPML import, Apple/Spotify/Amazon/YouTube links, delete shows |

### v0.0.22 (2026-11-18)
- New app icon: a striped skipjack inside a pair of headphones, on deep blue. It replaces the icon from v0.0.19 in the browser tab (`icon-32.png`), on the iPhone home screen (`icon-180.png`, full-bleed so iOS rounds the corners itself), and in the web app manifest (`icon-192.png`, `icon.png`, and a new `icon-512.png`).
- The vector source is in `app/static/icon.svg`.
- For the Unraid Docker tab, copy `app/static/icon.png` to `/mnt/user/icons/podcast-app.png` again.

### v0.0.21 (2026-11-18)
- The app is now called **SkipJack**: browser tab, home screen name, and web app manifest. The repo, the image (`ghcr.io/abateman121/podcast-app`), and the `/data` folder keep their names, so nothing changes in the compose file.
- **Set intro/outro** now opens with the current times filled in. It picks the episode the template was cut from if that episode is still downloaded, and fills in the saved times. For other episodes it fills in the intro and outro detected in that episode. Changing the episode fills the rows again.
- **Repeated audio**: Play now opens a progress bar for that section, with the current time and −5s and +5s buttons. The bar covers the section plus 5 seconds on each side, so you can hear the edges. Play turns into Pause, and pressing it again resumes where you stopped.
- The episode and times each template was cut from are now saved (new show columns `intro_template_ref` and `outro_template_ref`). Templates made before this version have none, so the dialog uses the detected times until you save again. The database upgrades itself on start.

### v0.0.20 (2026-11-18)
- **Repeated audio**: a show has one intro template and one outro template, and the last one accepted replaces the one before. Choosing **Use as intro** or **Use as outro** on a section that is shorter than the current template now asks first. This stops a partial section from replacing the full intro or outro by mistake.

### v0.0.19 (2026-11-17)
- **Set intro/outro**: the "Intro ends at" and "Outro ends at" rows now have a **Play from here** button next to **Play 3s before** (#4).
- **Tag an ad**: the dialog now has its own play and pause button, a position slider, and back and forward buttons (15 and 5 seconds), so you can find the edges of an ad without closing it (#5).
- New app icon in the browser tab, on the tab bar, on the iPhone home screen, and in a web app manifest (#6).
- For the Unraid Docker tab, copy `app/static/icon.png` to `/mnt/user/icons/podcast-app.png`. The compose example below already points there.

### v0.0.18 (2026-11-17)
- New **Shorten Silence**: pauses in speech play faster, with a global default (off at first) and a per-show override.
- A new background worker checks each downloaded, unplayed episode for pauses. Episodes already on disk are checked after the update, newest first.
- Time remaining is shown in real time, and the player shows the effective speed.
- Time saved is recorded per episode and per show, and the total is shown on the Shows tab, split into speed, silence, and skipped ads.
- New routes: `POST /api/episodes/{id}/analyze`, `GET /api/episodes/{id}/silence`, `GET` and `POST /api/stats/time-saved`, `GET` and `POST /api/settings/silence`, `POST /api/shows/{id}/silence`. Maps are stored in `/data/silence`.
- Tests: `tests/check_silence.py` and `tests/silence_rules.test.js`. A standalone listening test tool for the feature is in `tools/silence_test`.

### v0.0.17 (2026-11-13)
- New **Import from Overcast** on the Shows tab: shows, play history, positions, and playlists from an Overcast "All data" export.
- A dry run reports what the import would do before anything is saved.
- Older unplayed episodes that are not in the export are taken off the lists, with a setting for how many days to keep (default 14).
- Running the import again adds only what is new.
- Release tags like `v0.0.17` now also build a version-tagged image, so a version can be pinned in the compose file.

### v0.0.16 (2026-11-13)
- Playlists were rebuilt as smart playlists: show priorities (High, Normal, Low), four orders, manual reordering, per-episode add and hide, icons and colours, continuous play, and "play the top episode next". Existing playlists are converted. Their minutes and count limits are dropped.
- New built-in playlists: **Up Next** (with Play Next and Play Last on every episode), **Starred**, and **Downloaded**.
- Playlists now list every unplayed episode, not only downloaded ones. The next episode is downloaded automatically when it is next up.
- Per show **Keep N downloaded** replaces "Keep last N episodes". The default is 5 (`AUTO_DOWNLOAD_LATEST`). It no longer hides episodes from playlists.
- Played files are deleted 12 hours after the episode was finished (`CLEANUP_HOURS`, 0 turns it off). Starred episodes are kept. Files of episodes played before this version are deleted on the first pass.
- Episodes can be starred, and removed from all playlists. Shows can be unsubscribed without losing their history.
- The Queue tab is now called **Downloads**, so it is not confused with Up Next.

### v0.0.15 (2026-11-12)
- New **Repeated audio** list on each show page. The app compares the newest downloaded episodes of a show and flags sections found in more than one, to confirm as intro, outro, or ad.
- The playback bar now shows detected ads in red and the intro and outro in blue.
- "Only from the last N days" moved from the playlist to the show, and a new per-show **Keep last N episodes** limit was added. Playlists saved earlier with a days rule keep working.
- New **Edit** button on each skipped range in the Tag ad dialog, to correct a start or end that is off by a few seconds. The corrected range is learned in place of the old one.
- Fixed: tagging an ad failed with a length error when the feed listed the episode as shorter than the file.
- New settings `REPEAT_EPISODES`, `REPEAT_MIN_EPISODES`, and `REPEAT_MIN_SECONDS`.
- New `app/repeats.py` and a sixth worker. The database upgrades itself on start.

### v0.0.14 (2026-11-11)
- Automatic intro and outro detection for every episode. It uses an audio fingerprint of the show's theme, then chapters, then the fixed seconds as a fallback. The fixed seconds are now called fallback seconds on the show page.
- New **Set intro/outro** tool on each show page to mark the theme once, with **Re-detect intro/outro** to redo a show.
- A cold open before the intro is no longer skipped, and seeking back into a skipped intro does not skip it again.
- New **Tag ad** button in the player for ads that were missed. Tagged ads are skipped at once, and the app learns them by audio and by phrase to catch them in other episodes.
- Added **Not an ad** for ranges that were skipped by mistake.
- New settings `SKIP_CONFIDENCE`, `AD_AUDIO_CONFIDENCE`, `INTRO_WINDOW_SECONDS`, and `OUTRO_WINDOW_SECONDS`.
- Added numpy and scipy. The database upgrades itself on start.
- Fixes: a `podcast:chapters` tag with no attributes no longer fails the feed refresh, and the delete-template route always closes the database connection.

### v0.0.13 (2026-11-10)
- Added the app version in the bottom right of the player bar, so it appears on every screen.
- Playback speed now changes in 0.05x increments instead of cycling through a fixed list (1.0, 1.25, 1.5, 1.75, 2.0, 0.8). Added `−`/`+` buttons and a tap-to-reset on the speed label.
- Rewrote this README to cover all current features.

### v0.0.12 (2026-11-10)
- A GitHub Actions workflow (`.github/workflows/docker.yml`) builds the image and publishes it to `ghcr.io/abateman121/podcast-app:latest`, plus a tag per commit for rolling back.
- The compose file uses `image:` instead of `build:`. The package on ghcr.io is public, so Tower pulls it without a login.

### v0.0.11 (2026-11-10)
- Ad detection rules rewritten. Opening phrases ("brought to you by", "sponsored by") and closing phrases (promo codes, URLs, "percent off") are treated differently:
  - An opening phrase alone skips about 45 seconds.
  - A closing phrase alone skips only if it matches at least two offer phrases, so normal talk is not cut.
  - An opening and a closing phrase within 75 seconds mark the whole span between them.
- Playing an episode loads its latest ad ranges first, and the player re-checks every 20 seconds, so a scan that finishes mid-episode takes effect.
- Each scan saves its transcript in `/data/transcripts`, and **Re-scan ads** re-applies the current rules without running Whisper again.

### v0.0.1 (2026-11-10)
- New download queue tab (now **Downloads**) with a live count, progress bars for downloads and ad scans, errors, and **Retry**. Downloads and scans run in separate threads, and a restart puts unfinished items back in line.
- **Import OPML**: adds every feed in the file and queues only the newest episode of each show, to protect the GPU.
- The add box accepts RSS, Apple Podcasts, Spotify, Amazon Music, YouTube, and plain web pages that link to a feed. Spotify and Amazon shows are matched by name on Apple Podcasts. YouTube channels and playlists work through yt-dlp (latest 15 videos).
- Each show has a **Delete** button that removes the show and its downloaded audio.

## Releasing a new version
The version is set in one place: the `<div id="ver">` line in `app/static/index.html`. **Bump it with every update** and add a section to the version history above.

1. Push the changes to a branch named after the version, for example `v0.0.22`.
2. Open a pull request into `main` and merge it. Pushing to `main` runs the GitHub Actions workflow, which builds the image and publishes it as `latest` (and as the commit SHA).
3. Push a tag with the same name on the merge commit (`v0.0.22`). The workflow runs again and publishes `ghcr.io/abateman121/podcast-app:v0.0.22`, so that version can be pinned. A build takes about 2 minutes.
4. On Tower, **Compose Up** the stack. With `image: …:latest` and `pull_policy: always` it pulls the newest `main` build. To pin a version, use the version tag instead of `latest`.

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
    labels:
      net.unraid.docker.icon: "/mnt/user/icons/podcast-app.png"
```

The app icon is in the repo at `app/static/icon.png` (256 x 256), with the vector source in `app/static/icon.svg`. The running app also serves it at `http://<server>:8099/icon.png`.

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
- **Backend:** Python, FastAPI, SQLite (`app/main.py`, `feeds.py`, `sources.py`, `workers.py`, `adscan.py`, `db.py`, plus `fingerprint.py`, `markers.py`, `adlearn.py`, and `repeats.py` for intro/outro detection, learning from tagged ads, and finding repeated audio, `playlists.py`, `overcast.py`, and `silence.py` for Shorten Silence)
- **Frontend:** a single static page, `app/static/index.html`, with no build step
- **Workers:** eight background threads for feed refresh, downloading, ad scanning, intro/outro detection, matching tagged ads, finding pauses, finding repeated audio, and cleanup of played files. Only the ad scan uses the GPU.
- **Tests:** `tests/check_silence.py` (server, inside the container) and `tests/silence_rules.test.js` (player rate schedule, in node). The commands are at the top of each file.
- **Image:** Nvidia CUDA runtime base with ffmpeg, built and published by GitHub Actions
