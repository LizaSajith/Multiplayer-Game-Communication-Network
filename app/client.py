"""client.py - the pygame game: connect screen, lobby, race with a live HUD, results.

    python -m app.client                                    # type the server address on the first screen
    python -m app.client --server 10.0.0.1:5005 --name Ana --shape square
    python -m app.client --local                            # try it alone: private server + 3 autopilot opponents
    python -m app.client --server 127.0.0.1:5005 --auto     # the autopilot drives for you (demo)
    python -m app.client --selftest 90 --local --laps 1     # no window: auto-play one race (install check)

All networking lives in netclient.NetClient (it has no pygame); this file only turns its state into a
picture and the keyboard into controls.  Keys:  W/A/S/D or the arrows drive,  R ready,  1/2/3 shape,
Enter starts the race (host only),  Esc leaves (twice during a race),  F3 network details,  F11 full screen.
"""
from __future__ import annotations

import os

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import argparse
import math
import random
import sys
import time
from typing import Callable, List, Optional, Tuple

try:
    import pygame
except ImportError:                                          # a plain message instead of a traceback
    sys.exit("pygame is not installed.  Install it with:   python -m pip install pygame")

try:                                                         # normal use: python -m app.client
    from . import gfx, hud
    from . import protocol as P
    from .autopilot import Autopilot
    from .console import tolerant_output
    from .game import SHAPE_STATS
    from .local import LocalGame
    from .netclient import (DISCONNECTED, FAILED, IDLE, JOINED, JOINING, LEFT, LOST, REJECTED, NetClient,
                            parse_server)
    from .track import UNITS_PER_METER
except ImportError:                                          # script use: python app/client.py
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from app import gfx, hud
    from app import protocol as P
    from app.autopilot import Autopilot
    from app.console import tolerant_output
    from app.game import SHAPE_STATS
    from app.local import LocalGame
    from app.netclient import (DISCONNECTED, FAILED, IDLE, JOINED, JOINING, LEFT, LOST, REJECTED, NetClient,
                               parse_server)
    from app.track import UNITS_PER_METER

CONNECT, JOINING_SCREEN, LOBBY, RACE, RESULTS = "connect", "joining", "lobby", "race", "results"
MENU_SCENES = (CONNECT, JOINING_SCREEN, LOBBY)
FPS = 60
DEFAULT_SIZE = (1100, 700)
MENU_SIZE = (1100, 660)                      # the menus are laid out for this size, then shrunk to fit a smaller window
SHAPE_LABELS = ("Circle", "Square", "Triangle")
FAILED_STATES = (FAILED, REJECTED, LOST, DISCONNECTED)
STEER_RISE, STEER_FALL = 5.0, 9.0            # keyboard steering ramps up to full lock in 0.2 s
ESC_WINDOW_S = 2.0                           # second Esc within this time leaves a race
WRONG_WAY_AFTER_S = 0.5
BTN_FILL, BTN_HOVER, BTN_ON = (44, 50, 64), (62, 70, 90), (40, 110, 120)


