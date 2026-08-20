#!/usr/bin/env python3
"""Hermetic tests for usage.py and picker.py.

Never touches the network: every live probe goes through an injected opener.
Never touches a real space: every path is a temporary directory. Run directly:

    python3 ~/.local/share/cc-accounts/test_usage.py
"""

import importlib.util
import io
import json
import os
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path

HERE = Path(__file__).resolve().parent
_TMP = Path(tempfile.mkdtemp(prefix="cca-usage-test-"))
os.environ["CCA_HOME"] = str(_TMP / "accounts-home")


def _load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


usage = _load("usage")
picker = _load("picker")

NOW = 1_800_000_000.0
HOUR = 3600.0


def iso(epoch):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def body(five=10.0, week=20.0, five_at=NOW + 2 * HOUR, week_at=NOW + 48 * HOUR,
         credits=None):
    return {
        "five_hour": {"utilization": five, "resets_at": iso(five_at) if five_at else None},
        "seven_day": {"utilization": week, "resets_at": iso(week_at) if week_at else None},
        "seven_day_opus": None,
        "extra_usage": credits,
        "limits": [],
    }


def space(name, *, token="tok", cached=None):
    """A throwaway Claude space with credentials and an optional agent cache."""
    root = _TMP / name
    root.mkdir(parents=True, exist_ok=True)
    (root / ".credentials.json").write_text(json.dumps(
        {"claudeAiOauth": {"accessToken": token, "subscriptionType": "max"}}))
    config = {"oauthAccount": {"emailAddress": f"{name}@example.com"}}
    if cached is not None:
        config["cachedUsageUtilization"] = cached
    (root / ".claude.json").write_text(json.dumps(config))
    return {"name": name, "tool": "claude", "config_dir": str(root),
            "config_file": str(root / ".claude.json"),
            "credentials_file": str(root / ".credentials.json"),
            "plan": "max", "email": f"{name}@example.com"}


class FakeOpener:
    """Stands in for urlopen. Records requests, replays a scripted answer."""

    def __init__(self, payload=None, error=None):
        self.payload, self.error = payload, error
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append((request, timeout))
        if self.error:
            raise self.error
        data = json.dumps(self.payload).encode()

        class Response(io.BytesIO):
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *_):
                return False

        return Response(data)


# -- parsing --------------------------------------------------------------- #


class TestParse(unittest.TestCase):
    def test_both_windows_are_read(self):
        report = usage.parse_utilization(body(19, 45), space("p1"),
                                         source="live", as_of=NOW, now=NOW)
        self.assertEqual(report["windows"]["five_hour"]["used_pct"], 19.0)
        self.assertEqual(report["windows"]["five_hour"]["free_pct"], 81.0)
        self.assertEqual(report["windows"]["seven_day"]["used_pct"], 45.0)
        self.assertEqual(report["windows"]["five_hour"]["resets_in"], 7200)

    def test_binding_window_is_the_tighter_one(self):
        report = usage.parse_utilization(body(5, 99), space("p2"),
                                         source="live", as_of=NOW, now=NOW)
        self.assertEqual(report["binding"], "seven_day")
        self.assertEqual(report["free_pct"], 1.0)

    def test_a_window_past_its_reset_reads_as_empty(self):
        """The one repair a stale figure allows: it rolled over meanwhile."""
        report = usage.parse_utilization(
            body(90, 40, five_at=NOW - HOUR), space("p3"),
            source="cache", as_of=NOW - 6 * HOUR, now=NOW)
        five = report["windows"]["five_hour"]
        self.assertEqual(five["used_pct"], 0.0)
        self.assertTrue(five["rolled_over"])
        self.assertIsNone(five["resets_at"])
        self.assertEqual(report["binding"], "seven_day")

    def test_missing_window_is_none_not_zero(self):
        report = usage.parse_utilization({"five_hour": None, "seven_day": None},
                                         space("p4"), source="live", as_of=NOW, now=NOW)
        self.assertIsNone(report["windows"]["five_hour"])
        self.assertIsNone(report["binding"])
        self.assertIsNone(report["free_pct"])

    def test_percentages_are_clamped(self):
        report = usage.parse_utilization(body(-5, 140), space("p5"),
                                         source="live", as_of=NOW, now=NOW)
        self.assertEqual(report["windows"]["five_hour"]["used_pct"], 0.0)
        self.assertEqual(report["windows"]["seven_day"]["used_pct"], 100.0)

    def test_credits_convert_out_of_minor_units(self):
        report = usage.parse_utilization(
            body(credits={"is_enabled": True, "monthly_limit": 100000,
                          "used_credits": 47848, "utilization": 47.848,
                          "currency": "USD", "decimal_places": 2}),
            space("p6"), source="live", as_of=NOW, now=NOW)
        self.assertEqual(report["credits"]["used"], 478.48)
        self.assertEqual(report["credits"]["limit"], 1000.0)

    def test_credits_absent_when_not_enabled(self):
        report = usage.parse_utilization(body(credits={"is_enabled": False}),
                                         space("p7"), source="live", as_of=NOW, now=NOW)
        self.assertIsNone(report["credits"])

    def test_plan_and_email_come_from_the_spec(self):
        report = usage.parse_utilization(body(), space("p8"), source="live",
                                         as_of=NOW, now=NOW)
        self.assertEqual(report["plan"], "max")
        self.assertEqual(report["email"], "p8@example.com")


