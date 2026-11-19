"""netclient.py - client side of the UDP protocol, with no pygame dependency.

The pygame client (client.py) and Role 3's bots both use :class:`NetClient`::

    c = NetClient(("10.0.0.1", 5005), name="bot1", shape=P.SHAPE_SQUARE)
    c.start()                                   # first JOIN goes out
    while c.state in (JOINING, JOINED):
        c.pump()                                # receive + every timer (retries, ping, INPUT ...)
        if c.state == JOINED and c.game_state == P.ST_RACING and c.me:
            c.set_controls(throttle, steer)
        time.sleep(0.005)

``pump()`` never blocks.  It re-sends JOIN until answered, keeps the session alive,
measures RTT with PING/PONG, re-sends commands until their effect is visible,
acknowledges game events, tracks loss / reordering / jitter of the downlink and
feeds a :class:`SnapshotBuffer` that interpolates positions about 100 ms in the past.

Headless bots for demos:  ``python -m app.netclient --server 127.0.0.1:5005 --bots 3``
"""
from __future__ import annotations

import argparse
import csv
import math
import os
import random
import socket
import sys
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, Dict, List, Optional, Tuple

try:                                    # normal use: python -m app.netclient / imported by app.client
    from . import protocol as P
    from .console import tolerant_output
    from .netstats import Jitter, RateMeter, describe
    from .track import UNITS_PER_METER, Track, build_track
except ImportError:                     # script use: python app/netclient.py
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from app import protocol as P
    from app.console import tolerant_output
    from app.netstats import Jitter, RateMeter, describe
    from app.track import UNITS_PER_METER, Track, build_track

# client states
IDLE, JOINING, JOINED = "idle", "joining", "joined"
REJECTED = "rejected"            # the server refused the JOIN (see reject_reason)
FAILED = "failed"                # no answer to JOIN, or the track does not match
LOST = "lost"                    # the server went silent
DISCONNECTED = "disconnected"    # the server removed us (timeout, shutdown ...)
LEFT = "left"                    # we quit
ACTIVE = (JOINING, JOINED)

IDLE_INPUT_HZ = 5                # INPUT rate in the lobby / results screen (keepalive + event acks)
MAX_EXTRAPOLATION_S = 0.25       # how far past the newest snapshot a car may be extrapolated
TELEPORT_UNITS = 120.0           # a bigger jump between snapshots is a respawn: do not slide


# ---------------------------------------------------------------------- interpolation
@dataclass
class Pose:
    x: float
    y: float
    heading: float
    speed: float
    status: int


def _lerp_angle(a: float, b: float, t: float) -> float:
    d = (b - a + math.pi) % math.tau - math.pi
    return (a + d * t) % math.tau


