"""Discord notifications for newly watched episodes (stdlib only).

The announced-state file (outside the repo) is the source of truth for "already notified":
an episode is announced at most once, no matter how often it is unmarked and re-marked.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SITE_URL = "https://watching.fujo.dev"
WEBHOOK_ENV = "WATCHING_DISCORD_WEBHOOK"
WEBHOOK_FILE = "~/.config/watching/discord_webhook"
DEFAULTS = {"enabled": True, "titles": True, "bulk_threshold": 15, "bulk": "summary", "mute": []}
MAX_SEGMENTS = 6  # episode-list entries in a multi-episode message before "+N more"
SEND_DELAY = 1.0  # seconds between messages
MAX_RETRY_AFTER = 60.0


def log(message):
    print(f"notify: {message}")


def warn(message):
    print(f"notify: {message}", file=sys.stderr)


# ---------------------------------------------------------------- settings


def notify_config(config):
    return {**DEFAULTS, **(config.get("notify") or {})}


def state_path():
    base = os.environ.get("XDG_STATE_HOME") or "~/.local/state"
    return Path(base).expanduser() / "watching" / "announced.json"


def load_webhook(path=WEBHOOK_FILE):
    """Webhook URL from the environment, else from a chmod-600 file; None if unavailable."""
    url = os.environ.get(WEBHOOK_ENV, "").strip()
    if url:
        return url
    path = Path(path).expanduser()
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        return None
    if mode & 0o077:
        warn(f"{path} is readable by group/others (chmod 600 it), ignoring it")
        return None
    return path.read_text().strip() or None


# ---------------------------------------------------------------- state


def load_announced(path):
    """Set of announced keys, or None when there is no (usable) state yet."""
    try:
        data = json.loads(Path(path).read_text())
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, UnicodeDecodeError):
        warn(f"{path} is unreadable, re-seeding it")
        return None
    return set(data.get("announced", []))


def save_announced(path, keys):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"announced": sorted(keys)}, ensure_ascii=False, indent=1) + "\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------- episodes


def episode_key(series, ep):
    show = f"tmdb:{series['tmdb']}" if series.get("tmdb") else f"name:{series['name'].casefold()}"
    number = ep["e"] if ep.get("e") is not None else f"?{ep.get('title', '')}"
    return f"{show}|{ep['s']}|{number}"


def watched_episodes(snapshot):
    """[(series, episode, key)] for every watched episode, series oldest activity first."""
    out = []
    for series in reversed(snapshot["series"]):
        for season in series["seasons"]:
            for ep in season["episodes"]:
                if ep["state"] == "watched":
                    out.append((series, ep, episode_key(series, ep)))
    return out


# ---------------------------------------------------------------- messages


def md_escape(text):
    return re.sub(r"([\\*_~`|])", r"\\\1", text)


def label(ep):
    return f"S{ep['s']:02d}E{ep['e']:02d}" if ep.get("e") is not None else f"S{ep['s']:02d}E?"


def compact(eps):
    """'S01E02, S01E05–S01E06' with consecutive episodes of one season collapsed to ranges."""
    runs = []
    for ep in eps:
        prev = runs[-1][-1] if runs else None
        if (
            prev is not None
            and ep.get("e") is not None
            and prev.get("e") is not None
            and ep["s"] == prev["s"]
            and ep["e"] == prev["e"] + 1
        ):
            runs[-1].append(ep)
        else:
            runs.append([ep])
    parts = [label(r[0]) if len(r) == 1 else f"{label(r[0])}–{label(r[-1])}" for r in runs]
    if len(parts) > MAX_SEGMENTS:
        shown = runs[: MAX_SEGMENTS - 1]
        rest = len(eps) - sum(len(r) for r in shown)
        parts = parts[: MAX_SEGMENTS - 1] + [f"+{rest} more"]
    return ", ".join(parts)


def with_link(text):
    return f"{text}\n<{SITE_URL}>"


def show_message(series, eps, titles=True):
    name = f"**{md_escape(series['name'])}**"
    if len(eps) == 1:
        ep = eps[0]
        text = f"Mari watched {name} {label(ep)}"
        if titles and ep.get("title"):
            text += f" · {md_escape(ep['title'])}"
    else:
        text = f"Mari watched {len(eps)} episodes of {name} ({compact(eps)})"
    return with_link(text)


def bulk_message(count, shows):
    return with_link(
        f"Mari marked {count} episodes as watched across {shows} show{'s' if shows != 1 else ''}"
    )


def plan(new, ncfg):
    """[(content or None, keys)]: one message per show, or a single bulk entry.
    content None means "mark announced without sending"."""
    by_show = {}
    for series, ep, key in new:
        entry = by_show.setdefault(id(series), (series, [], []))
        entry[1].append(ep)
        entry[2].append(key)
    if len(new) > int(ncfg["bulk_threshold"]):
        keys = [key for _, _, key in new]
        if ncfg["bulk"] == "skip":
            return [(None, keys)]
        return [(bulk_message(len(new), len(by_show)), keys)]
    return [(show_message(s, eps, ncfg["titles"]), keys) for s, eps, keys in by_show.values()]


# ---------------------------------------------------------------- run


def run(snapshot, config, path, sender, dry_run=False, sleep=time.sleep):
    """Announce newly watched episodes. `sender(content) -> bool` (None: no webhook).
    dry_run prints the messages and never changes an existing state file."""
    ncfg = notify_config(config)
    watched = watched_episodes(snapshot)
    announced = load_announced(path)
    if announced is None:
        save_announced(path, {key for _, _, key in watched})
        log(f"seeded announced state with {len(watched)} watched episodes, nothing sent")
        return

    if not ncfg["enabled"]:
        log("disabled in config, skipping")
        return
    if sender is None and not dry_run:
        log("no webhook configured, skipping")
        return

    # Muted shows are marked silently, so un-muting later doesn't replay their backlog.
    muted = {name.casefold() for name in ncfg["mute"]}
    pending = [w for w in watched if w[2] not in announced]
    silent = {key for series, _, key in pending if series["name"].casefold() in muted}
    new = [w for w in pending if w[2] not in silent]
    if silent and not dry_run:
        announced |= silent
        save_announced(path, announced)
    if not new:
        log("nothing new")
        return

    messages = plan(new, ncfg)
    if dry_run:
        for content, keys in messages:
            if content is None:
                log(f"would skip {len(keys)} episodes (bulk)")
            else:
                print(f"--- would send ({len(keys)} episodes)\n{content}")
        return

    sent = failed = 0
    for i, (content, keys) in enumerate(messages):
        if content is None:
            log(f"{len(keys)} new episodes exceed bulk threshold, marked without sending")
        else:
            if i:
                sleep(SEND_DELAY)
            if not sender(content):
                failed += len(keys)
                continue
            sent += 1
        announced |= set(keys)
        save_announced(path, announced)
    log(f"sent {sent} message(s)" + (f", {failed} episode(s) will retry next run" if failed else ""))


class DiscordWebhook:
    """Callable sender: POST one message, honour 429 retry_after. Never logs the URL."""

    def __init__(self, url, opener=urllib.request.urlopen, sleep=time.sleep, attempts=4):
        parts = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qsl(parts.query) + [("wait", "true")]
        self.url = urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))
        self.opener = opener
        self.sleep = sleep
        self.attempts = attempts

    def __call__(self, content):
        body = json.dumps(
            {"content": content, "username": "watching", "allowed_mentions": {"parse": []}}
        ).encode()
        for _ in range(self.attempts):
            request = urllib.request.Request(
                self.url,
                data=body,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": f"watching-sync ({SITE_URL}, 1)",
                },
            )
            try:
                with self.opener(request, timeout=20) as response:
                    if 200 <= response.status < 300:
                        return True
                    warn(f"send failed: HTTP {response.status}")
                    return False
            except urllib.error.HTTPError as exc:
                if exc.code != 429:
                    warn(f"send failed: HTTP {exc.code}")
                    return False
                self.sleep(retry_after(exc))
            except (urllib.error.URLError, OSError) as exc:
                reason = getattr(exc, "reason", exc)
                warn(f"send failed: {type(exc).__name__}: {reason}")
                return False
        warn("send failed: still rate limited")
        return False


def retry_after(exc):
    """Seconds to wait from a 429 response (JSON body, else Retry-After header)."""
    try:
        seconds = float(json.loads(exc.read() or b"{}").get("retry_after"))
    except (TypeError, ValueError, AttributeError, OSError):
        try:
            seconds = float(exc.headers.get("Retry-After"))
        except (TypeError, ValueError, AttributeError):
            seconds = 1.0
    return min(max(seconds, 0.0), MAX_RETRY_AFTER)