# -- the live call --------------------------------------------------------- #


class TestFetchLive(unittest.TestCase):
    def test_request_carries_the_token_and_the_oauth_beta(self):
        opener = FakeOpener(body())
        usage.fetch_live(space("f1", token="secret-token"), opener=opener)
        request = opener.requests[0][0]
        self.assertEqual(request.full_url, usage.USAGE_URL)
        self.assertEqual(request.get_header("Authorization"), "Bearer secret-token")
        self.assertEqual(request.get_header("Anthropic-beta"), usage.OAUTH_BETA)

    def test_a_logged_out_space_never_reaches_the_network(self):
        spec = space("f2")
        Path(spec["credentials_file"]).write_text("{}")
        opener = FakeOpener(body())
        with self.assertRaises(usage.UsageError):
            usage.fetch_live(spec, opener=opener)
        self.assertEqual(opener.requests, [])

    def test_401_says_to_log_in_again(self):
        opener = FakeOpener(error=urllib.error.HTTPError(
            usage.USAGE_URL, 401, "Unauthorized", {}, None))
        with self.assertRaises(usage.UsageError) as caught:
            usage.fetch_live(space("f3"), opener=opener)
        self.assertIn("log in again", str(caught.exception))

    def test_429_quotes_the_servers_own_retry_after(self):
        """A cooldown on *asking* — never phrased as the account being spent."""
        opener = FakeOpener(error=urllib.error.HTTPError(
            usage.USAGE_URL, 429, "Too Many Requests", {"Retry-After": "108"}, None))
        with self.assertRaises(usage.UsageError) as caught:
            usage.fetch_live(space("f6"), opener=opener)
        message = str(caught.exception)
        self.assertEqual("usage API busy, retry in 108s", message)
        self.assertNotIn("rate", message.lower())

    def test_429_without_a_retry_after_still_reads_sensibly(self):
        opener = FakeOpener(error=urllib.error.HTTPError(
            usage.USAGE_URL, 429, "Too Many Requests", {}, None))
        with self.assertRaises(usage.UsageError) as caught:
            usage.fetch_live(space("f7"), opener=opener)
        self.assertEqual("usage API busy, retry shortly", str(caught.exception))

    def test_unreachable_is_reported_not_raised_as_urlerror(self):
        opener = FakeOpener(error=urllib.error.URLError("no route"))
        with self.assertRaises(usage.UsageError) as caught:
            usage.fetch_live(space("f4"), opener=opener)
        self.assertIn("unreachable", str(caught.exception))

    def test_timeout_is_passed_through(self):
        opener = FakeOpener(body())
        usage.fetch_live(space("f5"), timeout=2.5, opener=opener)
        self.assertEqual(opener.requests[0][1], 2.5)


# -- probe: live, then the labelled fallbacks ------------------------------ #