class SnapshotBuffer:
    """Recent snapshots on the server's tick timeline; ``sample`` renders slightly in the past.

    The mapping from local time to server time uses the *smallest* recent
    ``arrival - server_time`` offset, so network jitter makes packets look late
    rather than early.  Rendering ``delay`` seconds behind lets one lost or late
    snapshot pass without any visible stutter.
    """

    def __init__(self, tick_rate: int = 60, delay: float = 0.1, capacity: int = 64) -> None:
        self.tick_rate, self.delay = tick_rate, delay
        self._snaps: Deque[Tuple[float, P.Snapshot]] = deque(maxlen=capacity)
        self._offsets: Deque[float] = deque(maxlen=100)
        self.offset = 0.0

    def __len__(self) -> int:
        return len(self._snaps)

    def clear(self) -> None:
        self._snaps.clear()
        self._offsets.clear()

    def add(self, snap: P.Snapshot, arrival_s: float) -> bool:
        if self._snaps and snap.tick <= self._snaps[-1][1].tick:
            return False
        server_t = snap.tick / float(self.tick_rate)
        self._snaps.append((server_t, snap))
        self._offsets.append(arrival_s - server_t)
        self.offset = min(self._offsets)
        return True

    @staticmethod
    def _pose(p: P.PlayerState) -> Pose:
        return Pose(p.x, p.y, p.heading, p.speed, p.status)

    def sample(self, now_s: float, delay: Optional[float] = None) -> Dict[int, Pose]:
        """Interpolated pose of every player at ``now_s - offset - delay`` (server time).

        ``delay`` defaults to the buffer's own; 0 shows the newest state, extrapolated (used for the
        local car, so it answers the keys without the interpolation lag)."""
        snaps = self._snaps
        if not snaps:
            return {}
        t = now_s - self.offset - (self.delay if delay is None else delay)
        if t <= snaps[0][0]:
            return {p.id: self._pose(p) for p in snaps[0][1].players}
        if t >= snaps[-1][0]:                                   # newer than anything we have
            st, snap = snaps[-1]
            dt = min(t - st, MAX_EXTRAPOLATION_S)
            out = {}
            for p in snap.players:
                pose = self._pose(p)
                if not p.status & (P.STATUS_WRECKED | P.STATUS_FINISHED):
                    pose.x += math.cos(p.heading) * p.speed * dt
                    pose.y += math.sin(p.heading) * p.speed * dt
                out[p.id] = pose
            return out
        for i in range(len(snaps) - 1, 0, -1):
            if snaps[i - 1][0] <= t <= snaps[i][0]:
                (ta, sa), (tb, sb) = snaps[i - 1], snaps[i]
                break
        alpha = (t - ta) / max(tb - ta, 1e-6)
        before = {p.id: p for p in sa.players}
        out = {}
        for pb in sb.players:
            pa = before.get(pb.id)
            if pa is None:
                out[pb.id] = self._pose(pb)
            elif math.hypot(pb.x - pa.x, pb.y - pa.y) > TELEPORT_UNITS:
                out[pb.id] = self._pose(pa if alpha < 0.5 else pb)
            else:
                out[pb.id] = Pose(pa.x + (pb.x - pa.x) * alpha, pa.y + (pb.y - pa.y) * alpha,
                                  _lerp_angle(pa.heading, pb.heading, alpha),
                                  pa.speed + (pb.speed - pa.speed) * alpha, pb.status)
        return out


# ---------------------------------------------------------------------- view helpers (no pygame)
def remaining_m(player: P.PlayerState, laps: int, track_length: float) -> float:
    """Distance still to drive in metres; 0 once the player has finished."""
    return max(0.0, laps * track_length - player.progress) / UNITS_PER_METER


def ranking(players) -> List[P.PlayerState]:
    """Live order, by the server's rule: finishers by place, then everybody else by distance driven."""
    return sorted(players, key=lambda p: (0, p.place, p.id) if p.status & P.STATUS_FINISHED
                  else (1, -p.progress, p.id))


def parse_server(text: str, default_port: int = P.DEFAULT_PORT) -> Tuple[str, int]:
    """``'10.0.0.1'``, ``'10.0.0.1:5005'``, ``':5005'`` or ``'localhost:6000'`` -> ``(host, port)``.

    Raises ValueError with a message that can be shown to the player as it is."""
    text = text.strip()
    if not text:
        raise ValueError("type the server address, for example 192.168.1.20:5005")
    host, sep, port_text = text.rpartition(":")
    if not sep:
        host, port_text = text, ""
    host = host.strip() or "127.0.0.1"
    if any(ch.isspace() for ch in host):
        raise ValueError("the server address must not contain spaces")
    if port_text.strip() == "":
        return host, default_port
    if not port_text.strip().isdigit() or not 1 <= int(port_text) <= 65535:
        raise ValueError("the port must be a number from 1 to 65535")
    return host, int(port_text)