# ---------------------------------------------------------------------- pure helpers (unit tested)
def menu_fit(window: Tuple[int, int], design: Tuple[int, int] = MENU_SIZE) -> Tuple[float, Tuple[int, int]]:
    """Where a menu drawn at ``design`` size goes in a ``window``: (scale, top-left corner).
    It is shrunk to fit a small window, never enlarged, and centred."""
    scale = max(0.05, min(1.0, window[0] / design[0], window[1] / design[1]))
    width, height = max(1, int(design[0] * scale)), max(1, int(design[1] * scale))
    return scale, ((window[0] - width) // 2, (window[1] - height) // 2)


def fit_size(wanted: Tuple[int, int], desktop: Optional[Tuple[int, int]]) -> Tuple[int, int]:
    """The window size to open: ``wanted``, but not bigger than a window can use on a desktop of that size
    (the title bar and the taskbar take about 14 % of the height).  An unknown or absurd desktop leaves it alone."""
    width, height = wanted
    if desktop is not None and desktop[0] >= 640 and desktop[1] >= 400:
        width, height = min(width, int(desktop[0] * 0.96)), min(height, int(desktop[1] * 0.86))
    return width, height


def desktop_size() -> Optional[Tuple[int, int]]:
    """The main screen's size as this program sees it (with Windows display scaling that is the scaled size)."""
    try:
        pygame.display.init()
        sizes = pygame.display.get_desktop_sizes()          # pygame 2
        if sizes:
            return int(sizes[0][0]), int(sizes[0][1])
    except (pygame.error, AttributeError):
        pass
    try:
        info = pygame.display.Info()
        if info.current_w > 0 and info.current_h > 0:
            return info.current_w, info.current_h
    except pygame.error:
        pass
    return None


def screen_for(client: Optional[NetClient]) -> str:
    """Which screen to show for the NetClient's state."""
    if client is None or client.state == JOINING:
        return CONNECT if client is None else JOINING_SCREEN
    if client.state != JOINED:
        return CONNECT
    if client.game_state == P.ST_LOBBY:
        return LOBBY
    return RESULTS if client.game_state == P.ST_FINISHED else RACE


def read_axes(keys) -> Tuple[float, float]:
    """(throttle, steer) from the keyboard, each -1, 0 or 1; steer +1 is a right turn."""
    up = keys[pygame.K_w] or keys[pygame.K_UP]
    down = keys[pygame.K_s] or keys[pygame.K_DOWN]
    left = keys[pygame.K_a] or keys[pygame.K_LEFT]
    right = keys[pygame.K_d] or keys[pygame.K_RIGHT]
    return float(bool(up)) - float(bool(down)), float(bool(right)) - float(bool(left))


def smooth_axis(value: float, target: float, dt: float, rise: float = STEER_RISE, fall: float = STEER_FALL) -> float:
    """Move ``value`` toward ``target``; slower when pushing further out, faster when returning to centre."""
    pushing = target != 0.0 and value * target >= 0.0 and abs(target) >= abs(value)
    step = (rise if pushing else fall) * max(0.0, dt)
    if value < target:
        return min(target, value + step)
    return max(target, value - step)


def wrong_way(track, x: float, y: float, heading: float, speed: float) -> bool:
    """True when the car is driving forward against the direction of the circuit."""
    if speed < 40.0:
        return False
    s = track.locate(x, y).s
    _x, _y, tx, ty, _nx, _ny = track.frame_at(s)
    return math.cos(heading) * tx + math.sin(heading) * ty < -0.3


def failure_text(client: NetClient) -> str:
    """What to tell the player after a failed or ended connection."""
    if client.state == FAILED and client.error == "no answer from the server":
        return "No answer from the server. Is it running, and is UDP port %d open?" % client.server[1]
    if client.state == REJECTED:
        return "The server said no: %s." % client.error
    if client.state == DISCONNECTED:
        return "You were removed by the server (%s)." % client.error
    return (client.error[:1].upper() + client.error[1:] + ".") if client.error else "Connection ended."


def default_name() -> str:
    raw = os.environ.get("USERNAME") or os.environ.get("USER") or "Player"
    return "".join(ch for ch in raw if ch.isprintable())[:12] or "Player"


def wrap_text(font, string: str, width: int) -> List[str]:
    lines, line = [], ""
    for word in string.split():
        trial = (line + " " + word).strip()
        if line and font.size(trial)[0] > width:
            lines.append(line)
            line = word
        else:
            line = trial
    return lines + ([line] if line else [])


# ---------------------------------------------------------------------- the application
class App:
    def __init__(self, server: str = "127.0.0.1:%d" % P.DEFAULT_PORT, name: str = "", shape: int = P.SHAPE_CIRCLE, *,
                 size: Tuple[int, int] = DEFAULT_SIZE, auto: bool = False, skill: float = 0.9, wait_players: int = 1,
                 shots_dir: Optional[str] = None, race_shot_every: float = 8.0, autoconnect: bool = False,
                 show_net: bool = False) -> None:
        pygame.init()
        pygame.display.set_caption("Race 17 - multiplayer over UDP")
        self.screen = pygame.display.set_mode(size, pygame.RESIZABLE)
        self.fonts = hud.Fonts()
        self.fps = 0.0
        self.show_net = show_net                                 # F3: the network details panel
        self.rates = (0.0, 0.0)                                  # kbit/s down, up (measured once a second)
        self._rate_mark: Optional[Tuple[float, int, int]] = None
        self.server_field = hud.TextField("Server  (host:port)", server, 40)
        self.name_field = hud.TextField("Your name", name or default_name(), 16)
        self.shape = shape if shape in (0, 1, 2) else P.SHAPE_CIRCLE
        self.focus = 0                                       # connect form: 0 server, 1 name, 2 shape
        self.client: Optional[NetClient] = None
        self.message = ""                                    # last connection problem, shown on the connect screen
        self.running = True
        self.scene, self.scene_since, self.t = CONNECT, 0.0, 0.0
        self.cam = gfx.Camera(*size)
        self.toasts = hud.Toasts()
        self.pending_joins: List[Tuple[float, P.Event]] = []     # "X joined" waits until the roster knows X's name
        self.flash = self.shake = 0.0
        self.steer = 0.0
        self.wrong_since: Optional[float] = None
        self.esc_until = 0.0
        self.buttons: List[Tuple[pygame.Rect, Callable[[], None]]] = []
        self._views = {}                                     # track hash -> (TrackView, MiniMap, big MiniMap)
        self.view = self.minimap = self.big_map = None
        self._bg = None
        self._menu = None                                    # see-through canvas for menus drawn smaller than MENU_SIZE
        self._in_menu = False                                # draw_menu is running: it has filled the background
        self.ui_scale, self.ui_offset = 1.0, (0, 0)          # how the last frame mapped menu pixels to the window
        # autopilot / selftest
        self.auto, self.skill, self.wait_players = auto, skill, max(1, wait_players)
        self.pilot: Optional[Autopilot] = None
        self._auto_started = False
        # screenshots
        self.shots_dir, self.race_shot_every = shots_dir, race_shot_every
        self._shots_done = set()
        self._next_race_shot = 0.0
        self.shot_log: List[str] = []
        self.results_seen_at: Optional[float] = None
        self.final: Optional[dict] = None
        if shots_dir:
            os.makedirs(shots_dir, exist_ok=True)
        if autoconnect:
            self.connect()

    # ------------------------------------------------------------------ connection
    def connect(self) -> None:
        try:
            host, port = parse_server(self.server_field.value)
        except ValueError as exc:
            self.message = str(exc)
            return
        self.close_client()
        try:
            self.client = NetClient((host, port), self.name_field.value.strip() or "Player", self.shape,
                                    on_event=self.on_event)
        except OSError as exc:                               # e.g. the host name does not exist
            self.message = "Cannot use that address (%s)." % (exc.strerror or exc)
            return
        self.message = ""
        self.pilot = None
        self._auto_started = False
        self.pending_joins.clear()
        self.client.start()

    def close_client(self) -> None:
        if self.client is not None:
            self.client.leave()
            self.client.close()
            self.client = None

    def leave(self, message: str = "") -> None:
        self.close_client()
        self.message = message
        self.steer = 0.0
        self.pending_joins.clear()

    def quit(self) -> None:
        self.running = False

    def on_event(self, ev: P.Event) -> None:
        c = self.client
        if c is None:
            return
        if ev.player == c.player_id and ev.type in (P.EV_HIT, P.EV_WRECK):
            self.flash = 0.35 if ev.type == P.EV_HIT else 0.6
            self.shake = 0.6 if ev.type == P.EV_HIT else 1.0
        if ev.type == P.EV_PLAYER_JOIN and ev.player != c.player_id:
            self.pending_joins.append((self.t + 1.5, ev))            # the name arrives with the next ROSTER
            return
        toast = hud.describe_event(ev, c)
        if toast is not None:
            self.toasts.add(toast[0], toast[1], self.t)

    def flush_joins(self) -> None:
        c, waiting = self.client, []
        for until, ev in self.pending_joins:
            if c is None:
                continue
            if ev.player in c.roster or self.t >= until:
                toast = hud.describe_event(ev, c)
                if toast is not None:
                    self.toasts.add(toast[0], toast[1], self.t)
            else:
                waiting.append((until, ev))
        self.pending_joins = waiting

    # ------------------------------------------------------------------ input
    def handle_event(self, e) -> None:
        if e.type == pygame.QUIT:
            self.quit()
        elif e.type == pygame.KEYDOWN:
            if e.key == pygame.K_F11:
                pygame.display.toggle_fullscreen()
            elif e.key == pygame.K_F3:
                self.show_net = not self.show_net
            else:
                self.key(e)
        elif e.type == pygame.MOUSEBUTTONDOWN and e.button == 1:
            pos = self.to_menu(e.pos)
            for rect, action in self.buttons:
                if rect.collidepoint(pos):
                    action()
                    break

    def to_menu(self, pos) -> Tuple[int, int]:
        """A window pixel as the pixel it is in the menu the last frame drew (the identity outside the menus)."""
        (ox, oy), scale = self.ui_offset, self.ui_scale
        return int((pos[0] - ox) / scale), int((pos[1] - oy) / scale)

    def key(self, e) -> None:
        {CONNECT: self.key_connect, JOINING_SCREEN: self.key_joining, LOBBY: self.key_lobby,
         RACE: self.key_race, RESULTS: self.key_results}[self.scene](e)

    def key_connect(self, e) -> None:
        k = e.key
        if k == pygame.K_ESCAPE:
            self.quit()
        elif k in (pygame.K_TAB, pygame.K_DOWN):
            step = -1 if (k == pygame.K_TAB and e.mod & pygame.KMOD_SHIFT) else 1
            self.focus = (self.focus + step) % 3
        elif k == pygame.K_UP:
            self.focus = (self.focus - 1) % 3
        elif k in (pygame.K_RETURN, pygame.K_KP_ENTER):
            self.connect()
        elif self.focus == 2:
            if k in (pygame.K_LEFT, pygame.K_a):
                self.shape = (self.shape - 1) % 3
            elif k in (pygame.K_RIGHT, pygame.K_d):
                self.shape = (self.shape + 1) % 3
            elif pygame.K_1 <= k <= pygame.K_3:
                self.shape = k - pygame.K_1
        else:
            (self.server_field, self.name_field)[self.focus].key(e)

    def key_joining(self, e) -> None:
        if e.key == pygame.K_ESCAPE:
            self.leave()

    def toggle_ready(self) -> None:
        c = self.client
        if c is None or c.state != JOINED:
            return
        pending = c.pending_arg(P.CMD_READY)
        mine = c.roster.get(c.player_id)
        now_ready = bool(pending) if pending is not None else bool(mine and mine.ready)
        c.send_cmd(P.CMD_READY, 0 if now_ready else 1)

    def pick_shape(self, shape: int) -> None:
        if self.scene == CONNECT:
            self.shape = shape
        elif self.client is not None and self.scene == LOBBY:
            self.client.send_cmd(P.CMD_SHAPE, shape)

    def start_race(self) -> None:
        c = self.client
        if c is None or self.scene != LOBBY:
            return
        if c.is_host:
            c.send_cmd(P.CMD_START)
        else:
            self.toasts.add("Only the host can start the race", hud.MUTED, self.t)

    def key_lobby(self, e) -> None:
        k = e.key
        if k == pygame.K_ESCAPE:
            self.leave()
        elif k == pygame.K_r:
            self.toggle_ready()
        elif pygame.K_1 <= k <= pygame.K_3:
            self.pick_shape(k - pygame.K_1)
        elif k in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_SPACE):
            self.start_race()

    def key_race(self, e) -> None:
        if e.key == pygame.K_ESCAPE:
            if self.t < self.esc_until:
                self.leave()
            else:
                self.esc_until = self.t + ESC_WINDOW_S
                self.toasts.add("Press Esc again to leave the race", hud.MUTED, self.t)

    def key_results(self, e) -> None:
        if e.key == pygame.K_ESCAPE:
            self.leave()

    # ------------------------------------------------------------------ per-frame logic
    def frame(self, dt: float) -> None:
        """Network, scene changes, controls, drawing - everything except waiting for the next frame."""
        self.t += dt
        if dt > 0.0:
            self.fps = 1.0 / dt if self.fps == 0.0 else 0.9 * self.fps + 0.1 / dt
        self.screen = pygame.display.get_surface() or self.screen
        self.cam.resize(*self.screen.get_size())
        c = self.client
        if c is not None:
            c.pump()
        self.measure_rates()
        self.sync_scene()
        self.flush_joins()
        self.track_results()
        if self.auto:
            self.auto_actions()
        self.update_controls(dt)
        self.draw(dt)
        pygame.display.flip()
        self.maybe_shot()

    def measure_rates(self) -> None:
        """kbit/s received from and sent to the server, over the last second or so."""
        c = self.client
        if c is None or c.state != JOINED:
            self._rate_mark = None
            return
        mark = self._rate_mark
        if mark is None or c.bytes_in < mark[1]:
            self._rate_mark = (self.t, c.bytes_in, c.bytes_out)
        elif self.t - mark[0] >= 1.0:
            span = self.t - mark[0]
            self.rates = ((c.bytes_in - mark[1]) * 8.0 / 1000.0 / span, (c.bytes_out - mark[2]) * 8.0 / 1000.0 / span)
            self._rate_mark = (self.t, c.bytes_in, c.bytes_out)

    def sync_scene(self) -> None:
        c = self.client
        if c is not None and c.state == JOINED and c.track is not None:
            self.ensure_view()
        if c is not None and c.state in FAILED_STATES:         # shown even when it happened between two frames
            self.message = failure_text(c)
        scene = screen_for(c)
        if scene == self.scene:
            return
        previous = self.scene
        self.scene, self.scene_since = scene, self.t
        if scene == RACE and previous != RACE:
            self.cam.placed = False
            self.steer, self.wrong_since, self.flash, self.shake = 0.0, None, 0.0, 0.0
            self.toasts.items.clear()
            if self.pilot is not None:
                self.pilot.reset()
        if scene == RESULTS:
            self.results_seen_at = self.t
        if scene != RACE:
            self.wrong_since = None

    def track_results(self) -> None:
        if self.scene == RESULTS and self.client is not None and self.client.state == JOINED:
            self.final = self.capture_final()

    def ensure_view(self) -> None:
        track = self.client.track
        entry = self._views.get(track.hash)
        if entry is None:
            view = gfx.TrackView(track)
            entry = self._views[track.hash] = (view, gfx.MiniMap(view, 250, 150), gfx.MiniMap(view, 440, 270))
        self.view, self.minimap, self.big_map = entry

    def update_controls(self, dt: float) -> None:
        c = self.client
        if c is None or c.state != JOINED:
            self.steer = 0.0
            return
        if self.scene != RACE:                               # lobby / results: sit still
            self.steer = 0.0
            c.set_controls(0.0, 0.0)
            return
        me = c.me
        if self.auto:
            if me is not None and c.game_state == P.ST_RACING and not me.status & P.STATUS_FINISHED:
                if self.pilot is None:
                    self.pilot = Autopilot(c.track, skill=self.skill)
                c.set_controls(*self.pilot.control(me.x, me.y, me.heading, me.speed, c.race_time_ms / 1000.0))
            else:
                c.set_controls(0.0, 0.0)
            return
        throttle, target = read_axes(pygame.key.get_pressed())
        self.steer = smooth_axis(self.steer, target, dt)
        c.set_controls(throttle, self.steer)                 # during the countdown this gives a flying start

    def auto_actions(self) -> None:
        """Autopilot mode: ready up, and as host start the race once enough players are in."""
        c = self.client
        if c is None:
            if self.scene == CONNECT and self.t > 0.3 and not self.message:
                self.connect()
            return
        if self.scene != LOBBY or c.state != JOINED:
            return
        mine = c.roster.get(c.player_id)
        if mine is not None and not mine.ready and c.pending_arg(P.CMD_READY) is None:
            c.send_cmd(P.CMD_READY, 1)
        if (c.is_host and not self._auto_started and len(c.roster) >= self.wait_players
                and self.t - self.scene_since > 1.5):
            c.send_cmd(P.CMD_START)
            self._auto_started = True
        if self.scene == LOBBY and self.t - self.scene_since < 0.1:
            self._auto_started = False

    # ------------------------------------------------------------------ drawing: shared pieces
    def backdrop(self) -> None:
        if self._in_menu:                                    # draw_menu has already filled the whole window
            return
        size = self.screen.get_size()
        if self._bg is None or self._bg.get_size() != size:
            bg = pygame.Surface(size)
            top, bottom = (22, 28, 40), (10, 12, 18)
            for y in range(size[1]):
                k = y / max(1, size[1] - 1)
                pygame.draw.line(bg, tuple(int(a + (b - a) * k) for a, b in zip(top, bottom)), (0, y), (size[0], y))
            self._bg = bg
        self.screen.blit(self._bg, (0, 0))

    def button(self, rect: pygame.Rect, label: str, action: Callable[[], None], *, enabled: bool = True,
               active: bool = False) -> None:
        hover = enabled and rect.collidepoint(self.to_menu(pygame.mouse.get_pos()))
        fill = BTN_ON if active else (BTN_HOVER if hover else BTN_FILL)
        pygame.draw.rect(self.screen, fill, rect, border_radius=10)
        pygame.draw.rect(self.screen, hud.ACCENT if (active or hover) else (80, 86, 102), rect, 2, border_radius=10)
        if label:
            hud.text(self.screen, self.fonts.bold, label, rect.center, hud.WHITE if enabled else (110, 114, 126),
                     "center", shadow=False)
        if enabled:
            self.buttons.append((rect, action))

    def shape_cards(self, origin: Tuple[int, int], selected: int, color, card_w: int = 138) -> None:
        mx = max(s.max_speed for s in SHAPE_STATS.values())
        ma = max(s.accel for s in SHAPE_STATS.values())
        mt = max(s.turn_rate for s in SHAPE_STATS.values())
        for shape in (P.SHAPE_CIRCLE, P.SHAPE_SQUARE, P.SHAPE_TRIANGLE):
            rect = pygame.Rect(origin[0] + shape * (card_w + 10), origin[1], card_w, 108)
            on = shape == selected
            self.button(rect, "", lambda s=shape: self.pick_shape(s), active=on)
            gfx.draw_shape(self.screen, (rect.centerx, rect.y + 24), shape, -math.pi / 2, 14, color)
            hud.text(self.screen, self.fonts.small, "%d  %s" % (shape + 1, SHAPE_LABELS[shape]),
                     (rect.centerx, rect.y + 50), hud.WHITE, "center", shadow=False)
            st = SHAPE_STATS[shape]
            for i, (name, value) in enumerate((("speed", st.max_speed / mx), ("accel", st.accel / ma),
                                               ("turn", st.turn_rate / mt))):
                y = rect.y + 66 + i * 13
                hud.text(self.screen, self.fonts.tiny, name, (rect.x + 10, y + 4), hud.MUTED, "midleft", shadow=False)
                hud.bar(self.screen, pygame.Rect(rect.x + 52, y, rect.w - 64, 8), value, hud.ACCENT)

    # ------------------------------------------------------------------ drawing: screens
    def draw(self, dt: float) -> None:
        self.buttons = []
        if self.scene in MENU_SCENES:
            self.draw_menu()
            return
        self.ui_scale, self.ui_offset = 1.0, (0, 0)
        {RACE: lambda: self.draw_race(dt), RESULTS: lambda: self.draw_race(dt)}[self.scene]()

    def draw_menu(self) -> None:
        """The menus are laid out for MENU_SIZE.  In a window that big or bigger they are drawn on a centred part
        of the window; in a smaller one they are drawn at full size on a see-through canvas that is then shrunk,
        so the buttons and the track picture are never cut off."""
        window = self.screen
        self.backdrop()                                      # the whole window
        scale, offset = menu_fit(window.get_size())
        self.ui_scale, self.ui_offset = scale, offset
        draw = {CONNECT: self.draw_connect, JOINING_SCREEN: self.draw_joining, LOBBY: self.draw_lobby}[self.scene]
        self._in_menu = True
        try:
            if scale >= 1.0:
                self.screen = window.subsurface(pygame.Rect(offset, MENU_SIZE))
                draw()
            else:
                if self._menu is None:
                    self._menu = pygame.Surface(MENU_SIZE, pygame.SRCALPHA)
                self._menu.fill((0, 0, 0, 0))
                self.screen = self._menu
                draw()
                window.blit(pygame.transform.smoothscale(self._menu, (max(1, int(MENU_SIZE[0] * scale)),
                                                                      max(1, int(MENU_SIZE[1] * scale)))), offset)
        finally:
            self.screen = window
            self._in_menu = False

    def draw_connect(self) -> None:
        s, f = self.screen, self.fonts
        w, h = s.get_size()
        self.backdrop()
        cx, top = w // 2, max(10, h // 2 - 330)
        hud.text(s, f.huge, "RACE 17", (cx, top + 75), hud.ACCENT, "center")
        hud.text(s, f.normal, "multiplayer racing over UDP  -  circle, square or triangle",
                 (cx, top + 145), hud.MUTED, "center")
        form = pygame.Rect(0, 0, 520, 372)
        form.midtop = (cx, top + 175)
        hud.panel(s, form, 190)
        server_rect = pygame.Rect(form.x + 30, form.y + 44, 460, 42)
        name_rect = pygame.Rect(form.x + 30, form.y + 118, 460, 42)
        self.server_field.draw(s, f, server_rect, self.focus == 0, self.t)
        self.name_field.draw(s, f, name_rect, self.focus == 1, self.t)
        self.buttons.append((server_rect, lambda: setattr(self, "focus", 0)))
        self.buttons.append((name_rect, lambda: setattr(self, "focus", 1)))
        hud.text(s, f.small, "Shape  (left / right, or 1 2 3)", (form.x + 30, form.y + 174), hud.MUTED, shadow=False)
        self.shape_cards((form.x + 30, form.y + 196), self.shape, (205, 210, 222))
        self.button(pygame.Rect(form.x + 30, form.bottom - 56, 460, 42), "CONNECT   (Enter)", self.connect)
        if self.focus == 2:
            pygame.draw.rect(s, hud.ACCENT, pygame.Rect(form.x + 26, form.y + 192, 468, 116), 2, border_radius=12)
        for i, line in enumerate(wrap_text(f.normal, self.message, 520)):
            hud.text(s, f.normal, line, (cx, form.bottom + 22 + i * 24), hud.RED, "center")
        hud.text(s, f.small, "Tab = next field    Enter = connect    Esc = quit    F11 = full screen",
                 (cx, h - 22), hud.MUTED, "center", shadow=False)

    def draw_joining(self) -> None:
        s, f, c = self.screen, self.fonts, self.client
        w, h = s.get_size()
        self.backdrop()
        age = self.t - self.scene_since
        hud.text(s, f.big, "Connecting" + "." * (int(age * 3) % 4), (w // 2, h // 2 - 40), hud.WHITE, "center")
        hud.text(s, f.normal, "%s:%d" % c.server, (w // 2, h // 2 + 6), hud.MUTED, "center")
        bar = pygame.Rect(0, 0, 320, 10)
        bar.center = (w // 2, h // 2 + 44)
        hud.bar(s, bar, age / P.JOIN_TIMEOUT_S, hud.ACCENT)
        hud.text(s, f.small, "Esc = cancel", (w // 2, h // 2 + 80), hud.MUTED, "center", shadow=False)

    def draw_lobby(self) -> None:
        s, f, c = self.screen, self.fonts, self.client
        w, h = s.get_size()
        self.backdrop()
        pad = 30
        hud.text(s, f.big, "LOBBY", (pad, 20))
        rtt = "%.0f ms" % c.rtt_ms[-1] if c.rtt_ms else "-"
        hud.text(s, f.small, "server %s:%d     RTT %s" % (c.server[0], c.server[1], rtt), (w - pad, 34),
                 hud.MUTED, "topright", shadow=False)
        # --- players
        entries = sorted(c.roster.values(), key=lambda e: e.player_id)
        row_h = max(20, min(32, (h - 200) // max(1, len(entries))))
        box = pygame.Rect(pad, 78, min(500, w // 2 - pad), 52 + row_h * max(1, len(entries)))
        hud.panel(s, box)
        hud.text(s, f.bold, "PLAYERS  %d / %d" % (len(entries), c.ack.max_players), (box.x + 16, box.y + 12),
                 hud.MUTED, shadow=False)
        if not entries:
            hud.text(s, f.normal, "waiting for the player list...", (box.x + 16, box.y + 56), hud.MUTED)
        for i, e in enumerate(entries):
            cy = box.y + 52 + i * row_h + row_h // 2
            if e.player_id == c.player_id:
                glow = pygame.Surface((box.w - 12, row_h - 2), pygame.SRCALPHA)
                glow.fill((255, 255, 255, 30))
                s.blit(glow, (box.x + 6, cy - row_h // 2))
            gfx.draw_shape(s, (box.x + 30, cy), e.shape, -math.pi / 2, 9, gfx.slot_color(e.slot))
            hud.text(s, f.normal, e.name[:16] + ("  (you)" if e.player_id == c.player_id else ""),
                     (box.x + 52, cy), hud.WHITE, "midleft")
            if e.host:
                hud.text(s, f.tiny, "HOST", (box.x + 262, cy), hud.GOLD, "midleft")
            hud.text(s, f.small, "READY" if e.ready else "not ready", (box.right - 16, cy),
                     hud.GREEN if e.ready else hud.MUTED, "midright")
        # --- track + controls
        x0 = box.right + 30
        width = max(260, w - x0 - pad)
        prev = pygame.Rect(x0, 78, min(width, 470), 296)
        hud.panel(s, prev)
        if self.big_map is not None:
            surf = self.big_map.surface
            s.blit(surf, surf.get_rect(center=(prev.centerx, prev.y + 142)))
        t = c.track
        info = "%d lap%s  |  %d m per lap  |  %d checkpoints  |  %d obstacles" % (
            c.ack.laps, "" if c.ack.laps == 1 else "s", round(t.length / UNITS_PER_METER), len(t.checkpoints),
            len(t.obstacles))
        hud.text(s, f.small, info, (prev.centerx, prev.bottom - 18), hud.MUTED, "center", shadow=False)
        mine = c.roster.get(c.player_id)
        pending = c.pending_arg(P.CMD_SHAPE)
        shape = pending if pending is not None else (mine.shape if mine else c.shape)
        color = gfx.slot_color(mine.slot if mine else c.slot)
        hud.text(s, f.small, "Your shape  (keys 1 2 3)", (x0, prev.bottom + 12), hud.MUTED, shadow=False)
        self.shape_cards((x0, prev.bottom + 34), shape, color)
        by = prev.bottom + 34 + 108 + 16
        ready_pending = c.pending_arg(P.CMD_READY)
        ready = bool(ready_pending) if ready_pending is not None else bool(mine and mine.ready)
        self.button(pygame.Rect(x0, by, 226, 44), "READY  (R)" if ready else "get READY  (R)", self.toggle_ready,
                    active=ready)
        self.button(pygame.Rect(x0 + 236, by, 226, 44), "START RACE  (Enter)", self.start_race, enabled=c.is_host)
        status = ("You are the host: press Enter when everyone is ready." if c.is_host
                  else "Waiting for the host to start the race...")
        hud.text(s, f.normal, status, (x0, by + 62), hud.WHITE if c.is_host else hud.MUTED)
        hud.text(s, f.small, "Esc = leave the lobby", (pad, h - 22), hud.MUTED, "midleft", shadow=False)
        self.toasts.draw(s, f, self.t, box.centerx, h - 125, limit=3)

    def draw_race(self, dt: float) -> None:
        s, f, c, cam = self.screen, self.fonts, self.client, self.cam
        w, h = s.get_size()
        now = c.clock()
        poses = c.buffer.sample(now)                                   # everybody, 100 ms in the past
        mine = c.buffer.sample(now, delay=0.0).get(c.player_id)       # my own car: right now
        if mine is not None:
            poses[c.player_id] = mine
            look = max(0.0, min(mine.speed, 360.0)) * 0.25
            cam.follow(mine.x + math.cos(mine.heading) * look, mine.y + math.sin(mine.heading) * look, dt)
        elif not cam.placed:
            sx, sy = c.track.frame_at(0.0)[:2]
            cam.snap(sx, sy)
        ox = oy = 0.0
        if self.shake > 0.0:
            amp = 7.0 * self.shake
            ox, oy = random.uniform(-amp, amp), random.uniform(-amp, amp)
            self.shake = max(0.0, self.shake - dt * 3.0)
        cam.x, cam.y = cam.x + ox, cam.y + oy
        s.fill(gfx.GRASS)
        self.view.draw(s, cam)
        gfx.draw_obstacles(s, cam, c.track, c.race_time_now(now) + c.buffer.delay)
        order = sorted(poses.items(), key=lambda kv: (kv[0] == c.player_id, not kv[1].status & P.STATUS_WRECKED, kv[0]))
        for pid, pose in order:
            shape, color = hud.player_style(c, pid)
            state = c.players.get(pid)
            gfx.draw_player(s, cam, pose, shape=shape, color=color, name=hud.player_name(c, pid),
                            health=state.health if state else 0, local=pid == c.player_id, font=f.tiny, t=self.t)
        cam.x, cam.y = cam.x - ox, cam.y - oy
        if self.flash > 0.0:
            veil = pygame.Surface((w, h), pygame.SRCALPHA)
            veil.fill((220, 40, 30, int(150 * min(1.0, self.flash / 0.35))))
            s.blit(veil, (0, 0))
            self.flash = max(0.0, self.flash - dt)
        self.draw_hud(w, h, mine)

    def draw_hud(self, w: int, h: int, mine) -> None:
        s, f, c = self.screen, self.fonts, self.client
        rows = hud.build_rows(c)
        pad = 10
        if self.scene == RESULTS:
            self.toasts.draw(s, f, self.t, w // 2, pad + 4)
            hud.draw_results(s, f, rows, c.race_time_ms / 1000.0, c.countdown_ms / 1000.0)
            return
        sw = 300
        hud.draw_standings(s, f, rows, pygame.Rect(w - sw - pad, pad, sw, min(h - 2 * pad - 20, 44 + 46 * len(rows))))
        hud.draw_minimap(s, self.minimap, c, (pad, pad))
        hud.draw_self_panel(s, f, c, rows, pygame.Rect(pad, h - 110 - pad, 340, 110))
        hud.draw_netinfo(s, f, c, (w - pad, h - 6), self.fps, hint=not self.show_net)
        if self.show_net:
            top = pad + self.minimap.surface.get_height() + 8 + pad
            room = (h - 110 - pad) - 8 - top                 # down to the own-status panel
            if room >= 160:                                  # its six rows need 160 px; in a shorter window it would cover it
                hud.draw_netpanel(s, f, c, pygame.Rect(pad, top, 300, min(250, room)), self.rates)
        self.toasts.draw(s, f, self.t, w // 2 - 10, pad + 4)
        me = c.me
        if me is not None and mine is not None and c.game_state == P.ST_RACING:
            if me.status & P.STATUS_WRECKED:
                hud.text(s, f.big, "WRECKED  -  back in a moment", (w // 2, h // 2 - 130), hud.RED, "center")
            elif wrong_way(c.track, mine.x, mine.y, mine.heading, mine.speed):
                self.wrong_since = self.wrong_since if self.wrong_since is not None else self.t
                if self.t - self.wrong_since > WRONG_WAY_AFTER_S and int(self.t * 3) % 2 == 0:
                    hud.text(s, f.big, "WRONG WAY", (w // 2, h // 2 - 130), hud.RED, "center")
            else:
                self.wrong_since = None
        if c.game_state == P.ST_COUNTDOWN:
            hud.draw_countdown(s, f, c)
        else:
            hud.draw_go(s, f, c.race_time_ms)

    # ------------------------------------------------------------------ screenshots (selftest / documentation)
    def maybe_shot(self) -> None:
        if not self.shots_dir:
            return
        c, age = self.client, self.t - self.scene_since
        name = None
        if self.scene == CONNECT and age > 0.2:
            name = "connect_error" if self.message else "connect"
        elif self.scene == JOINING_SCREEN:
            name = "joining"
        elif self.scene == LOBBY and age > 1.2:
            name = "lobby"
        elif self.scene == RACE and c is not None:
            if c.game_state == P.ST_COUNTDOWN and age > 0.8:
                name = "countdown"
            elif c.game_state == P.ST_RACING and self.t >= self._next_race_shot:
                name = "race_%02d" % (len([n for n in self._shots_done if n.startswith("race_")]) + 1)
                self._next_race_shot = self.t + self.race_shot_every
        elif self.scene == RESULTS and age > 1.0:
            name = "results"
        if name and name not in self._shots_done:
            self._shots_done.add(name)
            path = os.path.join(self.shots_dir, name + ".png")
            pygame.image.save(self.screen, path)
            self.shot_log.append(path)

    # ------------------------------------------------------------------ main loop
    def capture_final(self) -> Optional[dict]:
        c = self.client
        if c is None:
            return None
        joined = c.state == JOINED and c.track is not None
        return {"state": c.state, "summary": c.summary(), "me": c.me, "rows": hud.build_rows(c) if joined else []}

    def idle_until(self, deadline: float) -> None:
        """Wait for the next frame but keep reading the network, so PONGs and snapshots are handled at once
        (the RTT and jitter shown are then the network's, not the frame rate's)."""
        while True:
            left = deadline - time.monotonic()
            if left <= 0.0:
                return
            if self.client is not None:
                self.client.pump()
            time.sleep(min(left, 0.002))

    def run(self, duration: float = 0.0, stop_after_results: float = 0.0, stop_on_error: bool = False) -> None:
        """Run until the window is closed.  The other arguments are for the selftest."""
        started = last = time.monotonic()
        try:
            while self.running:
                for e in pygame.event.get():
                    self.handle_event(e)
                now = time.monotonic()
                self.frame(min(0.1, now - last))
                last = now
                if duration and now - started > duration:
                    break
                if stop_after_results and self.results_seen_at is not None \
                        and self.t - self.results_seen_at > stop_after_results:
                    break
                if stop_on_error and self.message and self.scene == CONNECT:
                    break
                self.idle_until(now + 1.0 / FPS)
        finally:
            if self.final is None:
                self.final = self.capture_final()
            self.close_client()
            pygame.quit()


# ---------------------------------------------------------------------- command line
def selftest_report(app: App) -> int:
    print("selftest: %d screenshot(s) saved in %s" % (len(app.shot_log), app.shots_dir or "-"))
    for path in app.shot_log:
        print("   ", path)
    final = app.final
    if final is None or not final["rows"]:
        print("RESULT: never joined a game.", app.message or "")
        return 1
    print("final standings:")
    for r in final["rows"]:
        print("   %2d. %-14s %s%s" % (r["rank"], r["name"], r["label"], "   <- you" if r["local"] else ""))
    keys = ("rtt_mean_ms", "rtt_p95_ms", "jitter_ms", "rx_loss_pct", "snapshots", "events", "bad_datagrams")
    print("network: " + "  ".join("%s=%.2f" % (k, final["summary"][k]) for k in keys))
    me = final["me"]
    ok = me is not None and bool(me.status & P.STATUS_FINISHED)
    print("RESULT:", "race finished" if ok else "the local car did not finish")
    return 0 if ok else 1


def main(argv: Optional[List[str]] = None) -> int:
    tolerant_output()
    ap = argparse.ArgumentParser(prog="python -m app.client", description="Race 17: the pygame client.")
    ap.add_argument("--server", default="127.0.0.1:%d" % P.DEFAULT_PORT, metavar="HOST:PORT")
    ap.add_argument("--name", default="", help="your name (default: your user name)")
    ap.add_argument("--shape", choices=("circle", "square", "triangle"), default="circle")
    ap.add_argument("--size", default=None, metavar="WxH",
                    help="window size (default %dx%d, made smaller if your screen is small)" % DEFAULT_SIZE)
    ap.add_argument("--local", action="store_true",
                    help="try the game alone: start a private server and autopilot opponents inside this program")
    ap.add_argument("--bots", type=int, default=3, help="with --local: number of autopilot opponents (default 3)")
    ap.add_argument("--laps", type=int, default=3, help="with --local: laps per race (default 3)")
    ap.add_argument("--auto", action="store_true", help="the autopilot connects, readies up, starts (host) and drives")
    ap.add_argument("--skill", type=float, default=0.9, help="autopilot skill 0.5 .. 1.0")
    ap.add_argument("--wait", type=int, default=0, metavar="N",
                    help="auto host: start when N players are in (default: everybody of --local, else 1)")
    ap.add_argument("--selftest", type=float, metavar="SECONDS",
                    help="no window: autopilot plays one race (at most SECONDS), exit code 0 if it finished")
    ap.add_argument("--shots", metavar="DIR", help="save PNG screenshots of every screen in DIR")
    ap.add_argument("--net", action="store_true", help="start with the F3 network panel open")
    args = ap.parse_args(argv)
    if args.selftest:
        os.environ["SDL_VIDEODRIVER"] = "dummy"
        os.environ["SDL_AUDIODRIVER"] = "dummy"
    if args.size is None:
        width, height = fit_size(DEFAULT_SIZE, None if args.selftest else desktop_size())
    else:
        try:
            width, height = (int(v) for v in args.size.lower().split("x"))
        except ValueError:
            ap.error("--size must look like 1100x700")
    local: Optional[LocalGame] = None
    if args.local:
        try:
            local = LocalGame(args.laps, max(0, args.bots), skill=0.85)
        except (ValueError, OSError) as exc:
            print("cannot start the local game: %s" % exc, file=sys.stderr)
            return 2
        local.start()
        args.server = "%s:%d" % local.address
    wait = args.wait or (args.bots + 1 if local else 1)
    try:
        app = App(args.server, args.name, ("circle", "square", "triangle").index(args.shape), size=(width, height),
                  auto=args.auto or bool(args.selftest), skill=args.skill, wait_players=wait,
                  shots_dir=args.shots, autoconnect=local is not None, show_net=args.net)
        if not args.selftest:
            app.run()
            return 0
        app.run(duration=args.selftest, stop_after_results=2.0, stop_on_error=True)
        return selftest_report(app)
    finally:
        if local is not None:
            local.close()


if __name__ == "__main__":
    sys.exit(main())
