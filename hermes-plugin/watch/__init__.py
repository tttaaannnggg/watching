"""Hermes plugin: /watch — per-show Discord role subscriptions for watching.fujo.dev (zero LLM tokens).

/watch | /watch list    the shows in Mari's Jellyfin library, marking the ones you are subscribed to
/watch <show>           subscribe: get the show's role, pinged when Mari watches an episode
/watch off <show>       unsubscribe (the role itself stays)

Each show's role is recorded in $XDG_STATE_HOME/watching/roles.json (default
~/.local/state/watching/roles.json); sync.py reads it to ping the role in that show's message.
The show list is library.json next to it (every series, written by sync.py), falling back to the
page's progress.json when that is missing or unreadable.
Any member of the guild may use it, for their own roles only. Discord REST v10 with the bot token
from DISCORD_BOT_TOKEN, which only ever goes into the Authorization header.
"""

import difflib
import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

GUILD_ID = "1565154742836138125"
API = "https://discord.com/api/v10"
TOKEN_ENV = "DISCORD_BOT_TOKEN"
DEFAULT_PROGRESS = "~/dev/watching/data/progress.json"
ROLE_PREFIX = "📺 "
ROLE_NAME_MAX = 100
BUDGET = 10.0  # seconds for all Discord calls of one command
MAX_REPLY = 1900
MAX_CANDIDATES = 8
MAX_LISTED = 40  # longer libraries: /watch list shows only your subscriptions plus a count
UNKNOWN_ROLE = 10011
NO_PERMISSION = (50001, 50013)  # Missing Access, Missing Permissions
USAGE = (
    "/watch — shows, ✅ = you're subscribed\n"
    "/watch <show> — get pinged when Mari watches it\n"
    "/watch off <show> — stop"
)
NO_MANAGE_ROLES = "I can't manage roles here: ask Mari to give the Hermes Agent role Manage Roles."

_roles_lock = threading.Lock()  # one roles.json read-modify-write (and role creation) at a time


class Unavailable(Exception):
    """A local file problem; the message is the reply."""


class DiscordError(Exception):
    def __init__(self, status, code=None, message="", retry_after=None):
        super().__init__(f"HTTP {status} ({code}): {message}")
        self.status = status
        self.code = code
        self.message = message
        self.retry_after = retry_after


class Discord:
    """Minimal Discord REST v10 client for the guild. All calls share one BUDGET-second deadline."""

    def __init__(self, token, opener=urllib.request.urlopen, budget=BUDGET, clock=time.monotonic):
        self._auth = f"Bot {token}"
        self.opener = opener
        self.clock = clock
        self.deadline = clock() + budget

    def request(self, method, path, body=None):
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise DiscordError(0, message="timed out")
        headers = {
            "Authorization": self._auth,
            "User-Agent": "DiscordBot (https://watching.fujo.dev, 1)",
            "X-Audit-Log-Reason": "watch subscription",
        }
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(API + path, data=data, method=method, headers=headers)
        try:
            with self.opener(request, timeout=min(10.0, remaining)) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            try:
                info = json.loads(exc.read() or b"{}")
            except (ValueError, OSError):
                info = {}
            finally:
                exc.close()
            info = info if isinstance(info, dict) else {}
            raise DiscordError(
                exc.code, info.get("code"), str(info.get("message") or exc.reason), info.get("retry_after")
            ) from None
        except (urllib.error.URLError, OSError) as exc:
            raise DiscordError(0, message=str(getattr(exc, "reason", exc))) from None
        return json.loads(raw) if raw else None

    def member_roles(self, user_id):
        return set(self.request("GET", f"/guilds/{GUILD_ID}/members/{user_id}").get("roles", []))

    def create_role(self, name):
        body = {"name": name, "permissions": "0", "hoist": False, "mentionable": False}
        return str(self.request("POST", f"/guilds/{GUILD_ID}/roles", body)["id"])

    def add_role(self, user_id, role_id):
        self.request("PUT", f"/guilds/{GUILD_ID}/members/{user_id}/roles/{role_id}")

    def remove_role(self, user_id, role_id):
        self.request("DELETE", f"/guilds/{GUILD_ID}/members/{user_id}/roles/{role_id}")


def error_text(exc):
    if exc.code in NO_PERMISSION or exc.status == 403:
        return NO_MANAGE_ROLES
    if exc.status == 401:
        return "Discord rejected the bot token."
    if exc.status == 429:
        return f"Discord is rate limiting me, try again in {math.ceil(float(exc.retry_after or 5))} s."
    if exc.status == 0:
        return f"Couldn't reach Discord ({exc.message[:80]})."
    return f"Discord error {exc.status}: {exc.message[:120]}"