# ---------------------------------------------------------------------- the client
class NetClient:
    def __init__(self, server: Tuple[str, int], name: str = "Player", shape: int = P.SHAPE_CIRCLE, *,
                 clock: Optional[Callable[[], float]] = None, sock=None,
                 on_event: Optional[Callable[[P.Event], None]] = None,
                 interp_delay: float = 0.1, nonce: Optional[int] = None) -> None:
        self.server = (socket.gethostbyname(server[0]), int(server[1]))
        self.name, self.shape = name, shape
        self.clock = clock or time.monotonic
        self.t0 = self.clock()
        self.on_event = on_event
        self.interp_delay = interp_delay
        self.state = IDLE
        self._seq = 0                           # our outgoing sequence numbers keep counting across sessions
        self._reset_session(nonce)
        self.own_sock = sock is None
        if sock is None:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind(("", 0))
            sock.setblocking(False)
        self.sock = sock

    def _reset_session(self, nonce: Optional[int] = None) -> None:
        """Forget everything that belongs to one session: a new server counts its packets from zero."""
        self.nonce = random.getrandbits(32) if nonce is None else nonce & 0xFFFFFFFF
        self.error = ""                         # human-readable reason for FAILED / LOST
        self.reject_reason = 0                  # REJ_* after REJECTED
        self.disconnect_reason = 0              # DISC_* after DISCONNECTED
        self.session = 0
        self.player_id: Optional[int] = None
        self.slot = 0
        self.ack: Optional[P.JoinAck] = None
        self.track: Optional[Track] = None
        # lobby / race view
        self.roster: Dict[int, P.RosterEntry] = {}
        self.roster_ver: Optional[int] = None
        self.is_host = False
        self.game_state = P.ST_LOBBY
        self.countdown_ms = 0
        self.race_time_ms = 0
        self.players: Dict[int, P.PlayerState] = {}
        self.snapshot: Optional[P.Snapshot] = None
        self.buffer = SnapshotBuffer(60, self.interp_delay)
        self.events: Deque[P.Event] = deque(maxlen=256)
        self.event_count = 0
        self._last_event: Optional[int] = None
        # measurements
        self.tracker = P.SeqTracker()           # server -> us
        self.jitter = Jitter()
        self.rtt_ms: Deque[float] = deque(maxlen=100000)
        self.snapshot_count = 0
        self._first_snapshot_at: Optional[float] = None         # arrival times of the first and the newest snapshot
        self._last_snapshot_at: Optional[float] = None
        self.bad_datagrams = 0
        self.packets_in = self.packets_out = 0          # datagrams to / from the server, this session
        self.bytes_in = self.bytes_out = 0              # their sizes (our header + payload, no IP / UDP header)
        self._arrivals: Deque[float] = deque(maxlen=128)
        # protocol bookkeeping
        self._throttle = self._steer = 0.0
        self._pings: Dict[int, float] = {}
        self._ping_id = 0
        self._cmds: Dict[int, list] = {}        # cmd -> [arg, tries, next_send_time]
        self._join_started = 0.0
        self._next_join = self._next_ping = self._next_input = self._next_roster_req = 0.0
        self._snap_roster_ver: Optional[int] = None
        self.last_rx = 0.0

    # ------------------------------------------------------------------ control
    @property
    def me(self) -> Optional[P.PlayerState]:
        return self.players.get(self.player_id) if self.player_id is not None else None

    @property
    def active(self) -> bool:
        return self.state in ACTIVE

    def start(self) -> None:
        """Begin joining; JOIN is re-sent until the server answers (see ``state``).

        Calling it again while JOINING just sends another JOIN, and while JOINED it does nothing.
        After a failure, a disconnect or ``leave()`` it starts a completely new session (fresh nonce,
        statistics, track and sequence tracking).
        """
        if self.state == JOINED:
            return
        if self.state != IDLE and self.state != JOINING:
            self._reset_session()
        now = self.clock()
        self.state, self.error = JOINING, ""
        self._join_started = self._next_join = now
        self._send_join(now)

    def set_controls(self, throttle: float, steer: float) -> None:
        ok = math.isfinite(throttle) and math.isfinite(steer)
        self._throttle = max(-1.0, min(1.0, throttle)) if ok else 0.0
        self._steer = max(-1.0, min(1.0, steer)) if ok else 0.0

    def send_cmd(self, cmd: int, arg: int = 0) -> None:
        """READY / START / SHAPE; re-sent every 0.25 s until the roster or state shows the effect."""
        if self.state == JOINED:
            self._cmds[cmd] = [arg, 0, 0.0]

    def pending_arg(self, cmd: int) -> Optional[int]:
        """The argument of a command that is still being re-sent (not yet visible in the roster), else None."""
        st = self._cmds.get(cmd)
        return st[0] if st is not None else None

    def leave(self) -> None:
        if self.state == JOINED:
            for _ in range(3):
                self._send(P.MSG_LEAVE)
        if self.state in ACTIVE:
            self.state = LEFT

    def close(self) -> None:
        if self.own_sock:
            try:
                self.sock.close()
            except OSError:
                pass

    def wait_joined(self, timeout: float = 6.0, step: float = 0.005) -> bool:
        """Blocking helper for scripts: pump until the join finishes."""
        end = self.clock() + timeout
        while self.state == JOINING and self.clock() < end:
            self.pump()
            time.sleep(step)
        return self.state == JOINED

    # ------------------------------------------------------------------ sending
    def _ms(self) -> int:
        return P.ms32(self.clock() - self.t0)

    def _send(self, mtype: int, payload: bytes = b"") -> None:
        seq = self._seq
        self._seq = P.seq_next(seq)
        ack = self.tracker.highest if self.tracker.started else 0
        data = P.encode(mtype, seq, ack, self.session, self._ms(), payload)
        try:
            self.sock.sendto(data, self.server)
            self.packets_out += 1
            self.bytes_out += len(data)
        except OSError:
            pass                                # unreachable server: the timers report it

    def _send_join(self, now: float) -> None:
        self._send(P.MSG_JOIN, P.pack_join(self.nonce, self.name, self.shape))
        self._next_join = now + P.JOIN_RETRY_S

    # ------------------------------------------------------------------ pump
    def pump(self, now: Optional[float] = None) -> None:
        now = self.clock() if now is None else now
        for _ in range(256):
            try:
                data, addr = self.sock.recvfrom(2048)
            except BlockingIOError:
                break
            except OSError:                     # Windows: ICMP "port unreachable" from an earlier send
                continue
            self.handle_datagram(data, addr, now)
        self._timers(now)

    def handle_datagram(self, data: bytes, addr: Tuple[str, int], now: float) -> None:
        if addr != self.server or self.state not in ACTIVE:
            return
        self.packets_in += 1
        self.bytes_in += len(data)
        try:
            h, payload = P.decode(data)
            if self.state == JOINING:
                if h.msg_type == P.MSG_JOIN_ACK:
                    self._on_join_ack(h, payload, now)
                elif h.msg_type == P.MSG_JOIN_REJECT:
                    nonce, reason = P.unpack_join_reject(payload)
                    if nonce == self.nonce:
                        self.state, self.reject_reason = REJECTED, reason
                        self.error = P.REASON_TEXT.get(reason, "refused (%d)" % reason)
                return
            if h.session != self.session:       # the server answers an unknown client with session 0
                if h.msg_type == P.MSG_DISCONNECT and h.session == 0:
                    self._on_disconnect(P.unpack_reason(payload))
                return
            self.last_rx = now
            kind = self.tracker.update(h.seq)
            if kind in (P.SeqTracker.DUPLICATE, P.SeqTracker.TOO_OLD):
                return
            if kind == P.SeqTracker.NEW:
                self.jitter.update(h.ts, now)
            if h.msg_type == P.MSG_SNAPSHOT:
                if kind == P.SeqTracker.NEW:    # a late snapshot is already out of date
                    self._on_snapshot(P.unpack_snapshot(payload), now)
            elif h.msg_type == P.MSG_PONG:
                sent = self._pings.pop(P.unpack_ping(payload), None)
                if sent is not None:
                    self.rtt_ms.append((now - sent) * 1000.0)
            elif h.msg_type == P.MSG_ROSTER:
                self._on_roster(*P.unpack_roster(payload))
            elif h.msg_type == P.MSG_DISCONNECT:
                self._on_disconnect(P.unpack_reason(payload))
        except P.ProtocolError:
            self.bad_datagrams += 1

    def _on_join_ack(self, h: P.Header, payload: bytes, now: float) -> None:
        ack = P.unpack_join_ack(payload)
        if ack.nonce != self.nonce:
            return
        try:
            track = build_track(ack.track_id, ack.track_seed)
        except (ValueError, KeyError):
            track = None
        if track is None or track.hash != ack.track_hash:
            self.state = FAILED
            self.error = "the track built here does not match the server's (different versions?)"
            return
        self.ack, self.track = ack, track
        self.session, self.player_id, self.slot = h.session, ack.player_id, ack.slot
        self.buffer = SnapshotBuffer(ack.tick_rate, self.buffer.delay)
        self.state = JOINED
        self.last_rx = now
        self.tracker.update(h.seq)
        self._next_ping = self._next_input = now
        self._next_roster_req = 0.0

    def _on_snapshot(self, snap: P.Snapshot, now: float) -> None:
        self.snapshot, self.snapshot_count = snap, self.snapshot_count + 1
        if self._first_snapshot_at is None:
            self._first_snapshot_at = now
        self._last_snapshot_at = now
        self.game_state, self.countdown_ms, self.race_time_ms = snap.state, snap.countdown_ms, snap.race_time_ms
        self.is_host = bool(snap.flags & P.SNAPF_HOST)
        self.players = {p.id: p for p in snap.players}
        self._snap_roster_ver = snap.roster_ver
        self.buffer.add(snap, now)
        self._arrivals.append(now)
        for ev in snap.events:                  # oldest first; skip what we already processed
            if self._last_event is None or P.seq_gt(ev.id, self._last_event):
                self._last_event = ev.id
                self.events.append(ev)
                self.event_count += 1
                if self.on_event is not None:
                    self.on_event(ev)

    def _on_roster(self, version: int, entries: List[P.RosterEntry]) -> None:
        self.roster = {e.player_id: e for e in entries}
        self.roster_ver = version

    def _on_disconnect(self, reason: int) -> None:
        if self.state in ACTIVE:
            self.state, self.disconnect_reason = DISCONNECTED, reason
            self.error = P.DISC_TEXT.get(reason, "disconnected (%d)" % reason)

    # ------------------------------------------------------------------ timers
    def _timers(self, now: float) -> None:
        if self.state == JOINING:
            if now - self._join_started > P.JOIN_TIMEOUT_S:
                self.state, self.error = FAILED, "no answer from the server"
            elif now >= self._next_join:
                self._send_join(now)
            return
        if self.state != JOINED:
            return
        if now - self.last_rx > P.SERVER_TIMEOUT_S:
            self.state, self.error = LOST, "the server stopped answering"
            return
        if self._snap_roster_ver != self.roster_ver and now >= self._next_roster_req:
            self._send(P.MSG_ROSTER_REQ)
            self._next_roster_req = now + P.ROSTER_REQ_INTERVAL_S
        for cmd, st in list(self._cmds.items()):
            arg, tries, due = st
            if self._cmd_done(cmd, arg) or tries >= P.CMD_MAX_TRIES:
                del self._cmds[cmd]
            elif now >= due:
                self._send(P.MSG_CMD, P.pack_cmd(cmd, arg))
                st[1], st[2] = tries + 1, now + P.CMD_RETRY_S
        if now >= self._next_ping:
            self._ping_id = (self._ping_id + 1) & 0xFFFF
            self._pings[self._ping_id] = now
            for stale in [i for i, t in self._pings.items() if now - t > 10.0]:
                del self._pings[stale]
            self._send(P.MSG_PING, P.pack_ping(self._ping_id))
            self._next_ping = now + P.PING_INTERVAL_S
        if now >= self._next_input:
            hz = P.INPUT_HZ if self.game_state in (P.ST_COUNTDOWN, P.ST_RACING) else IDLE_INPUT_HZ
            self._send(P.MSG_INPUT, P.pack_input(self._throttle, self._steer, 0, self._last_event or 0))
            self._next_input += 1.0 / hz
            if self._next_input < now:
                self._next_input = now + 1.0 / hz

    def _cmd_done(self, cmd: int, arg: int) -> bool:
        mine = self.roster.get(self.player_id)
        if cmd == P.CMD_READY:
            return mine is not None and mine.ready == bool(arg)
        if cmd == P.CMD_SHAPE:
            return mine is not None and mine.shape == arg
        if cmd == P.CMD_START:
            return self.game_state != P.ST_LOBBY or not self.is_host
        return True

    # ------------------------------------------------------------------ measurements
    def updates_per_s(self, now: Optional[float] = None, window: float = 1.0) -> float:
        now = self.clock() if now is None else now
        return sum(1 for t in self._arrivals if now - t <= window) / window

    def snapshot_hz(self) -> float:
        """Snapshots per second on average over the whole time they have been arriving (0 until two arrived)."""
        if self.snapshot_count < 2 or self._first_snapshot_at is None or self._last_snapshot_at is None:
            return 0.0
        span = self._last_snapshot_at - self._first_snapshot_at
        return (self.snapshot_count - 1) / span if span > 0 else 0.0

    def race_time_now(self, now: Optional[float] = None) -> float:
        """Race time in seconds for the frame being drawn (other cars are shown ``interp_delay`` in the past)."""
        base = self.race_time_ms / 1000.0
        if self.game_state != P.ST_RACING or not self._arrivals:
            return base
        now = self.clock() if now is None else now
        return max(0.0, base + (now - self._arrivals[-1]) - self.buffer.delay)

    def summary(self) -> Dict[str, float]:
        """Client-side metrics for the experiments (RTT, jitter, loss, snapshots)."""
        rtt = describe(self.rtt_ms)
        t = self.tracker
        return {"rtt_n": rtt["n"], "rtt_mean_ms": rtt["mean"], "rtt_p50_ms": rtt["p50"],
                "rtt_p95_ms": rtt["p95"], "rtt_max_ms": rtt["max"], "jitter_ms": self.jitter.value,
                "rx_expected": t.expected, "rx_lost": t.lost, "rx_loss_pct": 100.0 * t.loss_ratio,
                "rx_reordered": t.reordered, "rx_duplicates": t.duplicates,
                "snapshots": self.snapshot_count, "snapshot_hz": self.snapshot_hz(), "events": self.event_count,
                "bad_datagrams": self.bad_datagrams,
                "rx_packets": self.packets_in, "rx_bytes": self.bytes_in,
                "tx_packets": self.packets_out, "tx_bytes": self.bytes_out}


