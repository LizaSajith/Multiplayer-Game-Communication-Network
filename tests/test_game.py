"""Unit tests for app/game.py (run: python -m unittest discover -s tests -t . -v).

The rules engine has no sockets, so every test steps the simulation directly.
Helpers teleport cars with ``Game._place`` and drive them over gates with a
short burst of forward / backward speed; this is deterministic and fast.
"""
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app import protocol as P
from app.autopilot import Autopilot
from app.game import (HIT_COOLDOWN, REVERSE_MAX, RESPAWN_INVULN, SHAPE_STATS, START_HEALTH,
                      WRECK_TIME, Game, GameConfig)
from app.track import KIND_CIRCLE


# ------------------------------------------------------------------ helpers
def make_game(players=1, shapes=None, **cfg):
    cfg.setdefault("countdown_s", 0.2)
    g = Game(GameConfig(**cfg))
    for i in range(players):
        shape = shapes[i] if shapes else P.SHAPE_CIRCLE
        g.add_player(i + 1, "p%d" % (i + 1), shape, i)
    g.drain_events()
    return g


def start_race(g):
    assert g.begin_countdown()
    for _ in range(10000):
        if g.state == P.ST_RACING:
            break
        g.step()
    assert g.state == P.ST_RACING
    g.drain_events()


def clear_obstacles(g):
    g.track.obstacles = []
    g.track._obstacle_s = []


def put(g, p, s, lateral=0.0, speed=0.0):
    """Teleport a car to track position (s, lateral), facing along the track."""
    x, y, hdg = g.track.point_at(s, lateral)
    g._place(p, x, y, hdg)
    p.speed = speed


def cross(g, p, gate_s, forward=True, speed=120.0):
    """Drive a car over the gate at ``gate_s`` (forwards or backwards)."""
    if forward:
        put(g, p, gate_s - 3.0, speed=speed)
    else:
        put(g, p, gate_s + 3.0, speed=-speed)
    for _ in range(10):
        g.step()


def run_lap(g, p):
    for c in g.track.checkpoints:
        cross(g, p, c)
    cross(g, p, 0.0)


def first_circle_obstacle(g):
    return next(o for o in g.track.obstacles if not o.moving and o.kind == KIND_CIRCLE)


def put_on(g, p, ob):
    x, y = ob.center(g.race_time)
    g._place(p, x, y, math.atan2(ob.ty, ob.tx))


def types(events):
    return [e[0] for e in events]


# ------------------------------------------------------------------ lobby / joining
class LobbyTests(unittest.TestCase):
    def test_join_parks_cars_on_the_grid_lowest_id_first(self):
        g = Game(GameConfig())
        g.add_player(5, "five", P.SHAPE_SQUARE, 1)
        g.add_player(2, "two", P.SHAPE_CIRCLE, 0)
        self.assertEqual(g.players[2].grid_index, 0)
        self.assertEqual(g.players[5].grid_index, 1)
        x, y, hdg, _, _ = g.track.grid_slot(0)
        self.assertAlmostEqual(g.players[2].x, x, places=6)
        self.assertAlmostEqual(g.players[2].y, y, places=6)
        self.assertEqual(types(g.drain_events()), [P.EV_PLAYER_JOIN, P.EV_PLAYER_JOIN])
        self.assertEqual(g.drain_events(), [])

    def test_unknown_shape_becomes_circle(self):
        g = Game(GameConfig())
        self.assertEqual(g.add_player(1, "a", 99, 0).shape, P.SHAPE_CIRCLE)

    def test_room_full_and_race_in_progress_are_refused(self):
        g = make_game(players=2, max_players=2)
        self.assertEqual(g.can_join(), (False, P.REJ_FULL))
        with self.assertRaises(RuntimeError):
            g.add_player(3, "c", 0, 2)
        g2 = make_game(players=1)
        self.assertEqual(g2.can_join(), (True, 0))
        self.assertTrue(g2.begin_countdown())
        self.assertEqual(g2.can_join(), (False, P.REJ_IN_PROGRESS))

    def test_remove_player(self):
        g = make_game(players=2)
        g.remove_player(2, P.DISC_KICKED)
        self.assertEqual(g.drain_events(), [(P.EV_PLAYER_LEAVE, 2, P.DISC_KICKED, 0)])
        g.remove_player(2)                       # already gone: no event, no error
        self.assertEqual(g.drain_events(), [])

    def test_removing_the_last_player_resets_or_ends(self):
        g = make_game(players=1)
        self.assertTrue(g.begin_countdown())
        g.remove_player(1)
        self.assertEqual(g.state, P.ST_LOBBY)
        g = make_game(players=1)
        start_race(g)
        g.remove_player(1)
        self.assertEqual(g.state, P.ST_FINISHED)
        self.assertIn(P.EV_RACE_END, types(g.drain_events()))

    def test_ready_and_shape_only_change_in_the_lobby(self):
        g = make_game(players=1)
        g.set_ready(1, True)
        g.set_shape(1, P.SHAPE_TRIANGLE)
        g.set_shape(1, 9)                        # invalid, ignored
        self.assertTrue(g.players[1].ready)
        self.assertEqual(g.players[1].shape, P.SHAPE_TRIANGLE)
        self.assertTrue(g.begin_countdown())
        self.assertFalse(g.players[1].ready)     # countdown clears it
        g.set_ready(1, True)
        g.set_shape(1, P.SHAPE_SQUARE)
        self.assertFalse(g.players[1].ready)
        self.assertEqual(g.players[1].shape, P.SHAPE_TRIANGLE)

    def test_config_validation(self):
        for bad in ({"laps": 0}, {"laps": 51}, {"max_players": 0}, {"max_players": 33},
                    {"tick_rate": 9}, {"tick_rate": 241},
                    {"tick_rate": 30, "snapshot_rate": 31}, {"snapshot_rate": 0}):
            with self.assertRaises(ValueError, msg=str(bad)):
                Game(GameConfig(**bad))
        Game(GameConfig(laps=1, max_players=32, tick_rate=10, snapshot_rate=10))