# ---------------------------------------------------------------- local files


def state_dir():
    base = os.environ.get("XDG_STATE_HOME") or "~/.local/state"
    return Path(base).expanduser() / "watching"


def roles_path():
    return state_dir() / "roles.json"


def library_path():
    return state_dir() / "library.json"


def series_key(series):
    """Same key as notify.series_key in the watching repo: sync.py looks roles up by it."""
    return f"tmdb:{series['tmdb']}" if series.get("tmdb") else f"name:{series['name'].casefold()}"


def load_roles(path):
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {"guild_id": GUILD_ID, "shows": {}}
    except (OSError, ValueError):
        data = None
    # Never overwrite a file we can't read: its roles would be orphaned and created again.
    if not isinstance(data, dict) or not isinstance(data.get("shows"), dict) or data.get("guild_id") != GUILD_ID:
        raise Unavailable(f"{path.name} is broken or belongs to another server; ask Mari to look at it.")
    return data


def save_roles(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n")
    os.replace(tmp, path)


def load_library(path):
    """[{"key", "name", "year"}] from sync.py's library.json, by name; None if missing or unusable."""
    try:
        series = json.loads(path.read_text())["series"]
        shows = [{"key": s["key"], "name": s["name"], "year": s.get("year")} for s in series]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not all(isinstance(s["key"], str) and s["key"] and isinstance(s["name"], str) and s["name"] for s in shows):
        return None
    return shows


def load_progress(path):
    """[{"key", "name", "year"}] in page order."""
    try:
        series = json.loads(path.read_text())["series"]
        return [{"key": series_key(s), "name": s["name"], "year": s.get("year")} for s in series if s.get("name")]
    except (OSError, ValueError, KeyError, TypeError):
        raise Unavailable("Can't read the show list right now.") from None


def load_shows(library, progress):
    """Every show in the library; the shows on the page if library.json is missing or broken."""
    shows = load_library(library)
    return shows if shows is not None else load_progress(progress)


def role_name(show_name):
    return (ROLE_PREFIX + show_name)[:ROLE_NAME_MAX]


# ---------------------------------------------------------------- matching


def norm(text):
    text = re.sub(r"['’]", "", text.casefold())
    return " ".join(re.sub(r"[\W_]+", " ", text).split())


def similarity(query, name):
    """Typo tolerance, against the whole name and against its start (so short queries count)."""
    return max(
        difflib.SequenceMatcher(None, query, name).ratio(),
        difflib.SequenceMatcher(None, query, name[: len(query)]).ratio(),
    )


def match(query, shows):
    """Shows matching `query`: exact, else prefix, else substring, else close (case/punctuation-blind).
    "<name> <year>" counts too, to tell remakes apart."""
    q = norm(query)
    if not q:
        return []
    normed = [
        (show, [norm(show["name"])] + ([norm(f"{show['name']} {show['year']}")] if show.get("year") else []))
        for show in shows
    ]
    for test in (str.__eq__, str.startswith, lambda name, q: q in name):
        hits = [show for show, names in normed if any(test(name, q) for name in names)]
        if hits:
            return hits
    scored = sorted(((similarity(q, names[0]), i) for i, (_, names) in enumerate(normed)), reverse=True)
    return [normed[i][0] for score, i in scored if score >= 0.75]


def labels(shows):
    """Display names, with the year added where several shows share a name."""
    counts = {}
    for show in shows:
        counts[show["name"].casefold()] = counts.get(show["name"].casefold(), 0) + 1
    return [
        f"{show['name']} ({show['year']})" if show.get("year") and counts[show["name"].casefold()] > 1 else show["name"]
        for show in shows
    ]


def pick(query, shows):
    """(show, None) for a single match, else (None, reply text)."""
    hits = match(query, shows)
    if len(hits) == 1:
        return hits[0], None
    if not hits:
        return None, f"No show matches “{query}”. /watch lists them."
    names = labels(hits[:MAX_CANDIDATES])
    more = f" (+{len(hits) - MAX_CANDIDATES} more)" if len(hits) > MAX_CANDIDATES else ""
    return None, "Which one? " + " · ".join(names) + more


def fit(lines):
    """Join lines, cutting the list to fit one Discord message."""
    out, size = [], 0
    for i, line in enumerate(lines):
        if size + len(line) + 1 > MAX_REPLY - 20:
            out.append(f"… +{len(lines) - i} more")
            break
        out.append(line)
        size += len(line) + 1
    return "\n".join(out)


# ---------------------------------------------------------------- command


def caller(platform, source):
    """The Discord user id when the command comes from a person in GUILD_ID, else None (fail closed)."""
    if platform != "discord" or source is None or getattr(source, "is_bot", False):
        return None
    guild = str(getattr(source, "scope_id", None) or getattr(source, "guild_id", None) or "")
    user = str(getattr(source, "user_id", None) or "")
    if guild != GUILD_ID or not user.isdigit():
        return None
    return user


class Watch:
    def __init__(self, progress_path=DEFAULT_PROGRESS, roles_file=None, library_file=None, client=Discord):
        self.progress_path = Path(progress_path).expanduser()
        # None: roles_path() / library_path() per call, honouring XDG_STATE_HOME
        self.roles_file = roles_file
        self.library_file = library_file
        self.client = client

    def handle(self, raw_args, *, platform="", source=None):
        user = caller(platform, source)
        if user is None:
            return "/watch only works in Mari's Discord server."
        args = " ".join((raw_args or "").split())
        word, _, rest = args.partition(" ")
        word = word.lower()
        if word == "help" or (word == "off" and not rest):
            return USAGE
        token = os.environ.get(TOKEN_ENV, "").strip()
        if not token:
            return "/watch isn't set up: the bot token is missing."
        api = self.client(token)
        try:
            if not args or args.lower() == "list":
                return self.list(api, user)
            if word == "off":
                return self.unsubscribe(api, user, rest)
            return self.subscribe(api, user, args)
        except Unavailable as exc:
            return str(exc)
        except DiscordError as exc:
            return error_text(exc)

    def roles_location(self):
        return Path(self.roles_file) if self.roles_file else roles_path()

    def shows(self):
        library = Path(self.library_file) if self.library_file else library_path()
        return load_shows(library, self.progress_path)

    def list(self, api, user):
        shows = self.shows()
        roles = load_roles(self.roles_location())["shows"]
        mine = api.member_roles(user)
        subscribed = {key for key, entry in roles.items() if entry.get("role_id") in mine}
        listed = {show["key"] for show in shows}
        gone = [f"✅ {roles[key].get('name') or key}" for key in sorted(subscribed - listed)]
        if not shows and not gone:
            return "No shows yet."
        names = labels(shows)
        if len(shows) <= MAX_LISTED:
            lines = [f"{'✅' if show['key'] in subscribed else '▫️'} {name}" for show, name in zip(shows, names)]
            return fit(["Shows (✅ = subscribed; /watch <show> to get pinged, /watch off <show> to stop):", *lines, *gone])
        lines = [f"✅ {name}" for show, name in zip(shows, names) if show["key"] in subscribed]
        others = len(shows) - len(lines)
        return fit([
            "Your shows (/watch off <show> to stop):" if lines or gone else "You're not subscribed to any show.",
            *lines,
            *gone,
            f"…and {others} more in the library: /watch <name> to get pinged for one.",
        ])

    def subscribe(self, api, user, query):
        show, reply = pick(query, self.shows())
        if show is None:
            return reply
        path = self.roles_location()
        with _roles_lock:
            data = load_roles(path)
            entry = data["shows"].get(show["key"])
            if entry and entry.get("role_id"):
                try:
                    api.add_role(user, entry["role_id"])
                    return f"Subscribed to {show['name']}"
                except DiscordError as exc:
                    if exc.code != UNKNOWN_ROLE:  # deleted by hand: make a new one
                        raise
            role_id = api.create_role(role_name(show["name"]))
            data["shows"][show["key"]] = {"name": show["name"], "role_id": role_id}
            save_roles(path, data)  # before add_role, so a failure there doesn't orphan the role
            api.add_role(user, role_id)
        return f"Subscribed to {show['name']}"

    def unsubscribe(self, api, user, query):
        roles = load_roles(self.roles_location())["shows"]
        shows = self.shows()
        listed = {show["key"] for show in shows}
        # Also shows that left the list, so their subscriptions can still be dropped.
        shows += [{"key": key, "name": entry.get("name") or key} for key, entry in roles.items() if key not in listed]
        show, reply = pick(query, shows)
        if show is None:
            return reply
        role_id = (roles.get(show["key"]) or {}).get("role_id")
        if not role_id or role_id not in api.member_roles(user):
            return f"You're not subscribed to {show['name']}"
        try:
            api.remove_role(user, role_id)
        except DiscordError as exc:
            if exc.code != UNKNOWN_ROLE:
                raise
        return f"Unsubscribed from {show['name']}"


def register(ctx):
    watch = Watch(ctx.get_config("progress_path", default=DEFAULT_PROGRESS) or DEFAULT_PROGRESS)
    ctx.register_command(
        "watch",
        watch.handle,
        description="Get pinged when Mari watches a show",
        args_hint="[show name | list | off <show>]",
    )
