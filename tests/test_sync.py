import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sync  # noqa: E402

CONFIG = {"user": "u", "include_inactive": False, "include_specials": False, "exclude": []}


def season(sid, number, name=None):
    return {"Id": sid, "IndexNumber": number, "Name": name or f"Season {number}"}


def episode(sid, s, e, name, played=False, pct=None, ticks=0, last=None):
    user_data = {"Played": played, "PlaybackPositionTicks": ticks}
    if pct is not None:
        user_data["PlayedPercentage"] = pct
    if last:
        user_data["LastPlayedDate"] = last
    return {"SeasonId": sid, "ParentIndexNumber": s, "IndexNumber": e, "Name": name, "UserData": user_data}


def series(name, year=2000, tmdb="1"):
    return {"Name": name, "ProductionYear": year, "ProviderIds": {"Tmdb": tmdb} if tmdb else {}}


def show(name, last, played=True):
    """A one-episode show, watched at `last` (or untouched when played is False)."""
    ep = episode("s1", 1, 1, "Pilot", played=played, last=last if played else None)
    return series(name), [season("s1", 1)], [ep]


class EpisodeStateTest(unittest.TestCase):
    def test_classification(self):
        self.assertEqual(sync.episode_state({"Played": True}), ("watched", 100))
        self.assertEqual(sync.episode_state({"Played": False}), ("unwatched", 0))
        self.assertEqual(sync.episode_state(None), ("unwatched", 0))
        self.assertEqual(
            sync.episode_state({"PlaybackPositionTicks": 5, "PlayedPercentage": 52.4}),
            ("in_progress", 52),
        )

    def test_resume_position_beats_played_flag(self):
        # Jellyfin keeps Played=true while a watched episode is being rewatched.
        state = sync.episode_state({"Played": True, "PlaybackPositionTicks": 9, "PlayedPercentage": 52.2})
        self.assertEqual(state, ("in_progress", 52))

    def test_in_progress_pct_never_reads_as_done_or_empty(self):
        self.assertEqual(sync.episode_state({"PlaybackPositionTicks": 1, "PlayedPercentage": 0.2})[1], 1)
        self.assertEqual(sync.episode_state({"PlaybackPositionTicks": 1, "PlayedPercentage": 99.8})[1], 99)


class BuildSeriesTest(unittest.TestCase):
    def build(self, eps, seasons=None, config=CONFIG, s=None):
        seasons = seasons if seasons is not None else [season("s1", 1), season("s2", 2)]
        return sync.build_series(s or series("Show"), seasons, eps, config)

    def test_counts_states_and_last_activity(self):
        out = self.build([
            episode("s1", 1, 2, "Two", played=True, last="2026-11-20T10:00:00.1234567Z"),
            episode("s1", 1, 1, "One", played=True, last="2026-11-01T10:00:00Z"),
            episode("s2", 2, 1, "Three", ticks=10, pct=40.0, last="2026-11-24T06:00:17.3253093Z"),
            episode("s2", 2, 2, "Four"),
        ])
        self.assertEqual((out["watched"], out["total"]), (2, 4))
        self.assertEqual(out["last_activity"], "2026-11-24T06:00:17Z")
        self.assertEqual(out["tmdb"], "1")
        self.assertEqual([s["number"] for s in out["seasons"]], [1, 2])
        s1, s2 = out["seasons"]
        self.assertEqual([e["e"] for e in s1["episodes"]], [1, 2])  # sorted by episode number
        self.assertEqual(
            s2["episodes"][0],
            {"s": 2, "e": 1, "title": "Three", "state": "in_progress", "pct": 40},
        )
        self.assertEqual(s2["episodes"][1]["state"], "unwatched")

    def test_specials_skipped_by_default(self):
        seasons = [season("s0", 0, "Specials"), season("s1", 1), season("bonus", None, "BONUS MATERIAL")]
        eps = [
            episode("s0", 0, 1, "Special", played=True, last="2026-11-25T00:00:00Z"),
            episode("bonus", 19, 94, "Behind the scenes", played=True, last="2026-11-25T00:00:00Z"),
            episode("s1", 1, 1, "Pilot", played=True, last="2026-11-01T00:00:00Z"),
        ]
        out = self.build(eps, seasons)
        self.assertEqual(out["total"], 1)
        self.assertEqual([s["number"] for s in out["seasons"]], [1])
        self.assertEqual(out["last_activity"], "2026-11-01T00:00:00Z")

        with_specials = self.build(eps, seasons, config={**CONFIG, "include_specials": True})
        self.assertEqual(with_specials["total"], 3)
        self.assertEqual([s["number"] for s in with_specials["seasons"]], [1, 0, 0])

    def test_inactive_series_hidden_unless_configured(self):
        eps = [episode("s1", 1, 1, "Pilot"), episode("s1", 1, 2, "Two")]
        self.assertIsNone(self.build(eps))
        out = self.build(eps, config={**CONFIG, "include_inactive": True})
        self.assertEqual((out["watched"], out["total"], out["last_activity"]), (0, 2, None))

    def test_in_progress_alone_counts_as_activity(self):
        out = self.build([episode("s1", 1, 1, "Pilot", ticks=3, pct=10, last="2026-11-02T00:00:00Z")])
        self.assertIsNotNone(out)
        self.assertEqual(out["watched"], 0)

    def test_unnumbered_episodes(self):
        seasons = [season("a", 1), season("b", 2)]
        eps = [
            # all-unnumbered season keeps server order and is numbered 1..N
            episode("a", None, None, "Show - S01E011 - First", played=True, last="2026-11-01T00:00:00Z"),
            episode("a", None, None, "Show - S01E012 - Second"),
            # mixed season: numbered ones sorted first, unknown ones appended without a number
            episode("b", None, None, "Mystery"),
            episode("b", 2, 6, "Six"),
            episode("b", 2, 2, "Two"),
        ]
        out = self.build(eps, seasons)
        a, b = out["seasons"]
        self.assertEqual([(e["e"], e["title"]) for e in a["episodes"]],
                         [(1, "S01E011 - First"), (2, "S01E012 - Second")])
        self.assertEqual([e["e"] for e in b["episodes"]], [2, 6, None])