# ------------------------------------------------------------------ lifecycle
class LifecycleTests(unittest.TestCase):
    def test_countdown_needs_players(self):
        g = Game(GameConfig())
        self.assertFalse(g.begin_countdown())
        self.assertEqual(g.state, P.ST_LOBBY)

    def test_countdown_then_race_start(self):
        g = make_game(players=1, countdown_s=1.0)
        self.assertTrue(g.begin_countdown())
        self.assertFalse(g.begin_countdown())                # already counting down
        self.assertEqual(g.countdown_ms(), 1000)
        for _ in range(30):
            g.step()
        self.assertEqual(g.state, P.ST_COUNTDOWN)
        self.assertAlmostEqual(g.countdown_ms(), 500, delta=20)
        for _ in range(30):
            g.step()
        self.assertEqual(g.state, P.ST_RACING)
        self.assertEqual(g.countdown_ms(), 0)
        self.assertEqual(types(g.drain_events()), [P.EV_RACE_START])
        self.assertEqual(g.race_ticks, 0)

    def test_cars_are_frozen_during_the_countdown(self):
        g = make_game(players=1, countdown_s=1.0)
        p = g.players[1]
        self.assertTrue(g.begin_countdown())
        before = (p.x, p.y, p.heading)
        g.set_input(1, 1.0, 1.0)
        for _ in range(50):
            g.step()
        self.assertEqual((p.x, p.y, p.heading), before)
        self.assertEqual(p.speed, 0.0)

    def test_input_is_clamped_and_sanitised(self):
        g = make_game(players=1)
        p = g.players[1]
        g.set_input(1, 5.0, -5.0)
        self.assertEqual((p.throttle, p.steer), (1.0, -1.0))
        g.set_input(1, float("nan"), float("inf"))
        self.assertEqual((p.throttle, p.steer), (0.0, 0.0))
        g.set_input(99, 1.0, 1.0)                            # unknown player: ignored


