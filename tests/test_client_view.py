"""The pygame-free view helpers of netclient.py: ranking, distance to goal, render time, local-car sampling."""
import unittest

from app import protocol as P
from app.netclient import SnapshotBuffer, ranking, remaining_m
from app.track import UNITS_PER_METER
from tests.nethelpers import TRACK, Env, car


class ViewHelperTests(unittest.TestCase):
    def test_distance_to_goal_in_metres(self):
        length = TRACK.length
        self.assertAlmostEqual(remaining_m(car(1, progress=0.0), 3, length), 3 * length / UNITS_PER_METER)
        self.assertAlmostEqual(remaining_m(car(1, progress=-64.0), 1, length),
                               (length + 64.0) / UNITS_PER_METER)            # still behind the start line
        self.assertEqual(remaining_m(car(1, progress=3 * length), 3, length), 0.0)
        self.assertEqual(remaining_m(car(1, progress=3 * length + 50.0), 3, length), 0.0)

    def test_ranking_follows_the_server_rule(self):
        players = [car(1, progress=500.0), car(2, progress=900.0), car(3, progress=100.0),
                   car(4, progress=5000.0, status=P.STATUS_FINISHED, place=2),
                   car(5, progress=5000.0, status=P.STATUS_FINISHED, place=1)]
        self.assertEqual([p.id for p in ranking(players)], [5, 4, 2, 1, 3])
        tied = [car(7, progress=10.0), car(6, progress=10.0)]
        self.assertEqual([p.id for p in ranking(tied)], [6, 7])

    def test_race_time_runs_on_between_snapshots(self):
        env = Env()
        c = env.accept()
        self.assertEqual(c.race_time_now(), 0.0)
        env.snapshot(60, race_time_ms=10000)
        env.run(0.05)                      # read at the first step: 0.04 s ago; the cars are drawn 0.1 s late
        self.assertAlmostEqual(c.race_time_now(), 10.0 + 0.04 - 0.1, places=6)
        env.run(0.5)
        self.assertAlmostEqual(c.race_time_now(), 10.0 + 0.54 - 0.1, places=6)
        env.snapshot(61, state=P.ST_FINISHED, race_time_ms=12000)
        env.run(0.05)
        self.assertEqual(c.race_time_now(), 12.0)             # frozen once the race is over

    def test_the_local_car_can_be_sampled_without_delay(self):
        buf = SnapshotBuffer(60, 0.1)
        buf.add(P.Snapshot(0, P.ST_RACING, 0, 0, 0, 0, [car(1, 0.0, 0.0, speed=100.0)], []), 10.0)
        buf.add(P.Snapshot(12, P.ST_RACING, 0, 0, 0, 0, [car(1, 100.0, 0.0, speed=100.0)], []), 10.2)
        now = 10.2                                             # the newest snapshot has just arrived
        self.assertAlmostEqual(buf.sample(now)[1].x, 50.0, places=6)               # others: 0.1 s in the past
        self.assertAlmostEqual(buf.sample(now, delay=0.0)[1].x, 100.0, places=6)   # local car: right now


if __name__ == "__main__":
    unittest.main()
