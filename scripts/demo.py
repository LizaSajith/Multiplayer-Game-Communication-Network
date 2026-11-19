#!/usr/bin/env python3
"""demo.py - test cases and results in one command (Role 3).

    sudo python3 scripts/demo.py --prepare            # once, on the Mininet VM: real measurements (about 15 minutes)
    python3 scripts/demo.py                           # test cases + results table + Excel / CSV (about 15 s, no root)
    python3 scripts/demo.py --excel                   # the same, then opens the Excel file
    sudo python3 scripts/demo.py --prepare --resume   # keep the finished runs, measure only the missing ones
    sudo python3 scripts/demo.py --live               # plus one congested SDN race measured on the spot

--prepare measures 2, 4 and 8 players in four cases: no congestion, congested without SDN, congested with static
priority, congested with dynamic SDN.  Without --resume it measures all of them again and replaces the old runs.
--live never touches those runs: its race is stored in results/live.

What the run does:

    1. runs the named test cases (TC01 ... TC23) with Python's unittest and prints PASS / FAIL for each
    2. summarises the measured runs in results/runs and prints the comparison table
    3. saves the results: results/project17_results.xlsx and results/comparison.csv, test_cases.csv, summary.csv

Graphs are a separate command: python3 analysis/plot.py (PNG files in results/plots).
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import math
import os
import shlex
import subprocess
import sys
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

RESULTS = os.path.join(REPO, "results")
RUNS = os.path.join(RESULTS, "runs")
PLOTS = os.path.join(RESULTS, "plots")
LIVE = os.path.join(RESULTS, "live")                    # the --live race: kept apart from the prepared runs
ALL_CASES = "baseline,congested_nosdn,congested_static,congested_sdn"

# (id, rubric area, what is checked, unittest name) - every one runs without root, Mininet or pygame
TEST_CASES = [
    ("TC01", "UDP protocol", "Packet header encodes and decodes correctly", "tests.test_protocol.HeaderTests.test_roundtrip"),
    ("TC02", "UDP protocol", "Malformed / random packets never crash the server", "tests.test_protocol.CodecTests.test_fuzz_decoders_never_crash"),
    ("TC03", "UDP protocol", "Sequence numbers detect lost packets", "tests.test_protocol.SeqTrackerTests.test_loss_counted"),
    ("TC04", "UDP protocol", "Reordered and duplicate packets are detected", "tests.test_protocol.SeqTrackerTests.test_reorder_and_duplicate"),
    ("TC05", "Game server", "JOIN / JOIN_ACK handshake gives a session", "tests.test_server.HandshakeTests.test_join_ack_carries_session_and_config"),
    ("TC06", "Game client", "Lost JOIN is retried until answered", "tests.test_netclient.JoinTests.test_join_is_repeated_until_answered"),
    ("TC07", "Game server", "Full room / race in progress refused", "tests.test_server.HandshakeTests.test_full_room_and_race_in_progress_are_refused"),
    ("TC08", "Game server", "Only the newest INPUT is applied", "tests.test_server.CommandTests.test_input_applies_only_the_newest_packet"),
    ("TC09", "Game server", "20 SNAPSHOTs per second reach every player", "tests.test_server.SnapshotAndEventTests.test_twenty_snapshots_per_second_with_all_players"),
    ("TC10", "Reliability", "Game events re-sent until acknowledged", "tests.test_server.SnapshotAndEventTests.test_events_repeat_until_the_client_acknowledges_them"),
    ("TC11", "Game logic", "Checkpoints in order, no shortcuts", "tests.test_game.GateTests.test_no_shortcuts"),
    ("TC12", "Game logic", "Autopilot players finish a whole race", "tests.test_game.IntegrationTests.test_autopilot_bots_finish_a_race"),
    ("TC13", "Fault handling", "Silent player is timed out, others are told", "tests.test_server.TimeoutAndStatsTests.test_silent_client_times_out_and_the_others_are_told"),
    ("TC14", "Measurement", "RTT measured with PING / PONG", "tests.test_netclient_run.RunningTests.test_ping_and_pong_measure_the_rtt"),
    ("TC15", "Measurement", "Server measures loss, reordering, duplicates", "tests.test_server.TimeoutAndStatsTests.test_loss_reordering_and_duplicates_are_measured"),
    ("TC16", "Measurement", "Server measures jitter (RFC 3550)", "tests.test_server.TimeoutAndStatsTests.test_jitter_is_measured_from_the_ts_field"),
    ("TC17", "Packet loss", "Race survives loss and duplicates", "tests.test_netclient_wire.WireTests.test_race_survives_loss_and_duplicates"),
    ("TC18", "Packet loss", "4 players race over real UDP: 10% loss, reordering", "tests.test_loopback.LoopbackTests.test_four_players_race_over_a_lossy_reordering_link"),
    ("TC19", "Socket", "Real UDP round trip through the operating system", "tests.test_server.ConfigAndSocketTests.test_real_udp_round_trip"),
    ("TC20", "SDN integration", "Player join / leave is reported to the controller", "tests.test_role3.ServerHookTests.test_join_leave_and_shutdown_are_announced"),
    ("TC21", "SDN integration", "REST register / unregister reaches the controller", "tests.test_role3.SdnClientTests.test_register_and_unregister_reach_the_controller"),
    ("TC22", "Fault handling", "Controller down: the game keeps running", "tests.test_role3.ServerHookTests.test_a_broken_controller_does_not_break_the_game"),
    ("TC23", "SDN integration", "Controller calls are retried after a failure", "tests.test_role3.SdnClientTests.test_retries_after_a_failure_and_logs_every_attempt"),
]

GREEN, RED, YELLOW, BOLD, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[1m", "\033[0m"
if not sys.stdout.isatty():
    GREEN = RED = YELLOW = BOLD = RESET = ""


def banner(text):
    print("\n" + BOLD + "=" * 74 + "\n  " + text + "\n" + "=" * 74 + RESET, flush=True)


# ---------------------------------------------------------------------- 1. test cases
def run_test_cases():
    banner("STEP 1 - automated test cases (%d)" % len(TEST_CASES))
    os.chdir(REPO)
    loader = unittest.defaultTestLoader
    rows = []
    t_all = time.time()
    for tc_id, area, what, name in TEST_CASES:
        t0 = time.time()
        try:
            suite = loader.loadTestsFromName(name)
        except Exception as exc:                        # noqa: BLE001 - report, never stop the demo
            rows.append((tc_id, area, what, "FAIL", 0.0, "cannot load: %s" % exc))
            print("  %s  %-58s %sFAIL%s" % (tc_id, what[:58], RED, RESET))
            continue
        result = unittest.TestResult()
        stream = io.StringIO()
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = stream               # tests print a little; keep the table clean
        try:
            suite.run(result)
        finally:
            sys.stdout, sys.stderr = old_out, old_err
        secs = time.time() - t0
        if result.errors or result.failures:
            status = "FAIL"
            detail = (result.errors + result.failures)[0][1].strip().splitlines()[-1][:160]
        elif result.skipped:
            status, detail = "SKIP", result.skipped[0][1][:160]
        else:
            status, detail = "PASS", ""
        colour = {"PASS": GREEN, "FAIL": RED, "SKIP": YELLOW}[status]
        print("  %s  %-15s %-57s %s%s%s %4.1fs" % (tc_id, area, what[:57], colour, status, RESET, secs), flush=True)
        rows.append((tc_id, area, what, status, secs, detail))
    passed = sum(1 for r in rows if r[3] == "PASS")
    failed = sum(1 for r in rows if r[3] == "FAIL")
    print("\n  %sResult: %d passed, %d failed, %d skipped in %.1f s%s"
          % (GREEN if not failed else RED, passed, failed, len(rows) - passed - failed, time.time() - t_all, RESET))
    return rows


# ---------------------------------------------------------------------- 2. measurements
def run_measurements(players, cases, short, resume=False, reps=1, out=None, extra=()):
    """Real Mininet + Ryu runs through scripts/run_experiment.py (needs sudo)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("run_experiment", os.path.join(REPO, "scripts", "run_experiment.py"))
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    argv = ["--players", players, "--cases", cases, "--reps", str(reps), "--laps", "1"]
    if resume:                                         # keep what is measured, add only what is missing
        argv.append("--resume")
    if out:                                            # the live run: its own folder, the prepared runs stay as they are
        argv += ["--out", out]
    if short:                                          # live: a 30-second race is enough to show the reaction
        argv += ["--race-limit", "30", "--max-seconds", "80"]
    rc = runner.main(argv + list(extra))
    give_back_to_user()
    return rc