# ------------------------------------------------------------------ physics (no obstacles)
class PhysicsTests(unittest.TestCase):
    def speed_after(self, shape, seconds):
        g = make_game(players=1, shapes=[shape])
        clear_obstacles(g)
        start_race(g)
        g.set_input(1, 1.0, 0.0)
        for _ in range(int(seconds * 60)):
            g.step()
        return g.players[1].speed

    def test_shapes_handle_differently(self):
        circle, square, tri = P.SHAPE_CIRCLE, P.SHAPE_SQUARE, P.SHAPE_TRIANGLE
        # acceleration after 0.5 s: triangle > circle > square
        a = [self.speed_after(s, 0.5) for s in (tri, circle, square)]
        self.assertGreater(a[0], a[1])
        self.assertGreater(a[1], a[2])
        # the top speed is reached eventually and differs: square > circle > triangle
        top = {s: self.speed_after(s, 2.5) for s in (circle, square, tri)}
        for s in top:
            self.assertAlmostEqual(top[s], SHAPE_STATS[s].max_speed, places=3)
        self.assertGreater(top[square], top[circle])
        self.assertGreater(top[circle], top[tri])

    def test_brake_reverse_and_coast(self):
        g = make_game(players=1)
        clear_obstacles(g)
        start_race(g)
        p = g.players[1]
        p.speed = 200.0
        g.set_input(1, 0.0, 0.0)
        for _ in range(30):                                   # coasting: loses speed slowly
            g.step()
        self.assertTrue(130.0 < p.speed < 200.0)
        coasted = p.speed
        g.set_input(1, -1.0, 0.0)
        for _ in range(10):                                   # braking: loses speed much faster
            g.step()
        self.assertAlmostEqual(p.speed, coasted - 520.0 * 10 / 60.0, delta=1.0)
        for _ in range(120):                                  # holding it: stops, then reverses, capped
            g.step()
        self.assertAlmostEqual(p.speed, -REVERSE_MAX, places=6)

    def test_walls_keep_cars_on_the_track(self):
        limit = None
        for side in (+1, -1):
            g = make_game(players=1)
            clear_obstacles(g)
            start_race(g)
            p = g.players[1]
            limit = g.track.half_width - 14.0
            x, y, hdg = g.track.point_at(200.0, 0.0)
            g._place(p, x, y, hdg + side * math.pi / 2.0)    # facing a wall
            g.set_input(1, 1.0, 0.0)
            for _ in range(180):
                g.step()
                self.assertLessEqual(abs(g.track.locate(p.x, p.y).lateral), limit + 1e-6)
            self.assertEqual(p.health, START_HEALTH)         # walls never hurt
            self.assertLess(p.speed, 80.0)                   # but they slow you down


