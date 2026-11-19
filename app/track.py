"""track.py - the race circuit: geometry, checkpoints, start grid and obstacles.

The server simulates on this track and every client builds the *same* track
locally (nothing but ``track_id``/``seed``/``track_hash`` is sent over UDP), so
the generation uses only deterministic arithmetic (+ - * / sqrt and an integer
PRNG).  ``Track.hash`` lets a client verify it rebuilt an identical track.

Coordinates: world units, x right, y down (screen convention).  10 units = 1 m.
A track position is described by ``s`` (distance along the centerline, 0..length,
s = 0 is the start/finish line) and ``lateral`` (signed distance from the
centerline along the left normal N = (-ty, tx)).
"""
from __future__ import annotations

import math
import zlib
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from typing import List, Optional, Tuple

UNITS_PER_METER = 10.0
HALF_WIDTH = 110.0            # track is 220 units (22 m) wide
SAMPLE_SPACING = 10.0         # centerline polyline resolution
SEARCH_WINDOW = 14            # segments searched either side of the hint
GRID_COLUMNS = 3
GRID_ROW_SPACING = 40.0
GRID_FIRST_ROW = 50.0         # first row is this far behind the start line

TRACK_CIRCUIT = 0
TRACK_NAMES = {TRACK_CIRCUIT: "circuit"}

# Control points of the closed circuit (travel order, start/finish on the first
# point, first straight heads east).  Hand designed, verified by tools/track_report.py.
_CIRCUIT_POINTS = [
    (0, 520), (520, 520), (980, 490), (1330, 340), (1480, 60),
    (1380, -250), (1050, -440), (650, -380), (350, -520), (-80, -520),
    (-500, -400), (-760, -140), (-800, 130), (-700, 345), (-480, 430),
    (-280, 490), (-120, 520),
]
_CIRCUIT_CHECKPOINT_FRACTIONS = (0.17, 0.38, 0.60, 0.80)


class Rng:
    """Tiny xorshift32 PRNG: identical output on every platform / Python version."""

    def __init__(self, seed: int) -> None:
        self.s = ((seed * 2654435761 + 12345) & 0xFFFFFFFF) or 1

    def next_u32(self) -> int:
        x = self.s
        x ^= (x << 13) & 0xFFFFFFFF
        x ^= x >> 17
        x ^= (x << 5) & 0xFFFFFFFF
        self.s = x
        return x

    def rand(self) -> float:
        return self.next_u32() / 4294967296.0

    def uniform(self, a: float, b: float) -> float:
        return a + (b - a) * self.rand()


KIND_CIRCLE, KIND_RECT = 0, 1


@dataclass
class Obstacle:
    id: int
    kind: int
    s: float              # anchor position along the centerline
    v0: float             # lateral centre (rest position)
    rx: float             # half extent along the track (circle: radius)
    ry: float             # half extent across the track (circle: radius)
    amp: float = 0.0      # lateral oscillation amplitude (0 = static)
    period: float = 4.0   # seconds
    phase: float = 0.0
    damage: int = 15
    # anchor frame, filled in by Track
    ax: float = 0.0
    ay: float = 0.0
    tx: float = 1.0
    ty: float = 0.0
    nx: float = 0.0
    ny: float = 1.0

    @property
    def moving(self) -> bool:
        return self.amp > 0.0

    def lateral(self, t: float) -> float:
        if self.amp <= 0.0:
            return self.v0
        return self.v0 + self.amp * math.sin(math.tau * t / self.period + self.phase)

    def center(self, t: float) -> Tuple[float, float]:
        v = self.lateral(t)
        return self.ax + self.nx * v, self.ay + self.ny * v


@dataclass
class Location:
    seg: int
    s: float
    lateral: float
    dist: float                  # distance to the centerline
    qx: float                    # nearest centerline point
    qy: float
    nx: float                    # unit vector centerline -> point (outward)
    ny: float