class TestProbe(unittest.TestCase):
    def setUp(self):
        cache = usage.cache_path()
        if cache.exists():
            cache.unlink()

    def test_live_success_is_labelled_live_and_not_stale(self):
        report = usage.probe(space("q1"), opener=FakeOpener(body(19, 45)), now=NOW)
        self.assertEqual(report["source"], "live")
        self.assertEqual(report["stale_seconds"], 0)
        self.assertIsNone(report["error"])

    def test_a_good_probe_is_remembered_for_the_next_failure(self):
        spec = space("q2")
        usage.probe(spec, opener=FakeOpener(body(11, 22)), now=NOW)
        self.assertTrue(usage.cache_path().exists())

        failing = FakeOpener(error=urllib.error.URLError("down"))
        later = usage.probe(spec, opener=failing, now=NOW + 600)
        self.assertEqual(later["source"], "cache")
        self.assertEqual(later["windows"]["seven_day"]["used_pct"], 22.0)
        self.assertEqual(later["stale_seconds"], 600)
        self.assertIn("unreachable", later["error"])

    def test_falls_back_to_the_agents_own_cache(self):
        spec = space("q3", cached={
            "fetchedAtMs": (NOW - 2 * HOUR) * 1000,
            "utilization": body(30, 60),
        })
        report = usage.probe(spec, opener=FakeOpener(
            error=urllib.error.URLError("down")), now=NOW)
        self.assertEqual(report["source"], "cache")
        self.assertEqual(report["windows"]["seven_day"]["used_pct"], 60.0)
        self.assertEqual(report["stale_seconds"], int(2 * HOUR))

    def test_no_cache_anywhere_is_an_honest_failure(self):
        report = usage.probe(space("q4"), opener=FakeOpener(
            error=urllib.error.URLError("down")), now=NOW)
        self.assertFalse(report["ok"])
        self.assertIsNone(report["free_pct"])
        self.assertIn("unreachable", report["error"])

    def test_cached_mode_makes_no_request_at_all(self):
        spec = space("q5", cached={"fetchedAtMs": NOW * 1000, "utilization": body(7, 8)})
        opener = FakeOpener(body(99, 99))
        report = usage.probe(spec, live=False, opener=opener, now=NOW)
        self.assertEqual(opener.requests, [])
        self.assertEqual(report["windows"]["five_hour"]["used_pct"], 7.0)

    def test_asking_for_the_cache_is_not_an_error(self):
        """`--cached` got what it asked for; nothing failed."""
        spec = space("q6", cached={"fetchedAtMs": NOW * 1000, "utilization": body()})
        self.assertIsNone(usage.probe(spec, live=False, now=NOW)["error"])


# -- codex ----------------------------------------------------------------- #


class TestCodexRollout(unittest.TestCase):
    def _codex(self, name, events):
        root = _TMP / name
        day = root / "sessions" / "2026" / "08" / "13"
        day.mkdir(parents=True, exist_ok=True)
        path = day / "rollout-2026-08-13T18-53-42-abc.jsonl"
        path.write_text("\n".join(json.dumps(e) for e in events))
        os.utime(path, (NOW - HOUR, NOW - HOUR))
        return {"name": name, "tool": "codex", "config_dir": str(root),
                "config_file": str(root / "config.toml"),
                "credentials_file": str(root / "auth.json"),
                "plan": "plus", "email": None}

    def test_the_last_rate_limits_event_wins(self):
        spec = self._codex("cx1", [
            {"payload": {"info": {"rate_limits": {
                "primary": {"used_percent": 10.0, "window_minutes": 10080,
                            "resets_at": NOW + 5 * HOUR}}}}},
            {"payload": {"info": {"rate_limits": {
                "primary": {"used_percent": 49.0, "window_minutes": 10080,
                            "resets_at": NOW + 3 * HOUR}}}}},
        ])
        report = usage.probe(spec, now=NOW)
        self.assertEqual(report["source"], "rollout")
        self.assertEqual(report["windows"]["seven_day"]["used_pct"], 49.0)
        self.assertIsNone(report["windows"]["five_hour"])
        self.assertEqual(report["stale_seconds"], int(HOUR))

    def test_window_length_decides_which_window_it_is(self):
        spec = self._codex("cx2", [{"payload": {"info": {"rate_limits": {
            "primary": {"used_percent": 20.0, "window_minutes": 300,
                        "resets_at": NOW + HOUR},
            "secondary": {"used_percent": 70.0, "window_minutes": 10080,
                          "resets_at": NOW + 40 * HOUR}}}}}])
        report = usage.probe(spec, now=NOW)
        self.assertEqual(report["windows"]["five_hour"]["used_pct"], 20.0)
        self.assertEqual(report["windows"]["seven_day"]["used_pct"], 70.0)
        self.assertEqual(report["binding"], "seven_day")

    def test_no_rollout_says_so_rather_than_reporting_zero(self):
        spec = self._codex("cx3", [])
        report = usage.probe(spec, now=NOW)
        self.assertFalse(report["ok"])
        self.assertIsNone(report["free_pct"])

    def test_codex_never_calls_the_claude_endpoint(self):
        spec = self._codex("cx4", [])
        opener = FakeOpener(body())
        usage.probe(spec, opener=opener, now=NOW)
        self.assertEqual(opener.requests, [])


