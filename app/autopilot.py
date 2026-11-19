"""autopilot.py - reference AI driver (used by tests, demos and Role 3's bots).

``Autopilot.control(x, y, heading, speed, race_time)`` returns ``(throttle,
steer)`` for the shape's current state.  It follows the centerline with a pure
pursuit lookahead, picks a lateral lane that avoids upcoming obstacles (moving
ones are predicted at their arrival time), slows down for bends and backs out
if it gets stuck.  ``skill`` (0.5..1.0) scales speed so bots can differ.
"""
from __future__ import annotations

import math
from typing import Optional, Tuple

from .game import PLAYER_RADIUS
from .track import KIND_CIRCLE, Track

_CANDIDATES = [c * 10.0 for c in range(-8, 9)]       # lateral lanes -80..80
_MARGIN = 8.0


def _wrap_angle(a: float) -> float:
    return (a + math.pi) % math.tau - math.pi


class Autopilot:
    def __init__(self, track: Track, max_speed: float = 300.0, skill: float = 1.0) -> None:
        self.track = track
        self.max_speed = max_speed
        self.skill = max(0.3, min(1.0, skill))
        self._hint: Optional[int] = None
        self._lane = 0.0
        self._stuck = 0.0
        self._reverse_left = 0.0
        self._last_t: Optional[float] = None

    def reset(self) -> None:
        self._hint = None
        self._lane = 0.0
        self._stuck = self._reverse_left = 0.0
        self._last_t = None

    def _blocked(self, lane: float, s: float, speed: float, t: float, lookahead: float) -> bool:
        for ob in self.track.obstacles_near(s + lookahead / 2.0, lookahead / 2.0 + 30.0):
            ds = (ob.s - s) % self.track.length
            if ds > lookahead + 30.0:
                continue
            arrive = t + ds / max(speed, 120.0)
            v = ob.lateral(arrive)
            half = (ob.rx if ob.kind == KIND_CIRCLE else ob.ry) + PLAYER_RADIUS + _MARGIN
            if ob.kind == KIND_CIRCLE and ob.moving:
                half += 6.0
            if abs(lane - v) < half:
                return True
        return False

    def control(self, x: float, y: float, heading: float, speed: float,
                race_time: float) -> Tuple[float, float]:
        tr = self.track
        loc = tr.locate(x, y, self._hint)
        self._hint = loc.seg
        dt = 0.0 if self._last_t is None else max(0.0, min(0.2, race_time - self._last_t))
        self._last_t = race_time

        look = max(70.0, min(230.0, 60.0 + speed * 0.5))
        # --- choose a lane for the lookahead point
        best, best_cost = self._lane, 1e9
        for lane in _CANDIDATES:
            cost = abs(lane - loc.lateral) * 1.0 + abs(lane) * 0.3 + abs(lane - self._lane) * 0.5
            if self._blocked(lane, loc.s, speed, race_time, look):
                cost += 1000.0
            if cost < best_cost:
                best, best_cost = lane, cost
        self._lane = best
        tx, ty, _ = tr.point_at(loc.s + look, self._lane)
        err = _wrap_angle(math.atan2(ty - y, tx - x) - heading)
        steer = max(-1.0, min(1.0, err * 2.4))

        # --- speed: slow down for bends and when pointing the wrong way
        _, _, h_now = tr.point_at(loc.s)
        _, _, h_far = tr.point_at(loc.s + 140.0)
        bend = abs(_wrap_angle(h_far - h_now))
        target = self.max_speed * self.skill * (1.0 - 0.45 * min(1.0, bend / 0.9))
        target *= 1.0 - 0.5 * min(1.0, abs(err) / 1.0)
        if speed > target + 25.0:
            throttle = -0.6
        elif speed > target:
            throttle = 0.0
        else:
            throttle = 1.0

        # --- stuck recovery: reverse for a moment, steering the other way
        if self._reverse_left > 0.0:
            self._reverse_left -= dt
            return -1.0, -steer
        self._stuck = self._stuck + dt if abs(speed) < 12.0 and race_time > 1.0 else 0.0
        if self._stuck > 1.2:
            self._stuck = 0.0
            self._reverse_left = 0.7
        return throttle, steer