# ---------------------------------------------------------------------- bot results
BOT_CSV_COLUMNS = ("label", "bot", "state", "finished", "place", "progress_m", "health",
                   "rtt_n", "rtt_mean_ms", "rtt_p50_ms", "rtt_p95_ms", "rtt_max_ms", "jitter_ms",
                   "rx_expected", "rx_lost", "rx_loss_pct", "rx_reordered", "rx_duplicates",
                   "snapshots", "snapshot_hz", "events", "bad_datagrams",
                   "rx_packets", "rx_bytes", "tx_packets", "tx_bytes")


def write_bot_csv(path: str, label: str, clients: List[NetClient]) -> None:
    """Append one row per bot to a CSV file (the header is written when the file is new).

    This is the client side of the experiment: what the network did to the server -> player direction (loss,
    reordering, jitter, snapshot rate) and the round-trip times, next to how the race ended for that bot.
    ``label`` names the case (for example ``baseline``) exactly like the server's ``--label``."""
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    new = not os.path.exists(path) or os.path.getsize(path) == 0
    with open(path, "a", newline="", encoding="utf-8") as f:
        out = csv.writer(f)
        if new:
            out.writerow(BOT_CSV_COLUMNS)
        for c in clients:
            s, me = c.summary(), c.me
            row = {"label": label, "bot": c.name, "state": c.state,
                   "finished": int(bool(me is not None and me.status & P.STATUS_FINISHED)),
                   "place": me.place if me is not None else 0,
                   "progress_m": round(me.progress / UNITS_PER_METER, 1) if me is not None else 0.0,
                   "health": me.health if me is not None else 0}
            row.update(s)
            out.writerow([round(row[k], 3) if isinstance(row[k], float) else row[k] for k in BOT_CSV_COLUMNS])