# ------------------------------------------------------------------ obstacles, wrecks, respawn
class ObstacleTests(unittest.TestCase):
    def test_hit_costs_health_once_per_cooldown(self):
        g = make_game(players=1)
        start_race(g)
        p = g.players[1]
        ob = first_circle_obstacle(g)
        put_on(g, p, ob)
        g.step()
        self.assertEqual(p.health, START_HEALTH - ob.damage)
        self.assertEqual(p.hits, 1)
        self.assertEqual(g.drain_events(), [(P.EV_HIT, 1, ob.damage, ob.id)])
        for _ in range(30):                                   # 0.5 s: still invulnerable
            put_on(g, p, ob)
            g.step()
        self.assertEqual(p.hits, 1)
        for _ in range(25):                                   # now past the 0.8 s cooldown
            g.step()
        put_on(g, p, ob)
        g.step()
        self.assertEqual(p.hits, 2)
        self.assertEqual(p.health, START_HEALTH - 2 * ob.damage)
        self.assertGreaterEqual(HIT_COOLDOWN, 0.5)

    def test_sweeping_obstacles_hurt_more(self):
        g = make_game(players=1)
        tr = g.track
        moving = [o for o in tr.obstacles if o.moving]
        static = [o for o in tr.obstacles if not o.moving]
        self.assertTrue(moving and static)
        self.assertTrue(all(o.damage == 20 for o in moving))
        self.assertTrue(all(o.damage == 15 for o in static))

    def test_wreck_freezes_then_respawns_at_the_grid(self):
        g = make_game(players=1)
        start_race(g)
        p = g.players[1]
        ob = first_circle_obstacle(g)
        p.health = 10
        put_on(g, p, ob)
        g.step()
        self.assertTrue(p.wrecked)
        self.assertEqual((p.health, p.speed, p.wrecks), (0, 0.0, 1))
        self.assertEqual(types(g.drain_events()), [P.EV_HIT, P.EV_WRECK])
        frozen = (p.x, p.y)
        g.set_input(1, 1.0, 1.0)
        for _ in range(100):
            g.step()
        self.assertEqual((p.x, p.y), frozen)
        self.assertTrue(p.wrecked)
        g.set_input(1, 0.0, 0.0)
        for _ in range(300):
            if not p.wrecked:
                break
            g.step()
        self.assertFalse(p.wrecked)
        self.assertGreaterEqual(g.race_time - p.wreck_at, WRECK_TIME - 1e-9)
        self.assertEqual(types(g.drain_events()), [P.EV_RESPAWN])
        self.assertEqual(p.health, START_HEALTH)
        self.assertGreater(p.invuln_until, g.race_time)
        self.assertAlmostEqual(p.invuln_until - g.race_time, RESPAWN_INVULN, delta=0.05)
        gx, gy, _, _, _ = g.track.grid_slot(p.grid_index)
        self.assertAlmostEqual(p.x, gx, places=6)            # never crossed the line: grid slot
        self.assertAlmostEqual(p.y, gy, places=6)

    def respawn_s(self, started, cp):
        g = make_game(players=1)
        start_race(g)
        p = g.players[1]
        p.started, p.cp = started, cp
        p.health = 1
        put_on(g, p, first_circle_obstacle(g))
        for _ in range(400):
            g.step()
            if p.wrecked is False and p.wrecks == 1:
                break
        self.assertEqual(p.wrecks, 1)
        self.assertFalse(p.wrecked)
        loc = g.track.locate(p.x, p.y)
        self.assertLess(abs(loc.lateral), 1.0)
        return g, loc.s

    def test_respawn_at_last_checkpoint_or_just_after_the_line(self):
        g, s = self.respawn_s(True, 2)
        self.assertAlmostEqual(s, g.track.checkpoints[1] + 25.0, delta=1.0)
        g, s = self.respawn_s(True, 0)
        self.assertAlmostEqual(s, 40.0, delta=1.0)

    def test_without_respawn_wrecked_players_are_out(self):
        g = make_game(players=2, respawn=False)
        start_race(g)
        ob = first_circle_obstacle(g)
        p1, p2 = g.players[1], g.players[2]
        p1.health = 1
        put_on(g, p1, ob)
        g.step()
        self.assertTrue(p1.wrecked)
        self.assertEqual(g.state, P.ST_RACING)                # p2 is still in the race
        for _ in range(300):
            g.step()
        self.assertTrue(p1.wrecked)                           # never comes back
        p2.health = 1
        put_on(g, p2, ob)
        g.step()
        g.step()
        self.assertEqual(g.state, P.ST_FINISHED)
        self.assertTrue(p1.dnf and p2.dnf)

    def test_snapshot_status_bits_for_invulnerable_and_wrecked(self):
        g = make_game(players=1)
        start_race(g)
        p = g.players[1]
        ob = first_circle_obstacle(g)
        put_on(g, p, ob)
        g.step()
        self.assertTrue(g.player_states()[0].status & P.STATUS_INVULN)
        for _ in range(60):
            g.step()
        self.assertFalse(g.player_states()[0].status & P.STATUS_INVULN)
        p.health = 1
        put_on(g, p, ob)
        g.step()
        st = g.player_states()[0]
        self.assertTrue(st.status & P.STATUS_WRECKED)
        self.assertEqual(st.health, 0)


# ------------------------------------------------------------------ gates, laps
class GateTests(unittest.TestCase):
    def setUp(self):
        self.g = make_game(players=1, laps=2)
        clear_obstacles(self.g)
        start_race(self.g)
        self.p = self.g.players[1]
        self.m = len(self.g.track.checkpoints)

    def test_start_line_checkpoints_in_order_then_lap(self):
        g, p = self.g, self.p
        self.assertFalse(p.started)
        self.assertLess(p.progress, 0.0)                      # behind the line
        cross(g, p, 0.0)
        self.assertTrue(p.started)
        self.assertEqual((p.cp, p.laps_done), (0, 0))
        self.assertGreater(p.progress, 0.0)
        self.assertEqual(g.drain_events(), [])
        for i, c in enumerate(g.track.checkpoints, start=1):
            cross(g, p, c)
            self.assertEqual(p.cp, i)
        ev = g.drain_events()
        self.assertEqual([(e[0], e[2]) for e in ev], [(P.EV_CHECKPOINT, i) for i in range(1, self.m + 1)])
        cross(g, p, 0.0)
        self.assertEqual((p.laps_done, p.cp), (1, 0))
        lap = g.drain_events()
        self.assertEqual([(e[0], e[2]) for e in lap], [(P.EV_LAP, 1)])
        self.assertGreater(lap[0][3], 0)                      # lap time in ms
        self.assertEqual(p.best_lap_ms, lap[0][3])
        self.assertFalse(p.finished)

    def test_no_shortcuts(self):
        g, p = self.g, self.p
        cross(g, p, 0.0)
        cross(g, p, 0.0)                                      # line again, no checkpoints
        self.assertEqual(p.laps_done, 0)
        cross(g, p, g.track.checkpoints[1])                   # checkpoint 2 before 1
        self.assertEqual(p.cp, 0)
        cross(g, p, g.track.checkpoints[0])
        self.assertEqual(p.cp, 1)
        cross(g, p, g.track.checkpoints[2])                   # skipping 2
        self.assertEqual(p.cp, 1)
        cross(g, p, 0.0)                                      # line with a lap not done
        self.assertEqual(p.laps_done, 0)

    def test_crossing_backwards_undoes_gates(self):
        g, p = self.g, self.p
        cps = g.track.checkpoints
        cross(g, p, 0.0)
        cross(g, p, cps[0])
        cross(g, p, cps[1])
        self.assertEqual(p.cp, 2)
        cross(g, p, cps[1], forward=False)
        self.assertEqual(p.cp, 1)
        cross(g, p, cps[0], forward=False)
        self.assertEqual(p.cp, 0)
        cross(g, p, 0.0, forward=False)                       # back over the start line
        self.assertFalse(p.started)
        self.assertEqual(p.laps_done, 0)

    def test_backing_over_the_line_does_not_repeat_the_lap_event(self):
        g, p = self.g, self.p
        cross(g, p, 0.0)
        run_lap(g, p)
        self.assertEqual(p.laps_done, 1)
        g.drain_events()
        cross(g, p, 0.0, forward=False)                       # undo the lap...
        self.assertEqual((p.laps_done, p.cp), (0, self.m))
        cross(g, p, 0.0)                                      # ...and redo it
        self.assertEqual(p.laps_done, 1)
        self.assertEqual(g.drain_events(), [])                # no second LAP event