def give_back_to_user():
    """Files written under sudo belong to root; hand results/ back so the normal user can re-plot later."""
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if uid and gid and os.path.isdir(RESULTS):
        subprocess.run(["chown", "-R", "%s:%s" % (uid, gid), RESULTS], check=False)


def live_note():
    """One sentence about the race that --live just measured (results/live), for the report and the terminal."""
    from analysis import metrics as M
    rows = M.read_csv(os.path.join(LIVE, "summary.csv"))
    row = rows[-1] if rows else None
    if not row or str(row.get("valid")) != "1":
        return "The live run failed - see error.txt and the logs in results/live/runs; the graphs are from the prepared runs."
    check = {"1": "ok", "0": "FAILED"}.get(str(row.get("verify_ok", "")), "n/a")
    return ("Live run at %s (%s players, congested, dynamic SDN, 30 s race): RTT mean %s ms, p95 %s ms, loss %s %%, "
            "%s snapshots/s, controller reaction %s s, SDN check %s. It is stored in results/live and is not part of "
            "the graphs below." % (dt.datetime.now().strftime("%H:%M"), row.get("players", "?"),
                                   row.get("rtt_mean_ms") or "-", row.get("rtt_p95_ms") or "-",
                                   row.get("loss_down_pct") or "-", row.get("updates_per_s") or "-",
                                   row.get("sdn_reaction_s") or "-", check))


