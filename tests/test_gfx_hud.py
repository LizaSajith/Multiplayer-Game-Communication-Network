"""gfx.py and hud.py: world-to-pixel alignment, camera, what the HUD shows.  Needs pygame (skipped without it).

The drawing runs on pygame's "dummy" video driver, so no window opens."""
import importlib.util
import math
import os
import unittest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

from app import protocol as P      # needs no pygame: used at import time below, so it must not sit under the check

HAVE_PYGAME = importlib.util.find_spec("pygame") is not None
if HAVE_PYGAME:
    import pygame

    from app import gfx, hud
    from tests.nethelpers import TRACK, Env, car, ev


@unittest.skipUnless(HAVE_PYGAME, "pygame is not installed")
class GfxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pygame.init()
        cls.view = gfx.TrackView(TRACK)

    @classmethod
    def tearDownClass(cls):
        pygame.quit()

    def pixel(self, x, y):
        return tuple(self.view.surface.get_at((int(round(x - self.view.ox)), int(round(y - self.view.oy))))[:3])

    def test_the_picture_of_the_road_lines_up_with_the_physics(self):
        """Inside the walls the picture is road, outside it is grass: the drawing uses the same geometry."""
        hw = TRACK.half_width
        gates = [0.0] + list(TRACK.checkpoints)
        checked = 0
        for k in range(60):
            s = (k + 0.5) * TRACK.length / 60.0
            if any(min((s - g) % TRACK.length, (g - s) % TRACK.length) < 60.0 for g in gates):
                continue                                      # the start line and gates are painted on the road
            x, y, tx, ty, nx, ny = TRACK.frame_at(s)
            for side in (-1, 1):
                self.assertEqual(self.pixel(x + nx * 70 * side, y + ny * 70 * side), gfx.ROAD, (s, side))
                self.assertEqual(self.pixel(x + nx * (hw + 50) * side, y + ny * (hw + 50) * side), gfx.GRASS, (s, side))
            checked += 1
        self.assertGreater(checked, 40)

    def test_the_start_line_is_painted_across_the_road(self):
        x, y, tx, ty, nx, ny = TRACK.frame_at(0.0)
        seen = {self.pixel(x + tx * 10 + nx * lat, y + ty * 10 + ny * lat) for lat in range(-90, 91, 10)}
        self.assertIn((240, 240, 240), seen)
        self.assertIn((24, 24, 28), seen)

    def test_minimap_places_points_inside_itself(self):
        mm = gfx.MiniMap(self.view, 250, 150)
        w, h = mm.surface.get_size()
        self.assertLessEqual(w, 250)
        self.assertLessEqual(h, 150)
        for s in range(0, int(TRACK.length), 400):
            x, y = TRACK.frame_at(float(s))[:2]
            mx, my = mm.to_map(x, y)
            self.assertTrue(0 <= mx < w and 0 <= my < h, (s, mx, my))

    def test_camera_follows_and_translates(self):
        cam = gfx.Camera(800, 600)
        cam.follow(100.0, 50.0, 0.016)
        self.assertEqual((cam.x, cam.y), (100.0, 50.0))        # the first call snaps
        self.assertEqual(cam.to_screen(100.0, 50.0), (400, 300))
        self.assertEqual(cam.to_screen(150.0, 40.0), (450, 290))
        cam.follow(200.0, 50.0, 0.1)                           # then it eases toward the target
        self.assertTrue(100.0 < cam.x < 200.0)
        for _ in range(200):
            cam.follow(200.0, 50.0, 0.05)
        self.assertAlmostEqual(cam.x, 200.0, places=3)
        self.assertTrue(cam.sees(200.0 + 400, 50.0))
        self.assertFalse(cam.sees(200.0 + 500, 50.0, margin=60.0))

    def test_a_car_is_drawn_at_its_place_on_the_screen(self):
        from app.netclient import Pose
        screen = pygame.Surface((400, 300))
        screen.fill((0, 0, 0))
        cam = gfx.Camera(400, 300)
        cam.snap(1000.0, 1000.0)
        font = pygame.font.Font(None, 16)
        gfx.draw_player(screen, cam, Pose(1000.0, 1000.0, 0.0, 0.0, 0), shape=P.SHAPE_CIRCLE, color=(250, 10, 10),
                        name="x", health=100, local=False, font=font, t=0.0)
        self.assertEqual(screen.get_at((200, 158))[:3], (250, 10, 10))      # the car body is at the screen centre
        self.assertEqual(screen.get_at((20, 20))[:3], (0, 0, 0))            # nothing elsewhere
        # a car far outside the picture is skipped (no error, nothing drawn)
        screen.fill((0, 0, 0))
        gfx.draw_player(screen, cam, Pose(9000.0, 9000.0, 0.0, 0.0, 0), shape=P.SHAPE_SQUARE, color=(250, 10, 10),
                        name="y", health=100, local=False, font=font, t=0.0)
        self.assertEqual(pygame.transform.average_color(screen)[:3], (0, 0, 0))

    def test_all_three_shapes_are_distinct(self):
        pics = []
        for shape in (P.SHAPE_CIRCLE, P.SHAPE_SQUARE, P.SHAPE_TRIANGLE):
            surf = pygame.Surface((60, 60))
            surf.fill((0, 0, 0))
            gfx.draw_shape(surf, (30, 30), shape, 0.0, 20, (200, 200, 200))
            pics.append(pygame.image.tostring(surf, "RGB"))
        self.assertEqual(len(set(pics)), 3)


