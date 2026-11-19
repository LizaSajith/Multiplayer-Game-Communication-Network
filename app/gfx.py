"""gfx.py - drawing the world for the pygame client: camera, pre-rendered track, obstacles, cars, mini-map.

World coordinates are the game's own (x right, y down, 10 units = 1 m), so nothing is flipped: the camera
only translates and north stays up.  This module needs pygame; netclient.py does not.
"""
from __future__ import annotations

import math
from typing import Tuple

import pygame

from . import protocol as P
from .game import PLAYER_RADIUS
from .track import KIND_CIRCLE, Track

GRASS = (26, 44, 36)
ROAD = (58, 62, 72)
ROAD_ARROW = (78, 84, 98)
ROAD_LINE = (150, 150, 120)
CURB_A, CURB_B = (232, 232, 238), (196, 52, 52)
GATE = (80, 220, 230)
INK = (14, 16, 22)
WHITE = (240, 240, 245)
GOLD = (255, 214, 64)

SLOT_COLORS = [
    (255, 99, 71), (66, 165, 245), (102, 187, 106), (255, 202, 40), (171, 71, 188), (38, 198, 218),
    (255, 138, 101), (236, 64, 122), (156, 204, 101), (121, 134, 203), (255, 241, 118), (77, 182, 172),
    (186, 104, 200), (255, 171, 64), (144, 164, 174), (240, 98, 146),
]


def slot_color(slot: int) -> Tuple[int, int, int]:
    return SLOT_COLORS[slot % len(SLOT_COLORS)]


def health_color(fraction: float) -> Tuple[int, int, int]:
    return (92, 200, 110) if fraction > 0.6 else (240, 196, 60) if fraction > 0.3 else (232, 84, 70)


def dim(color, factor: float = 0.45) -> Tuple[int, int, int]:
    return tuple(int(c * factor) for c in color)


def lighten(color, amount: float = 0.6) -> Tuple[int, int, int]:
    return tuple(int(c + (255 - c) * amount) for c in color)


class Camera:
    """Follows a point (the local car).  ``to_screen`` is a plain translation."""

    def __init__(self, width: int, height: int) -> None:
        self.w, self.h = width, height
        self.x = self.y = 0.0
        self.placed = False

    def resize(self, width: int, height: int) -> None:
        self.w, self.h = width, height

    def snap(self, x: float, y: float) -> None:
        self.x, self.y, self.placed = x, y, True

    def follow(self, x: float, y: float, dt: float, rate: float = 7.0) -> None:
        if not self.placed:
            self.snap(x, y)
            return
        k = 1.0 - math.exp(-rate * max(0.0, dt))
        self.x += (x - self.x) * k
        self.y += (y - self.y) * k

    def to_screen(self, x: float, y: float) -> Tuple[int, int]:
        return int(round(x - self.x + self.w / 2)), int(round(y - self.y + self.h / 2))

    def sees(self, x: float, y: float, margin: float = 60.0) -> bool:
        return abs(x - self.x) <= self.w / 2 + margin and abs(y - self.y) <= self.h / 2 + margin