class Track:
    def __init__(self, name: str, pts: List[Tuple[float, float]], half_width: float,
                 checkpoint_fractions, obstacle_seed: int) -> None:
        self.name = name
        self.half_width = half_width
        self.pts = pts
        n = self.n = len(pts)
        self.seg_dx: List[float] = []
        self.seg_dy: List[float] = []
        self.seg_len: List[float] = []
        self.cum: List[float] = []
        total = 0.0
        for i in range(n):
            x0, y0 = pts[i]
            x1, y1 = pts[(i + 1) % n]
            dx, dy = x1 - x0, y1 - y0
            ln = math.sqrt(dx * dx + dy * dy)
            self.cum.append(total)
            self.seg_len.append(ln)
            self.seg_dx.append(dx / ln)
            self.seg_dy.append(dy / ln)
            total += ln
        self.length = total
        # vertex tangents / normals (central difference) for drawing smooth edges
        self.tan: List[Tuple[float, float]] = []
        for i in range(n):
            ax, ay = self.seg_dx[i - 1], self.seg_dy[i - 1]
            bx, by = self.seg_dx[i], self.seg_dy[i]
            tx, ty = ax + bx, ay + by
            ln = math.sqrt(tx * tx + ty * ty) or 1.0
            self.tan.append((tx / ln, ty / ln))
        self.nrm = [(-ty, tx) for tx, ty in self.tan]
        self.checkpoints = [f * self.length for f in checkpoint_fractions]
        self.obstacles: List[Obstacle] = []
        self._build_obstacles(obstacle_seed)
        self._obstacle_s = [o.s for o in self.obstacles]
        self.hash = self._compute_hash()

    # ------------------------------------------------------------ geometry
    def _seg_at(self, s: float) -> Tuple[int, float]:
        s %= self.length
        i = bisect_right(self.cum, s) - 1
        i = max(0, min(self.n - 1, i))
        return i, (s - self.cum[i]) / self.seg_len[i]

    def point_at(self, s: float, lateral: float = 0.0) -> Tuple[float, float, float]:
        """World (x, y, heading) at track position (s, lateral)."""
        i, t = self._seg_at(s)
        x0, y0 = self.pts[i]
        x = x0 + self.seg_dx[i] * self.seg_len[i] * t
        y = y0 + self.seg_dy[i] * self.seg_len[i] * t
        nx, ny = -self.seg_dy[i], self.seg_dx[i]
        return x + nx * lateral, y + ny * lateral, math.atan2(self.seg_dy[i], self.seg_dx[i])

    def frame_at(self, s: float) -> Tuple[float, float, float, float, float, float]:
        """(x, y, tx, ty, nx, ny) of the centerline at s."""
        i, t = self._seg_at(s)
        x0, y0 = self.pts[i]
        x = x0 + self.seg_dx[i] * self.seg_len[i] * t
        y = y0 + self.seg_dy[i] * self.seg_len[i] * t
        return x, y, self.seg_dx[i], self.seg_dy[i], -self.seg_dy[i], self.seg_dx[i]

    def locate(self, x: float, y: float, hint: Optional[int] = None) -> Location:
        """Nearest centerline point to (x, y).

        With a ``hint`` (previous segment index) only a window of segments is
        searched, which keeps the mapping continuous even where two parts of
        the circuit pass close to each other.  Without a hint the whole loop is
        scanned (used once after spawning / respawning).
        """
        n = self.n
        if hint is None:
            cands = range(n)
        else:
            cands = [(hint + k) % n for k in range(-SEARCH_WINDOW, SEARCH_WINDOW + 1)]
        best_i, best_d2, best_t = 0, 1e30, 0.0
        pts, dxs, dys, lens = self.pts, self.seg_dx, self.seg_dy, self.seg_len
        for i in cands:
            x0, y0 = pts[i]
            px, py = x - x0, y - y0
            t = px * dxs[i] + py * dys[i]
            ln = lens[i]
            t = 0.0 if t < 0.0 else ln if t > ln else t
            qx, qy = px - dxs[i] * t, py - dys[i] * t
            d2 = qx * qx + qy * qy
            if d2 < best_d2:
                best_i, best_d2, best_t = i, d2, t
        i = best_i
        x0, y0 = pts[i]
        qx = x0 + dxs[i] * best_t
        qy = y0 + dys[i] * best_t
        dist = math.sqrt(best_d2)
        if dist > 1e-9:
            ox, oy = (x - qx) / dist, (y - qy) / dist
        else:
            ox, oy = -dys[i], dxs[i]
        side = ox * (-dys[i]) + oy * dxs[i]                  # >0: left of travel
        lateral = dist if side >= 0.0 else -dist
        return Location(i, (self.cum[i] + best_t) % self.length, lateral, dist, qx, qy, ox, oy)

    def meters(self, units: float) -> float:
        return units / UNITS_PER_METER

    # ------------------------------------------------------------ start grid
    def grid_slot(self, index: int) -> Tuple[float, float, float, float, float]:
        """Start position of the index-th car: (x, y, heading, s, lateral).

        Three columns, staggered rows, all *behind* the start line (s close to
        ``length``), so the first crossing of the line starts lap 1.
        """
        row, col = divmod(index, GRID_COLUMNS)
        lateral = (-1, 0, 1)[col] * 58.0
        s = self.length - GRID_FIRST_ROW - row * GRID_ROW_SPACING - (14.0 if col == 1 else 0.0)
        x, y, hdg = self.point_at(s, lateral)
        return x, y, hdg, s, lateral

    def grid_depth(self, players: int) -> float:
        rows = (players + GRID_COLUMNS - 1) // GRID_COLUMNS
        return GRID_FIRST_ROW + rows * GRID_ROW_SPACING + 14.0

    # ------------------------------------------------------------ obstacles
    def _build_obstacles(self, seed: int) -> None:
        rng = Rng(seed)
        L = self.length
        hw = self.half_width
        keep_out = [(L - 330.0, L + 10.0), (-10.0, 330.0)]
        for c in self.checkpoints:
            keep_out.append((c - 110.0, c + 110.0))

        def blocked(s: float) -> bool:
            return any(a <= s <= b for a, b in keep_out)

        s = 420.0
        oid = 0
        while s < L - 330.0:
            if blocked(s):
                s += 40.0
                continue
            r = rng.rand()
            if r < 0.45:                                           # one static obstacle
                if rng.rand() < 0.5:
                    rad = rng.uniform(16.0, 26.0)
                    v0 = rng.uniform(-hw + rad + 8.0, hw - rad - 8.0)
                    self._add(oid, KIND_CIRCLE, s, v0, rad, rad)
                else:
                    rx, ry = rng.uniform(14.0, 22.0), rng.uniform(26.0, 42.0)
                    v0 = rng.uniform(-hw + ry + 8.0, hw - ry - 8.0)
                    self._add(oid, KIND_RECT, s, v0, rx, ry)
                oid += 1
            elif r < 0.70:                                         # pair with a gap
                gap = rng.uniform(86.0, 110.0)
                centre = rng.uniform(-45.0, 45.0)
                left_edge, right_edge = centre + gap / 2.0, centre - gap / 2.0
                for edge, sign in ((left_edge, 1.0), (right_edge, -1.0)):
                    ry = (hw - abs(edge)) / 2.0
                    if ry < 8.0:
                        continue
                    self._add(oid, KIND_RECT, s, edge + sign * ry, rng.uniform(14.0, 20.0), ry)
                    oid += 1
            else:                                                  # sweeping obstacle
                rad = rng.uniform(15.0, 20.0)
                amp = rng.uniform(40.0, 62.0)
                self._add(oid, KIND_CIRCLE, s, 0.0, rad, rad, amp=amp,
                          period=rng.uniform(3.0, 5.0), phase=rng.uniform(0.0, math.tau),
                          damage=20)
                oid += 1
            s += rng.uniform(150.0, 260.0)

    def _add(self, oid, kind, s, v0, rx, ry, amp=0.0, period=4.0, phase=0.0, damage=15):
        x, y, tx, ty, nx, ny = self.frame_at(s)
        self.obstacles.append(Obstacle(oid, kind, s, v0, rx, ry, amp, period, phase, damage,
                                       x, y, tx, ty, nx, ny))

    def obstacles_near(self, s: float, window: float = 80.0) -> List[Obstacle]:
        lo = bisect_left(self._obstacle_s, s - window)
        hi = bisect_right(self._obstacle_s, s + window)
        return self.obstacles[lo:hi]

    # ------------------------------------------------------------ integrity
    def _compute_hash(self) -> int:
        parts = ["%d" % self.n, "%.1f" % self.length, "%.1f" % self.half_width]
        parts += ["%.1f,%.1f" % p for p in self.pts[::7]]
        parts += ["%.1f" % c for c in self.checkpoints]
        for o in self.obstacles:
            parts.append("%d,%d,%.1f,%.1f,%.1f,%.1f,%.1f,%.2f,%.2f,%d" % (
                o.id, o.kind, o.s, o.v0, o.rx, o.ry, o.amp, o.period, o.phase, o.damage))
        return zlib.crc32("|".join(parts).encode("ascii")) & 0xFFFFFFFF