class SnapshotTest(unittest.TestCase):
    def test_sorted_by_activity_desc_then_name(self):
        entries = [
            show("Old", "2026-01-01T00:00:00Z"),
            show("beta", "2026-11-24T00:00:00Z"),
            show("Alpha", "2026-11-24T00:00:00Z"),
            show("Newest", "2026-11-25T12:00:00Z"),
        ]
        snap = sync.build_snapshot(entries, CONFIG, generated="2026-11-26T00:00:00Z")
        self.assertEqual([s["name"] for s in snap["series"]], ["Newest", "Alpha", "beta", "Old"])
        self.assertEqual(snap["generated"], "2026-11-26T00:00:00Z")

    def test_inactive_sorted_last_when_included(self):
        entries = [show("Idle", None, played=False), show("Busy", "2026-11-24T00:00:00Z")]
        snap = sync.build_snapshot(entries, {**CONFIG, "include_inactive": True})
        self.assertEqual([s["name"] for s in snap["series"]], ["Busy", "Idle"])

    def test_exclude_list_is_case_insensitive(self):
        entries = [show("Keep", "2026-11-24T00:00:00Z"), show("Hide Me", "2026-11-25T00:00:00Z")]
        snap = sync.build_snapshot(entries, {**CONFIG, "exclude": ["hide me"]})
        self.assertEqual([s["name"] for s in snap["series"]], ["Keep"])


class ChangeDetectionTest(unittest.TestCase):
    def test_generated_timestamp_ignored(self):
        entries = [show("A", "2026-11-24T00:00:00Z")]
        first = sync.build_snapshot(entries, CONFIG, generated="2026-11-26T00:00:00Z")
        second = sync.build_snapshot(entries, CONFIG, generated="2026-11-26T00:30:00Z")
        self.assertFalse(sync.content_changed(first, second))

    def test_progress_change_detected(self):
        before = sync.build_snapshot([show("A", "2026-11-24T00:00:00Z", played=True)], CONFIG)
        after = sync.build_snapshot(
            [show("A", "2026-11-24T00:00:00Z", played=True), show("B", "2026-11-25T00:00:00Z")], CONFIG
        )
        self.assertTrue(sync.content_changed(before, after))

    def test_missing_previous_file_counts_as_changed(self):
        self.assertTrue(sync.content_changed(None, {"generated": "x", "series": []}))


if __name__ == "__main__":
    unittest.main()