# -- ranking --------------------------------------------------------------- #


class TestRank(unittest.TestCase):
    def _report(self, name, five, week, ok=True):
        spec = {"name": name, "tool": "claude", "plan": "max", "email": None}
        if not ok:
            return usage._failed(spec, "unreachable", NOW)
        return usage.parse_utilization(body(five, week), spec,
                                       source="live", as_of=NOW, now=NOW)

    def test_the_tighter_window_decides(self):
        """5% of the 5h window is worth nothing behind a full weekly one."""
        roomy = self._report("roomy", 60, 60)
        blocked = self._report("blocked", 5, 99)
        self.assertEqual(usage.best([blocked, roomy])["account"], "roomy")

    def test_ties_break_on_the_other_window(self):
        a = self._report("a", 40, 30)   # binding 5h -> 60 free, other 70
        b = self._report("b", 40, 10)   # binding 5h -> 60 free, other 90
        self.assertEqual([r["account"] for r in usage.rank([a, b])], ["b", "a"])

    def test_an_unmeasured_account_sorts_last_and_is_never_best(self):
        broken = self._report("broken", 0, 0, ok=False)
        used = self._report("used", 95, 95)
        self.assertEqual([r["account"] for r in usage.rank([broken, used])],
                         ["used", "broken"])
        self.assertEqual(usage.best([broken, used])["account"], "used")

    def test_best_of_nothing_measurable_is_none(self):
        self.assertIsNone(usage.best([self._report("x", 0, 0, ok=False)]))
        self.assertIsNone(usage.best([]))


# -- collect --------------------------------------------------------------- #


class TestCollect(unittest.TestCase):
    def test_every_account_comes_back_even_when_one_fails(self):
        good, bad = space("c1"), space("c2")
        Path(bad["credentials_file"]).write_text("{}")
        reports = usage.collect([good, bad], opener=FakeOpener(body()))
        self.assertEqual([r["account"] for r in reports], ["c1", "c2"])
        self.assertTrue(reports[0]["ok"])
        self.assertFalse(reports[1]["ok"])

    def test_order_is_preserved(self):
        specs = [space(f"c{i}") for i in range(3, 8)]
        reports = usage.collect(specs, opener=FakeOpener(body()))
        self.assertEqual([r["account"] for r in reports],
                         [s["name"] for s in specs])


# -- formatting ------------------------------------------------------------ #


class TestFormatting(unittest.TestCase):
    def test_duration_never_exceeds_six_characters(self):
        """The picker lays this column out at a fixed width."""
        for seconds in (0, 1, 59, 60, 3599, 3600, 35999, 36000, 86399,
                        86400, 200000, 899999, 900000, 30_000_000):
            self.assertLessEqual(len(usage.fmt_duration(seconds)), 6,
                                 f"{seconds}s -> {usage.fmt_duration(seconds)!r}")

    def test_a_fresh_fallback_does_not_read_as_stale_now(self):
        """A cache taken seconds after a good probe still has a real age."""
        self.assertEqual("just now", usage.fmt_age(3))
        self.assertEqual("50s ago", usage.fmt_age(50))
        self.assertEqual("2h 04m ago", usage.fmt_age(7440))

    def test_bar_shows_any_use_at_all(self):
        self.assertEqual(usage.bar(0, 8).count("▇"), 0)
        self.assertEqual(usage.bar(1, 8).count("▇"), 1)
        self.assertEqual(usage.bar(100, 8).count("▇"), 8)

    def test_bar_is_always_its_declared_width(self):
        for pct in (None, 0, 3, 50, 99.6, 100):
            self.assertEqual(len(usage.bar(pct, 10)), 10)