def update_summary():
    """results/runs -> results/summary.csv (one row per run).  Graphs are a separate step: analysis/plot.py."""
    banner("STEP 2 - results of the measured runs")
    from analysis import metrics as M
    if not os.path.isdir(RUNS) or not any(
            os.path.exists(os.path.join(RUNS, d, "meta.json")) for d in os.listdir(RUNS)):
        print_results([])
        return []
    rows = M.write_summary(RUNS, os.path.join(RESULTS, "summary.csv"))
    print_results(rows)
    return rows


# ---------------------------------------------------------------------- 3. numbers
def _f(row, key):
    try:
        return float(row.get(key, ""))
    except (TypeError, ValueError):
        return math.nan


def _mean(rows, case, n, key):
    vals = [_f(r, key) for r in rows if r.get("case") == case and str(r.get("players")) == str(n)
            and str(r.get("valid")) == "1"]
    vals = [v for v in vals if not math.isnan(v)]
    return sum(vals) / len(vals) if vals else math.nan


def _fmt(v, d=1, unit=""):
    return "-" if v is None or (isinstance(v, float) and math.isnan(v)) else ("%.*f%s" % (d, v, unit))


def key_numbers(rows):
    """Headline comparison for the player count that has the most cases (prefer 8)."""
    if not rows:
        return None, []
    counts = sorted({int(r["players"]) for r in rows if str(r.get("valid")) == "1"})
    if not counts:
        return None, []
    have = lambda n: {r["case"] for r in rows if str(r.get("players")) == str(n) and str(r.get("valid")) == "1"}
    n = max(counts, key=lambda k: (len(have(k)), k == 8, k))
    labels = [("baseline", "No congestion"), ("congested_nosdn", "Congested, NO SDN"),
              ("congested_static", "Congested, static priority"), ("congested_sdn", "Congested, DYNAMIC SDN")]
    table = []
    for case, label in labels:
        if case in have(n):
            table.append((label, case, _mean(rows, case, n, "rtt_mean_ms"), _mean(rows, case, n, "rtt_p95_ms"),
                          _mean(rows, case, n, "jitter_ms"), _mean(rows, case, n, "loss_down_pct"),
                          _mean(rows, case, n, "updates_per_s"), _mean(rows, case, n, "players_finished"),
                          _mean(rows, case, n, "sdn_reaction_s"), rows_verify(rows, case, n)))
    return n, table


def rows_verify(rows, case, n):
    v = [r.get("verify_ok", "") for r in rows if r.get("case") == case and str(r.get("players")) == str(n)]
    if any(x == "0" for x in v):
        return "FAILED"
    if v and all(x == "1" for x in v):
        return "ok"
    return "-"


def open_file(path):
    """Open a file with the desktop's default program (LibreOffice Calc for .xlsx) - never as root."""
    user = os.environ.get("SUDO_USER")
    cmd = ["xdg-open", path]
    if user and os.geteuid() == 0:
        cmd = ["sudo", "-u", user, "env", "DISPLAY=%s" % os.environ.get("DISPLAY", ":0")] + cmd
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError:
        return False


