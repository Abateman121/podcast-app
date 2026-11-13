# Overcast import

How the app reads an Overcast "All data" export and what it does with it. The code is in `app/overcast.py`.

## The file

The export is an OPML file. All data sits in attributes of `outline` elements. There are three kinds, told apart by `type`.

### Playlists: `type="podcast-playlist"`

| Attribute | Meaning | Used as |
|---|---|---|
| `title` | Playlist name | Playlist title |
| `smart` | `1` for a smart playlist | Not used. Every playlist becomes a smart playlist |
| `sorting` | Order, see below | Sort |
| `includePodcastIds` | Comma list of show ids | Shows in the playlist |
| `priorityPodcastIds` | Comma list of show ids, most important first | Priority shows (High), ranked in this order |
| `includeEpisodeIds` | Comma list of episode ids | Episodes added by hand |
| `sortedEpisodeIds` | Comma list of episode ids | Your own order |

Sorting values:

| Overcast | Here |
|---|---|
| `chronological` | Oldest first |
| `chronological-by-podcast` | Oldest first, grouped by show |
| `reverse-chronological` | Newest first |
| `reverse-chronological-by-podcast` | Newest first, grouped by show |
| anything else | Oldest first, with a warning in the report |

### Shows: `type="rss"`

| Attribute | Meaning | Used as |
|---|---|---|
| `overcastId` | Show id | Stored on the show, so a second run finds it again |
| `xmlUrl` | Feed address | Feed address |
| `title`, `text` | Show name | Title, until the feed gives its own |
| `subscribed` | `1` when you follow the show. Missing when you do not | Subscribed or not subscribed |
| `overcastAddedDate` | When you added it (RFC 822 date) | Not used |
| `htmlUrl` | Web page | Not used |

### Episodes: `type="podcast-episode"`, inside their show

| Attribute | Meaning | Used as |
|---|---|---|
| `overcastId` | Episode id | Stored on the episode |
| `title` | Episode title | Matching, and the title of history rows |
| `pubDate` | Publish date (ISO 8601 with offset) | Matching, and the date of history rows |
| `enclosureUrl` | Audio address | Matching |
| `played` | `1` when finished | Played |
| `progress` | Seconds listened | Position, when the episode is not played |
| `userDeleted` | `1` when you removed it from Overcast | Removed from all playlists |
| `userUpdatedDate` | Last change (ISO 8601) | Time it was finished, for played episodes |
| `url`, `overcastUrl` | Web links | Not used |

## Quirks

- The export only lists episodes you played or started. Unplayed, untouched episodes are not in it at all.
- Almost every listed episode has `userDeleted="1"`. Overcast deletes an episode when it is finished, so this flag mostly means "done".
- `played` and `progress` can both be present. Played wins.
- Flags are left out when they are off. There is no `played="0"`.
- Playlist id lists can point to episodes that are not in the file (unplayed ones). Those ids are skipped.
- Feeds drop old episodes. A long listening history holds many episodes that no feed carries any more.
- Audio addresses change over time: tracking prefixes are added or swapped, and query strings differ.
- Two date formats are used: RFC 822 on shows, ISO 8601 on episodes.

## What the import does

For every show in the file:

1. Find the show: by Overcast id, then by feed address. If it is not here, create it. A show without `subscribed="1"` is created as not subscribed. A show that is already here keeps its own setting.
2. Read the feed once and save its episodes. If the feed cannot be read, the import goes on with the export data alone and reports the error.
3. Match each export episode to an episode of the show. The first rule that fits wins, and each episode is used once:
   1. It already carries this Overcast id (an earlier import).
   2. Same audio address.
   3. Same audio address after cleaning: query string dropped, tracking prefixes dropped, `http` and `https` treated alike.
   4. Same title (letters and digits only) and publish dates no more than 2 days apart.
4. An export episode with no match is added as a history row. Its id in the database starts with `overcast:`. It keeps title, date, and state, and is not downloaded.
5. Copy the state. State is only added, never taken away:
   - played in the export or here: played
   - `userDeleted` in the export or removed here: removed from playlists
   - position: taken from `progress` only when there is no position here and the episode is not played
   - a download that was waiting for an episode that turns out played or removed is cancelled
6. Feed episodes that are not in the export get the cut-off rule below.

Then the playlists are created, or updated when a playlist from an earlier import has the same title. Priority shows become High in the listed order, all other shows Normal. Priority mode is "by rank" and "play the top episode next" is off. Imported playlists are marked with source `overcast`.

The import downloads and scans nothing. Afterwards the normal Keep N downloaded rule runs on the next feed check.

## The cut-off rule

Since the export says nothing about unplayed episodes, the app cannot know which ones you still wanted. Without a rule, the whole back catalogue of every feed would land on the playlists.

The rule: a feed episode that is not in the export, was published more than N days before the first import, and was never touched here (not played, starred, started, downloaded, or queued) is removed from the playlists. N is the "Keep unplayed episodes from the last N days" field, 14 by default. Empty keeps everything.

- The time of the first real import is saved. Later runs measure from that same moment, so a second run a month later does not remove a month of new episodes.
- A removed episode is not deleted. It can be put back from the show page.
- Episodes inside the N days stay, even when Overcast had them and you had not started them.

## Running it again

A second run with the same file changes nothing. A run with a newer export adds the plays, positions, and episodes that are new. Two things to know:

- Imported playlists are set back to what the export says: shows, priorities, and your own order. Changes made here to an imported playlist are overwritten. Rename the playlist first to keep them, since playlists are found again by title.
- The cut-off still measures from the first import.

## Routes

| Route | Does |
|---|---|
| `POST /api/import/overcast` | Body is the file. It is checked and kept in `DATA_DIR/import/`. Returns the totals of the file |
| `POST /api/import/overcast/run` | Body `{"dry": true, "keep_days": 14}`. Starts the job in the background. `keep_days: null` keeps everything |
| `GET /api/import/overcast/status` | `state` (idle, running, done, error), `step`, `total`, `label`, and `result` when done |

## Testing

`tests/fixtures/overcast-small.opml` is a small export with invented shows and episodes. It covers every flag combination, all four match rules, a feed that fails, an unknown sorting, and ids that point nowhere. `tests/check_overcast.py` serves matching test feeds itself and checks the dry run, the import, the playlists, and a second run.

```
docker run -d --name podcast-test -v /path/to/podcast-app/tests:/tests:ro -e DATA_DIR=/data -e DISABLE_WORKERS=1 <image> \
  python3 -m uvicorn main:app --host 0.0.0.0 --port 8099
docker exec podcast-test python3 /tests/check_overcast.py
```

A real export must never be committed. It holds a full listening history.