def _catmull_rom_closed(ctrl: List[Tuple[float, float]], per_span: int = 60):
    """Dense closed Catmull-Rom spline through the control points."""
    n = len(ctrl)
    out = []
    for i in range(n):
        p0, p1, p2, p3 = ctrl[(i - 1) % n], ctrl[i], ctrl[(i + 1) % n], ctrl[(i + 2) % n]
        for k in range(per_span):
            t = k / per_span
            t2, t3 = t * t, t * t * t
            x = 0.5 * (2 * p1[0] + (-p0[0] + p2[0]) * t + (2 * p0[0] - 5 * p1[0] + 4 * p2[0] - p3[0]) * t2
                       + (-p0[0] + 3 * p1[0] - 3 * p2[0] + p3[0]) * t3)
            y = 0.5 * (2 * p1[1] + (-p0[1] + p2[1]) * t + (2 * p0[1] - 5 * p1[1] + 4 * p2[1] - p3[1]) * t2
                       + (-p0[1] + 3 * p1[1] - 3 * p2[1] + p3[1]) * t3)
            out.append((x, y))
    return out


def _resample(dense: List[Tuple[float, float]], spacing: float) -> List[Tuple[float, float]]:
    """Resample a closed polyline at (almost exactly) constant arc spacing."""
    m = len(dense)
    cum = [0.0]
    for i in range(m):
        x0, y0 = dense[i]
        x1, y1 = dense[(i + 1) % m]
        cum.append(cum[-1] + math.sqrt((x1 - x0) ** 2 + (y1 - y0) ** 2))
    total = cum[-1]
    count = int(round(total / spacing))
    pts = []
    j = 0
    for k in range(count):
        target = total * k / count
        while cum[j + 1] < target:
            j += 1
        seg = cum[j + 1] - cum[j]
        t = (target - cum[j]) / seg if seg > 0 else 0.0
        x0, y0 = dense[j]
        x1, y1 = dense[(j + 1) % m]
        pts.append((x0 + (x1 - x0) * t, y0 + (y1 - y0) * t))
    return pts


