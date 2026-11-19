"""hud.py - everything drawn on top of the world: standings, own panel, mini-map, toasts, overlays, text fields.

``build_rows`` turns the NetClient's state into plain dictionaries, so the drawing code stays simple.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

import pygame

from . import protocol as P
from .gfx import GOLD, WHITE, MiniMap, draw_shape, health_color, slot_color
from .netclient import ranking, remaining_m
from .netstats import describe
from .track import UNITS_PER_METER

MUTED = (150, 156, 170)
ACCENT = (80, 220, 230)
RED = (232, 84, 70)
GREEN = (92, 200, 110)
PANEL = (10, 12, 18)


class Fonts:
    def __init__(self) -> None:
        def make(size: int, bold: bool = False):
            try:
                return pygame.font.SysFont("dejavusans,arial,helvetica,verdana", size, bold=bold)
            except Exception:                                   # no system fonts at all
                return pygame.font.Font(None, size + 4)
        self.tiny, self.small, self.normal = make(13), make(16), make(20)
        self.bold, self.big, self.huge = make(20, True), make(38, True), make(110, True)


_TEXT_CACHE: Dict[tuple, pygame.Surface] = {}


def _render(font, string: str, color) -> pygame.Surface:
    """Rendering text is the slowest thing a HUD does; most strings repeat from frame to frame."""
    key = (id(font), string, color)
    img = _TEXT_CACHE.get(key)
    if img is None:
        if len(_TEXT_CACHE) > 800:
            _TEXT_CACHE.clear()
        img = _TEXT_CACHE[key] = font.render(string, True, color) if string else pygame.Surface((1, font.get_height()), pygame.SRCALPHA)
    return img


def text(screen, font, string, pos, color=WHITE, anchor="topleft", shadow=True) -> pygame.Rect:
    img = _render(font, string, color)
    rect = img.get_rect(**{anchor: pos})
    if shadow:
        screen.blit(_render(font, string, (0, 0, 0)), rect.move(1, 2))
    screen.blit(img, rect)
    return rect


def panel(screen, rect, alpha: int = 170, color=PANEL) -> None:
    surf = pygame.Surface(rect.size, pygame.SRCALPHA)
    pygame.draw.rect(surf, (*color, alpha), surf.get_rect(), border_radius=10)
    pygame.draw.rect(surf, (255, 255, 255, 36), surf.get_rect(), 1, border_radius=10)
    screen.blit(surf, rect.topleft)


def bar(screen, rect, fraction: float, color) -> None:
    pygame.draw.rect(screen, (0, 0, 0), rect.inflate(2, 2), border_radius=3)
    pygame.draw.rect(screen, (40, 44, 54), rect, border_radius=3)
    filled = pygame.Rect(rect.x, rect.y, int(rect.w * max(0.0, min(1.0, fraction))), rect.h)
    if filled.w > 0:
        pygame.draw.rect(screen, color, filled, border_radius=3)


def ordinal(n: int) -> str:
    return "%d%s" % (n, "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th"))


def fmt_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    return "%d:%04.1f" % (int(seconds // 60), seconds % 60)


# ---------------------------------------------------------------------- what to show (no drawing)
def player_name(client, pid: int) -> str:
    entry = client.roster.get(pid)
    return entry.name if entry is not None else "P%d" % pid


def player_style(client, pid: int) -> Tuple[int, Tuple[int, int, int]]:
    """(shape, colour) of a player; the roster knows both, until it arrives use a circle and the id."""
    entry = client.roster.get(pid)
    if entry is not None:
        return entry.shape, slot_color(entry.slot)
    return (client.shape if pid == client.player_id else P.SHAPE_CIRCLE), slot_color(pid - 1)


def build_rows(client) -> List[Dict]:
    laps, length = client.ack.laps, client.track.length
    rows = []
    for rank, p in enumerate(ranking(client.players.values()), 1):
        shape, color = player_style(client, p.id)
        if p.status & P.STATUS_FINISHED:
            label, label_color = "FINISHED #%d" % p.place, GOLD
        elif p.status & P.STATUS_DNF:
            label, label_color = "DNF", MUTED
        elif p.status & P.STATUS_WRECKED:
            label, label_color = "WRECKED", RED
        else:
            label, label_color = "%d m" % round(remaining_m(p, laps, length)), WHITE
        rows.append(dict(rank=rank, id=p.id, name=player_name(client, p.id), shape=shape, color=color,
                         health=p.health, lap=min(p.lap + 1, laps), laps=laps, label=label,
                         label_color=label_color, local=p.id == client.player_id))
    return rows


def describe_event(ev: P.Event, client) -> Optional[Tuple[str, Tuple[int, int, int]]]:
    """The toast for a game event, or None when it is not worth a line.

    Your own hits, gates and laps are shown; for the other cars only joins, leaves, wrecks and finishes
    (with 16 players anything more would flood the screen)."""
    name, mine, kind = player_name(client, ev.player), ev.player == client.player_id, ev.type
    if kind == P.EV_PLAYER_JOIN:
        return ("%s joined" % name, MUTED) if not mine else None
    if kind == P.EV_PLAYER_LEAVE:
        if ev.a == P.DISC_LEFT:
            return "%s left the game" % name, MUTED
        return "%s %s" % (name, "timed out" if ev.a == P.DISC_TIMEOUT else "was removed (%s)" % P.DISC_TEXT.get(ev.a, "?")), MUTED
    if kind == P.EV_HIT:
        return ("Hit an obstacle  -%d HP" % ev.a, RED) if mine else None
    if kind == P.EV_CHECKPOINT:
        return ("Checkpoint %d" % ev.a, ACCENT) if mine else None
    if kind == P.EV_LAP:
        return ("Lap %d done in %s" % (ev.a, fmt_time(ev.b / 1000.0)), GOLD) if mine else None
    if kind == P.EV_FINISH:
        return "%s finished %s!" % ("You" if mine else name, ordinal(ev.a)), GOLD
    if kind == P.EV_WRECK:
        return ("You are wrecked" if mine else "%s was wrecked" % name), RED
    if kind == P.EV_RESPAWN:
        return ("Back in the race", GREEN) if mine else None
    if kind == P.EV_RACE_END:
        return "Race over", MUTED
    return None


# ---------------------------------------------------------------------- panels
def draw_standings(screen, fonts, rows, rect) -> None:
    panel(screen, rect)
    text(screen, fonts.bold, "STANDINGS", (rect.x + 14, rect.y + 8), MUTED, shadow=False)
    top = rect.y + 34
    row_h = min(46, (rect.bottom - top - 6) // max(1, len(rows)))
    compact = row_h < 38
    for i, r in enumerate(rows):
        y = top + i * row_h
        if r["local"]:
            glow = pygame.Surface((rect.w - 12, row_h - 2), pygame.SRCALPHA)
            glow.fill((255, 255, 255, 30))
            screen.blit(glow, (rect.x + 6, y))
        cy = y + (row_h // 2 if compact else 14)
        text(screen, fonts.small, str(r["rank"]), (rect.x + 20, cy), MUTED, "center")
        draw_shape(screen, (rect.x + 44, cy), r["shape"], 0.0, 8, r["color"])
        name = r["name"] + (" (you)" if r["local"] else "")
        text(screen, fonts.small, name[:14 if compact else 18], (rect.x + 62, cy), WHITE, "midleft")
        fraction = r["health"] / 100.0
        if compact:
            bar(screen, pygame.Rect(rect.x + 168, cy - 4, 46, 8), fraction, health_color(fraction))
            text(screen, fonts.small, r["label"], (rect.right - 12, cy), r["label_color"], "midright")
        else:
            text(screen, fonts.tiny, "L%d/%d" % (r["lap"], r["laps"]), (rect.right - 12, cy), MUTED, "midright")
            by = y + 31
            bar(screen, pygame.Rect(rect.x + 62, by, 104, 8), fraction, health_color(fraction))
            text(screen, fonts.tiny, "%d" % r["health"], (rect.x + 172, by + 4), WHITE, "midleft")
            text(screen, fonts.small, r["label"], (rect.right - 12, by + 4), r["label_color"], "midright")


def draw_self_panel(screen, fonts, client, rows, rect) -> None:
    me = client.me
    if me is None:
        return
    panel(screen, rect)
    place = next((r["rank"] for r in rows if r["local"]), 0)
    text(screen, fonts.big, "%s / %d" % (ordinal(place), len(rows)), (rect.x + 14, rect.y + 6), GOLD)
    mine = next((r for r in rows if r["local"]), None)
    if mine is not None:
        goal = mine["label"] if not mine["label"].endswith(" m") else "goal in " + mine["label"]
        text(screen, fonts.normal, goal, (rect.right - 14, rect.y + 20), mine["label_color"], "midright")
    fraction = me.health / 100.0
    bar(screen, pygame.Rect(rect.x + 14, rect.y + 58, rect.w - 28, 14), fraction, health_color(fraction))
    text(screen, fonts.small, "%d HP" % me.health, (rect.x + 20, rect.y + 65), (0, 0, 0), "midleft", shadow=False)
    kmh = abs(me.speed) / UNITS_PER_METER * 3.6
    laps = client.ack.laps
    text(screen, fonts.small, "lap %d/%d   %d km/h   %s" % (min(me.lap + 1, laps), laps, kmh,
                                                           fmt_time(client.race_time_now())),
         (rect.x + 14, rect.y + 84), WHITE)


def draw_minimap(screen, mm: MiniMap, client, pos) -> None:
    rect = pygame.Rect(pos[0], pos[1], mm.surface.get_width() + 8, mm.surface.get_height() + 8)
    panel(screen, rect, 150)
    screen.blit(mm.surface, (rect.x + 4, rect.y + 4))
    for pid, p in client.players.items():
        _shape, color = player_style(client, pid)
        x, y = mm.to_map(p.x, p.y)
        center = (rect.x + 4 + x, rect.y + 4 + y)
        if pid == client.player_id:
            pygame.draw.circle(screen, WHITE, center, 7, 2)
        pygame.draw.circle(screen, color, center, 4)


def draw_netinfo(screen, fonts, client, pos, fps: Optional[float] = None, hint: bool = False) -> None:
    rtt = "%.0f ms" % client.rtt_ms[-1] if client.rtt_ms else "-"
    line = "RTT %s   loss %.1f %%   jitter %.1f ms   %.0f updates/s" % (
        rtt, 100.0 * client.tracker.loss_ratio, client.jitter.value, client.updates_per_s())
    if fps is not None:
        line += "   %.0f fps" % fps
    if hint:
        line += "   [F3] details"
    text(screen, fonts.tiny, line, pos, MUTED, "bottomright")


def draw_netpanel(screen, fonts, client, rect, rates: Tuple[float, float] = (0.0, 0.0)) -> None:
    """The F3 panel: what the network is doing to this player, with the last minute of round-trip times."""
    panel(screen, rect, 205)
    text(screen, fonts.bold, "NETWORK", (rect.x + 14, rect.y + 8), MUTED, shadow=False)
    t = client.tracker
    samples = list(client.rtt_ms)[-60:]
    d = describe(samples)
    rows = (("RTT now / avg / p95", "%s / %.1f / %.1f ms" % ("%.0f" % samples[-1] if samples else "-", d["mean"], d["p95"])),
            ("jitter", "%.1f ms" % client.jitter.value),
            ("loss, server to you", "%.1f %%  (%d of %d)" % (100.0 * t.loss_ratio, t.lost, t.expected)),
            ("reordered / duplicates", "%d / %d" % (t.reordered, t.duplicates)),
            ("snapshots", "%.0f per second" % client.updates_per_s()),
            ("down / up", "%.1f / %.1f kbit/s" % rates))
    y = rect.y + 34
    for label, value in rows:
        text(screen, fonts.tiny, label, (rect.x + 14, y + 3), MUTED, shadow=False)
        text(screen, fonts.small, value, (rect.right - 14, y), WHITE, "topright", shadow=False)
        y += 21
    graph = pygame.Rect(rect.x + 14, y + 8, rect.w - 28, rect.bottom - y - 30)
    if graph.h < 20:                                         # a short panel: the numbers are enough
        return
    pygame.draw.rect(screen, (22, 26, 36), graph, border_radius=4)
    top = max(20.0, d["max"] * 1.2)
    if len(samples) >= 2:
        step = graph.w / float(len(samples) - 1)
        pts = [(graph.x + i * step, graph.bottom - 3 - (graph.h - 6) * min(1.0, v / top)) for i, v in enumerate(samples)]
        pygame.draw.lines(screen, ACCENT, False, pts, 2)
    text(screen, fonts.tiny, "RTT, last %d pings  (scale 0 - %.0f ms)" % (len(samples), top),
         (graph.x, graph.bottom + 4), MUTED, shadow=False)


class Toasts:
    LIFE, FADE = 4.0, 1.0

    def __init__(self) -> None:
        self.items: List[Tuple[float, str, Tuple[int, int, int]]] = []

    def add(self, message: str, color, now: float) -> None:
        self.items.append((now + self.LIFE, message, color))
        self.items = self.items[-6:]

    def draw(self, screen, fonts, now: float, center_x: int, y: int, limit: int = 6) -> None:
        self.items = [i for i in self.items if i[0] > now]
        for k, (until, message, color) in enumerate(list(reversed(self.items))[:limit]):     # newest on top
            alpha = int(255 * max(0.0, min(1.0, (until - now) / self.FADE)))
            img = fonts.small.render(message, True, color)
            pill = pygame.Surface((img.get_width() + 22, 24), pygame.SRCALPHA)
            pygame.draw.rect(pill, (*PANEL, min(170, alpha)), pill.get_rect(), border_radius=12)
            pill.blit(img, (11, 12 - img.get_height() // 2))
            pill.set_alpha(alpha)
            screen.blit(pill, (center_x - pill.get_width() // 2, y + k * 28))


# ---------------------------------------------------------------------- overlays
def draw_countdown(screen, fonts, client) -> None:
    w, h = screen.get_size()
    seconds = max(0, math.ceil(client.countdown_ms / 1000.0))
    text(screen, fonts.huge, str(seconds) if seconds else "GO!", (w // 2, int(h * 0.2)), ACCENT, "center")
    text(screen, fonts.normal, "W/A/S/D or the arrow keys: hold the throttle for a flying start",
         (w // 2, int(h * 0.2) + 78), WHITE, "center")


def draw_go(screen, fonts, race_ms: int) -> None:
    if 0 <= race_ms < 900:
        w, h = screen.get_size()
        img = fonts.huge.render("GO!", True, GREEN)
        img.set_alpha(int(255 * (1.0 - race_ms / 900.0)))
        screen.blit(img, img.get_rect(center=(w // 2, h // 3)))


def draw_results(screen, fonts, rows, race_s: float, back_in_s: float = 0.0) -> None:
    w, h = screen.get_size()
    rect = pygame.Rect(0, 0, 600, min(h - 60, 130 + 34 * len(rows)))
    rect.center = (w // 2, h // 2)
    panel(screen, rect, 225)
    text(screen, fonts.big, "RACE FINISHED", (rect.centerx, rect.y + 34), GOLD, "center")
    text(screen, fonts.small, "race time %s" % fmt_time(race_s), (rect.centerx, rect.y + 72), MUTED, "center")
    step = min(34, (rect.bottom - rect.y - 130) // max(1, len(rows)))
    for i, r in enumerate(rows):
        y = rect.y + 104 + i * step
        done = r["label"].startswith("FINISHED")
        draw_shape(screen, (rect.x + 40, y), r["shape"], 0.0, 9, r["color"])
        text(screen, fonts.normal, ordinal(r["rank"]) if done else "-", (rect.x + 70, y), GOLD if done else MUTED, "midleft")
        text(screen, fonts.normal, r["name"][:18] + (" (you)" if r["local"] else ""), (rect.x + 130, y), WHITE, "midleft")
        text(screen, fonts.small, r["label"], (rect.x + 400, y), r["label_color"], "midleft")
        text(screen, fonts.small, "%d HP" % r["health"], (rect.right - 20, y), MUTED, "midright")
    text(screen, fonts.small, "back to the lobby in %d s" % math.ceil(back_in_s), (rect.centerx, rect.bottom - 20),
         MUTED, "center")


class TextField:
    """A one-line text input for the connect screen."""

    def __init__(self, label: str, value: str = "", max_len: int = 40) -> None:
        self.label, self.value, self.max_len = label, value, max_len

    def key(self, event) -> None:
        if event.key == pygame.K_BACKSPACE:
            self.value = self.value[:-1]
        elif event.unicode and event.unicode.isprintable() and len(self.value) < self.max_len:
            self.value += event.unicode

    def draw(self, screen, fonts, rect, active: bool, now: float) -> None:
        text(screen, fonts.small, self.label, (rect.x, rect.y - 20), MUTED, shadow=False)
        panel(screen, rect, 200)
        pygame.draw.rect(screen, ACCENT if active else (70, 76, 90), rect, 2, border_radius=10)
        shown = self.value + ("|" if active and int(now * 2) % 2 == 0 else "")
        text(screen, fonts.normal, shown, (rect.x + 14, rect.centery), WHITE, "midleft")
