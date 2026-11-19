"""Unit tests for app/track.py."""
import math
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app import track as T

PLAYER_R = 14.0      # same value game.py uses


class TrackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tr = T.build_track(T.TRACK_CIRCUIT, 17)

    # ---- determinism: client (Windows) and server (Linux/Mininet) must agree
    def test_rng_reference_values(self):
        r = T.Rng(17)
        self.assertEqual([r.next_u32() for _ in range(4)],
                         [1328954402, 2666870555, 987703568, 542481198])

    def test_hash_is_pinned(self):
        # If this fails on another machine/Python the geometry is NOT identical
        # and clients would be refused by the hash check in NetClient.
        self.assertEqual(self.tr.hash, 0xD73F6807)
        self.assertEqual((self.tr.n, len(self.tr.obstacles)), (564, 26))

    def test_two_builds_identical_and_seed_changes_obstacles(self):
        again = T.build_track(T.TRACK_CIRCUIT, 17)
        self.assertEqual(self.tr.hash, again.hash)
        self.assertNotEqual(self.tr.hash, T.build_track(T.TRACK_CIRCUIT, 18).hash)

    def test_unknown_track_id(self):
        with self.assertRaises(ValueError):
            T.build_track(99)

    # ---- geometry
    def test_uniform_spacing_and_closed(self):
        tr = self.tr
        self.assertTrue(all(9.9 < ln < 10.1 for ln in tr.seg_len))
        x0, y0 = tr.pts[-1]
        x1, y1 = tr.pts[0]
        self.assertLess(math.hypot(x1 - x0, y1 - y0), 10.1)       # last segment closes the loop
        self.assertAlmostEqual(sum(tr.seg_len), tr.length, places=6)

    def test_design_limits(self):
        info = T.analyze(self.tr)
        self.assertGreater(info["min_turn_radius"], self.tr.half_width + 40)   # inner edge sane
        self.assertGreater(info["min_separation"], 2 * self.tr.half_width + 100)
        self.assertGreater(info["length_m"], 400)
        self.assertLess(info["length_m"], 800)

    def test_checkpoints(self):
        cps = self.tr.checkpoints
        self.assertEqual(len(cps), 4)
        self.assertEqual(cps, sorted(cps))
        self.assertTrue(all(0 < c < self.tr.length for c in cps))

    def test_point_at_and_locate_roundtrip(self):
        tr = self.tr
        rnd = random.Random(3)
        hint = None
        for k in range(600):
            s = (k * 9.3) % tr.length
            lat = rnd.uniform(-100, 100)
            x, y, hdg = tr.point_at(s, lat)
            loc = tr.locate(x, y, hint)
            hint = loc.seg
            ds = (loc.s - s + tr.length / 2) % tr.length - tr.length / 2
            # A polyline cannot offset sideways exactly on a curve: the error is
            # bounded by lateral * (heading change per 10-unit segment, <= 0.06 rad).
            self.assertLess(abs(ds), 1.0 + abs(lat) * 0.06, "s mismatch at k=%d" % k)
            self.assertLess(abs(loc.lateral - lat), 1.0)

    def test_locate_global_equals_windowed(self):
        tr = self.tr
        for s in range(0, int(tr.length), 97):
            x, y, _ = tr.point_at(s, 40.0)
            g = tr.locate(x, y)
            w = tr.locate(x, y, g.seg)
            self.assertEqual(g.seg, w.seg)

    def test_locate_progress_is_monotone_while_walking_forward(self):
        tr = self.tr
        hint, last, total = None, None, 0.0
        for k in range(int(tr.length / 4) + 50):
            x, y, _ = tr.point_at(k * 4.0, 60.0 * math.sin(k / 20.0))
            loc = tr.locate(x, y, hint)
            hint = loc.seg
            if last is not None:
                d = (loc.s - last + tr.length / 2) % tr.length - tr.length / 2
                self.assertGreater(d, 0.0)
                total += d
            last = loc.s
        self.assertAlmostEqual(total, (int(tr.length / 4) + 49) * 4.0, delta=8.0)

    def test_heading_matches_tangent(self):
        x, y, tx, ty, nx, ny = self.tr.frame_at(1234.5)
        _, _, hdg = self.tr.point_at(1234.5)
        self.assertAlmostEqual(math.cos(hdg), tx, places=6)
        self.assertAlmostEqual(math.sin(hdg), ty, places=6)
        self.assertAlmostEqual(nx, -ty)
        self.assertAlmostEqual(ny, tx)

    # ---- start grid
    def test_grid_slots(self):
        tr = self.tr
        slots = [tr.grid_slot(i) for i in range(32)]
        for x, y, hdg, s, lat in slots:
            self.assertLess(abs(lat), tr.half_width - PLAYER_R)
            self.assertGreater(s, tr.length - 700)
            self.assertLess(s, tr.length)
            loc = tr.locate(x, y)
            self.assertLess(abs(loc.lateral - lat), 1.0)
        for i in range(32):
            for j in range(i + 1, 32):
                d = math.hypot(slots[i][0] - slots[j][0], slots[i][1] - slots[j][1])
                self.assertGreater(d, 2 * PLAYER_R + 2, "grid slots %d and %d overlap" % (i, j))

    # ---- obstacles
    def test_obstacles_sorted_and_clear_of_special_zones(self):
        tr = self.tr
        ss = [o.s for o in tr.obstacles]
        self.assertEqual(ss, sorted(ss))
        self.assertEqual([o.id for o in tr.obstacles], list(range(len(tr.obstacles))))
        for o in tr.obstacles:
            self.assertGreater(o.s, 330)
            self.assertLess(o.s, tr.length - 330)
            for c in tr.checkpoints:
                self.assertGreater(abs(o.s - c), 100, "obstacle blocks checkpoint")

    def test_every_row_is_passable_at_all_times(self):
        tr = self.tr
        hw = tr.half_width
        rows = {}
        for o in tr.obstacles:
            rows.setdefault(round(o.s, 3), []).append(o)
        for s, obs in rows.items():
            for step in range(40):
                t = step * 0.25
                blocked = []
                for o in obs:
                    v = o.lateral(t)
                    blocked.append((v - o.ry - PLAYER_R, v + o.ry + PLAYER_R))
                free = [(-(hw - PLAYER_R), hw - PLAYER_R)]
                for a, b in blocked:
                    nxt = []
                    for lo, hi in free:
                        if b <= lo or a >= hi:
                            nxt.append((lo, hi))
                        else:
                            if a > lo:
                                nxt.append((lo, a))
                            if b < hi:
                                nxt.append((b, hi))
                    free = nxt
                widest = max((hi - lo for lo, hi in free), default=0.0)
                self.assertGreater(widest, 10.0, "row at s=%.0f blocked at t=%.2f" % (s, t))

    def test_obstacles_stay_inside_corridor(self):
        hw = self.tr.half_width
        for o in self.tr.obstacles:
            for step in range(30):
                v = o.lateral(step * 0.3)
                self.assertLessEqual(abs(v) + o.ry, hw + 1e-6)

    def test_obstacle_anchor_frames(self):
        for o in self.tr.obstacles:
            x, y, tx, ty, nx, ny = self.tr.frame_at(o.s)
            self.assertAlmostEqual(o.ax, x)
            self.assertAlmostEqual(o.tx * o.nx + o.ty * o.ny, 0.0, places=9)

    def test_obstacles_near(self):
        tr = self.tr
        o = tr.obstacles[5]
        near = tr.obstacles_near(o.s, 1.0)
        self.assertIn(o, near)
        self.assertTrue(all(abs(p.s - o.s) <= 1.0 for p in near))
        self.assertEqual(tr.obstacles_near(0.0, 50.0), [])


if __name__ == "__main__":
    unittest.main()
