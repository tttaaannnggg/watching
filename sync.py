#!/usr/bin/env python3
"""Snapshot a Jellyfin user's per-episode watch progress into data/progress.json.

Connection settings come from the environment or from a local env file outside
the repo (default ~/.config/watching/sync.env, override with WATCHING_ENV_FILE):

    JELLYFIN_URL=http://host:8096
    JELLYFIN_API_KEY_FILE=/path/to/key      (or JELLYFIN_API_KEY=...)
    JELLYFIN_USER_ID=...                    (optional fallback if the name lookup fails)

Display and notification settings live in config.json next to this script; see notify.py
for Discord notifications of newly watched episodes.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import notify

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
OUTPUT_PATH = ROOT / "data" / "progress.json"
DEFAULT_ENV_FILE = "~/.config/watching/sync.env"
DEFAULT_CONFIG = {
    "user": "mari",
    "include_inactive": False,
    "include_specials": False,
    "exclude": [],
}


# ---------------------------------------------------------------- settings


def load_env_file(path):
    """Parse KEY=VALUE lines; missing file yields {}."""
    values = {}
    try:
        text = Path(path).expanduser().read_text()
    except FileNotFoundError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def load_settings():
    settings = load_env_file(os.environ.get("WATCHING_ENV_FILE", DEFAULT_ENV_FILE))
    for key in ("JELLYFIN_URL", "JELLYFIN_API_KEY", "JELLYFIN_API_KEY_FILE", "JELLYFIN_USER_ID"):
        if os.environ.get(key):
            settings[key] = os.environ[key]
    if not settings.get("JELLYFIN_URL"):
        sys.exit("sync: JELLYFIN_URL is not set (env or settings file)")
    key = settings.get("JELLYFIN_API_KEY")
    if not key and settings.get("JELLYFIN_API_KEY_FILE"):
        key = Path(settings["JELLYFIN_API_KEY_FILE"]).expanduser().read_text().strip()
    if not key:
        sys.exit("sync: no Jellyfin API key (JELLYFIN_API_KEY or JELLYFIN_API_KEY_FILE)")
    settings["JELLYFIN_API_KEY"] = key
    return settings


def load_config(path=CONFIG_PATH):
    config = dict(DEFAULT_CONFIG)
    if Path(path).exists():
        config.update(json.loads(Path(path).read_text()))
    return config


# ---------------------------------------------------------------- jellyfin


class Jellyfin:
    def __init__(self, url, api_key):
        self.url = url.rstrip("/")
        self.headers = {
            "Authorization": f'MediaBrowser Token="{api_key}"',
            "Accept": "application/json",
        }

    def get(self, path, **params):
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        request = urllib.request.Request(
            f"{self.url}{path}{'?' + query if query else ''}", headers=self.headers
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)

    def user_id(self, name, fallback=None):
        try:
            for user in self.get("/Users"):
                if user.get("Name", "").lower() == name.lower():
                    return user["Id"]
        except Exception as exc:  # name lookup needs an admin key; fall back below
            print(f"sync: user lookup failed ({exc}), using fallback id", file=sys.stderr)
        if fallback:
            return fallback
        sys.exit(f"sync: user {name!r} not found and no JELLYFIN_USER_ID fallback")

    def series(self, uid):
        return self.get(
            f"/Users/{uid}/Items",
            Recursive="true",
            IncludeItemTypes="Series",
            EnableUserData="true",
            Fields="ProductionYear,ProviderIds",
        )["Items"]

    def seasons(self, uid, series_id):
        return self.get(f"/Shows/{series_id}/Seasons", UserId=uid)["Items"]

    def episodes(self, uid, series_id):
        return self.get(
            f"/Shows/{series_id}/Episodes", UserId=uid, EnableUserData="true"
        )["Items"]


# ---------------------------------------------------------------- transform


def iso_utc(value):
    """Normalise a Jellyfin timestamp (7-digit fractions, Z suffix) to second-precision ISO UTC."""
    if not value:
        return None
    text = re.sub(r"\.\d+", "", value.strip()).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def episode_state(user_data):
    """Return (state, pct). A resume position wins over Played (it is being (re)watched)."""
    user_data = user_data or {}
    if (user_data.get("PlaybackPositionTicks") or 0) > 0:
        pct = round(user_data.get("PlayedPercentage") or 0)
        return "in_progress", min(max(pct, 1), 99)
    if user_data.get("Played"):
        return "watched", 100
    return "unwatched", 0


def clean_title(title, series_name):
    title = (title or "").strip()
    prefix = f"{series_name} - "
    if series_name and title.startswith(prefix) and len(title) > len(prefix):
        title = title[len(prefix):]
    return title


def build_series(series, seasons, episodes, config):
    """Turn one Jellyfin series + its seasons/episodes into the output dict, or None if hidden."""
    name = series.get("Name", "")
    season_meta = {s["Id"]: s for s in seasons if s.get("Id")}
    groups = {}  # season key -> {"number", "name", "items"}

    for ep in episodes:
        meta = season_meta.get(ep.get("SeasonId"))
        # The season item is authoritative (e.g. "BONUS MATERIAL" has no index even when
        # its episodes carry a stray ParentIndexNumber); fall back to the episode's own field.
        number = meta.get("IndexNumber") if meta else ep.get("ParentIndexNumber")
        meta = meta or {}
        is_special = number is None or number == 0 or ep.get("ParentIndexNumber") == 0
        if is_special and not config.get("include_specials"):
            continue
        key = ep.get("SeasonId") or f"n{number}"
        group = groups.setdefault(
            key,
            {
                "number": number if number is not None else 0,
                "name": meta.get("Name") or ep.get("SeasonName") or f"Season {number}",
                "items": [],
            },
        )
        group["items"].append(ep)

    out_seasons = []
    watched = in_progress = total = 0
    last_activity = None
    flat = []  # (played_at, episode dict) in display order
    for group in sorted(groups.values(), key=lambda g: (g["number"] == 0, g["number"])):
        items = group["items"]
        numbered = sorted(
            (e for e in items if e.get("IndexNumber") is not None), key=lambda e: e["IndexNumber"]
        )
        unnumbered = [e for e in items if e.get("IndexNumber") is None]
        # A season with no numbers at all keeps the server order and is numbered 1..N;
        # in a mixed season the unnumbered ones are appended without a number.
        if not numbered:
            ordered = [(i + 1, e) for i, e in enumerate(unnumbered)]
        else:
            ordered = [(e["IndexNumber"], e) for e in numbered] + [(None, e) for e in unnumbered]

        out_eps = []
        for number, ep in ordered:
            user_data = ep.get("UserData") or {}
            state, pct = episode_state(user_data)
            total += 1
            watched += state == "watched"
            in_progress += state == "in_progress"
            played_at = iso_utc(user_data.get("LastPlayedDate"))
            if played_at and (last_activity is None or played_at > last_activity):
                last_activity = played_at
            out_eps.append(
                {
                    "s": group["number"],
                    "e": number,
                    "title": clean_title(ep.get("Name"), name),
                    "state": state,
                    "pct": pct,
                }
            )
            flat.append((played_at, out_eps[-1]))
        out_seasons.append({"number": group["number"], "name": group["name"], "episodes": out_eps})

    if total == 0:
        return None
    if not config.get("include_inactive") and watched == 0 and in_progress == 0:
        return None

    tmdb = (series.get("ProviderIds") or {}).get("Tmdb")
    return {
        "name": name,
        "year": series.get("ProductionYear"),
        "tmdb": str(tmdb) if tmdb else None,
        "last_activity": last_activity,
        "watched": watched,
        "total": total,
        "current": current_episode(flat),
        "seasons": out_seasons,
    }


def current_episode(flat):
    """Where the viewer is: the most recently played episode if still in progress, else the
    next not-yet-watched episode after it (wrapping to the first one), else None (finished).
    Returned dict adds `index`, the episode's position in display order."""
    if not flat:
        return None
    played = [(at, i) for i, (at, _) in enumerate(flat) if at]
    start = 0
    if played:
        last = max(played)[1]
        if flat[last][1]["state"] == "in_progress":
            return {**flat[last][1], "index": last}
        start = last + 1
    for i in list(range(start, len(flat))) + list(range(0, start)):
        if flat[i][1]["state"] != "watched":
            return {**flat[i][1], "index": i}
    return None