# ------------------------------------------------------------------ race end
class RaceEndTests(unittest.TestCase):
    def finish(self, g, p):
        cross(g, p, 0.0)
        for _ in range(g.cfg.laps):
            run_lap(g, p)

    def test_finishing_places_and_race_end(self):
        g = make_game(players=2, laps=1)
        clear_obstacles(g)
        start_race(g)
        p1, p2 = g.players[1], g.players[2]
        self.finish(g, p1)
        self.assertTrue(p1.finished)
        self.assertEqual(p1.place, 1)
        self.assertEqual(p1.progress, g.cfg.laps * g.track.length)
        self.assertEqual(g.remaining_units(p1), 0.0)
        self.assertEqual(g.state, P.ST_RACING)                # p2 still racing
        self.assertTrue(g.player_states()[0].status & P.STATUS_FINISHED)
        self.finish(g, p2)
        self.assertEqual(p2.place, 2)
        self.assertEqual(g.state, P.ST_FINISHED)
        self.assertEqual(g.races_completed, 1)
        ev = g.drain_events()
        self.assertEqual([e[2] for e in ev if e[0] == P.EV_FINISH], [1, 2])
        self.assertEqual(types(ev)[-1], P.EV_RACE_END)
        rows = g.last_results
        self.assertEqual([r["player_id"] for r in rows], [1, 2])
        self.assertTrue(all(r["finished"] and not r["dnf"] for r in rows))
        self.assertGreater(rows[1]["finish_ms"], rows[0]["finish_ms"])

    def test_time_limit_marks_everyone_dnf(self):
        g = make_game(players=2, time_limit_s=1.0)
        start_race(g)
        for _ in range(70):
            g.step()
        self.assertEqual(g.state, P.ST_FINISHED)
        self.assertTrue(all(p.dnf for p in g.players.values()))
        self.assertTrue(all(s.status & P.STATUS_DNF for s in g.player_states()))

    def test_grace_period_after_the_first_finisher(self):
        g = make_game(players=2, laps=1, finish_grace_s=0.5, time_limit_s=0.0)
        clear_obstacles(g)
        start_race(g)
        p1, p2 = g.players[1], g.players[2]
        self.finish(g, p1)
        self.assertEqual(g.state, P.ST_RACING)
        for _ in range(20):
            g.step()
        self.assertEqual(g.state, P.ST_RACING)                # 0.33 s < 0.5 s
        for _ in range(30):
            g.step()
        self.assertEqual(g.state, P.ST_FINISHED)
        self.assertTrue(p2.dnf)
        self.assertFalse(p1.dnf)

    def test_a_leaving_player_lets_the_race_end(self):
        g = make_game(players=2, laps=1)
        clear_obstacles(g)
        start_race(g)
        self.finish(g, g.players[1])
        self.assertEqual(g.state, P.ST_RACING)
        g.remove_player(2)
        g.step()
        self.assertEqual(g.state, P.ST_FINISHED)

    def test_standings_order(self):
        g = make_game(players=3, laps=1)
        clear_obstacles(g)
        start_race(g)
        p1, p2, p3 = (g.players[i] for i in (1, 2, 3))
        p3.started = True
        put(g, p3, 300.0)
        p2.started = True
        put(g, p2, 900.0)
        self.assertEqual([p.id for p in g.standings()], [2, 3, 1])   # unstarted car is last
        self.finish(g, p3)
        self.assertEqual([p.id for p in g.standings()], [3, 2, 1])
        self.assertLess(g.remaining_units(p2), g.remaining_units(p1))

    def test_results_screen_returns_to_the_lobby(self):
        g = make_game(players=1, time_limit_s=0.5, results_s=0.5)
        start_race(g)
        for _ in range(40):
            g.step()
        self.assertEqual(g.state, P.ST_FINISHED)
        self.assertAlmostEqual(g.countdown_ms(), 500 - g.state_ticks * 1000 / 60.0, delta=1.0)
        for _ in range(40):
            g.step()
        self.assertEqual(g.state, P.ST_LOBBY)
        p = g.players[1]
        self.assertEqual((p.health, p.laps_done, p.cp, p.ready, p.dnf), (START_HEALTH, 0, 0, False, False))
        self.assertTrue(g.last_results)                       # kept for the server's logs
        self.assertTrue(g.begin_countdown())                  # and a rematch can start


