# watching

What Mari is watching, synced from Jellyfin. Served at [watching.fujo.dev](https://watching.fujo.dev)
by GitHub Pages.

Each show is a card with a segmented progress bar: one segment per episode, a wider gap between
seasons (labelled S1, S2…), watched segments filled, the in-progress one filled up to its playback
percentage. Very long shows wrap onto several rows instead of overflowing. Above the bar is where
you are (`S06E10 · 88% · Title`, `Next: S01E08`, or `Finished`); a small dot marks that episode's
segment. Tap or hover a segment (or focus the bar and use ←/→) for
`S01E07 · Title · watched/52%/unwatched`.

## Layout

| Path | What |
| --- | --- |
| `index.html`, `style.css`, `app.js` | The static page. No build step, no dependencies. |
| `data/progress.json` | Snapshot the page renders, written by `sync.py`. |
| `sync.py` | Pulls progress from Jellyfin (Python 3 stdlib only). |
| `notify.py` | Discord messages for newly watched episodes, called by `sync.py`. |
| `config.json` | What to show. |
| `deploy/` | systemd user units that run the sync every 30 minutes. |
| `tests/` | Unit tests for the transform and the notifications. |

## How sync works

`sync.py` looks up the configured user, fetches every series with its seasons and episodes, and
builds one entry per show: name, year, TMDB id, `last_activity` (newest episode play date, UTC),
watched/total counts, and every episode's state (`watched`, `in_progress` with a percentage, or
`unwatched`). An episode with a resume position counts as in progress even if Jellyfin also marks
it played (a rewatch). Shows are sorted by most recent activity.

The file is only rewritten when something other than the `generated` timestamp changed, so the
timer does not produce empty commits.

```sh
python3 sync.py            # update data/progress.json if progress changed
python3 sync.py --dry-run  # print the snapshot, write nothing
python3 sync.py --push     # update, then git add/commit/push data/progress.json if it changed
python3 sync.py --no-notify        # no Discord messages this run
python3 sync.py --notify-dry-run   # print the Discord messages instead of sending them
```

### Connection settings

Server address and credentials never live in this repo. `sync.py` reads them from the environment
or from `~/.config/watching/sync.env` (override the location with `WATCHING_ENV_FILE`):

```sh
JELLYFIN_URL=http://<host>:8096
JELLYFIN_API_KEY_FILE=<path to a file containing the API key>   # or JELLYFIN_API_KEY=<key>
JELLYFIN_USER_ID=<id>   # optional: used if looking the user up by name fails
```

Keep that file `chmod 600`.

### config.json

```json
{"user": "mari", "include_inactive": false, "include_specials": false, "exclude": [],
 "notify": {"enabled": true, "titles": true, "bulk_threshold": 15, "bulk": "summary", "mute": []}}
```

- `user`: Jellyfin user name whose progress is published.
- `include_inactive`: also list shows with nothing watched or in progress.
- `include_specials`: include Season 0 and unnumbered seasons (bonus material).
- `exclude`: series names to hide (case-insensitive). Hidden shows are never announced either.
- `notify.enabled`: send Discord messages for newly watched episodes.
- `notify.titles`: include the episode title in single-episode messages.
- `notify.bulk_threshold`: more newly watched episodes than this in one run counts as a bulk
  change (e.g. marking a whole season played).
- `notify.bulk`: what to do with a bulk change: `"summary"` sends one
  `Mari marked 42 episodes as watched across 3 shows` message, `"skip"` sends nothing.
- `notify.mute`: series names (case-insensitive) shown on the page but never announced.

## Discord notifications

After writing (and pushing) the snapshot, every run announces episodes that are watched now and
were never announced before, one message per show:

```
Mari watched **JoJo's Bizarre Adventure** S06E10 · Scary Monsters (2)
Mari watched 3 episodes of **Romance of the Three Kingdoms** (S01E07–S01E09)
```

each followed by a link to the page. This runs whether or not the JSON changed.

What has been announced is tracked in `$XDG_STATE_HOME/watching/announced.json` (default
`~/.local/state/watching/announced.json`), outside the repo, so an episode is announced once even
if it is unmarked and marked again. The first run without that file records every watched
episode and sends nothing. An episode is only recorded after Discord accepted its message; if
sending fails it is retried on the next run, and the sync still exits 0. Episodes of muted shows
are recorded without a message.

Set up the webhook (Discord: channel settings → Integrations → Webhooks → Copy Webhook URL):

```sh
mkdir -p ~/.config/watching
install -m 600 /dev/null ~/.config/watching/discord_webhook
$EDITOR ~/.config/watching/discord_webhook   # paste the URL on one line
```

The file must be `chmod 600`; a group- or world-readable file is ignored with a warning. The
environment variable `WATCHING_DISCORD_WEBHOOK` takes precedence over the file. Without a webhook
the run logs `notify: no webhook configured, skipping` (with `notify.enabled` false:
`notify: disabled in config, skipping`) and leaves the announced state alone (first-run seeding
still happens), so nothing is lost once a webhook is added; a backlog larger than
`bulk_threshold` is then sent as one summary. `--dry-run` sends nothing; combine it with
`--notify-dry-run` to see the messages.

## Install the timer

```sh
mkdir -p ~/.config/systemd/user
cp deploy/watching-sync.service deploy/watching-sync.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now watching-sync.timer
systemctl --user list-timers watching-sync.timer   # next run
journalctl --user -u watching-sync.service          # sync output
```

The service runs `sync.py --push` from `~/dev/watching`, so the checkout needs a branch with an
upstream and push credentials that work non-interactively. To keep syncing while logged out:
`loginctl enable-linger $USER`.

## Development

```sh
python3 -m unittest -v
python3 -m http.server 8000   # then open http://localhost:8000
```