@unittest.skipUnless(HAVE_PYGAME, "pygame is not installed")
class HudTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pygame.init()
        cls.fonts = hud.Fonts()

    @classmethod
    def tearDownClass(cls):
        pygame.quit()

    def joined(self, cars, entries=()):
        env = Env()
        c = env.accept(player_id=1, slot=0, laps=3)
        env.roster(1, list(entries))
        env.snapshot(60, cars, roster_ver=1, race_time_ms=65000)
        c.pump()
        return c

    @staticmethod
    def entry(pid, name, shape=P.SHAPE_CIRCLE, host=False):
        return P.RosterEntry(pid, pid - 1, shape, False, host, name)

    def test_small_text_helpers(self):
        self.assertEqual([hud.ordinal(n) for n in (1, 2, 3, 4, 11, 12, 13, 21, 22, 101, 111)],
                         ["1st", "2nd", "3rd", "4th", "11th", "12th", "13th", "21st", "22nd", "101st", "111th"])
        self.assertEqual(hud.fmt_time(0), "0:00.0")
        self.assertEqual(hud.fmt_time(65.04), "1:05.0")
        self.assertEqual(hud.fmt_time(-3), "0:00.0")

    def test_standings_rows_show_name_health_and_distance_to_goal(self):
        L = TRACK.length
        cars = [car(1, progress=500.0, health=80), car(2, progress=900.0, health=55, lap=1),
                car(3, progress=3 * L, status=P.STATUS_FINISHED, place=1, health=100),
                car(4, progress=100.0, status=P.STATUS_WRECKED, health=0),
                car(5, progress=700.0, status=P.STATUS_DNF, health=40)]
        entries = [self.entry(1, "ann"), self.entry(2, "bob", P.SHAPE_SQUARE), self.entry(3, "cy", P.SHAPE_TRIANGLE)]
        c = self.joined(cars, entries)
        rows = hud.build_rows(c)
        self.assertEqual([r["id"] for r in rows], [3, 2, 5, 1, 4])           # server order: finisher first
        by_id = {r["id"]: r for r in rows}
        self.assertEqual(by_id[3]["label"], "FINISHED #1")
        self.assertEqual(by_id[4]["label"], "WRECKED")
        self.assertEqual(by_id[5]["label"], "DNF")
        self.assertEqual(by_id[1]["label"], "%d m" % round((3 * L - 500.0) / 10))
        self.assertEqual((by_id[2]["name"], by_id[2]["shape"], by_id[2]["health"], by_id[2]["lap"]), ("bob", 1, 55, 2))
        self.assertEqual(by_id[4]["name"], "P4")                              # not in the roster yet
        self.assertEqual([r["local"] for r in rows], [False, False, False, True, False])
        self.assertEqual(by_id[1]["rank"], 4)

    def test_event_texts(self):
        c = self.joined([car(1), car(2)], [self.entry(1, "ann"), self.entry(2, "bob")])
        text = lambda e: (hud.describe_event(e, c) or ("",))[0]
        self.assertEqual(text(ev(1, P.EV_HIT, 1, 15, 4)), "Hit an obstacle  -15 HP")
        self.assertEqual(text(ev(2, P.EV_HIT, 2, 15, 4)), "")                 # other people's hits are not news
        self.assertEqual(text(ev(3, P.EV_CHECKPOINT, 1, 2, 5000)), "Checkpoint 2")
        self.assertEqual(text(ev(4, P.EV_LAP, 1, 2, 61500)), "Lap 2 done in 1:01.5")
        self.assertEqual(text(ev(5, P.EV_LAP, 2, 2, 61500)), "")
        self.assertEqual(text(ev(6, P.EV_FINISH, 2, 1, 70000)), "bob finished 1st!")
        self.assertEqual(text(ev(7, P.EV_FINISH, 1, 3, 70000)), "You finished 3rd!")
        self.assertEqual(text(ev(8, P.EV_WRECK, 2)), "bob was wrecked")
        self.assertEqual(text(ev(9, P.EV_WRECK, 1)), "You are wrecked")
        self.assertEqual(text(ev(10, P.EV_PLAYER_JOIN, 2)), "bob joined")
        self.assertEqual(text(ev(11, P.EV_PLAYER_JOIN, 1)), "")
        self.assertEqual(text(ev(12, P.EV_PLAYER_LEAVE, 2, P.DISC_LEFT)), "bob left the game")
        self.assertEqual(text(ev(13, P.EV_PLAYER_LEAVE, 2, P.DISC_TIMEOUT)), "bob timed out")
        self.assertEqual(text(ev(14, P.EV_RACE_START, 0)), "")

    def test_the_hud_draws_sixteen_players(self):
        cars = [car(i + 1, progress=100.0 * (16 - i), health=100 - 5 * i) for i in range(16)]
        entries = [self.entry(i + 1, "player%d" % (i + 1), i % 3, host=i == 0) for i in range(16)]
        c = self.joined(cars, entries)
        rows = hud.build_rows(c)
        for size in ((1100, 700), (800, 480)):
            screen = pygame.Surface(size)
            screen.fill((10, 70, 10))
            hud.draw_standings(screen, self.fonts, rows, pygame.Rect(size[0] - 310, 10, 300, min(size[1] - 40, 44 + 46 * 16)))
            hud.draw_self_panel(screen, self.fonts, c, rows, pygame.Rect(10, size[1] - 120, 340, 110))
            self.assertNotEqual(screen.get_at((size[0] - 150, 30))[:3], (10, 70, 10))     # the panel is there
            self.assertNotEqual(screen.get_at((100, size[1] - 60))[:3], (10, 70, 10))

    def test_the_network_panel_draws_with_and_without_measurements(self):
        c = self.joined([car(1), car(2)], [self.entry(1, "ann"), self.entry(2, "bob")])
        rect = pygame.Rect(10, 10, 300, 250)
        screen = pygame.Surface((400, 300))
        screen.fill((10, 70, 10))
        hud.draw_netpanel(screen, self.fonts, c, rect, (23.5, 12.1))                 # no RTT samples yet
        empty = pygame.image.tostring(screen, "RGB")
        c.rtt_ms.extend([4.0, 5.0, 4.5, 30.0, 6.0])
        screen.fill((10, 70, 10))
        hud.draw_netpanel(screen, self.fonts, c, rect, (23.5, 12.1))
        graphed = pygame.image.tostring(screen, "RGB")
        self.assertNotEqual(empty, graphed)                                          # the sparkline appears
        self.assertNotEqual(screen.get_at((20, 20))[:3], (10, 70, 10))               # and the panel itself
        line = pygame.Surface((400, 40))
        hud.draw_netinfo(line, self.fonts, c, (390, 35), fps=59.7, hint=True)
        hud.draw_netinfo(line, self.fonts, c, (390, 35))
        short = pygame.Surface((400, 300))
        hud.draw_netpanel(short, self.fonts, c, pygame.Rect(10, 10, 300, 165), (1.0, 1.0))   # no room for the graph: numbers only
        hud.draw_netpanel(short, self.fonts, c, pygame.Rect(10, 10, 300, 40), (1.0, 1.0))    # absurdly short: still no error

    def test_toasts_expire_and_keep_the_newest_six(self):
        toasts = hud.Toasts()
        for i in range(9):
            toasts.add("msg %d" % i, hud.WHITE, 10.0)
        self.assertEqual([t[1] for t in toasts.items], ["msg %d" % i for i in range(3, 9)])
        screen = pygame.Surface((400, 300))
        toasts.draw(screen, self.fonts, 11.0, 200, 10)
        self.assertEqual(len(toasts.items), 6)
        toasts.draw(screen, self.fonts, 10.0 + toasts.LIFE + 0.1, 200, 10)
        self.assertEqual(toasts.items, [])

    def test_text_field_editing(self):
        f = hud.TextField("Name", "ab", max_len=4)

        def key(k, ch=""):
            f.key(pygame.event.Event(pygame.KEYDOWN, key=k, unicode=ch, mod=0))

        key(pygame.K_c, "c")
        key(pygame.K_d, "d")
        key(pygame.K_e, "e")                                    # beyond max_len: ignored
        self.assertEqual(f.value, "abcd")
        key(pygame.K_BACKSPACE)
        key(pygame.K_BACKSPACE)
        self.assertEqual(f.value, "ab")
        key(pygame.K_F5, "")                                    # keys without a character change nothing
        key(pygame.K_TAB, "\t")                                 # control characters are not printable
        self.assertEqual(f.value, "ab")
        screen = pygame.Surface((300, 100))
        f.draw(screen, self.fonts, pygame.Rect(10, 30, 280, 40), True, 0.0)
        f.draw(screen, self.fonts, pygame.Rect(10, 30, 280, 40), False, 0.0)


if __name__ == "__main__":
    unittest.main()