def sort_series(series_list):
    """Most recent activity first; shows without activity last; ties by name."""
    by_name = sorted(series_list, key=lambda s: s["name"].casefold())
    return sorted(by_name, key=lambda s: s["last_activity"] or "", reverse=True)


def build_snapshot(entries, config, generated=None):
    """entries: iterable of (series, seasons, episodes) tuples from Jellyfin."""
    excluded = {name.casefold() for name in config.get("exclude", [])}
    result = []
    for series, seasons, episodes in entries:
        if series.get("Name", "").casefold() in excluded:
            continue
        built = build_series(series, seasons, episodes, config)
        if built:
            result.append(built)
    return {"generated": generated or now_utc(), "series": sort_series(result)}


def content_changed(old, new):
    """Compare two snapshots ignoring the `generated` timestamp."""
    if old is None:
        return True
    strip = lambda snap: {k: v for k, v in snap.items() if k != "generated"}
    return strip(old) != strip(new)


def dump(snapshot):
    return json.dumps(snapshot, ensure_ascii=False, indent=1) + "\n"


# ---------------------------------------------------------------- main


def fetch_entries(client, uid):
    for series in client.series(uid):
        sid = series["Id"]
        yield series, client.seasons(uid, sid), client.episodes(uid, sid)


def git_push(path):
    rel = str(path.relative_to(ROOT))
    for cmd in (
        ["git", "add", rel],
        ["git", "commit", "-m", "Update progress"],
        ["git", "push"],
    ):
        subprocess.run(cmd, cwd=ROOT, check=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="print JSON, write nothing")
    parser.add_argument("--push", action="store_true", help="commit and push if changed")
    parser.add_argument("--no-notify", action="store_true", help="skip Discord notifications")
    parser.add_argument(
        "--notify-dry-run",
        action="store_true",
        help="print the Discord messages instead of sending them; leaves an existing "
        "announced state untouched (a missing one is still seeded)",
    )
    args = parser.parse_args(argv)

    settings = load_settings()
    config = load_config()
    client = Jellyfin(settings["JELLYFIN_URL"], settings["JELLYFIN_API_KEY"])
    uid = client.user_id(config["user"], settings.get("JELLYFIN_USER_ID"))
    snapshot = build_snapshot(fetch_entries(client, uid), config)

    status = 0
    if args.dry_run:
        sys.stdout.write(dump(snapshot))
    else:
        status = write_snapshot(snapshot, args.push)

    # Independent of whether the file changed or the push worked; --dry-run sends nothing
    # unless --notify-dry-run asks to see the messages.
    if not args.no_notify and (args.notify_dry_run or not args.dry_run):
        webhook = None if args.notify_dry_run else notify.load_webhook()
        sender = notify.DiscordWebhook(webhook) if webhook else None
        notify.run(
            snapshot,
            config,
            notify.state_path(),
            sender,
            dry_run=args.notify_dry_run,
            roles=notify.load_roles(notify.roles_path()),
        )
    return status


def write_snapshot(snapshot, push):
    old = None
    if OUTPUT_PATH.exists():
        try:
            old = json.loads(OUTPUT_PATH.read_text())
        except json.JSONDecodeError:
            old = None
    count = len(snapshot["series"])
    if not content_changed(old, snapshot):
        print(f"sync: unchanged ({count} series), nothing written")
        return 0

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(dump(snapshot))
    print(f"sync: wrote {OUTPUT_PATH.relative_to(ROOT)} ({count} series)")
    if push:
        try:
            git_push(OUTPUT_PATH)
        except subprocess.CalledProcessError as exc:
            print(f"sync: git failed ({exc})", file=sys.stderr)
            return 1
        print("sync: committed and pushed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
