"""app/local.py: a private server with autopilot opponents (what ``python -m app.client --local`` starts).

Real UDP on 127.0.0.1, so it takes a few seconds."""
import time
import unittest

from app import protocol as P
from app.local import LocalGame
from app.netclient import NetClient


class LocalGameTests(unittest.TestCase):
    def wait_for(self, client, condition, seconds=6.0):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            client.pump()
            if condition():
                return True
            time.sleep(0.005)
        return condition()

    def test_opponents_join_after_the_human_ready_up_and_race_when_the_host_starts(self):
        local = LocalGame(laps=1, bots=2, countdown=0.5, join_delay=0.2)
        local.start()
        human = NetClient(local.address, "human", P.SHAPE_CIRCLE)
        try:
            human.start()
            self.assertTrue(human.wait_joined())
            self.assertEqual(human.player_id, 1)                   # first in, so the host
            bots_ready = lambda: len(human.roster) == 3 and all(
                e.ready for e in human.roster.values() if e.player_id != human.player_id)
            self.assertTrue(self.wait_for(human, bots_ready))
            self.assertEqual(sorted(e.name for e in human.roster.values()), ["bot1", "bot2", "human"])
            self.assertTrue(self.wait_for(human, lambda: human.is_host))
            human.send_cmd(P.CMD_START)
            self.assertTrue(self.wait_for(human, lambda: human.game_state == P.ST_RACING))
            moving = lambda: sum(1 for pid, p in human.players.items() if pid != human.player_id and p.speed > 30) == 2
            self.assertTrue(self.wait_for(human, moving, 8.0))     # both opponents are driving
        finally:
            human.leave()
            human.close()
            local.close()
        self.assertFalse(local.server._thread.is_alive())          # everything stopped
        self.assertFalse(local._thread.is_alive())

    def test_bad_settings_are_reported_not_hidden(self):
        with self.assertRaises(ValueError):
            LocalGame(laps=0)

    def test_no_opponents_is_fine(self):
        local = LocalGame(laps=1, bots=0)
        local.start()
        human = NetClient(local.address, "solo", P.SHAPE_TRIANGLE)
        try:
            human.start()
            self.assertTrue(human.wait_joined())
            self.assertTrue(self.wait_for(human, lambda: len(human.roster) == 1))
        finally:
            human.leave()
            human.close()
            local.close()


if __name__ == "__main__":
    unittest.main()