def print_results(rows):
    """The comparison as a plain table in the terminal: every case for every player count."""
    from analysis.export_results import comparison_rows
    comp = comparison_rows(rows)
    if not comp:
        print("  No measured runs in results/runs yet. Run once on the Mininet VM (about 15 minutes):\n"
              "      sudo python3 scripts/demo.py --prepare")
        return
    head = "  %-30s %9s %9s %8s %8s %8s %9s %9s  %s" % ("Case", "RTT mean", "RTT p95", "Jitter", "Loss",
                                                       "Snap/s", "Finished", "Reaction", "SDN check")
    for n in sorted({r["players"] for r in comp}):
        print("\n  %s%d players%s" % (BOLD, n, RESET))
        print(head)
        print("  " + "-" * (len(head) - 2))
        for r in (x for x in comp if x["players"] == n):
            print("  %-30s %9s %9s %8s %8s %8s %9s %9s  %s" % (
                r["case_label"], _fmt(r["rtt_mean_ms"], 1, " ms"), _fmt(r["rtt_p95_ms"], 1, " ms"),
                _fmt(r["jitter_ms"], 1, " ms"), _fmt(r["loss_down_pct"], 1, " %"), _fmt(r["updates_per_s"], 1),
                "%s/%d" % (_fmt(r["players_finished"], 0), n), _fmt(r["sdn_reaction_s"], 1, " s"), r["sdn_check"]))



# ---------------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(description="Test cases + results table + Excel / CSV files.")
    ap.add_argument("--prepare", action="store_true",
                    help="measure the real Mininet runs first (needs sudo, about 15 minutes for the default set)")
    ap.add_argument("--resume", action="store_true",
                    help="with --prepare: keep the runs that are already in results/runs, measure only the missing ones")
    ap.add_argument("--reps", type=int, default=1,
                    help="with --prepare: repetitions of every case (3 gives error bars in the graphs)")
    ap.add_argument("--live", action="store_true",
                    help="also measure one short congested race with dynamic SDN now (needs sudo, about 1 minute); "
                         "it is stored in results/live and does not change the prepared runs")
    ap.add_argument("--players", default=None, help="player counts for --prepare (default 2,4,8) or --live (default 4)")
    ap.add_argument("--cases", default=ALL_CASES, help="cases for --prepare (default: %s)" % ALL_CASES)
    ap.add_argument("--runner-args", default="", metavar="'OPTIONS'",
                    help="extra options handed to scripts/run_experiment.py, in quotes (for example '--iperf-rate 30')")
    ap.add_argument("--no-tests", action="store_true", help="skip the test cases")
    ap.add_argument("--excel", action="store_true",
                    help="open results/project17_results.xlsx in LibreOffice Calc / Excel at the end")
    args = ap.parse_args(argv)
    if args.reps < 1:
        ap.error("--reps must be at least 1")
    extra = shlex.split(args.runner_args)
    t0 = time.time()
    meas_note = ""

    if args.prepare:
        banner("PREPARE - real measurements in Mininet (leave it running)%s"
               % (": only the runs that are still missing" if args.resume else ""))
        rc = run_measurements(args.players or "2,4,8", args.cases, short=False, resume=args.resume, reps=args.reps,
                              extra=extra)
        if rc not in (0, 1):
            return rc
        if rc == 1:
            meas_note = ("Some prepared runs failed - see error.txt in their folders under results/runs, then run "
                         "sudo python3 scripts/demo.py --prepare --resume to measure only those again.")

    tests = [] if args.no_tests else run_test_cases()

    if args.live:
        banner("LIVE - one congested race with dynamic SDN, measured now (about 1 minute)")
        rc = run_measurements(args.players or "4", "congested_sdn", short=True, out=LIVE, extra=extra)
        note = live_note() if rc in (0, 1) else ("The live run could not start (see the messages in the terminal); "
                                                 "the results below are from the prepared runs.")
        meas_note = (meas_note + " " + note).strip()

    rows = update_summary()
    banner("STEP 3 - results saved as spreadsheets (Excel + CSV)")
    from analysis.export_results import export
    sheets = export(RESULTS, tests)
    give_back_to_user()

    banner("DONE in %.0f s" % (time.time() - t0))
    if meas_note:
        print("  " + meas_note + "\n")
    if sheets["xlsx"]:
        print("  Excel:   %s" % sheets["xlsx"])
    print("  CSV:     %s" % ", ".join(os.path.relpath(p, REPO) for p in sheets["csv"]))
    print("  Graphs:  python3 analysis/plot.py   (PNG files in results/plots)")
    if args.excel and sheets["xlsx"] and open_file(sheets["xlsx"]):
        print("  (opened the Excel file)")
    failed = sum(1 for t in tests if t[3] == "FAIL")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
