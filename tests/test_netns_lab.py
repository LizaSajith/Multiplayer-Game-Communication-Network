"""tools/netns_lab.py: the parts that need neither Linux nor root (which files belong to a case, the summary)."""
import contextlib
import csv
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

from tools import netns_lab as lab


def write_csv(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        out = csv.writer(f)
        out.writerow(header)
        out.writerows(rows)


def touch(folder, *names):
    for name in names:
        open(os.path.join(folder, name), "w").close()


class CaseFilesTests(unittest.TestCase):
    def test_a_case_owns_only_its_own_files(self):
        with tempfile.TemporaryDirectory() as out:
            touch(out, "congested_stats.csv", "congested_bots_h2.csv", "congested_h2_bots.log",
                  "congested_server.log", "congested_iperf_client.log",
                  "congested_q1000_stats.csv", "congested_q1000_bots_h2.csv", "congested_q1000_server.log",
                  "baseline_stats.csv", "notes.txt", "congested_bots_extra.csv")
            names = sorted(os.path.basename(p) for p in lab.case_files(out, "congested"))
            self.assertEqual(names, ["congested_bots_h2.csv", "congested_h2_bots.log", "congested_iperf_client.log",
                                     "congested_server.log", "congested_stats.csv"])
            names = sorted(os.path.basename(p) for p in lab.case_files(out, "congested_q1000"))
            self.assertEqual(names, ["congested_q1000_bots_h2.csv", "congested_q1000_server.log",
                                     "congested_q1000_stats.csv"])

    def test_the_case_name_is_not_a_pattern(self):
        with tempfile.TemporaryDirectory() as out:
            touch(out, "a.b_stats.csv", "aXb_stats.csv")
            self.assertEqual([os.path.basename(p) for p in lab.case_files(out, "a.b")], ["a.b_stats.csv"])


class SummaryTests(unittest.TestCase):
    BOT = ("label", "bot", "rtt_mean_ms", "rtt_p95_ms", "rtt_max_ms", "jitter_ms", "rx_loss_pct", "snapshot_hz")
    STATS = ("label", "player_id", "rx_loss_pct")
    RESULTS = ("race", "place", "player_id", "finished", "finish_s")

    def folder(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return tmp.name

    def test_the_numbers_of_all_bot_hosts_are_merged(self):
        out = self.folder()
        write_csv(os.path.join(out, "x_bots_h2.csv"), self.BOT,
                  [("x", "h2-1", 10, 20, 30, 1, 2, 20), ("x", "h2-2", 20, 40, 90, 3, 4, 18)])
        write_csv(os.path.join(out, "x_bots_h3.csv"), self.BOT, [("x", "h3-1", 30, 60, 50, 2, 6, 16)])
        write_csv(os.path.join(out, "other_bots_h2.csv"), self.BOT, [("other", "o-1", 999, 999, 999, 99, 99, 1)])
        write_csv(os.path.join(out, "x_stats.csv"), self.STATS,
                  [("x", 1, 1.0), ("x", 2, 5.0), ("x", 1, 3.0)])          # the last row of a player counts
        write_csv(os.path.join(out, "x_results.csv"), self.RESULTS,
                  [(1, 1, 1, 1, 41.5), (1, 2, 2, 1, 44.0), (1, 3, 3, 0, "")])
        s = lab.summarize(out, "x", 3)
        self.assertEqual(s["bots_reporting"], 3)
        self.assertAlmostEqual(s["rtt_mean_ms"], 20.0)
        self.assertAlmostEqual(s["rtt_p95_ms"], 40.0)
        self.assertEqual(s["rtt_max_ms"], 90.0)
        self.assertAlmostEqual(s["jitter_ms"], 2.0)
        self.assertAlmostEqual(s["down_loss_pct"], 4.0)
        self.assertAlmostEqual(s["up_loss_pct"], 4.0)                      # mean of 3.0 and 5.0
        self.assertAlmostEqual(s["snapshot_hz"], 18.0)
        self.assertEqual(s["finished"], 2)
        self.assertEqual(s["race_s"], 44.0)

    def test_a_run_that_produced_nothing_gives_nan_and_zero_finishers(self):
        s = lab.summarize(self.folder(), "x", 4)
        self.assertEqual((s["bots_reporting"], s["finished"], s["race_s"]), (0, 0, 0.0))
        self.assertNotEqual(s["rtt_mean_ms"], s["rtt_mean_ms"])           # NaN

    def test_the_table_has_a_header_and_one_line_per_case(self):
        s = {"case": "baseline", "players": 8, "rtt_mean_ms": 4.7, "rtt_p95_ms": 4.9, "rtt_max_ms": 19.0,
             "jitter_ms": 2.0, "down_loss_pct": 0.0, "up_loss_pct": 0.0, "snapshot_hz": 20.0, "finished": 8}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            lab.print_table([s])
        lines = buf.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("RTT mean", lines[0])
        self.assertIn("baseline", lines[1])
        self.assertIn("8 / 8", lines[1])


class StartupTests(unittest.TestCase):
    def test_it_refuses_politely_without_root(self):
        err = io.StringIO()
        with mock.patch.object(sys, "platform", "linux"), \
                mock.patch.object(os, "geteuid", create=True, return_value=1000), \
                mock.patch.object(lab, "play") as play, \
                contextlib.redirect_stderr(err):
            self.assertEqual(lab.main(["--case", "x"]), 2)
        play.assert_not_called()
        self.assertIn("needs Linux and root", err.getvalue())

    def test_it_refuses_politely_on_windows(self):
        err = io.StringIO()
        with mock.patch.object(sys, "platform", "win32"), \
                mock.patch.object(lab, "play") as play, \
                contextlib.redirect_stderr(err):
            self.assertEqual(lab.main([]), 2)
        play.assert_not_called()


if __name__ == "__main__":
    unittest.main()
