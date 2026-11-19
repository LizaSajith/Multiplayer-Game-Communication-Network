"""The bots' results file: write_bot_csv and the --csv / --label options of ``python -m app.netclient``."""
import contextlib
import csv
import io
import os
import tempfile
import unittest

from app import protocol as P
from app.game import GameConfig
from app.netclient import BOT_CSV_COLUMNS, main, write_bot_csv
from app.server import GameServer
from tests.nethelpers import Env, car


def rows(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


class BotCsvTests(unittest.TestCase):
    def finished_client(self):
        env = Env()
        c = env.accept()
        env.snapshot(1, [car(c.player_id, progress=5000.0, status=P.STATUS_FINISHED, place=2, health=65)])
        env.run(1.2)                                            # one PING / PONG round, so there is an RTT sample
        env.pong(0xFFFF)
        env.run(0.1)
        return c

    def test_one_row_per_bot_the_header_once_and_rows_are_appended(self):
        c = self.finished_client()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "results", "bots.csv")      # the folder is created when it is missing
            write_bot_csv(path, "baseline", [c])
            write_bot_csv(path, "congested", [c, c])
            with open(path, encoding="utf-8") as f:
                self.assertEqual(f.readline().strip().split(","), list(BOT_CSV_COLUMNS))
            got = rows(path)
        self.assertEqual([r["label"] for r in got], ["baseline", "congested", "congested"])
        first = got[0]
        self.assertEqual((first["bot"], first["state"], first["finished"], first["place"], first["health"]),
                         ("Tester", "joined", "1", "2", "65"))
        self.assertEqual(float(first["progress_m"]), 500.0)
        self.assertEqual(int(first["snapshots"]), 1)
        self.assertEqual(int(first["rx_packets"]), c.packets_in)
        self.assertEqual(set(first), set(BOT_CSV_COLUMNS))

    def test_a_bot_that_never_got_a_snapshot_still_gets_a_row(self):
        env = Env()
        c = env.accept()                                        # joined, but no snapshot yet: no player state
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "b.csv")
            write_bot_csv(path, "", [c])
            got = rows(path)
        self.assertEqual((got[0]["finished"], got[0]["place"], got[0]["progress_m"]), ("0", "0", "0.0"))


class BotCommandLineTests(unittest.TestCase):
    def test_the_command_line_writes_a_csv_after_a_real_udp_session(self):
        server = GameServer(GameConfig(laps=1, countdown_s=0.5), "127.0.0.1", 0, verbose=False)
        server.start_thread()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "bots.csv")
                with contextlib.redirect_stdout(io.StringIO()):          # the bots print a summary line each
                    code = main(["--server", "127.0.0.1:%d" % server.address[1], "--bots", "2", "--prefix", "t-",
                                 "--start", "--duration", "3.2", "--linger", "0", "--csv", path, "--label", "unit"])
                got = rows(path)
        finally:
            server.stop()
            server.close()
        self.assertEqual(code, 0)
        self.assertEqual([r["bot"] for r in got], ["t-1", "t-2"])
        for r in got:
            self.assertEqual(r["label"], "unit")
            self.assertGreater(int(r["snapshots"]), 30)               # about 20 a second for about 3 s
            self.assertTrue(15.0 < float(r["snapshot_hz"]) < 25.0, r["snapshot_hz"])
            self.assertGreaterEqual(int(r["rtt_n"]), 2)
            self.assertLess(float(r["rx_loss_pct"]), 5.0)

    def test_an_unwritable_csv_path_is_reported_not_raised(self):
        server = GameServer(GameConfig(laps=1, countdown_s=0.5), "127.0.0.1", 0, verbose=False)
        server.start_thread()
        try:
            errors = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(errors):
                code = main(["--server", "127.0.0.1:%d" % server.address[1], "--bots", "1", "--duration", "0.6",
                             "--linger", "0", "--csv", os.path.join(os.devnull, "x", "bots.csv")])
            self.assertIn("cannot write", errors.getvalue())
        finally:
            server.stop()
            server.close()
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