# ------------------------------------------------------------------ end to end
class IntegrationTests(unittest.TestCase):
    def test_player_states_survive_the_wire(self):
        g = make_game(players=3, shapes=[0, 1, 2])
        start_race(g)
        for i, p in enumerate(g.players.values()):
            p.throttle = 1.0
            p.steer = 0.1 * i
        for _ in range(45):
            g.step()
        states = g.player_states()
        blob = P.pack_players(states)
        data = P.pack_snapshot_parts(g.tick, g.state, 0, 7, g.countdown_ms(), g.race_ms,
                                     len(states), blob, [])
        snap = P.unpack_snapshot(data)
        self.assertEqual((snap.state, snap.roster_ver, len(snap.players)), (P.ST_RACING, 7, 3))
        for a, b in zip(states, snap.players):
            self.assertEqual((a.id, a.health, a.lap, a.cp, a.status, a.place),
                             (b.id, b.health, b.lap, b.cp, b.status, b.place))
            self.assertAlmostEqual(a.x, b.x, delta=1e-3 * max(1.0, abs(a.x)))
            self.assertAlmostEqual(a.y, b.y, delta=1e-3 * max(1.0, abs(a.y)))
            self.assertAlmostEqual(a.speed, b.speed, delta=0.06)
            d = abs((a.heading - b.heading + math.pi) % math.tau - math.pi)
            self.assertLess(d, math.tau / 65536.0 + 1e-9)

    def test_same_inputs_give_identical_races(self):
        def run():
            g = make_game(players=3, shapes=[0, 1, 2])
            start_race(g)
            for tick in range(900):
                for pid in g.players:
                    g.set_input(pid, 1.0, 1.0 if (tick // 45 + pid) % 2 else -1.0)
                g.step()
            return ([(p.x, p.y, p.heading, p.speed, p.health, p.hits) for p in g.players.values()],
                    g.drain_events())
        self.assertEqual(run(), run())

    def test_autopilot_bots_finish_a_race(self):
        g = make_game(players=3, shapes=[0, 1, 2], laps=1, countdown_s=0.2)
        pilots = {pid: Autopilot(g.track, skill=1.0 - 0.1 * pid) for pid in g.players}
        self.assertTrue(g.begin_countdown())
        for _ in range(60 * 150):
            if g.state == P.ST_FINISHED:
                break
            if g.state == P.ST_RACING:
                for pid, p in g.players.items():
                    g.set_input(pid, *pilots[pid].control(p.x, p.y, p.heading, p.speed, g.race_time))
            g.step()
        self.assertEqual(g.state, P.ST_FINISHED)
        self.assertEqual(sorted(p.place for p in g.players.values()), [1, 2, 3])
        self.assertTrue(all(p.finished for p in g.players.values()))
        self.assertEqual(g.races_completed, 1)


if __name__ == "__main__":
    unittest.main()
