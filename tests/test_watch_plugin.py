import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "hermes-plugin"))

import notify  # noqa: E402
import watch  # noqa: E402

GUILD = watch.GUILD_ID
TOKEN = "bot-token-secret"
USER, OTHER = "42", "43"
SHOWS = [
    {"name": "JoJo's Bizarre Adventure", "tmdb": "45790"},
    {"name": "Romance of the Three Kingdoms", "tmdb": "72645"},
    {"name": "Monster", "tmdb": "30981"},
    {"name": "Monster Musume", "tmdb": "62104"},
    {"name": "No Tmdb Show", "tmdb": None},
]


class Response:
    def __init__(self, status, body=None):
        self.status = status
        self.body = b"" if body is None else json.dumps(body).encode()

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeDiscord:
    """Opener standing in for urlopen: a tiny in-memory guild behind Discord's REST routes."""

    def __init__(self, can_manage=True):
        self.can_manage = can_manage
        self.roles = {}
        self.members = {USER: set(), OTHER: set()}
        self.calls = []
        self.next_id = 1000

    def fail(self, request, status, code, message):
        body = io.BytesIO(json.dumps({"code": code, "message": message}).encode())
        raise urllib.error.HTTPError(request.full_url, status, message, {}, body)

    def __call__(self, request, timeout):
        assert timeout <= 10
        assert request.get_header("Authorization") == f"Bot {TOKEN}"
        method = request.get_method()
        path = request.full_url.removeprefix("https://discord.com/api/v10")
        body = json.loads(request.data) if request.data else None
        self.calls.append((method, path, body))
        parts = path.strip("/").split("/")
        assert parts[:2] == ["guilds", GUILD], path
        if method == "GET" and parts[2] == "members":
            return Response(200, {"user": {"id": parts[3]}, "roles": sorted(self.members[parts[3]])})
        if not self.can_manage:
            self.fail(request, 403, 50013, "Missing Permissions")
        if method == "POST" and parts[2:] == ["roles"]:
            self.next_id += 1
            self.roles[str(self.next_id)] = body
            return Response(200, {"id": str(self.next_id), **body})
        if parts[2] == "members" and parts[4] == "roles":
            if parts[5] not in self.roles:
                self.fail(request, 404, 10011, "Unknown Role")
            getattr(self.members[parts[3]], "add" if method == "PUT" else "discard")(parts[5])
            return Response(204)
        raise AssertionError(f"unexpected {method} {path}")


def source(user=USER, guild=GUILD, **extra):
    return SimpleNamespace(user_id=user, scope_id=guild, guild_id=guild, is_bot=False, **extra)


class WatchTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.progress = self.dir / "progress.json"
        self.progress.write_text(json.dumps({"series": SHOWS}))
        self.roles_file = self.dir / "state" / "watching" / "roles.json"
        self.api = FakeDiscord()
        self.watch = watch.Watch(
            self.progress, self.roles_file, client=lambda token: watch.Discord(token, opener=self.api)
        )
        env = mock.patch.dict(os.environ, {watch.TOKEN_ENV: TOKEN})
        env.start()
        self.addCleanup(env.stop)

    def run_cmd(self, args, user=USER, **kw):
        reply = self.watch.handle(args, platform="discord", source=source(user, **kw))
        self.assertNotIn(TOKEN, reply)
        return reply

    def roles(self):
        return json.loads(self.roles_file.read_text())

    def test_fails_closed(self):
        cases = [
            ("discord", None),
            ("discord", SimpleNamespace(user_id=None, scope_id=GUILD, is_bot=False)),
            ("discord", source(guild=None)),
            ("discord", source(guild="1")),
            ("discord", SimpleNamespace(user_id=USER, scope_id=GUILD, is_bot=True)),
            ("telegram", source()),
            ("cli", None),
        ]
        for platform, src in cases:
            reply = self.watch.handle("JoJo", platform=platform, source=src)
            self.assertIn("only works in", reply, (platform, src))
        self.assertEqual(self.api.calls, [])
        self.assertFalse(self.roles_file.exists())

    def test_subscribe_creates_role_records_it_and_adds_caller(self):
        self.assertEqual(self.run_cmd("jojo"), "Subscribed to JoJo's Bizarre Adventure")
        (role_id, role), = self.api.roles.items()
        self.assertEqual(
            role, {"name": "📺 JoJo's Bizarre Adventure", "permissions": "0", "hoist": False, "mentionable": False}
        )
        self.assertEqual(
            self.roles(),
            {"guild_id": GUILD, "shows": {"tmdb:45790": {"name": "JoJo's Bizarre Adventure", "role_id": role_id}}},
        )
        self.assertEqual(self.api.members[USER], {role_id})
        # sync.py reads the same file and pings that role.
        self.assertEqual(notify.load_roles(self.roles_file), {"tmdb:45790": role_id})

    def test_second_subscriber_reuses_role(self):
        self.run_cmd("JoJo's Bizarre Adventure")
        self.assertEqual(self.run_cmd("JOJOS BIZARRE", user=OTHER), "Subscribed to JoJo's Bizarre Adventure")
        self.assertEqual(len(self.api.roles), 1)
        self.assertEqual(self.api.members[USER], self.api.members[OTHER])

    def test_role_deleted_by_hand_is_recreated(self):
        self.run_cmd("jojo")
        self.api.roles.clear()
        self.api.members[USER].clear()
        self.assertEqual(self.run_cmd("jojo"), "Subscribed to JoJo's Bizarre Adventure")
        new_id = self.roles()["shows"]["tmdb:45790"]["role_id"]
        self.assertEqual(set(self.api.roles), {new_id})
        self.assertEqual(self.api.members[USER], {new_id})

    def test_long_name_role_truncated(self):
        long = "A" * 120
        self.progress.write_text(json.dumps({"series": [{"name": long, "tmdb": "1"}]}))
        self.run_cmd("a")
        (role,) = self.api.roles.values()
        self.assertEqual(role["name"], "📺 " + "A" * 98)

    def test_matching(self):
        self.assertEqual(self.run_cmd("three kingdoms"), "Subscribed to Romance of the Three Kingdoms")
        self.assertEqual(self.run_cmd("romanse of the thre"), "Subscribed to Romance of the Three Kingdoms")
        self.assertEqual(self.run_cmd("monster"), "Subscribed to Monster")  # exact beats prefix
        self.assertEqual(self.run_cmd("no tmdb"), "Subscribed to No Tmdb Show")
        self.assertIn("name:no tmdb show", self.roles()["shows"])

    def test_ambiguous_and_unknown_touch_nothing(self):
        reply = self.run_cmd("mon")
        self.assertTrue(reply.startswith("Which one?"), reply)
        self.assertIn("Monster · Monster Musume", reply)
        self.assertIn("No show matches", self.run_cmd("breaking bad"))
        self.assertEqual(self.api.calls, [])
        self.assertFalse(self.roles_file.exists())

    def test_list_marks_own_subscriptions(self):
        self.run_cmd("jojo", user=OTHER)
        self.run_cmd("monster musume")
        lines = self.run_cmd("list").splitlines()
        self.assertIn("✅ Monster Musume", lines)
        self.assertIn("▫️ JoJo's Bizarre Adventure", lines)  # OTHER's subscription, not mine
        self.assertIn("▫️ Monster", lines)
        self.assertEqual(self.run_cmd(""), "\n".join(lines))

    def test_off_removes_only_callers_role(self):
        self.run_cmd("jojo")
        self.run_cmd("jojo", user=OTHER)
        before = self.roles()
        self.assertEqual(self.run_cmd("off jojo"), "Unsubscribed from JoJo's Bizarre Adventure")
        self.assertEqual(self.api.members[USER], set())
        self.assertEqual(len(self.api.members[OTHER]), 1)
        self.assertEqual(len(self.api.roles), 1)  # role stays
        self.assertEqual(self.roles(), before)
        self.assertEqual(self.run_cmd("off jojo"), "You're not subscribed to JoJo's Bizarre Adventure")
        self.assertEqual(self.run_cmd("off monster"), "You're not subscribed to Monster")

    def test_off_works_for_show_gone_from_page(self):
        self.run_cmd("monster musume")
        self.progress.write_text(json.dumps({"series": SHOWS[:1]}))
        self.assertIn("✅ Monster Musume", self.run_cmd("list").splitlines())
        self.assertEqual(self.run_cmd("off musume"), "Unsubscribed from Monster Musume")

    def test_missing_manage_roles_is_readable(self):
        self.api.can_manage = False
        reply = self.run_cmd("jojo")
        self.assertIn("ask Mari to give the Hermes Agent role Manage Roles", reply)
        self.assertFalse(self.roles_file.exists())

    def test_network_error_is_readable(self):
        def down(request, timeout):
            raise urllib.error.URLError(TimeoutError("timed out"))

        self.watch.client = lambda token: watch.Discord(token, opener=down)
        self.assertEqual(self.run_cmd("jojo"), "Couldn't reach Discord (timed out).")

    def test_missing_token(self):
        with mock.patch.dict(os.environ, {watch.TOKEN_ENV: ""}):
            self.assertIn("token is missing", self.run_cmd("jojo"))
        self.assertEqual(self.api.calls, [])

    def test_broken_roles_file_is_not_overwritten(self):
        self.roles_file.parent.mkdir(parents=True)
        for text in ("{oops", json.dumps({"guild_id": "1", "shows": {}})):
            self.roles_file.write_text(text)
            self.assertIn("ask Mari", self.run_cmd("jojo"))
            self.assertEqual(self.roles_file.read_text(), text)
        self.assertEqual(self.api.calls, [])

    def test_series_key_matches_sync(self):
        for series in SHOWS:
            self.assertEqual(watch.series_key(series), notify.series_key(series))

    def test_register(self):
        registered = {}
        ctx = SimpleNamespace(
            get_config=lambda key, default=None: str(self.progress),
            register_command=lambda name, handler, **kw: registered.update({name: (handler, kw)}),
        )
        watch.register(ctx)
        handler, kw = registered["watch"]
        self.assertEqual(kw["args_hint"], "[show name | list | off <show>]")
        self.assertIn("only works in", handler("jojo", platform="discord", source=None))


if __name__ == "__main__":
    unittest.main()
