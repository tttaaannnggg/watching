# watching

What Mari is watching, synced from Jellyfin. Served at [watching.fujo.dev](https://watching.fujo.dev)
by GitHub Pages.

Each show is a card with an episode grid: one row per season (S1, S2… on the left), one small
square per episode that wraps within the row. Watched squares are filled, the in-progress one fills
from the bottom up to its playback percentage, and the current episode is ringed. The card header
shows where you are: `S06E10 · 88%`, `Next: S01E08`, or `Finished`. Tap or hover a square (or focus
the grid and use ←/→) for `S01E07 · Title · watched/52%/unwatched`.

## Layout

| Path | What |
| --- | --- |
| `index.html`, `style.css`, `app.js` | The static page. No build step, no dependencies. |
| `data/progress.json` | Snapshot the page renders, written by `sync.py`. |
| `sync.py` | Pulls progress from Jellyfin (Python 3 stdlib only). |
| `config.json` | What to show. |
| `deploy/` | systemd user units that run the sync every 30 minutes. |
| `tests/` | Unit tests for the transform. |

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
{"user": "mari", "include_inactive": false, "include_specials": false, "exclude": []}
```

- `user`: Jellyfin user name whose progress is published.
- `include_inactive`: also list shows with nothing watched or in progress.
- `include_specials`: include Season 0 and unnumbered seasons (bonus material).
- `exclude`: series names to hide (case-insensitive).

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