# -- the picker's rendering ------------------------------------------------ #


def rows(n=4, ok=True):
    out = []
    for i in range(n):
        spec = {"name": f"acct{i}", "tool": "claude", "plan": "max", "email": None}
        if ok:
            out.append(usage.parse_utilization(body(i * 7, i * 11), spec,
                                               source="live", as_of=NOW, now=NOW))
        else:
            out.append(usage._failed(spec, "unreachable", NOW))
    return out


class TestRender(unittest.TestCase):
    def test_every_line_is_exactly_the_terminal_width(self):
        for width in (40, 55, 68, 74, 80, 86, 100, 132):
            for line in picker.render(rows(), 1, width, 24, color=False):
                self.assertEqual(len(line), width,
                                 f"width {width}: {line!r} is {len(line)}")

    def test_width_holds_with_colour_on(self):
        for line in picker.render(rows(), 1, 100, 24, color=True):
            self.assertEqual(len(picker._strip(line)), 100)

    def test_width_holds_for_unmeasured_accounts(self):
        for width in (40, 68, 100):
            for line in picker.render(rows(ok=False), 0, width, 24, color=False):
                self.assertEqual(len(line), width)

    def test_the_footer_survives_a_short_terminal(self):
        lines = picker.render(rows(12), 11, 90, 8, color=False)
        self.assertLessEqual(len(lines), 8)
        self.assertIn("q quit", lines[-1])
        self.assertIn("more", lines[-1])

    def test_the_window_follows_the_cursor(self):
        body_lines = picker.render(rows(12), 11, 90, 8, color=False)
        self.assertTrue(any("acct11" in line for line in body_lines))
        self.assertFalse(any("acct0 " in line for line in body_lines))

    def test_the_best_account_is_starred(self):
        reports = rows(3)
        lines = picker.render(reports, 0, 100, 24, best_name="acct0", color=False)
        starred = [line for line in lines if "★" in line]
        self.assertEqual(len(starred), 1)
        self.assertIn("acct0", starred[0])

    def test_a_selected_row_stays_reverse_video_across_its_whole_width(self):
        """An inner colour reset would end the highlight partway along."""
        lines = picker.render(rows(3), 1, 100, 24, color=True)
        selected = [line for line in lines if picker.REVERSE in line]
        self.assertEqual(len(selected), 1)
        highlighted = selected[0].split(picker.REVERSE, 1)[1]
        self.assertEqual(highlighted.count(picker.RESET), 1)
        self.assertTrue(highlighted.endswith(picker.RESET + "│"))

    def test_an_empty_registry_still_draws_a_frame(self):
        lines = picker.render([], 0, 80, 24, color=False)
        self.assertEqual([len(line) for line in lines], [80, 80, 80])
        self.assertIn("no accounts", lines[1])

    def test_a_stale_row_says_how_stale(self):
        report = usage.parse_utilization(body(), {"name": "s", "tool": "claude"},
                                         source="cache", as_of=NOW - 2 * HOUR, now=NOW)
        report["error"] = "unreachable"
        text = "\n".join(picker.render([report], 0, 120, 24, color=False))
        self.assertIn("last reading 2h 00m ago", text)
        self.assertIn("unreachable", text)


class TestKeys(unittest.TestCase):
    def test_a_burst_is_split_into_single_keys(self):
        keys = picker._Keys(0)
        keys.pending = "\x1b[Bjq\x1b[A\x1bOB\x1b"
        got = []
        while keys.pending:
            got.append(keys._pop())
        self.assertEqual(got, ["\x1b[B", "j", "q", "\x1b[A", "\x1bOB", "\x1b"])

    def test_arrows_are_recognised_in_both_encodings(self):
        for key in ("\x1b[A", "\x1bOA"):
            self.assertIn(key, picker.KEYS_UP)
        for key in ("\x1b[B", "\x1bOB"):
            self.assertIn(key, picker.KEYS_DOWN)

    def test_the_picker_refuses_a_pipe_rather_than_drawing_into_it(self):
        with self.assertRaises(RuntimeError) as caught:
            picker.run(lambda: rows())
        self.assertIn("terminal", str(caught.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
