"""Tests for Role 3: sdn_client, the server's SDN hook, bot recorder, metrics, experiment helpers, plots."""
import contextlib
import csv
import importlib.util
import io
import json
import math
import os
import stat
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace

from analysis import metrics as M
from analysis import synthetic
from app import protocol as P
from app.bot import Recorder
from app.sdn_client import SdnClient, SdnError
from tools.fake_controller import FakeController

def slurp(path, mode="r"):
    with open(path, mode, newline="") as f:
        return f.read()


def spit(path, text):
    with open(path, "w") as f:
        f.write(text)


def rows_of(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, "scripts", name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_runner():
    return load_script("run_experiment")


class SdnClientTests(unittest.TestCase):
    def setUp(self):
        self.fc = FakeController().start()
        self.tmp = tempfile.mkdtemp()
        self.log = os.path.join(self.tmp, "calls.csv")
        self.sdn = SdnClient(self.fc.url, log_path=self.log, label="t", retry_delay=0.01)

    def tearDown(self):
        self.sdn.close()
        self.fc.stop()

    def test_register_and_unregister_reach_the_controller(self):
        self.sdn.register("10.0.0.2", 4000)
        self.assertTrue(self.sdn.flush())
        self.assertEqual(self.fc.calls("/game/register"), [{"ip": "10.0.0.2", "port": 4000}])
        self.sdn.unregister("10.0.0.2")
        self.sdn.flush()
        self.assertEqual(self.fc.calls("/game/unregister"), [{"ip": "10.0.0.2"}])
        self.assertEqual(self.fc.status()["players"], [])

    def test_players_sharing_an_ip_are_reference_counted(self):
        self.sdn.register("10.0.0.2", 1)
        self.sdn.register("10.0.0.2", 2)
        self.sdn.unregister("10.0.0.2")
        self.sdn.flush()
        self.assertEqual(len(self.fc.calls("/game/register")), 1)       # told once
        self.assertEqual(self.fc.calls("/game/unregister"), [])         # one player is still there
        self.sdn.unregister("10.0.0.2")
        self.sdn.flush()
        self.assertEqual(len(self.fc.calls("/game/unregister")), 1)     # last one gone
        self.sdn.unregister("10.0.0.2")                                 # unknown now: ignored
        self.sdn.flush()
        self.assertEqual(len(self.fc.calls("/game/unregister")), 1)

    def test_unregister_all_and_mode_and_status(self):
        self.sdn.register("10.0.0.2")
        self.sdn.register("10.0.0.3")
        self.sdn.unregister_all()
        self.sdn.flush()
        self.assertEqual(self.fc.status()["players"], [])
        self.assertEqual(self.sdn.set_mode("static")["mode"], "static")
        self.assertTrue(self.sdn.get_status()["priority_on"])
        with self.assertRaises(ValueError):
            self.sdn.set_mode("turbo")

    def test_retries_after_a_failure_and_logs_every_attempt(self):
        self.fc.fail_next = 2
        self.sdn.register("10.0.0.9")
        self.sdn.flush()
        self.assertEqual(self.sdn.counters["registered"], 1)
        rows = rows_of(self.log)
        self.assertEqual([r["ok"] for r in rows], ["0", "0", "1"])
        self.assertEqual([r["attempt"] for r in rows], ["1", "2", "3"])

    def test_unreachable_controller_never_raises_or_blocks_the_caller(self):
        sdn = SdnClient("http://127.0.0.1:1", timeout=0.2, retries=2, retry_delay=0.01)
        t0 = time.time()
        sdn.register("10.0.0.2")                                        # returns at once
        self.assertLess(time.time() - t0, 0.1)
        sdn.flush(5)
        self.assertEqual(sdn.counters["failed"], 1)
        self.assertFalse(sdn.reachable())
        with self.assertRaises(SdnError):
            sdn.get_status()
        sdn.close()


class ServerHookTests(unittest.TestCase):
    def test_join_leave_and_shutdown_are_announced(self):
        from tests.test_server import Env, Peer
        calls = []
        sdn = SimpleNamespace(register=lambda ip, port: calls.append(("reg", ip, port)),
                              unregister=lambda ip: calls.append(("unreg", ip)),
                              unregister_all=lambda: calls.append(("all",)), close=lambda: calls.append(("close",)))
        env = Env(sdn=sdn)
        a, b = Peer(env, 4001, "a"), Peer(env, 4002, "b")
        a.join()
        b.join()
        self.assertEqual(calls, [("reg", a.addr[0], 4001), ("reg", b.addr[0], 4002)])
        a.raw(P.MSG_LEAVE)
        self.assertIn(("unreg", a.addr[0]), calls)
        env.server.close()
        self.assertEqual(calls[-2:], [("all",), ("close",)])

    def test_a_broken_controller_does_not_break_the_game(self):
        from tests.test_server import Env, Peer

        def boom(*a):
            raise RuntimeError("controller down")
        env = Env(sdn=SimpleNamespace(register=boom, unregister=boom, unregister_all=boom, close=boom))
        p = Peer(env, 4003, "p")
        self.assertIsNotNone(p.join())                                  # still joined
        env.server.close()


class FakeClient:
    def __init__(self, name):
        self.name, self.rtt_ms, self.game_state, self.state = name, [], 2, "joined"
        self.jitter = SimpleNamespace(value=1.5)
        self.tracker = SimpleNamespace(expected=10, lost=1)

    def updates_per_s(self):
        return 19.0


class RecorderTests(unittest.TestCase):
    def test_writes_new_rtt_samples_once_and_timeline_rows_at_the_interval(self):
        d = tempfile.mkdtemp()
        now = [1000.0]
        rec = Recorder(os.path.join(d, "t.csv"), os.path.join(d, "r.csv"), label="x", interval=1.0, clock=lambda: now[0])
        c = FakeClient("h2-1")
        rec([c])                                    # first call: one timeline row, no samples
        c.rtt_ms += [5.0, 6.0]
        now[0] += 0.5
        rec([c])                                    # samples written, no new timeline row yet
        rec([c])                                    # nothing new: no duplicates
        now[0] += 0.6
        rec([c])                                    # interval passed: second timeline row
        rec.close()
        rtt = rows_of(os.path.join(d, "r.csv"))
        tl = rows_of(os.path.join(d, "t.csv"))
        self.assertEqual([r["rtt_ms"] for r in rtt], ["5.000", "6.000"])
        self.assertEqual(len(tl), 2)
        self.assertEqual(tl[1]["rtt_ms"], "6.000")
        self.assertEqual(tl[0]["snap_per_s"], "19.0")


class MetricsTests(unittest.TestCase):
    def test_percentile_matches_linear_interpolation(self):
        self.assertAlmostEqual(M.percentile([1, 2, 3, 4], 50), 2.5)
        self.assertAlmostEqual(M.percentile(range(1, 101), 95), 95.05)
        self.assertTrue(math.isnan(M.percentile([], 95)))
        self.assertEqual(M.percentile([7], 95), 7)

    def test_iperf_parser_reads_intervals_units_and_skips_the_summary(self):
        text = ("[  3]  0.0- 1.0 sec  1.19 MBytes  9.99 Mbits/sec   0.112 ms  200/  851 (23%)\n"
                "[  3]  1.0- 2.0 sec   120 KBytes   980 Kbits/sec   0.300 ms    0/   85 (0%)\n"
                "[  3]  0.0-30.0 sec  35.0 MBytes  9.80 Mbits/sec   0.100 ms  900/ 9000 (10%)\n")
        rows = M.parse_iperf_sink(text)
        self.assertEqual(len(rows), 2)
        self.assertAlmostEqual(rows[0]["mbps"], 9.99)
        self.assertAlmostEqual(rows[1]["mbps"], 0.98)
        self.assertEqual((rows[0]["lost"], rows[0]["total"]), (200, 851))

    def test_window_loss_uses_growth_inside_the_window(self):
        rows = [{"bot": "a", "rx_expected": "100", "rx_lost": "10"}, {"bot": "a", "rx_expected": "200", "rx_lost": "30"},
                {"bot": "b", "rx_expected": "0", "rx_lost": "0"}, {"bot": "b", "rx_expected": "100", "rx_lost": "0"}]
        self.assertAlmostEqual(M._window_loss(rows, "bot", "rx_expected", "rx_lost"), 100.0 * 20 / 200)

    def test_synthetic_matrix_summarises_with_the_expected_shape(self):
        d = tempfile.mkdtemp()
        runs = synthetic.make_matrix(d, players=(4,), reps=1)
        rows = {r["case"]: r for r in M.write_summary(runs, os.path.join(d, "s.csv"))}
        self.assertEqual(len(rows), 4)
        self.assertLess(rows["baseline"]["rtt_p95_ms"], 10)
        self.assertGreater(rows["congested_nosdn"]["rtt_mean_ms"], 100)
        self.assertGreater(rows["congested_nosdn"]["loss_down_pct"], 50)
        self.assertLess(rows["congested_static"]["rtt_p95_ms"], 10)
        self.assertAlmostEqual(rows["congested_sdn"]["sdn_reaction_s"], 2.4, places=1)
        self.assertEqual(rows["baseline"]["sdn_reaction_s"], "")        # only dynamic runs have one
        self.assertGreater(rows["congested_sdn"]["iperf_got_mbps"], 9)
        self.assertEqual(rows["congested_sdn"]["sdn_registrations"], 4)

    def test_missing_files_give_an_invalid_run_not_a_crash(self):
        d = tempfile.mkdtemp()
        spit(os.path.join(d, "meta.json"), json.dumps({"case": "baseline", "players": 4}))
        self.assertEqual(M.summarize_run(d)["valid"], 0)


class RunnerHelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.R = load_runner()

    def test_split_bots_spreads_players_evenly(self):
        self.assertEqual(self.R.split_bots(12, 12), [1] * 12)
        self.assertEqual(self.R.split_bots(8, 3), [3, 3, 2])
        self.assertEqual(sum(self.R.split_bots(13, 5)), 13)

    def test_tc_and_flow_parsers(self):
        tc = ("class htb 1:1 parent 1:fffe leaf 11: prio 1 rate 1Mbit ceil 10Mbit\n Sent 5000 bytes 50 pkt (dropped 7, overlimits 0 requeues 0)\n"
              "class htb 1:2 parent 1:fffe leaf 12: prio 0 rate 6Mbit ceil 10Mbit\n Sent 90000 bytes 900 pkt (dropped 0, overlimits 0 requeues 0)\n")
        cls = self.R.parse_tc_classes(tc)
        self.assertEqual(cls["1:2"]["pkts"], 900)
        self.assertEqual(cls["1:1"]["dropped"], 7)
        fl = self.R.parse_flows(" cookie=0x6a3e, table=0, n_packets=120, priority=100,udp actions=set_queue:1,goto_table:1\n"
                                " table=0, n_packets=5, priority=0 actions=goto_table:1\n")
        self.assertEqual(fl, {"priority_flows": 1, "game_flow_pkts": 120})

    def test_evaluate_verify_judges_each_mode(self):
        ev = self.R.evaluate_verify
        self.assertTrue(ev("off", True, {"game_queue_pkts": 3})["ok"])
        self.assertFalse(ev("off", True, {"game_queue_pkts": 5000})["ok"])
        self.assertTrue(ev("static", True, {"game_queue_pkts": 900, "priority_flows_race": 8})["ok"])
        self.assertFalse(ev("static", True, {"game_queue_pkts": 0})["ok"])
        self.assertTrue(ev("dynamic", True, {"game_queue_pkts": 900})["ok"])
        self.assertFalse(ev("dynamic", True, {"game_queue_pkts": 0})["ok"])
        self.assertTrue(ev("dynamic", False, {"game_queue_pkts": 0, "priority_flows_race": 0})["ok"])
        self.assertFalse(ev("dynamic", False, {"game_queue_pkts": 0, "priority_flows_race": 4})["ok"])
        self.assertFalse(ev("dynamic", False, {"game_queue_pkts": 5000})["ok"])
        self.assertIsNone(ev("static", True, {})["ok"])

    def test_no_rules_left_after_the_race_is_not_a_failure(self):
        # When the race is over the players have left and the controller has removed their rules, so the flow table
        # read afterwards is empty in every mode.  The packet counters of the game queue are the evidence.
        ev = self.R.evaluate_verify
        for mode in ("static", "dynamic"):
            v = ev(mode, True, {"game_queue_pkts": 3200, "priority_flows": 0, "game_flow_pkts": 0})
            self.assertTrue(v["ok"], (mode, v["reasons"]))

    def test_slice_csv_keeps_only_the_window(self):
        d = tempfile.mkdtemp()
        src, dst = os.path.join(d, "a.csv"), os.path.join(d, "out", "b.csv")
        spit(src, "time,event\n1,a\n5,b\n9,c\n")
        self.R.slice_csv(src, dst, 4, 8)
        self.assertEqual([r["event"] for r in M.read_csv(dst)], ["b"])

    def test_plan_rejects_bad_input_and_expands_the_matrix(self):
        args = self.R.build_parser().parse_args(["--players", "4,8", "--reps", "2"])
        self.assertEqual(len(self.R.plan(args)), 4 * 2 * 2)
        with self.assertRaises(SystemExit):
            self.R.plan(self.R.build_parser().parse_args(["--cases", "nope"]))


class DemoScriptTests(unittest.TestCase):
    def test_results_table_is_printed_for_every_player_count(self):
        demo = load_script("demo")
        d = tempfile.mkdtemp()
        rows = M.write_summary(synthetic.make_matrix(d, players=(4, 8), reps=1), os.path.join(d, "s.csv"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            demo.print_results(rows)
        text = out.getvalue()
        for part in ("4 players", "8 players", "Congested, no SDN (baseline)", "Congested, dynamic SDN", "RTT p95"):
            self.assertIn(part, text)


    def demo_in_temp_folder(self):
        """The demo module with its results folder moved to a temp folder and the measurements replaced by a stub."""
        demo = load_script("demo")
        tmp = tempfile.mkdtemp()
        demo.RESULTS, demo.RUNS, demo.PLOTS = tmp, os.path.join(tmp, "runs"), os.path.join(tmp, "plots")
        demo.LIVE = os.path.join(tmp, "live")
        calls = []
        demo.run_measurements = lambda players, cases, short, **kw: calls.append((players, cases, short, kw)) or 2
        return demo, calls

    def test_prepare_hands_all_four_cases_resume_and_repetitions_to_the_runner(self):
        demo, calls = self.demo_in_temp_folder()
        with contextlib.redirect_stdout(io.StringIO()):
            rc = demo.main(["--prepare", "--resume", "--reps", "3", "--runner-args", "--iperf-rate 30"])
        self.assertEqual(rc, 2)                                         # the stub says "machine not ready": stop there
        self.assertEqual(calls, [("2,4,8", "baseline,congested_nosdn,congested_static,congested_sdn", False,
                                  {"resume": True, "reps": 3, "extra": ["--iperf-rate", "30"]})])
        self.assertFalse(os.path.exists(os.path.join(demo.RESULTS, "comparison.csv")))   # stopped before any output

    def test_a_live_run_goes_to_its_own_folder_and_is_reported_as_a_note(self):
        demo, calls = self.demo_in_temp_folder()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = demo.main(["--live", "--no-tests"])
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [("4", "congested_sdn", True, {"out": demo.LIVE, "extra": []})])
        self.assertIn("The live run could not start", out.getvalue())
        self.assertTrue(os.path.exists(os.path.join(demo.RESULTS, "comparison.csv")))
        os.makedirs(demo.LIVE)
        spit(os.path.join(demo.LIVE, "summary.csv"),
             "run,players,valid,rtt_mean_ms,rtt_p95_ms,loss_down_pct,updates_per_s,sdn_reaction_s,verify_ok\n"
             "congested_sdn_n4_r1,4,1,7.7,8.0,2.7,19.5,2.1,1\n")
        note = demo.live_note()
        for part in ("4 players", "RTT mean 7.7 ms", "p95 8.0 ms", "reaction 2.1 s", "SDN check ok", "results/live"):
            self.assertIn(part, note)
        spit(os.path.join(demo.LIVE, "summary.csv"), "run,players,valid\ncongested_sdn_n4_r1,4,0\n")
        self.assertIn("The live run failed", demo.live_note())

    def test_every_demo_test_case_names_a_real_test(self):
        demo = load_script("demo")
        ids = [t[0] for t in demo.TEST_CASES]
        self.assertEqual(len(ids), len(set(ids)))
        for tc_id, _, _, name in demo.TEST_CASES:
            self.assertGreater(unittest.defaultTestLoader.loadTestsFromName(name).countTestCases(), 0, tc_id)

    def test_key_numbers_pick_the_comparison(self):
        demo = load_script("demo")
        rows = [{"case": c, "players": "8", "valid": "1", "rtt_p95_ms": v, "verify_ok": "1"}
                for c, v in (("congested_nosdn", "160"), ("congested_sdn", "20"), ("baseline", "6"))]
        rows.append({"case": "baseline", "players": "4", "valid": "1", "rtt_p95_ms": "5"})
        n, table = demo.key_numbers(rows)
        self.assertEqual(n, 8)
        self.assertEqual([t[1] for t in table], ["baseline", "congested_nosdn", "congested_sdn"])
        self.assertEqual(table[1][3], 160.0)
        self.assertEqual(table[2][9], "ok")


RULES_IN_RACE = (
    " cookie=0x6a3e, duration=11.2s, table=0, n_packets=412, n_bytes=17304, priority=100,udp,nw_src=10.0.0.2,tp_dst=5005"
    " actions=set_queue:1,goto_table:1\n"
    " cookie=0x6a3e, duration=11.2s, table=0, n_packets=260, n_bytes=38220, priority=100,udp,nw_dst=10.0.0.2,tp_src=5005"
    " actions=set_queue:1,goto_table:1\n")
RULES_ALWAYS = (" cookie=0x0, duration=40.1s, table=0, n_packets=9000, n_bytes=990000, priority=0 actions=goto_table:1\n"
                " cookie=0x0, duration=40.1s, table=1, n_packets=12, n_bytes=900, priority=0 actions=CONTROLLER:65535\n")
TC_CLASSES = ("class htb 1:1 parent 1:fffe leaf 11: prio 1 rate 1Mbit ceil 10Mbit burst 1563b cburst 1564b\n"
              " Sent 36000000 bytes 24000 pkt (dropped 9000, overlimits 0 requeues 0)\n"
              "class htb 1:2 parent 1:fffe leaf 12: prio 0 rate 6Mbit ceil 10Mbit burst 1563b cburst 1564b\n"
              " Sent 600000 bytes 3200 pkt (dropped 0, overlimits 0 requeues 0)\n")


class SwitchEvidenceTests(unittest.TestCase):
    """MininetBackend.sample_flows / verify against stand-ins for ovs-ofctl, ovs-vsctl and tc (no Mininet needed)."""

    def setUp(self):
        self.R = load_runner()
        self.bin, self.run = tempfile.mkdtemp(), tempfile.mkdtemp()
        self.old_path = os.environ["PATH"]
        os.environ["PATH"] = self.bin + os.pathsep + self.old_path
        self.command("tc", "cat <<'END'\n%sEND" % TC_CLASSES)
        self.command("ovs-vsctl", "true")
        self.backend = self.R.MininetBackend(SimpleNamespace())

    def tearDown(self):
        os.environ["PATH"] = self.old_path

    def command(self, name, body):
        path = os.path.join(self.bin, name)
        spit(path, "#!/bin/sh\n%s\n" % body)
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)

    def flow_table(self, text):
        self.command("ovs-ofctl", "cat <<'END'\n%sEND" % text)

    def test_a_static_run_passes_although_the_rules_are_gone_after_the_race(self):
        self.flow_table(RULES_IN_RACE + RULES_ALWAYS)                   # mid-race: the players are registered
        race = self.backend.sample_flows(self.run)
        self.assertEqual((race["priority_flows"], race["game_flow_pkts"]), (2, 672))
        self.assertIn("set_queue:1", slurp(os.path.join(self.run, "flows_in_race.txt")))
        self.flow_table(RULES_ALWAYS)                                   # after the race: the players have left
        v = self.backend.verify(self.run, "static", True, race)
        self.assertEqual(v["priority_flows"], 0)
        self.assertEqual((v["priority_flows_race"], v["game_flow_pkts_race"]), (2, 672))
        self.assertEqual((v["game_queue_pkts"], v["other_queue_pkts"], v["other_queue_dropped"]), (3200, 24000, 9000))
        self.assertTrue(v["ok"], v["reasons"])
        self.assertIn("class htb 1:2", slurp(os.path.join(self.run, "verify.txt")))

    def test_without_priority_the_game_queue_must_stay_empty(self):
        self.flow_table(RULES_ALWAYS)
        v = self.backend.verify(self.run, "off", True, self.backend.sample_flows(self.run))
        self.assertEqual(v["priority_flows_race"], 0)
        self.assertFalse(v["ok"])                                       # 3200 packets in queue 1 with the mode off
        self.assertIn("mode off", v["reasons"][0])

    def test_a_failing_or_missing_ovs_ofctl_gives_an_error_entry_and_never_raises(self):
        self.command("ovs-ofctl", "echo 'ovs-ofctl: s1 is not a bridge or a socket' >&2; exit 1")
        race = self.backend.sample_flows(self.run)
        self.assertIn("not a bridge", race["error"])
        self.assertFalse(os.path.exists(os.path.join(self.run, "flows_in_race.txt")))
        v = self.backend.verify(self.run, "dynamic", True, race)        # the verdict still comes from the counters
        self.assertNotIn("priority_flows_race", v)
        self.assertTrue(v["ok"])
        os.remove(os.path.join(self.bin, "ovs-ofctl"))
        os.environ["PATH"] = self.bin                                   # no ovs-ofctl anywhere
        self.assertIn("error", self.backend.sample_flows(self.run))


class ExportResultsTests(unittest.TestCase):
    def test_csv_files_and_workbook_from_a_run_matrix(self):
        from analysis import export_results as X
        d = tempfile.mkdtemp()
        runs = synthetic.make_matrix(d, players=(4, 8), reps=2)
        M.write_summary(runs, os.path.join(d, "summary.csv"))
        tests = [("TC01", "UDP protocol", "header", "PASS", 0.1, ""), ("TC02", "Socket", "round trip", "FAIL", 1.0, "x")]
        res = X.export(d, tests, quiet=True)
        comp = rows_of(os.path.join(d, "comparison.csv"))
        self.assertEqual(len(comp), 8)                                   # 4 cases x 2 player counts
        self.assertEqual(comp[0]["Case key"], "baseline")
        self.assertEqual(comp[0]["Repetitions"], "2")
        self.assertEqual(comp[0]["SDN check"], "ok")
        self.assertEqual([t[3] for t in X.read_test_cases_csv(os.path.join(d, "test_cases.csv"))], ["PASS", "FAIL"])
        if importlib.util.find_spec("openpyxl") is None:
            self.assertIsNone(res["xlsx"])
            self.assertIn("openpyxl", res["warning"])
            return
        from openpyxl import load_workbook
        wb = load_workbook(res["xlsx"])
        self.assertEqual(wb.sheetnames[:4], ["Comparison", "SDN vs No SDN", "Test cases", "All runs"])
        self.assertIn("SYNTHETIC", wb["Comparison"]["A3"].value)       # fake data is labelled in the file
        self.assertEqual(wb["Comparison"].cell(row=5, column=1).value, "No congestion")
        self.assertTrue(str(wb["SDN vs No SDN"]["D5"].value).startswith("=IF(ISNUMBER(Comparison!"))
        self.assertEqual(wb["All runs"].max_row, 4 + 16)                 # 16 runs

    def test_no_runs_still_writes_an_empty_comparison(self):
        from analysis import export_results as X
        d = tempfile.mkdtemp()
        spit(os.path.join(d, "summary.csv"), ",".join(M.SUMMARY_COLUMNS) + "\n")
        X.export(d, None, quiet=True)
        self.assertEqual(rows_of(os.path.join(d, "comparison.csv")), [])


class LocalRunTests(unittest.TestCase):
    """A real (short) run of the whole pipeline on the loopback: server, bots, fake controller, metrics."""

    def test_one_local_run_produces_every_file_and_valid_metrics(self):
        R = load_runner()

        class PretendSwitch(R.LocalBackend):                            # the loopback run, plus canned switch answers
            def sample_flows(self, run_dir):
                return {"priority_flows": 4, "game_flow_pkts": 1200}

            def verify(self, run_dir, mode, congestion, race=None):
                v = {"priority_flows": 0, "game_queue_pkts": 5000, "other_queue_pkts": 9000}
                if race:
                    v["priority_flows_race"] = race["priority_flows"]
                return R.evaluate_verify(mode, congestion, v)

        out = tempfile.mkdtemp()
        args = R.build_parser().parse_args(["--backend", "local", "--out", out, "--laps", "1", "--race-limit", "12",
                                            "--max-seconds", "40", "--iperf-start", "3", "--flow-sample-after", "4"])
        s = R.run_case("congested_static", 2, 1, args, backend_factory=PretendSwitch)
        run = os.path.join(out, "runs", "congested_static_n2_r1")
        for f in ("meta.json", "server_stats.csv", "server_results.csv", "server_events.csv", "sdn_calls.csv",
                  "h2_bots.csv", "h2_timeline.csv", "h2_rtt.csv", "h3_rtt.csv"):
            self.assertTrue(os.path.exists(os.path.join(run, f)), f)
        meta = json.loads(slurp(os.path.join(run, "meta.json")))
        self.assertTrue(meta["finished"])
        self.assertEqual(s["valid"], 1)
        self.assertEqual(s["sdn_registrations"], 1)                     # both bots share 127.0.0.1: one registration
        self.assertGreater(s["updates_per_s"], 15)
        self.assertEqual(meta["flows_in_race"]["priority_flows"], 4)    # sampled while the race was running ...
        self.assertEqual(meta["verify"]["priority_flows_race"], 4)      # ... and kept with the evidence
        self.assertTrue(meta["verify"]["ok"])
        self.assertEqual((s["priority_flows_race"], s["priority_flows"], s["verify_ok"]), (4, 0, 1))


@unittest.skipUnless(importlib.util.find_spec("matplotlib"), "matplotlib not installed")
class PlotTests(unittest.TestCase):
    def test_demo_figures_and_table_are_written(self):
        from analysis import plot
        d = tempfile.mkdtemp()
        runs = synthetic.make_matrix(d, players=(4, 8), reps=2)
        out = os.path.join(d, "plots")
        rc = plot.main(["--runs", runs, "--summary", os.path.join(d, "s.csv"), "--out", out,
                        "--table", os.path.join(d, "t.md")])
        self.assertEqual(rc, 0)
        for f in ("fig_latency.png", "fig_loss.png", "fig_jitter.png", "fig_updates.png", "fig_timeline_n4.png",
                  "fig_cdf_n8.png"):
            self.assertGreater(os.path.getsize(os.path.join(out, f)), 5000, f)
        table = slurp(os.path.join(d, "t.md"))
        self.assertIn("Dynamic SDN vs no SDN", table)
        self.assertIn("Dynamic vs static priority", table)
        self.assertIn("Priority flows on s1 during the race", table)
        self.assertIn("| Congested, no SDN (baseline) | 4 | 0 | 1000 | 0 | ok |", table)
        self.assertIn("| Congested, static priority | 4 | 36000 | 1000 | 8 | ok |", table)
        self.assertIn("| Congested, dynamic SDN | 8 | 72000 | 1000 | 16 | ok |", table)


if __name__ == "__main__":
    unittest.main()
