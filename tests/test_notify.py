import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import notify  # noqa: E402
import sync  # noqa: E402

LINK = "\n<https://watching.fujo.dev>"


def entry(name, tmdb, eps, season=1):
    """A Jellyfin (series, seasons, episodes) tuple; eps: [(number, title, played)]."""
    episodes = [
        {
            "SeasonId": "s",
            "IndexNumber": e,
            "Name": title,
            "UserData": {"Played": played, "LastPlayedDate": "2026-01-01T00:00:00Z" if played else None},
        }
        for e, title, played in eps
    ]
    return (
        {"Name": name, "ProviderIds": {"Tmdb": tmdb} if tmdb else {}},
        [{"Id": "s", "IndexNumber": season, "Name": f"Season {season}"}],
        episodes,
    )


def snapshot(entries, **config):
    cfg = {"user": "u", "include_inactive": False, "include_specials": False, "exclude": [], **config}
    return sync.build_snapshot(entries, cfg, generated="x"), cfg


class FakeSender:
    def __init__(self, ok=True):
        self.ok = ok
        self.sent = []

    def __call__(self, content):
        self.sent.append(content)
        return self.ok


class RunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "watching" / "announced.json"

    def tearDown(self):
        self.tmp.cleanup()

    def run_notify(self, entries, sender="fake", dry_run=False, **config):
        snap, cfg = snapshot(entries, **config)
        sender = FakeSender() if sender == "fake" else sender
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            notify.run(snap, cfg, self.path, sender, dry_run=dry_run, sleep=lambda s: None)
        return sender, out.getvalue()

    def seed(self, entries):
        """First run: seeds state from `entries` and must not send anything."""
        sender, _ = self.run_notify(entries)
        self.assertEqual(sender.sent, [])
        return sender

    def announced(self):
        return set(json.loads(self.path.read_text())["announced"])

    def test_first_run_seeds_silently(self):
        jojo = entry("JoJo", "1", [(1, "A", True), (2, "B", True), (3, "C", False)])
        self.seed([jojo])
        self.assertEqual(self.announced(), {"tmdb:1|1|1", "tmdb:1|1|2"})
        sender, _ = self.run_notify([jojo])
        self.assertEqual(sender.sent, [])

    def test_single_episode(self):
        self.seed([entry("JoJo's Bizarre Adventure", "1", [(9, "x", True), (10, "Scary Monsters (2)", False)], 6)])
        sender, _ = self.run_notify(
            [entry("JoJo's Bizarre Adventure", "1", [(9, "x", True), (10, "Scary Monsters (2)", True)], 6)]
        )
        self.assertEqual(
            sender.sent, ["Mari watched **JoJo's Bizarre Adventure** S06E10 · Scary Monsters (2)" + LINK]
        )
        self.assertIn("tmdb:1|6|10", self.announced())

    def test_titles_off(self):
        self.seed([entry("Show", "1", [(1, "Pilot", False)])])
        sender, _ = self.run_notify([entry("Show", "1", [(1, "Pilot", True)])], notify={"titles": False})
        self.assertEqual(sender.sent, ["Mari watched **Show** S01E01" + LINK])

    def test_multi_contiguous_one_message_per_show(self):
        name = "Romance of the Three Kingdoms"
        self.seed([entry(name, "2", [(e, f"t{e}", e < 7) for e in range(1, 11)]), entry("Other", "3", [(1, "a", False)])])
        sender, _ = self.run_notify(
            [entry(name, "2", [(e, f"t{e}", e < 10) for e in range(1, 11)]), entry("Other", "3", [(1, "a", True)])]
        )
        self.assertCountEqual(
            sender.sent,
            [
                f"Mari watched 3 episodes of **{name}** (S01E07–S01E09)" + LINK,
                "Mari watched **Other** S01E01 · a" + LINK,
            ],
        )

    def test_multi_non_contiguous(self):
        self.seed([entry("Show", "1", [(e, "t", e == 1) for e in range(1, 8)])])
        sender, _ = self.run_notify([entry("Show", "1", [(e, "t", e in (1, 2, 5, 6)) for e in range(1, 8)])])
        self.assertEqual(sender.sent, ["Mari watched 3 episodes of **Show** (S01E02, S01E05–S01E06)" + LINK])

    def test_long_list_is_capped(self):
        self.seed([entry("Show", "1", [(e, "t", False) for e in range(1, 30)])])
        sender, _ = self.run_notify([entry("Show", "1", [(e, "t", e % 2 == 1) for e in range(1, 30)])], notify={"bulk_threshold": 100})
        self.assertEqual(
            sender.sent,
            ["Mari watched 15 episodes of **Show** (S01E01, S01E03, S01E05, S01E07, S01E09, +10 more)" + LINK],
        )

    def test_mute_and_exclude(self):
        before = [entry(n, t, [(1, "a", False)]) for n, t in (("Muted", "1"), ("Hidden", "2"), ("Loud", "3"))]
        after = [entry(n, t, [(1, "a", True)]) for n, t in (("Muted", "1"), ("Hidden", "2"), ("Loud", "3"))]
        self.seed(before)
        sender, _ = self.run_notify(after, exclude=["hidden"], notify={"mute": ["MUTED"]})
        self.assertEqual(sender.sent, ["Mari watched **Loud** S01E01 · a" + LINK])
        # Muted is marked silently so un-muting later doesn't replay it; Hidden is never seen.
        self.assertEqual(self.announced(), {"tmdb:1|1|1", "tmdb:3|1|1"})
        sender, _ = self.run_notify(after, exclude=["hidden"])
        self.assertEqual(sender.sent, [])

    def bulk_case(self, mode):
        self.seed([entry("A", "1", [(e, "t", False) for e in range(1, 11)]), entry("B", "2", [(e, "t", False) for e in range(1, 11)])])
        after = [entry("A", "1", [(e, "t", True) for e in range(1, 11)]), entry("B", "2", [(e, "t", True) for e in range(1, 7)])]
        sender, _ = self.run_notify(after, notify={"bulk": mode})
        self.assertEqual(len(self.announced()), 16)
        return sender

    def test_bulk_summary(self):
        sender = self.bulk_case("summary")
        self.assertEqual(sender.sent, ["Mari marked 16 episodes as watched across 2 shows" + LINK])

    def test_bulk_skip(self):
        self.assertEqual(self.bulk_case("skip").sent, [])

    def test_failed_send_leaves_episode_unannounced(self):
        self.seed([entry("Show", "1", [(1, "a", False)])])
        after = [entry("Show", "1", [(1, "a", True)])]
        self.run_notify(after, sender=FakeSender(ok=False))
        self.assertEqual(self.announced(), set())
        sender, _ = self.run_notify(after)
        self.assertEqual(len(sender.sent), 1)
        self.assertEqual(self.announced(), {"tmdb:1|1|1"})

    def test_remarked_episode_not_resent(self):
        self.seed([entry("Show", "1", [(1, "a", False)])])
        watched = [entry("Show", "1", [(1, "a", True)])]
        self.assertEqual(len(self.run_notify(watched)[0].sent), 1)
        self.run_notify([entry("Show", "1", [(1, "a", False)])])  # unmarked
        self.assertEqual(self.run_notify(watched)[0].sent, [])  # re-marked

    def test_no_webhook_keeps_state(self):
        self.seed([entry("Show", "1", [(1, "a", False)])])
        _, out = self.run_notify([entry("Show", "1", [(1, "a", True)])], sender=None)
        self.assertIn("notify: no webhook configured, skipping", out)
        self.assertEqual(self.announced(), set())

    def test_dry_run_prints_and_keeps_state(self):
        self.seed([entry("Show", "1", [(1, "a", False)])])
        sender, out = self.run_notify([entry("Show", "1", [(1, "a", True)])], dry_run=True)
        self.assertEqual(sender.sent, [])
        self.assertIn("Mari watched **Show** S01E01 · a", out)
        self.assertEqual(self.announced(), set())

    def test_key_falls_back_to_name(self):
        self.seed([entry("No Tmdb", None, [(1, "a", True)])])
        self.assertEqual(self.announced(), {"name:no tmdb|1|1"})


class WebhookFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.file = Path(self.tmp.name) / "discord_webhook"
        self.file.write_text("https://discord.example/api/webhooks/1/secret\n")
        env = mock.patch.dict(os.environ, {notify.WEBHOOK_ENV: ""})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_private_file_is_used(self):
        self.file.chmod(0o600)
        self.assertEqual(notify.load_webhook(self.file), "https://discord.example/api/webhooks/1/secret")

    def test_readable_file_is_refused(self):
        for mode in (0o640, 0o604):
            self.file.chmod(mode)
            err = io.StringIO()
            with redirect_stderr(err):
                self.assertIsNone(notify.load_webhook(self.file))
            self.assertIn("chmod 600", err.getvalue())
            self.assertNotIn("secret", err.getvalue())

    def test_env_wins(self):
        with mock.patch.dict(os.environ, {notify.WEBHOOK_ENV: "https://env/hook"}):
            self.assertEqual(notify.load_webhook(self.file), "https://env/hook")

    def test_missing_file(self):
        self.assertIsNone(notify.load_webhook(Path(self.tmp.name) / "nope"))


class DiscordWebhookTest(unittest.TestCase):
    def test_retries_after_429_then_succeeds(self):
        calls, sleeps = [], []

        class Response:
            status = 204

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def opener(request, timeout):
            calls.append(request)
            if len(calls) == 1:
                raise urllib.error.HTTPError(
                    request.full_url, 429, "Too Many Requests", {}, io.BytesIO(b'{"retry_after": 2.5}')
                )
            return Response()

        hook = notify.DiscordWebhook("https://discord.example/api/webhooks/1/secret", opener=opener, sleep=sleeps.append)
        self.assertTrue(hook("hi"))
        self.assertEqual(sleeps, [2.5])
        body = json.loads(calls[0].data)
        self.assertEqual(body, {"content": "hi", "username": "watching", "allowed_mentions": {"parse": []}})
        self.assertTrue(calls[0].full_url.endswith("?wait=true"))

    def test_network_error_returns_false_without_url(self):
        def opener(request, timeout):
            raise urllib.error.URLError(ConnectionRefusedError("refused"))

        err = io.StringIO()
        with redirect_stderr(err):
            self.assertFalse(notify.DiscordWebhook("https://discord.example/api/webhooks/1/secret", opener=opener)("hi"))
        self.assertNotIn("secret", err.getvalue())


if __name__ == "__main__":
    unittest.main()
