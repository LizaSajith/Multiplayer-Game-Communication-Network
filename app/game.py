"""game.py - authoritative race simulation (no sockets, fully unit-testable).

The server owns one :class:`Game`.  Clients never simulate: they send controls
and render the snapshots.  Everything is advanced in fixed steps of
``1 / tick_rate`` seconds by :meth:`Game.step`, so results are reproducible.

Game rules
  * Objects are a circle, square or triangle (cosmetic shape + slightly
    different handling).  Controls: throttle -1..1, steer -1..1 (+1 = right).
  * Walls keep cars on the track (they only slow you down).  Obstacles damage:
    static ones 15 HP, sweeping ones 20 HP, with 0.8 s of invulnerability.
  * At 0 HP the car is wrecked; after 3 s it respawns at its last checkpoint
    with full health (``respawn=False`` eliminates it instead).
  * Race = ``laps`` laps.  Checkpoints must be passed in order, the finish line
    only counts once all checkpoints of the lap were passed (no shortcuts), and
    crossing a gate backwards undoes it.  Players start *behind* the line; the
    first crossing starts lap 1.
  * Cars pass through each other (ghost collisions) so a lost or late packet
    can never cause an unfair crash.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from . import protocol as P
from .track import KIND_CIRCLE, Track, build_track

PLAYER_RADIUS = 14.0
START_HEALTH = 100
HIT_COOLDOWN = 0.8            # s of invulnerability after an obstacle hit
HIT_SLOWDOWN = 0.40           # speed multiplier on a damaging hit
WRECK_TIME = 3.0              # s frozen before respawning
RESPAWN_INVULN = 2.0
WALL_DRAG = 0.60              # fraction of speed lost hitting a wall head-on
BRAKE = 520.0
COAST_DECEL = 110.0
REVERSE_MAX = 90.0


@dataclass(frozen=True)
class ShapeStats:
    max_speed: float          # units / s
    accel: float              # units / s^2
    turn_rate: float          # rad / s at full steering


SHAPE_STATS = {
    P.SHAPE_CIRCLE: ShapeStats(300.0, 260.0, 3.2),      # balanced
    P.SHAPE_SQUARE: ShapeStats(320.0, 220.0, 2.7),      # fast but sluggish
    P.SHAPE_TRIANGLE: ShapeStats(285.0, 300.0, 3.6),    # nimble
}


@dataclass
class GameConfig:
    laps: int = 3
    max_players: int = 16
    tick_rate: int = 60
    snapshot_rate: int = 20
    track_id: int = 0
    track_seed: int = 17
    respawn: bool = True
    countdown_s: float = 3.0
    time_limit_s: float = 180.0       # 0 = unlimited
    finish_grace_s: float = 20.0      # others get this long after the first finisher
    results_s: float = 8.0            # how long the results screen stays up

    def validate(self) -> None:
        if not 1 <= self.laps <= 50:
            raise ValueError("laps must be 1..50")
        if not 1 <= self.max_players <= P.MAX_PLAYERS_HARD:
            raise ValueError("max_players must be 1..%d" % P.MAX_PLAYERS_HARD)
        if not 10 <= self.tick_rate <= 240:
            raise ValueError("tick_rate must be 10..240")
        if not 1 <= self.snapshot_rate <= self.tick_rate:
            raise ValueError("snapshot_rate must be 1..tick_rate")


class Player:
    def __init__(self, pid: int, name: str, shape: int, slot: int) -> None:
        self.id, self.name, self.shape, self.slot = pid, name, shape, slot
        self.ready = False
        self.throttle = 0.0
        self.steer = 0.0
        self.reset_run()

    def reset_run(self) -> None:
        self.x = self.y = self.heading = self.speed = 0.0
        self.seg = 0
        self.s = 0.0
        self.lateral = 0.0
        self.health = START_HEALTH
        self.started = False
        self.laps_done = 0
        self.cp = 0
        self.cp_reported = 0
        self.laps_reported = 0
        self.progress = 0.0
        self.invuln_until = 0.0
        self.wrecked = False
        self.wreck_at = 0.0
        self.finished = False
        self.place = 0
        self.finish_ms = 0
        self.dnf = False
        self.hits = 0
        self.wrecks = 0
        self.lap_start_ms = 0
        self.best_lap_ms = 0
        self.grid_index = 0
        self.throttle = 0.0
        self.steer = 0.0


def _wrap_delta(d: float, length: float) -> float:
    """Wrap a position difference on the loop into [-length/2, length/2)."""
    return (d + length / 2.0) % length - length / 2.0


class Game:
    def __init__(self, cfg: Optional[GameConfig] = None, track: Optional[Track] = None) -> None:
        self.cfg = cfg or GameConfig()
        self.cfg.validate()
        self.track = track or build_track(self.cfg.track_id, self.cfg.track_seed)
        self.dt = 1.0 / self.cfg.tick_rate
        self.state = P.ST_LOBBY
        self.tick = 0
        self.state_ticks = 0
        self.race_ticks = 0
        self.players: Dict[int, Player] = {}
        self._events: List[Tuple[int, int, int, int]] = []
        self.finished_count = 0
        self.first_finish_ticks: Optional[int] = None
        self.races_completed = 0
        self.last_results: List[dict] = []

    # ------------------------------------------------------------- bookkeeping
    @property
    def race_time(self) -> float:
        return self.race_ticks * self.dt

    @property
    def race_ms(self) -> int:
        return int(self.race_ticks * 1000 / self.cfg.tick_rate)

    def emit(self, etype: int, pid: int, a: int = 0, b: int = 0) -> None:
        self._events.append((etype, pid, a, b))

    def drain_events(self) -> List[Tuple[int, int, int, int]]:
        ev, self._events = self._events, []
        return ev

    def can_join(self) -> Tuple[bool, int]:
        if self.state != P.ST_LOBBY:
            return False, P.REJ_IN_PROGRESS
        if len(self.players) >= self.cfg.max_players:
            return False, P.REJ_FULL
        return True, 0

    def add_player(self, pid: int, name: str, shape: int, slot: int) -> Player:
        ok, reason = self.can_join()
        if not ok:
            raise RuntimeError("cannot join (reason %d)" % reason)
        p = Player(pid, name, shape if shape in SHAPE_STATS else P.SHAPE_CIRCLE, slot)
        self.players[pid] = p
        self.emit(P.EV_PLAYER_JOIN, pid)
        self._assign_grid()
        return p

    def remove_player(self, pid: int, reason: int = P.DISC_TIMEOUT) -> None:
        if self.players.pop(pid, None) is None:
            return
        self.emit(P.EV_PLAYER_LEAVE, pid, reason)
        if self.state == P.ST_LOBBY:
            self._assign_grid()
        elif not self.players:
            self._end_race() if self.state == P.ST_RACING else self.reset_to_lobby()

    def set_input(self, pid: int, throttle: float, steer: float) -> None:
        p = self.players.get(pid)
        if p is None:
            return
        p.throttle = max(-1.0, min(1.0, throttle)) if math.isfinite(throttle) else 0.0
        p.steer = max(-1.0, min(1.0, steer)) if math.isfinite(steer) else 0.0

    def set_ready(self, pid: int, ready: bool) -> None:
        if pid in self.players and self.state == P.ST_LOBBY:
            self.players[pid].ready = ready

    def set_shape(self, pid: int, shape: int) -> None:
        if pid in self.players and self.state == P.ST_LOBBY and shape in SHAPE_STATS:
            self.players[pid].shape = shape

    # ------------------------------------------------------------- lifecycle
    def _assign_grid(self) -> None:
        """Park every car on its start-grid slot (lowest player id = pole)."""
        for index, pid in enumerate(sorted(self.players)):
            p = self.players[pid]
            x, y, hdg, s, lat = self.track.grid_slot(index)
            p.grid_index = index
            self._place(p, x, y, hdg)

    def _place(self, p: Player, x: float, y: float, heading: float) -> None:
        """Teleport (spawn / respawn): no gate crossings are generated."""
        p.x, p.y, p.heading, p.speed = x, y, heading % math.tau, 0.0
        loc = self.track.locate(x, y)
        p.seg, p.s, p.lateral = loc.seg, loc.s, loc.lateral
        self._update_progress(p)

    def begin_countdown(self) -> bool:
        if self.state != P.ST_LOBBY or not self.players:
            return False
        for p in self.players.values():
            p.reset_run()
            p.ready = False
        self._assign_grid()
        self.state, self.state_ticks = P.ST_COUNTDOWN, 0
        return True

    def reset_to_lobby(self) -> None:
        self.state, self.state_ticks, self.race_ticks = P.ST_LOBBY, 0, 0
        self.finished_count, self.first_finish_ticks = 0, None
        for p in self.players.values():
            p.reset_run()
            p.ready = False
        self._assign_grid()

    def _start_race(self) -> None:
        self.state, self.state_ticks, self.race_ticks = P.ST_RACING, 0, 0
        self.finished_count, self.first_finish_ticks = 0, None
        self.emit(P.EV_RACE_START, 0)

    def _end_race(self) -> None:
        for p in self.players.values():
            if not p.finished:
                p.dnf = True
        self.last_results = self.results()
        self.races_completed += 1
        self.state, self.state_ticks = P.ST_FINISHED, 0
        self.emit(P.EV_RACE_END, 0)

    def countdown_ms(self) -> int:
        """Milliseconds left in COUNTDOWN (before GO) or FINISHED (before lobby)."""
        if self.state == P.ST_COUNTDOWN:
            left = self.cfg.countdown_s - self.state_ticks * self.dt
        elif self.state == P.ST_FINISHED:
            left = self.cfg.results_s - self.state_ticks * self.dt
        else:
            return 0
        return max(0, int(left * 1000))

    # ------------------------------------------------------------- simulation
    def step(self) -> None:
        self.tick += 1
        self.state_ticks += 1
        if self.state == P.ST_COUNTDOWN:
            if self.state_ticks >= self.cfg.countdown_s * self.cfg.tick_rate:
                self._start_race()
        elif self.state == P.ST_RACING:
            self.race_ticks += 1
            for pid in sorted(self.players):
                self._step_player(self.players[pid])
            self._check_race_end()
        elif self.state == P.ST_FINISHED:
            if self.state_ticks >= self.cfg.results_s * self.cfg.tick_rate:
                self.reset_to_lobby()

    def _check_race_end(self) -> None:
        ps = self.players.values()
        if not ps:
            return self._end_race()
        done = all(p.finished or (p.wrecked and not self.cfg.respawn) for p in ps)
        timeout = 0 < self.cfg.time_limit_s <= self.race_time
        grace = (self.first_finish_ticks is not None and
                 (self.race_ticks - self.first_finish_ticks) * self.dt >= self.cfg.finish_grace_s)
        if done or timeout or grace:
            self._end_race()

    def _step_player(self, p: Player) -> None:
        dt, t = self.dt, self.race_time
        if p.wrecked:
            if self.cfg.respawn and t - p.wreck_at >= WRECK_TIME:
                self._respawn(p)
            return
        st = SHAPE_STATS[p.shape]
        thr, steer = (0.0, 0.0) if p.finished else (p.throttle, p.steer)
        # --- longitudinal
        if p.finished:
            p.speed = max(0.0, p.speed - BRAKE * 0.5 * dt)
        elif thr > 0.0:
            acc = BRAKE if p.speed < 0.0 else st.accel
            p.speed = min(st.max_speed, p.speed + acc * thr * dt)
        elif thr < 0.0:
            if p.speed > 0.0:
                p.speed = max(0.0, p.speed + BRAKE * thr * dt)
            else:
                p.speed = max(-REVERSE_MAX, p.speed + st.accel * 0.5 * thr * dt)
        else:
            dec = min(abs(p.speed), COAST_DECEL * dt)
            p.speed -= math.copysign(dec, p.speed) if p.speed else 0.0
        # --- steering (inverted while reversing, weaker at very low speed)
        factor = min(1.0, 0.30 + abs(p.speed) / 150.0)
        p.heading = (p.heading + steer * st.turn_rate * factor * dt *
                     (1.0 if p.speed >= 0.0 else -1.0)) % math.tau
        p.x += math.cos(p.heading) * p.speed * dt
        p.y += math.sin(p.heading) * p.speed * dt
        # --- collisions
        self._collide_walls(p, apply_drag=True)
        if not p.finished:
            self._collide_obstacles(p, t)
        # --- gates / progress
        loc = self.track.locate(p.x, p.y, p.seg)
        p.seg, p.lateral = loc.seg, loc.lateral
        d = _wrap_delta(loc.s - p.s, self.track.length)
        if d != 0.0 and not p.finished:
            self._cross_gates(p, p.s, d)
        p.s = loc.s
        self._update_progress(p)

    def _collide_walls(self, p: Player, apply_drag: bool) -> None:
        loc = self.track.locate(p.x, p.y, p.seg)
        limit = self.track.half_width - PLAYER_RADIUS
        if loc.dist <= limit:
            return
        p.x = loc.qx + loc.nx * limit
        p.y = loc.qy + loc.ny * limit
        if apply_drag and p.speed != 0.0:
            sgn = 1.0 if p.speed > 0 else -1.0
            into = (math.cos(p.heading) * loc.nx + math.sin(p.heading) * loc.ny) * sgn
            if into > 0.0:
                p.speed *= 1.0 - WALL_DRAG * into

    def _collide_obstacles(self, p: Player, t: float) -> None:
        loc = self.track.locate(p.x, p.y, p.seg)
        R = PLAYER_RADIUS
        for ob in self.track.obstacles_near(loc.s, 90.0):
            dx, dy = p.x - ob.ax, p.y - ob.ay
            u = dx * ob.tx + dy * ob.ty
            v = dx * ob.nx + dy * ob.ny
            vc = ob.lateral(t)
            if ob.kind == KIND_CIRCLE:
                du, dv = u, v - vc
                d = math.sqrt(du * du + dv * dv)
                pen = ob.rx + R - d
                if pen <= 0.0:
                    continue
                nu, nv = (du / d, dv / d) if d > 1e-9 else (0.0, 1.0)
            else:
                cu = max(-ob.rx, min(ob.rx, u))
                cv = max(vc - ob.ry, min(vc + ob.ry, v))
                du, dv = u - cu, v - cv
                d2 = du * du + dv * dv
                if d2 >= R * R:
                    continue
                if d2 > 1e-12:
                    d = math.sqrt(d2)
                    nu, nv, pen = du / d, dv / d, R - d
                else:                        # centre inside the box: leave through nearest face
                    faces = ((ob.rx - u, 1.0, 0.0), (u + ob.rx, -1.0, 0.0),
                             (vc + ob.ry - v, 0.0, 1.0), (v - (vc - ob.ry), 0.0, -1.0))
                    depth, nu, nv = min(faces, key=lambda f: f[0])
                    pen = depth + R
            p.x += (nu * ob.tx + nv * ob.nx) * pen
            p.y += (nu * ob.ty + nv * ob.ny) * pen
            if t >= p.invuln_until:
                p.health -= ob.damage
                p.hits += 1
                p.invuln_until = t + HIT_COOLDOWN
                p.speed *= HIT_SLOWDOWN
                self.emit(P.EV_HIT, p.id, ob.damage, ob.id)
                if p.health <= 0:
                    p.health = 0
                    p.wrecked = True
                    p.wreck_at = t
                    p.wrecks += 1
                    p.speed = 0.0
                    p.throttle = p.steer = 0.0
                    self.emit(P.EV_WRECK, p.id)
                    return
            else:
                p.speed *= 0.97               # grinding along an obstacle while invulnerable
        self._collide_walls(p, apply_drag=False)   # an obstacle may have pushed us into a wall

    def _respawn(self, p: Player) -> None:
        tr = self.track
        if p.cp > 0:
            s = tr.checkpoints[p.cp - 1] + 25.0
        elif p.started:
            s = 40.0
        else:
            x, y, hdg, _, _ = tr.grid_slot(p.grid_index)
            self._place(p, x, y, hdg)
            s = None
        if s is not None:
            x, y, hdg = tr.point_at(s, 0.0)
            self._place(p, x, y, hdg)
        p.health = START_HEALTH
        p.wrecked = False
        p.invuln_until = self.race_time + RESPAWN_INVULN
        self.emit(P.EV_RESPAWN, p.id)

    # ------------------------------------------------------------- laps & gates
    def _cross_gates(self, p: Player, s0: float, d: float) -> None:
        """Handle every gate crossed while moving from s0 by d along the loop."""
        L = self.track.length
        crossed = []
        gates = [(0, 0.0)] + [(k + 1, c) for k, c in enumerate(self.track.checkpoints)]
        for index, pos in gates:
            if d > 0.0:
                fwd = (pos - s0) % L
                if 0.0 < fwd <= d:
                    crossed.append((fwd, index))
            else:
                back = (s0 - pos) % L
                if back < -d:
                    crossed.append((back, index))
        crossed.sort()
        for _, index in crossed:
            (self._gate_forward if d > 0.0 else self._gate_backward)(p, index)

    def _gate_forward(self, p: Player, index: int) -> None:
        m = len(self.track.checkpoints)
        ms = self.race_ms
        if index == 0:                                   # finish / start line
            if not p.started:
                p.started = True
                p.cp = 0
                p.lap_start_ms = ms
            elif p.cp == m:
                p.laps_done += 1
                p.cp = 0
                if p.laps_done > p.laps_reported:
                    p.laps_reported = p.laps_done
                    p.cp_reported = 0
                    lap_ms = ms - p.lap_start_ms
                    p.lap_start_ms = ms
                    if p.best_lap_ms == 0 or lap_ms < p.best_lap_ms:
                        p.best_lap_ms = lap_ms
                    self.emit(P.EV_LAP, p.id, p.laps_done, lap_ms)
                if p.laps_done >= self.cfg.laps:
                    self._finish(p)
        elif p.started and p.cp == index - 1:
            p.cp = index
            if index > p.cp_reported:
                p.cp_reported = index
                self.emit(P.EV_CHECKPOINT, p.id, index, ms)

    def _gate_backward(self, p: Player, index: int) -> None:
        m = len(self.track.checkpoints)
        if index == 0:
            if p.started and p.cp == 0:
                if p.laps_done == 0:
                    p.started = False
                else:
                    p.laps_done -= 1
                    p.cp = m
        elif p.cp == index:
            p.cp = index - 1

    def _finish(self, p: Player) -> None:
        p.finished = True
        self.finished_count += 1
        p.place = self.finished_count
        p.finish_ms = self.race_ms
        p.throttle = p.steer = 0.0
        if self.first_finish_ticks is None:
            self.first_finish_ticks = self.race_ticks
        self.emit(P.EV_FINISH, p.id, p.place, p.finish_ms)

    def _update_progress(self, p: Player) -> None:
        L = self.track.length
        if p.finished:
            p.progress = self.cfg.laps * L
        elif p.started:
            p.progress = p.laps_done * L + p.s
        else:
            p.progress = p.s - L

    # ------------------------------------------------------------- views
    def standings(self) -> List[Player]:
        """Live ranking: finishers by place, then everyone else by distance."""
        return sorted(self.players.values(),
                      key=lambda p: (0, p.place, p.id) if p.finished else (1, -p.progress, p.id))

    def player_states(self) -> List[P.PlayerState]:
        out = []
        t = self.race_time
        for pid in sorted(self.players):
            p = self.players[pid]
            st = 0
            if p.started:
                st |= P.STATUS_STARTED
            if p.wrecked:
                st |= P.STATUS_WRECKED
            if p.finished:
                st |= P.STATUS_FINISHED
            if p.dnf:
                st |= P.STATUS_DNF
            if self.state == P.ST_RACING and t < p.invuln_until:
                st |= P.STATUS_INVULN
            out.append(P.PlayerState(pid, p.x, p.y, p.heading, p.speed, max(0, p.health),
                                     p.laps_done, p.cp, st, p.place, p.progress))
        return out

    def remaining_units(self, p: Player) -> float:
        return max(0.0, self.cfg.laps * self.track.length - p.progress)

    def results(self) -> List[dict]:
        rows = []
        for p in self.standings():
            rows.append({"player_id": p.id, "name": p.name, "shape": P.SHAPE_NAMES[p.shape],
                         "place": p.place, "finished": p.finished, "finish_ms": p.finish_ms,
                         "laps_done": p.laps_done, "checkpoints": p.cp,
                         "progress_m": round(p.progress / 10.0, 1), "health": max(0, p.health),
                         "hits": p.hits, "wrecks": p.wrecks, "best_lap_ms": p.best_lap_ms,
                         "dnf": p.dnf})
        return rows