# ---------------------------------------------------------------------- headless bots
def run_bots(server: Tuple[str, int], count: int, skill: float = 0.85, start: bool = False,
             duration: float = 0.0, verbose: bool = True, linger: float = 0.0, keep_going: bool = False,
             stop=None, prefix: str = "bot", on_tick=None) -> List[NetClient]:
    """Join ``count`` autopilot players and drive them until the race ends (or ``duration``).

    ``linger`` keeps them in the room that many seconds after the race, so human players see the results.
    ``keep_going`` makes them stay for race after race (they ready up again in every lobby) until ``stop``,
    a threading.Event, is set or ``duration`` has passed.  ``on_tick(clients)``, if given, is called once per loop pass
    (Role 3's app/bot.py samples RTT and snapshot rate with it).  The players are called ``prefix1``, ``prefix2`` ...
    (give each host its own prefix when bots run on several hosts, so the CSV rows can be told apart)."""
    try:
        from .autopilot import Autopilot            # imported late: bots only
    except ImportError:                             # script use
        from app.autopilot import Autopilot
    shapes = (P.SHAPE_CIRCLE, P.SHAPE_SQUARE, P.SHAPE_TRIANGLE)
    clients = [NetClient(server, "%s%d" % (prefix, i + 1), shapes[i % 3]) for i in range(count)]
    for c in clients:
        c.start()
    pilots: Dict[int, Autopilot] = {}
    last_state: Dict[int, int] = {}
    begin = time.monotonic()
    started_race, seen_race, done_at = False, False, None
    try:
        while any(c.active for c in clients) and not (stop is not None and stop.is_set()):
            for i, c in enumerate(clients):
                c.pump()
                if c.state != JOINED:
                    continue
                if c.track is not None and i not in pilots:
                    pilots[i] = Autopilot(c.track, skill=max(0.5, skill - 0.04 * i))
                    c.send_cmd(P.CMD_READY, 1)
                if keep_going and i in pilots:
                    if c.game_state == P.ST_COUNTDOWN and last_state.get(i) != P.ST_COUNTDOWN:
                        pilots[i].reset()                   # a new race: forget the old one
                    if c.game_state == P.ST_LOBBY:
                        mine = c.roster.get(c.player_id)
                        if mine is not None and not mine.ready and c.pending_arg(P.CMD_READY) is None:
                            c.send_cmd(P.CMD_READY, 1)      # ready flags are cleared after every race
                    last_state[i] = c.game_state
                if c.game_state == P.ST_RACING and c.me is not None and not c.me.status & P.STATUS_FINISHED:
                    me = c.me
                    c.set_controls(*pilots[i].control(me.x, me.y, me.heading, me.speed, c.race_time_ms / 1000.0))
                else:
                    c.set_controls(0.0, 0.0)
                if start and c.is_host and c.game_state == P.ST_LOBBY and not started_race \
                        and sum(1 for k in clients if k.state == JOINED) == count:
                    c.send_cmd(P.CMD_START)
                    started_race = True
            if on_tick is not None:
                on_tick(clients)
            racing = any(c.game_state == P.ST_RACING for c in clients if c.state == JOINED)
            seen_race = seen_race or racing
            if not keep_going and seen_race and all(
                    c.game_state in (P.ST_FINISHED, P.ST_LOBBY) for c in clients if c.state == JOINED):
                done_at = time.monotonic() if done_at is None else done_at
                if time.monotonic() - done_at >= linger:
                    break
            if duration and time.monotonic() - begin > duration:
                break
            time.sleep(0.004)
    finally:
        for c in clients:
            c.leave()
            c.close()
    if verbose:
        for c in clients:
            s = c.summary()
            print("%s: %s  rtt %.1f ms (p95 %.1f)  loss %.2f%%  jitter %.2f ms  snapshots %d" % (
                c.name, c.state, s["rtt_mean_ms"], s["rtt_p95_ms"], s["rx_loss_pct"], s["jitter_ms"],
                s["snapshots"]))
    return clients