class TrackView:
    """The whole circuit drawn once onto a big surface (1 pixel per world unit); a window is blitted per frame."""

    MARGIN = 300

    def __init__(self, track: Track) -> None:
        self.track = track
        pad = track.half_width + self.MARGIN
        xs, ys = [p[0] for p in track.pts], [p[1] for p in track.pts]
        self.ox, self.oy = min(xs) - pad, min(ys) - pad
        size = (int(max(xs) + pad - self.ox) + 1, int(max(ys) + pad - self.oy) + 1)
        surf = pygame.Surface(size)
        surf.fill(GRASS)
        self._road(surf)
        self._arrows(surf)
        self._start_line(surf)
        self._gates(surf)
        self.surface = surf.convert() if pygame.display.get_surface() else surf

    def _px(self, x: float, y: float) -> Tuple[float, float]:
        return x - self.ox, y - self.oy

    def _road(self, surf: pygame.Surface) -> None:
        t, hw, n = self.track, self.track.half_width, self.track.n
        left = [self._px(x + nx * hw, y + ny * hw) for (x, y), (nx, ny) in zip(t.pts, t.nrm)]
        right = [self._px(x - nx * hw, y - ny * hw) for (x, y), (nx, ny) in zip(t.pts, t.nrm)]
        mid = [self._px(x, y) for x, y in t.pts]
        for i in range(n):
            j = (i + 1) % n
            pygame.draw.polygon(surf, ROAD, [left[i], left[j], right[j], right[i]])
        for i in range(n):
            j = (i + 1) % n
            curb = CURB_A if (i // 2) % 2 == 0 else CURB_B
            pygame.draw.line(surf, curb, left[i], left[j], 7)
            pygame.draw.line(surf, curb, right[i], right[j], 7)
            if i % 4 < 2:
                pygame.draw.line(surf, ROAD_LINE, mid[i], mid[j], 3)

    def _arrows(self, surf: pygame.Surface) -> None:
        t = self.track
        for k in range(1, int(t.length // 300)):
            x, y, tx, ty, nx, ny = t.frame_at(k * 300.0 - 150.0)
            tip = self._px(x + tx * 22, y + ty * 22)
            a = self._px(x - tx * 10 + nx * 24, y - ty * 10 + ny * 24)
            b = self._px(x - tx * 10 - nx * 24, y - ty * 10 - ny * 24)
            pygame.draw.lines(surf, ROAD_ARROW, False, [a, tip, b], 6)

    def _start_line(self, surf: pygame.Surface) -> None:
        t, cell = self.track, 20.0
        x, y, tx, ty, nx, ny = t.frame_at(0.0)

        def corner(d: float, lat: float) -> Tuple[float, float]:
            return self._px(x + tx * d + nx * lat, y + ty * d + ny * lat)

        for row in range(2):
            d0 = (row - 1) * cell
            for k in range(int(t.half_width * 2 // cell)):
                lat = -t.half_width + k * cell
                shade = (240, 240, 240) if (k + row) % 2 == 0 else (24, 24, 28)
                pygame.draw.polygon(surf, shade, [corner(d0, lat), corner(d0 + cell, lat),
                                                  corner(d0 + cell, lat + cell), corner(d0, lat + cell)])

    def _gates(self, surf: pygame.Surface) -> None:
        t, hw = self.track, self.track.half_width
        font = pygame.font.Font(None, 30) if pygame.font.get_init() else None
        for number, s in enumerate(t.checkpoints, 1):
            x, y, tx, ty, nx, ny = t.frame_at(s)
            a = self._px(x + nx * hw, y + ny * hw)
            b = self._px(x - nx * hw, y - ny * hw)
            pygame.draw.line(surf, GATE, a, b, 4)
            for post in (a, b):
                pygame.draw.circle(surf, GATE, (int(post[0]), int(post[1])), 9)
                pygame.draw.circle(surf, INK, (int(post[0]), int(post[1])), 9, 2)
            if font is not None:
                label = font.render("CP%d" % number, True, GATE)
                at = self._px(x + nx * (hw - 44), y + ny * (hw - 44))
                surf.blit(label, (at[0] - label.get_width() / 2, at[1] - label.get_height() / 2))

    def draw(self, screen: pygame.Surface, cam: Camera) -> None:
        screen.blit(self.surface, (int(round(self.ox - cam.x + cam.w / 2)), int(round(self.oy - cam.y + cam.h / 2))))


class MiniMap:
    """A scaled copy of the circuit for the corner of the screen."""

    def __init__(self, view: TrackView, max_w: int = 250, max_h: int = 150) -> None:
        t = view.track
        pad = t.half_width + 12
        xs, ys = [p[0] for p in t.pts], [p[1] for p in t.pts]
        self.x0, self.y0 = min(xs) - pad, min(ys) - pad
        x1, y1 = max(xs) + pad, max(ys) + pad
        self.scale = min(max_w / (x1 - self.x0), max_h / (y1 - self.y0))
        region = pygame.Rect(int(self.x0 - view.ox), int(self.y0 - view.oy), int(x1 - self.x0), int(y1 - self.y0))
        size = (max(1, int((x1 - self.x0) * self.scale)), max(1, int((y1 - self.y0) * self.scale)))
        self.surface = pygame.transform.smoothscale(view.surface.subsurface(region), size)

    def to_map(self, x: float, y: float) -> Tuple[int, int]:
        return int((x - self.x0) * self.scale), int((y - self.y0) * self.scale)


def draw_obstacles(screen: pygame.Surface, cam: Camera, track: Track, race_t: float) -> None:
    for o in track.obstacles:
        cx, cy = o.center(race_t)
        if not cam.sees(cx, cy, 80.0):
            continue
        if o.moving:                                         # the path a sweeping obstacle patrols
            a = cam.to_screen(o.ax + o.nx * (o.v0 - o.amp), o.ay + o.ny * (o.v0 - o.amp))
            b = cam.to_screen(o.ax + o.nx * (o.v0 + o.amp), o.ay + o.ny * (o.v0 + o.amp))
            pygame.draw.line(screen, (120, 70, 150), a, b, 2)
        fill, rim = ((190, 90, 235), (70, 30, 110)) if o.moving else ((235, 120, 60), (120, 40, 20))
        if o.kind == KIND_CIRCLE:
            c = cam.to_screen(cx, cy)
            pygame.draw.circle(screen, fill, c, int(o.rx))
            pygame.draw.circle(screen, rim, c, int(o.rx), 3)
        else:
            pts = [cam.to_screen(cx + o.tx * sx * o.rx + o.nx * sy * o.ry,
                                 cy + o.ty * sx * o.rx + o.ny * sy * o.ry)
                   for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
            pygame.draw.polygon(screen, fill, pts)
            pygame.draw.polygon(screen, rim, pts, 3)


_OUTLINES = {P.SHAPE_SQUARE: [(-1.0, -1.0), (1.0, -1.0), (1.0, 1.0), (-1.0, 1.0)],
             P.SHAPE_TRIANGLE: [(1.25, 0.0), (-0.9, -0.95), (-0.9, 0.95)]}


def draw_shape(screen: pygame.Surface, pos: Tuple[int, int], shape: int, heading: float, radius: float,
               color, rim=INK) -> None:
    """A circle, square or triangle; the square and circle carry a line showing which way they face."""
    cx, cy = pos
    c, s = math.cos(heading), math.sin(heading)
    if shape == P.SHAPE_TRIANGLE or shape == P.SHAPE_SQUARE:
        k = radius * (0.9 if shape == P.SHAPE_SQUARE else 1.0)
        pts = [(cx + (x * c - y * s) * k, cy + (x * s + y * c) * k) for x, y in _OUTLINES[shape]]
        pygame.draw.polygon(screen, color, pts)
        pygame.draw.polygon(screen, rim, pts, 3)
    else:
        pygame.draw.circle(screen, color, pos, int(radius))
        pygame.draw.circle(screen, rim, pos, int(radius), 3)
    if shape != P.SHAPE_TRIANGLE:
        pygame.draw.line(screen, rim, pos, (cx + c * radius, cy + s * radius), 3)


def draw_player(screen: pygame.Surface, cam: Camera, pose, *, shape: int, color, name: str, health: int,
                local: bool, font, t: float) -> None:
    """One car at ``pose`` (a netclient.Pose) with its name and health bar above it."""
    if not cam.sees(pose.x, pose.y, 80.0):
        return
    pos = cam.to_screen(pose.x, pose.y)
    r = int(PLAYER_RADIUS)
    wrecked = bool(pose.status & P.STATUS_WRECKED)
    body = (110, 110, 118) if wrecked else color
    if pose.status & P.STATUS_INVULN and int(t * 8) % 2 == 0:        # blink while protected
        body = lighten(body)
    if local:
        pygame.draw.circle(screen, WHITE, pos, r + 6, 2)
    draw_shape(screen, pos, shape, pose.heading, r, body)
    if wrecked:
        pygame.draw.line(screen, (232, 84, 70), (pos[0] - r, pos[1] - r), (pos[0] + r, pos[1] + r), 4)
        pygame.draw.line(screen, (232, 84, 70), (pos[0] - r, pos[1] + r), (pos[0] + r, pos[1] - r), 4)
    if pose.status & P.STATUS_FINISHED:
        pygame.draw.circle(screen, GOLD, pos, r + 5, 3)
    bar = pygame.Rect(pos[0] - 18, pos[1] - r - 13, 36, 5)
    pygame.draw.rect(screen, INK, bar.inflate(2, 2))
    fraction = max(0.0, min(1.0, health / 100.0))
    pygame.draw.rect(screen, health_color(fraction), (bar.x, bar.y, int(bar.w * fraction), bar.h))
    label = font.render(name, True, WHITE)
    screen.blit(label, (pos[0] - label.get_width() // 2, pos[1] - r - 15 - label.get_height()))
