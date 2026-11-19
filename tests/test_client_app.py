"""client.py: which screen shows when, what the keys do, toasts, drawing, and a whole race through the App.

Runs on pygame's "dummy" video driver (no window) and is skipped when pygame is not installed.  The App is
fed by the scripted-server ``Env`` or, for the last test, by the real GameServer on a simulated clock."""
import importlib.util
import math
import os
import socket
import unittest
from collections import defaultdict
from unittest import mock

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

from app import protocol as P      # needs no pygame: used at import time below, so it must not sit under the check

HAVE_PYGAME = importlib.util.find_spec("pygame") is not None
if HAVE_PYGAME:
    import pygame

    from app import client as C
    from app import hud
    from app.autopilot import Autopilot
    from app.netclient import DISCONNECTED, FAILED, JOINED, JOINING, LEFT, REJECTED, NetClient
    from tests.nethelpers import TRACK, Env, Wire, car, ev


def entry(pid, name, shape=P.SHAPE_CIRCLE, host=False, ready=False):
    return P.RosterEntry(pid, pid - 1, shape, ready, host, name)


def keys_down(*codes):
    keys = defaultdict(bool)
    for code in codes:
        keys[code] = True
    return keys


@unittest.skipUnless(HAVE_PYGAME, "pygame is not installed")
class HelperTests(unittest.TestCase):
    """The pure helpers; they need pygame only for its key constants."""

    def test_screen_for_follows_the_client_state(self):
        self.assertEqual(C.screen_for(None), C.CONNECT)
        env = Env()
        self.assertEqual(C.screen_for(env.client), C.CONNECT)                    # IDLE
        env.client.start()
        self.assertEqual(C.screen_for(env.client), C.JOINING_SCREEN)
        env.deliver(P.MSG_JOIN_ACK, P.pack_join_ack(__import__("tests.nethelpers", fromlist=["x"]).make_ack()))
        env.client.pump()
        self.assertEqual(C.screen_for(env.client), C.LOBBY)
        for state, scene in ((P.ST_COUNTDOWN, C.RACE), (P.ST_RACING, C.RACE), (P.ST_FINISHED, C.RESULTS),
                             (P.ST_LOBBY, C.LOBBY)):
            env.client.game_state = state
            self.assertEqual(C.screen_for(env.client), scene)
        for state in (FAILED, REJECTED, DISCONNECTED, LEFT):
            env.client.state = state
            self.assertEqual(C.screen_for(env.client), C.CONNECT)

    def test_keyboard_axes(self):
        self.assertEqual(C.read_axes(keys_down()), (0.0, 0.0))
        self.assertEqual(C.read_axes(keys_down(pygame.K_w)), (1.0, 0.0))
        self.assertEqual(C.read_axes(keys_down(pygame.K_UP, pygame.K_RIGHT)), (1.0, 1.0))      # right is +1
        self.assertEqual(C.read_axes(keys_down(pygame.K_s, pygame.K_a)), (-1.0, -1.0))
        self.assertEqual(C.read_axes(keys_down(pygame.K_DOWN, pygame.K_LEFT)), (-1.0, -1.0))
        self.assertEqual(C.read_axes(keys_down(pygame.K_w, pygame.K_s, pygame.K_a, pygame.K_d)), (0.0, 0.0))

    def test_steering_ramps_instead_of_jumping(self):
        v = 0.0
        v = C.smooth_axis(v, 1.0, 0.1)
        self.assertAlmostEqual(v, 0.5)                          # 5 per second: full lock in 0.2 s
        v = C.smooth_axis(v, 1.0, 0.1)
        self.assertAlmostEqual(v, 1.0)
        self.assertEqual(C.smooth_axis(1.0, 1.0, 5.0), 1.0)     # never overshoots
        self.assertAlmostEqual(C.smooth_axis(1.0, 0.0, 0.05), 1.0 - 0.45)     # back to centre is quicker
        self.assertEqual(C.smooth_axis(0.3, 0.0, 1.0), 0.0)
        flip = C.smooth_axis(1.0, -1.0, 0.1)                    # from full right to full left passes the centre
        self.assertAlmostEqual(flip, 0.1)
        self.assertEqual(C.smooth_axis(0.25, 0.25, 0.1), 0.25)
        self.assertEqual(C.smooth_axis(0.25, 1.0, 0.0), 0.25)   # no time, no change
        v = 0.0
        for _ in range(100):
            v = C.smooth_axis(v, -1.0, 0.016)
        self.assertEqual(v, -1.0)

    def test_wrong_way_detection(self):
        x, y, tx, ty, nx, ny = TRACK.frame_at(1200.0)
        along = math.atan2(ty, tx)
        self.assertFalse(C.wrong_way(TRACK, x, y, along, 150.0))
        self.assertTrue(C.wrong_way(TRACK, x, y, along + math.pi, 150.0))
        self.assertFalse(C.wrong_way(TRACK, x, y, along + math.pi, 10.0))      # crawling: not worth a warning
        self.assertFalse(C.wrong_way(TRACK, x, y, along + math.pi, -80.0))     # reversing on purpose
        self.assertFalse(C.wrong_way(TRACK, x, y, along + 1.0, 150.0))         # a slide, not a U-turn

    def test_failure_messages(self):
        env = Env()
        c = env.client
        c.state, c.error = FAILED, "no answer from the server"
        self.assertEqual(C.failure_text(c), "No answer from the server. Is it running, and is UDP port 5005 open?")
        c.state, c.error = FAILED, "the track built here does not match the server's (different versions?)"
        self.assertTrue(C.failure_text(c).startswith("The track built here"))
        c.state, c.error = REJECTED, P.REASON_TEXT[P.REJ_FULL]
        self.assertEqual(C.failure_text(c), "The server said no: %s." % P.REASON_TEXT[P.REJ_FULL])
        c.state, c.error = DISCONNECTED, "timed out"
        self.assertEqual(C.failure_text(c), "You were removed by the server (timed out).")
        c.state, c.error = "lost", "the server stopped answering"
        self.assertEqual(C.failure_text(c), "The server stopped answering.")

    def test_text_wrapping_and_default_name(self):
        font = pygame.font.Font(None, 20) if pygame.font.get_init() else None
        pygame.font.init()
        font = pygame.font.Font(None, 20)
        lines = C.wrap_text(font, "one two three four five six seven eight nine ten", 100)
        self.assertGreater(len(lines), 2)
        self.assertEqual(" ".join(lines), "one two three four five six seven eight nine ten")
        self.assertTrue(all(font.size(line)[0] <= 100 or " " not in line for line in lines))
        self.assertEqual(C.wrap_text(font, "", 100), [])
        with mock.patch.dict(os.environ, {"USERNAME": "A very long user name indeed", "USER": ""}):
            self.assertLessEqual(len(C.default_name()), 12)
        with mock.patch.dict(os.environ, {"USERNAME": "", "USER": ""}):
            self.assertEqual(C.default_name(), "Player")

    def test_menu_fit_shrinks_never_enlarges_and_centres(self):
        self.assertEqual(C.menu_fit((1100, 660)), (1.0, (0, 0)))
        self.assertEqual(C.menu_fit((1600, 900)), (1.0, (250, 120)))          # bigger window: same size, centred
        scale, offset = C.menu_fit((800, 480))
        self.assertAlmostEqual(scale, 800 / 1100)
        self.assertEqual(offset, (0, 0))
        scale, (ox, oy) = C.menu_fit((1049, 528))                              # the height is the limit: side margins
        self.assertAlmostEqual(scale, 528 / 660)
        self.assertEqual((ox, oy), ((1049 - int(1100 * scale)) // 2, 0))
        self.assertGreater(C.menu_fit((0, 0))[0], 0)                           # a minimised window must not divide by zero

    def test_fit_size_keeps_the_window_inside_the_screen(self):
        self.assertEqual(C.fit_size((1100, 700), (1920, 1080)), (1100, 700))   # plenty of room
        self.assertEqual(C.fit_size((1100, 700), (1366, 768)), (1100, 660))    # the taskbar and title bar need room
        self.assertEqual(C.fit_size((1100, 700), (1280, 720)), (1100, 619))    # 1920x1080 at 150 % scaling
        self.assertEqual(C.fit_size((1100, 700), (1093, 614)), (1049, 528))    # 1366x768 at 125 % scaling
        self.assertEqual(C.fit_size((1100, 700), None), (1100, 700))           # screen size unknown
        self.assertEqual(C.fit_size((1100, 700), (0, 0)), (1100, 700))         # a display that reports nothing sensible
        self.assertEqual(C.fit_size((1100, 700), (-1, -1)), (1100, 700))

    def test_main_fits_the_default_window_to_the_screen_but_keeps_an_explicit_size(self):
        with mock.patch.object(C, "App") as app_class, mock.patch.object(C, "desktop_size", return_value=(1280, 720)):
            self.assertEqual(C.main(["--server", "127.0.0.1:5005"]), 0)
            self.assertEqual(app_class.call_args.kwargs["size"], (1100, 619))
            self.assertEqual(C.main(["--server", "127.0.0.1:5005", "--size", "900x500"]), 0)   # what was asked for stays
            self.assertEqual(app_class.call_args.kwargs["size"], (900, 500))
        with mock.patch.object(C, "App") as app_class, mock.patch.object(C, "desktop_size", return_value=None):
            self.assertEqual(C.main(["--server", "127.0.0.1:5005"]), 0)                        # screen unknown: default size
            self.assertEqual(app_class.call_args.kwargs["size"], C.DEFAULT_SIZE)

    def test_desktop_size_is_a_pair_or_none(self):
        size = C.desktop_size()
        self.assertTrue(size is None or (len(size) == 2 and min(size) > 0), size)


@unittest.skipUnless(HAVE_PYGAME, "pygame is not installed")
class AppTests(unittest.TestCase):
    def setUp(self):
        self.apps = []

    def tearDown(self):
        for app in self.apps:
            app.close_client()
        pygame.quit()
        hud._TEXT_CACHE.clear()

    def make_app(self, **kw):
        app = C.App(kw.pop("server", "127.0.0.1:59999"), kw.pop("name", "Tester"), kw.pop("shape", P.SHAPE_SQUARE), **kw)
        self.apps.append(app)
        return app

    def joined(self, host=True, state=P.ST_LOBBY, players=2, size=None, **ack):
        """An App whose NetClient is JOINED as player 1 in a room of ``players`` (the test plays the server)."""
        env = Env()
        c = env.accept(player_id=1, slot=0, **ack)
        entries = [entry(i + 1, "Tester" if i == 0 else "bob%d" % i, i % 3, host=(i == 0)) for i in range(players)]
        env.roster(1, entries)
        env.snapshot(60, [car(i + 1, 50.0 * i) for i in range(players)], state=state,
                     flags=P.SNAPF_HOST if host else 0, roster_ver=1)
        app = self.make_app(size=size) if size else self.make_app()
        app.client = c
        c.on_event = app.on_event
        app.frame(0.016)
        return app, env

    @staticmethod
    def press(app, key, text="", mod=0):
        app.handle_event(pygame.event.Event(pygame.KEYDOWN, key=key, unicode=text, mod=mod))

    @staticmethod
    def click(app, pos):
        app.handle_event(pygame.event.Event(pygame.MOUSEBUTTONDOWN, pos=pos, button=1))

    @classmethod
    def click_in_menu(cls, app, pos):
        """Click where a menu pixel (the coordinates in ``app.buttons``) ends up in the window."""
        (ox, oy), scale = app.ui_offset, app.ui_scale
        cls.click(app, (int(ox + pos[0] * scale), int(oy + pos[1] * scale)))

    def frames(self, app, n=1, dt=0.016):
        for _ in range(n):
            app.frame(dt)

    # ------------------------------------------------------------------ lobby
    def test_lobby_keys_send_commands(self):
        app, env = self.joined(host=True)
        self.assertEqual(app.scene, C.LOBBY)
        c = app.client
        self.press(app, pygame.K_r, "r")
        self.assertEqual(c.pending_arg(P.CMD_READY), 1)
        env.run(0.1)
        self.assertIn((P.CMD_READY, 1), [P.unpack_cmd(p) for p in env.payloads(P.MSG_CMD)])
        self.press(app, pygame.K_r, "r")                        # pressing again before the server answers undoes it
        self.assertEqual(c.pending_arg(P.CMD_READY), 0)
        self.press(app, pygame.K_3, "3")
        self.assertEqual(c.pending_arg(P.CMD_SHAPE), P.SHAPE_TRIANGLE)
        self.press(app, pygame.K_RETURN, "\r")
        self.assertIsNotNone(c.pending_arg(P.CMD_START))
        env.run(0.3)
        cmds = [P.unpack_cmd(p) for p in env.payloads(P.MSG_CMD)]
        self.assertIn((P.CMD_SHAPE, 2), cmds)
        self.assertIn((P.CMD_START, 0), cmds)

    def test_only_the_host_can_start(self):
        app, env = self.joined(host=False)
        self.press(app, pygame.K_RETURN, "\r")
        self.press(app, pygame.K_SPACE, " ")
        self.assertIsNone(app.client.pending_arg(P.CMD_START))
        self.assertTrue(any("host" in t[1] for t in app.toasts.items))

    def test_mouse_buttons_in_the_lobby(self):
        app, env = self.joined(host=True)
        wide = sorted((r for r, _ in app.buttons if r.size == (226, 44)), key=lambda r: r.x)
        ready, start = wide                                     # READY on the left, START RACE next to it
        self.click_in_menu(app, ready.center)
        self.assertEqual(app.client.pending_arg(P.CMD_READY), 1)
        self.click_in_menu(app, start.center)
        self.assertIsNotNone(app.client.pending_arg(P.CMD_START))

    def assert_buttons_inside(self, app, size):
        self.assertGreaterEqual(len(app.buttons), 4)
        window = pygame.Rect(0, 0, *size).inflate(2, 2)           # one pixel of rounding
        (ox, oy), scale = app.ui_offset, app.ui_scale
        for rect, _ in app.buttons:
            shown = pygame.Rect(ox + rect.x * scale, oy + rect.y * scale, rect.w * scale, rect.h * scale)
            self.assertTrue(window.contains(shown), (size, tuple(rect), tuple(shown)))

    def test_every_menu_button_is_inside_a_small_or_big_window(self):
        """The menus were laid out for about 1100x660: in a smaller window the buttons and the track picture
        used to fall off the bottom.  They are now drawn at that size and shrunk to fit."""
        for size in ((640, 400), (800, 480), (1049, 528), (1100, 619), (1100, 660), (1100, 700), (1600, 900)):
            with self.subTest(size=size):
                app, env = self.joined(host=True, size=size)                       # the lobby
                self.assertEqual(app.scene, C.LOBBY)
                self.assert_buttons_inside(app, size)
                connect = self.make_app(size=size)                                  # the connect screen
                connect.frame(0.016)
                self.assertEqual(connect.scene, C.CONNECT)
                self.assert_buttons_inside(connect, size)

    def test_clicks_land_on_the_right_button_in_a_shrunk_menu(self):
        for size in ((800, 480), (1049, 528), (1600, 900)):
            with self.subTest(size=size):
                app, env = self.joined(host=True, size=size)
                ready, start = sorted((r for r, _ in app.buttons if r.size == (226, 44)), key=lambda r: r.x)
                self.click_in_menu(app, ready.center)
                self.assertEqual(app.client.pending_arg(P.CMD_READY), 1)
                self.click_in_menu(app, start.center)
                self.assertIsNotNone(app.client.pending_arg(P.CMD_START))
                self.click(app, (2, 2))                                             # the margin is not a button
                self.assertEqual(app.scene, C.LOBBY)

    def test_escape_leaves_the_lobby(self):
        app, env = self.joined()
        self.press(app, pygame.K_ESCAPE)
        self.assertIsNone(app.client)
        app.frame(0.016)
        self.assertEqual(app.scene, C.CONNECT)
        self.assertEqual(len(env.payloads(P.MSG_LEAVE)), 3)

    # ------------------------------------------------------------------ race
    def test_driving_keys_become_controls(self):
        app, env = self.joined(state=P.ST_RACING)
        self.assertEqual(app.scene, C.RACE)
        with mock.patch("pygame.key.get_pressed", return_value=keys_down(pygame.K_w, pygame.K_d)):
            app.frame(0.1)
            self.assertEqual((app.client._throttle, round(app.client._steer, 3)), (1.0, 0.5))
            app.frame(0.1)
            self.assertEqual((app.client._throttle, app.client._steer), (1.0, 1.0))
        with mock.patch("pygame.key.get_pressed", return_value=keys_down(pygame.K_s, pygame.K_LEFT)):
            self.frames(app, 6, 0.1)
            self.assertEqual((app.client._throttle, app.client._steer), (-1.0, -1.0))
        env.clear()
        env.run(0.2)
        throttle, steer, _buttons, _ack = P.unpack_input(env.payloads(P.MSG_INPUT)[-1])
        self.assertTrue(throttle < -0.9 and steer < -0.9)       # what the server receives

    def test_countdown_allows_a_flying_start_but_the_lobby_does_not_drive(self):
        app, env = self.joined(state=P.ST_COUNTDOWN)
        self.assertEqual(app.scene, C.RACE)
        with mock.patch("pygame.key.get_pressed", return_value=keys_down(pygame.K_w)):
            app.frame(0.1)
            self.assertEqual(app.client._throttle, 1.0)
        app2, _env2 = self.joined(state=P.ST_LOBBY)
        with mock.patch("pygame.key.get_pressed", return_value=keys_down(pygame.K_w)):
            app2.frame(0.1)
            self.assertEqual(app2.client._throttle, 0.0)

    def test_escape_must_be_pressed_twice_to_leave_a_race(self):
        app, env = self.joined(state=P.ST_RACING)
        self.press(app, pygame.K_ESCAPE)
        self.assertIsNotNone(app.client)
        self.assertTrue(any("Esc again" in t[1] for t in app.toasts.items))
        app.t += C.ESC_WINDOW_S + 0.5                           # too late: this press only re-arms
        self.press(app, pygame.K_ESCAPE)
        self.assertIsNotNone(app.client)
        self.press(app, pygame.K_ESCAPE)
        self.assertIsNone(app.client)

    def test_game_events_become_toasts_and_a_damage_flash(self):
        app, env = self.joined(state=P.ST_RACING)
        env.snapshot(61, [car(1), car(2)], state=P.ST_RACING, flags=P.SNAPF_HOST, roster_ver=1,
                     events=[ev(1, P.EV_HIT, 1, 15, 4), ev(2, P.EV_HIT, 2, 15, 4), ev(3, P.EV_WRECK, 2),
                             ev(4, P.EV_FINISH, 1, 1, 9000)])
        app.frame(0.016)
        shown = [t[1] for t in app.toasts.items]
        self.assertEqual(shown, ["Hit an obstacle  -15 HP", "bob1 was wrecked", "You finished 1st!"])
        self.assertGreater(app.flash, 0.0)
        self.assertGreater(app.shake, 0.0)
        for _ in range(60):
            app.frame(0.016)
        self.assertEqual((app.flash, app.shake), (0.0, 0.0))    # both die away

    def test_f3_shows_the_network_panel_and_the_rates_are_measured(self):
        app, env = self.joined(state=P.ST_RACING)
        self.frames(app, 2)
        plain = pygame.image.tostring(app.screen, "RGB")
        self.press(app, pygame.K_F3)
        self.assertTrue(app.show_net)
        app.frame(0.016)
        self.assertNotEqual(plain, pygame.image.tostring(app.screen, "RGB"))
        for tick in range(70):                                  # 3.5 s of traffic both ways
            env.snapshot(70 + tick, [car(1), car(2)], state=P.ST_RACING, flags=P.SNAPF_HOST, roster_ver=1)
            env.run(0.05)
            app.frame(0.05)
        down, up = app.rates
        self.assertTrue(10.0 < down < 80.0, down)               # 2 cars: ~20 snapshots a second of ~70 bytes
        self.assertTrue(5.0 < up < 40.0, up)                    # INPUT at 30 per second
        self.press(app, pygame.K_F3)
        self.assertFalse(app.show_net)
        app.leave()
        app.frame(0.016)
        self.assertEqual(app._rate_mark, None)                  # no client, nothing to measure

    def test_the_network_panel_leaves_the_status_panel_alone_in_a_short_window(self):
        for size, panel_expected in (((900, 400), False), ((900, 480), True), ((900, 700), True)):
            with self.subTest(size=size):
                app, env = self.joined(state=P.ST_RACING, size=size)
                self.frames(app, 3)
                app.frame(0.0)
                before = app.screen.copy()
                self.press(app, pygame.K_F3)
                app.frame(0.0)
                after = app.screen.copy()
                pad, (w, h) = 10, size
                status = pygame.Rect(pad, h - 110 - pad, 340, 110)
                top = pad + app.minimap.surface.get_height() + 8 + pad
                room = status.y - 8 - top                                       # what is free above the status panel
                self.assertEqual(room >= 160, panel_expected, room)              # (the sizes above are meaningful)
                zone = pygame.Rect(pad, top, 300, min(120, max(1, room)))        # the upper part of the panel
                same = lambda rect: (pygame.image.tostring(before.subsurface(rect), "RGB")
                                     == pygame.image.tostring(after.subsurface(rect), "RGB"))
                self.assertTrue(same(status), "the F3 panel was drawn over the status panel")
                self.assertEqual(not same(zone), panel_expected)

    def test_a_join_toast_waits_for_the_name(self):
        app, env = self.joined(state=P.ST_LOBBY, players=2)
        env.snapshot(61, [car(1), car(2), car(3)], state=P.ST_LOBBY, flags=P.SNAPF_HOST, roster_ver=2,
                     events=[ev(1, P.EV_PLAYER_JOIN, 3)])
        app.frame(0.016)
        self.assertEqual(app.toasts.items, [])                  # player 3 is not in the roster yet: no "P3 joined"
        env.roster(2, [entry(1, "Tester", host=True), entry(2, "bob1"), entry(3, "carla")])
        app.frame(0.016)
        self.assertEqual([t[1] for t in app.toasts.items], ["carla joined"])
        env.snapshot(62, [car(1), car(2), car(3)], state=P.ST_LOBBY, flags=P.SNAPF_HOST, roster_ver=2,
                     events=[ev(2, P.EV_PLAYER_JOIN, 9)])        # a name that never arrives: after 1.5 s a plain id
        app.frame(0.016)
        self.assertEqual(len(app.toasts.items), 1)
        app.frame(1.6)
        self.assertEqual([t[1] for t in app.toasts.items], ["carla joined", "P9 joined"])

    def test_wrecked_and_wrong_way_banners_do_not_break_drawing(self):
        app, env = self.joined(state=P.ST_RACING)
        x, y, tx, ty, _nx, _ny = TRACK.frame_at(1500.0)
        along = math.atan2(ty, tx)
        env.snapshot(62, [car(1, x, y, along + math.pi, 200.0), car(2)], state=P.ST_RACING, flags=P.SNAPF_HOST,
                     roster_ver=1, race_time_ms=20000)
        for _ in range(60):                                     # long enough for the banner to be due
            app.frame(0.02)
        env.snapshot(63, [car(1, x, y, along, 0.0, status=P.STATUS_WRECKED), car(2)], state=P.ST_RACING,
                     flags=P.SNAPF_HOST, roster_ver=1, race_time_ms=21000)
        self.frames(app, 3)
        self.assertEqual(app.scene, C.RACE)

    # ------------------------------------------------------------------ drawing
    def test_every_screen_draws_with_sixteen_players(self):
        env = Env()
        c = env.accept(player_id=1, slot=0)
        entries = [entry(i + 1, "driver%d" % (i + 1), i % 3, host=i == 0, ready=i % 2 == 0) for i in range(16)]
        env.roster(1, entries)
        app = self.make_app()
        app.client = c
        c.on_event = app.on_event
        pictures = {}
        for name, state in (("lobby", P.ST_LOBBY), ("countdown", P.ST_COUNTDOWN), ("race", P.ST_RACING),
                            ("results", P.ST_FINISHED)):
            cars = [car(i + 1, *TRACK.frame_at(40.0 * i + 10.0)[:2], progress=40.0 * (16 - i), health=100 - 5 * i,
                        status=P.STATUS_FINISHED if state == P.ST_FINISHED and i < 3 else 0, place=i + 1 if i < 3 else 0)
                    for i in range(16)]
            env.snapshot(60 + len(pictures), cars, state=state, flags=P.SNAPF_HOST, roster_ver=1,
                         countdown_ms=2300 if state != P.ST_RACING else 0, race_time_ms=12500)
            self.frames(app, 3)
            self.assertEqual(app.scene, {"lobby": C.LOBBY, "countdown": C.RACE, "race": C.RACE, "results": C.RESULTS}[name])
            pictures[name] = pygame.image.tostring(app.screen, "RGB")
        self.assertEqual(len(set(pictures.values())), 4)        # four different pictures, none of them an error
        c.state = JOINING                                       # and the two screens before the game
        app.frame(0.016)
        self.assertEqual(app.scene, C.JOINING_SCREEN)
        app.client = None
        app.message = "Something went wrong with the connection, which is a long message that must wrap onto lines."
        app.frame(0.016)
        self.assertEqual(app.scene, C.CONNECT)

    def test_the_race_screen_survives_resizing_and_missing_data(self):
        app, env = self.joined(state=P.ST_RACING, players=3)
        pygame.display.set_mode((640, 400), pygame.RESIZABLE)
        self.frames(app, 3)
        self.assertEqual(app.cam.w, 640)
        pygame.display.set_mode((1400, 900), pygame.RESIZABLE)
        self.frames(app, 3)
        self.assertEqual((app.cam.w, app.cam.h), (1400, 900))
        app.client.players.clear()                              # no player data at all: still draws
        app.client.buffer.clear()
        app.frame(0.016)

    # ------------------------------------------------------------------ connect screen
    def test_connect_form_typing_focus_and_shape(self):
        app = self.make_app()
        self.assertEqual((app.scene, app.focus), (C.CONNECT, 0))
        app.frame(0.016)
        app.server_field.value = ""
        for ch in "1.2.3.4:5":
            self.press(app, ord(ch), ch)
        self.press(app, pygame.K_BACKSPACE)
        self.press(app, pygame.K_0, "0")
        self.assertEqual(app.server_field.value, "1.2.3.4:0")
        self.press(app, pygame.K_TAB)
        self.assertEqual(app.focus, 1)
        app.name_field.value = ""
        self.press(app, pygame.K_z, "Z")
        self.assertEqual(app.name_field.value, "Z")
        self.press(app, pygame.K_TAB)
        self.assertEqual(app.focus, 2)
        self.press(app, pygame.K_RIGHT)
        self.assertEqual(app.shape, (P.SHAPE_SQUARE + 1) % 3)
        self.press(app, pygame.K_1, "1")
        self.assertEqual(app.shape, P.SHAPE_CIRCLE)
        self.press(app, pygame.K_TAB, "", pygame.KMOD_SHIFT)
        self.assertEqual(app.focus, 1)
        self.press(app, pygame.K_UP)
        self.assertEqual(app.focus, 0)

    def test_connect_screen_mouse_selects_fields_and_shapes(self):
        app = self.make_app()
        app.frame(0.016)
        server, name, circle, square, triangle, connect = [r for r, _ in app.buttons]
        self.assertEqual((server.size, name.size, circle.size), ((460, 42), (460, 42), (138, 108)))
        self.click_in_menu(app, triangle.center)
        self.assertEqual(app.shape, P.SHAPE_TRIANGLE)
        self.click_in_menu(app, circle.center)
        self.assertEqual(app.shape, P.SHAPE_CIRCLE)
        self.click_in_menu(app, name.center)
        self.assertEqual(app.focus, 1)
        self.click_in_menu(app, server.center)
        self.assertEqual(app.focus, 0)

    def test_bad_addresses_are_explained_not_crashed_on(self):
        app = self.make_app()
        app.server_field.value = "host:notaport"
        self.press(app, pygame.K_RETURN, "\r")
        self.assertIn("port", app.message)
        self.assertIsNone(app.client)
        app.server_field.value = "   "
        self.press(app, pygame.K_RETURN, "\r")
        self.assertIn("server address", app.message)
        app.server_field.value = "no-such-host.example:5005"
        with mock.patch("app.netclient.socket.gethostbyname", side_effect=socket.gaierror(-2, "Name or service not known")):
            self.press(app, pygame.K_RETURN, "\r")
        self.assertIn("Cannot use that address", app.message)
        self.assertIsNone(app.client)

    def test_connect_joining_cancel_and_retry(self):
        app = self.make_app()
        app.server_field.value = "127.0.0.1:59998"
        app.name_field.value = "Zed"
        app.shape = P.SHAPE_TRIANGLE
        self.press(app, pygame.K_RETURN, "\r")                  # a real UDP socket; nobody listens there
        self.assertIsInstance(app.client, NetClient)
        self.assertEqual((app.client.state, app.client.name, app.client.shape), (JOINING, "Zed", P.SHAPE_TRIANGLE))
        app.frame(0.016)
        self.assertEqual(app.scene, C.JOINING_SCREEN)
        first = app.client
        self.press(app, pygame.K_ESCAPE)                        # cancel
        self.assertIsNone(app.client)
        app.frame(0.016)
        self.assertEqual(app.scene, C.CONNECT)
        self.press(app, pygame.K_RETURN, "\r")                  # try again: a brand-new client and socket
        self.assertIsNot(app.client, first)
        self.assertEqual(app.client.state, JOINING)

    def test_a_failed_join_returns_to_the_form_with_the_reason(self):
        env = Env()
        c = env.client
        c.start()
        env.run(6.0, keepalive=False)                           # nobody answers
        self.assertEqual(c.state, FAILED)
        app = self.make_app()
        app.client = c
        app.frame(0.016)
        self.assertEqual(app.scene, C.CONNECT)
        self.assertTrue(app.message.startswith("No answer from the server"))

    def test_losing_the_server_during_a_race_returns_to_the_form(self):
        app, env = self.joined(state=P.ST_RACING)
        env.run(6.0, keepalive=False)                           # the server goes silent
        app.frame(0.016)
        self.assertEqual(app.scene, C.CONNECT)
        self.assertIn("stopped answering", app.message)

    def test_closing_the_window_stops_the_loop(self):
        app = self.make_app()
        app.handle_event(pygame.event.Event(pygame.QUIT))
        self.assertFalse(app.running)

    def test_screenshots_are_written_once_per_screen(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            app = self.make_app(shots_dir=tmp)
            app.t = 5.0
            self.frames(app, 3)
            self.assertEqual(os.listdir(tmp), ["connect.png"])


@unittest.skipUnless(HAVE_PYGAME, "pygame is not installed")
class WholeRaceTests(unittest.TestCase):
    def tearDown(self):
        pygame.quit()
        hud._TEXT_CACHE.clear()

    def test_the_app_plays_a_whole_race_against_the_real_server(self):
        """The autopilot-driven App joins the real GameServer (simulated clock) with two bots: lobby, countdown,
        race, results; the local car finishes and the final standings are captured."""
        w = Wire(laps=1, results_s=2.0)
        ann = w.add("Ann", P.SHAPE_TRIANGLE)                    # first in, so the host
        app = C.App("10.0.0.1:5005", "Ann", P.SHAPE_TRIANGLE, auto=True, wait_players=3)
        app.client = ann
        ann.on_event = app.on_event
        bots = [w.add("bot1", P.SHAPE_CIRCLE), w.add("bot2", P.SHAPE_SQUARE)]
        pilots = [Autopilot(TRACK, skill=0.8), Autopilot(TRACK, skill=0.75)]
        dt = w.server.game.dt
        scenes = []
        for i in range(int(150.0 / dt)):
            w.step()
            for bot, pilot in zip(bots, pilots):
                if bot.game_state == P.ST_RACING and bot.me is not None and not bot.me.status & P.STATUS_FINISHED:
                    bot.set_controls(*pilot.control(bot.me.x, bot.me.y, bot.me.heading, bot.me.speed,
                                                    bot.race_time_ms / 1000.0))
                else:
                    bot.set_controls(0.0, 0.0)
            app.update_controls(dt)                             # steering must be smooth: every tick
            if i % 8 == 0:
                app.frame(8 * dt)                               # drawing is the slow part: 7.5 frames a second
                if not scenes or scenes[-1] != app.scene:
                    scenes.append(app.scene)
                if app.results_seen_at is not None and app.t - app.results_seen_at > 1.0:
                    break
        self.assertEqual([s for s in scenes if s != C.JOINING_SCREEN], [C.LOBBY, C.RACE, C.RESULTS])
        final = app.final
        self.assertIsNotNone(final)
        self.assertTrue(final["me"].status & P.STATUS_FINISHED)
        self.assertEqual(len(final["rows"]), 3)
        self.assertEqual(final["rows"][0]["rank"], 1)
        self.assertEqual(final["summary"]["rx_lost"], 0)
        self.assertEqual(final["summary"]["bad_datagrams"], 0)
        self.assertGreater(final["summary"]["events"], 5)
        app.close_client()


if __name__ == "__main__":
    unittest.main()