def build_track(track_id: int = TRACK_CIRCUIT, seed: int = 17) -> Track:
    """Build a track by id; client and server call this with the same arguments."""
    if track_id != TRACK_CIRCUIT:
        raise ValueError("unknown track id %r" % track_id)
    dense = _catmull_rom_closed([(float(x), float(y)) for x, y in _CIRCUIT_POINTS])
    pts = _resample(dense, SAMPLE_SPACING)
    return Track(TRACK_NAMES[track_id], pts, HALF_WIDTH, _CIRCUIT_CHECKPOINT_FRACTIONS, seed)


def analyze(track: Track) -> dict:
    """Design-time diagnostics (used by tests and tools/track_report.py)."""
    n = track.n
    # minimum turning radius: heading change summed over a 60-unit window
    turn = []
    for i in range(n):
        ax, ay = track.seg_dx[i - 1], track.seg_dy[i - 1]
        bx, by = track.seg_dx[i], track.seg_dy[i]
        turn.append(math.atan2(ax * by - ay * bx, ax * bx + ay * by))
    min_radius = 1e9
    win = 6
    for i in range(n):
        ang = abs(sum(turn[(i + k) % n] for k in range(-win // 2, win // 2)))
        if ang > 1e-9:
            min_radius = min(min_radius, win * SAMPLE_SPACING / ang)
    # minimum distance between centerline points that are far apart along the track
    min_sep = 1e9
    for i in range(0, n, 2):
        for j in range(i + 1, n, 2):
            gap = min(j - i, n - (j - i)) * SAMPLE_SPACING
            if gap < 600.0:
                continue
            dx = track.pts[i][0] - track.pts[j][0]
            dy = track.pts[i][1] - track.pts[j][1]
            d = math.sqrt(dx * dx + dy * dy)
            if d < min_sep:
                min_sep = d
    xs = [p[0] for p in track.pts]
    ys = [p[1] for p in track.pts]
    return {"length_units": track.length, "length_m": track.length / UNITS_PER_METER,
            "segments": n, "min_turn_radius": min_radius, "min_separation": min_sep,
            "bbox": (min(xs), min(ys), max(xs), max(ys)),
            "obstacles": len(track.obstacles),
            "moving": sum(1 for o in track.obstacles if o.moving),
            "checkpoints": list(track.checkpoints), "hash": track.hash}