def main(argv: Optional[List[str]] = None) -> int:
    tolerant_output()
    ap = argparse.ArgumentParser(prog="python -m app.netclient",
                                 description="Headless autopilot players for demos and tests.")
    ap.add_argument("--server", default="127.0.0.1:%d" % P.DEFAULT_PORT, metavar="HOST:PORT")
    ap.add_argument("--bots", type=int, default=3)
    ap.add_argument("--prefix", default="bot", help="bot names are PREFIX1, PREFIX2 ... (default bot)")
    ap.add_argument("--skill", type=float, default=0.85, help="0.5 .. 1.0, scales bot speed")
    ap.add_argument("--start", action="store_true", help="the first bot (host) starts the race")
    ap.add_argument("--duration", type=float, default=0.0, help="stop after this many seconds")
    ap.add_argument("--linger", type=float, default=10.0,
                    help="stay this many seconds after the race so players see the results (default 10)")
    ap.add_argument("--csv", metavar="FILE", help="append one row per bot (RTT, loss, jitter, snapshot rate, result) to FILE")
    ap.add_argument("--label", default="", help="text copied into every CSV row, for example baseline or sdn")
    args = ap.parse_args(argv)
    if sys.platform == "win32":                 # a 4 ms sleep must not be rounded up to 15.6 ms
        try:
            import ctypes
            ctypes.windll.winmm.timeBeginPeriod(1)
        except Exception:
            pass
    try:
        clients = run_bots(parse_server(args.server), args.bots, args.skill, args.start, args.duration,
                           linger=args.linger, prefix=args.prefix)
    except (OSError, ValueError) as exc:
        print("cannot run bots: %s" % exc, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0
    if args.csv:
        try:
            write_bot_csv(args.csv, args.label, clients)
        except OSError as exc:
            print("cannot write %s: %s" % (args.csv, exc), file=sys.stderr)
            return 2
    shut_down = lambda c: c.state == DISCONNECTED and c.disconnect_reason == P.DISC_SHUTDOWN   # server ended the session
    return 0 if all(c.state in (JOINED, LEFT) or shut_down(c) for c in clients) else 1


if __name__ == "__main__":
    sys.exit(main())
