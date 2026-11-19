"""protocol.py - wire protocol for the Project 17 UDP racing game.

Everything that travels over the network is defined here, so the server, the
pygame client, the bots (Role 3) and the SDN controller (Role 2, for traffic
classification) all share one contract.  See docs/PROTOCOL.md for the tables.

All integers are big-endian (network byte order).  Every datagram starts with
the same 16-byte header followed by a message-specific payload::

    offset size field    meaning
    0      2    magic    0x1717, rejects stray traffic
    2      1    version  PROTOCOL_VERSION
    3      1    type     MSG_* code
    4      2    seq      sender's packet counter (per direction, wraps at 65536)
    6      2    ack      highest seq received from the peer so far
    8      4    session  token handed out in JOIN_ACK (0 before joining)
    12     4    ts       sender's millisecond clock (uint32, wraps)

Loss / reordering strategy (summary):
  * every packet carries ``seq``; receivers use :class:`SeqTracker` to drop
    duplicates and stale packets and to measure loss / reordering;
  * INPUT and SNAPSHOT are *state* messages: a lost one is replaced by the next;
  * game events are repeated in every SNAPSHOT until the client acknowledges
    them (``ack_event`` in INPUT), and clients de-duplicate by event id.
"""
from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

PROTOCOL_VERSION = 1
MAGIC = 0x1717
DEFAULT_PORT = 5005
MAX_DATAGRAM = 1200          # stay well below the 1500 B MTU: never fragment
NAME_BYTES = 16
MAX_PLAYERS_HARD = 32        # snapshot size stays < MAX_DATAGRAM up to here
EVENTS_PER_SNAPSHOT = 16
TAU = math.tau

# ---------------------------------------------------------------- timing defaults
# Shared by the server, the clients and the bots so every node agrees on them.
INPUT_HZ = 30                # client INPUT packets per second while racing
PING_INTERVAL_S = 1.0        # client RTT probes
KEEPALIVE_S = 0.5            # a joined client sends *something* at least this often
CLIENT_TIMEOUT_S = 5.0       # server drops a client that has been silent this long
SERVER_TIMEOUT_S = 5.0       # client gives up on a server that has been silent this long
JOIN_RETRY_S = 0.5           # JOIN is re-sent at this interval until answered
JOIN_TIMEOUT_S = 5.0         # ... for at most this long
CMD_RETRY_S = 0.25           # a CMD is re-sent until its effect is visible
CMD_MAX_TRIES = 20
ROSTER_REQ_INTERVAL_S = 0.5  # at most one ROSTER_REQ per interval
EVENT_LOG_SIZE = 256         # the server remembers this many recent events

# ---------------------------------------------------------------- message types
MSG_JOIN = 1          # client -> server  request to join
MSG_JOIN_ACK = 2      # server -> client  accepted (carries session + config)
MSG_INPUT = 3         # client -> server  held controls, ~30/s
MSG_CMD = 4           # client -> server  READY / START / SHAPE
MSG_PING = 5          # client -> server  RTT probe
MSG_PONG = 6          # server -> client  RTT reply
MSG_LEAVE = 7         # client -> server  graceful leave
MSG_JOIN_REJECT = 8   # server -> client  refused (full / race running / version)
MSG_SNAPSHOT = 9      # server -> client  full world state, ~20/s
MSG_ROSTER_REQ = 10   # client -> server  "send me the player list"
MSG_ROSTER = 11       # server -> client  names, shapes, ready flags
MSG_DISCONNECT = 12   # server -> client  you were removed (timeout, unknown...)

MSG_NAMES = {1: "JOIN", 2: "JOIN_ACK", 3: "INPUT", 4: "CMD", 5: "PING",
             6: "PONG", 7: "LEAVE", 8: "JOIN_REJECT", 9: "SNAPSHOT",
             10: "ROSTER_REQ", 11: "ROSTER", 12: "DISCONNECT"}

# game states carried in every SNAPSHOT
ST_LOBBY, ST_COUNTDOWN, ST_RACING, ST_FINISHED = 0, 1, 2, 3
STATE_NAMES = ("LOBBY", "COUNTDOWN", "RACING", "FINISHED")

# per-recipient SNAPSHOT flags
SNAPF_HOST = 0x01            # you are the host and may send CMD_START

# per-player status bits inside SNAPSHOT
STATUS_WRECKED = 0x01
STATUS_FINISHED = 0x02
STATUS_INVULN = 0x04
STATUS_STARTED = 0x08        # has crossed the start line
STATUS_DNF = 0x10            # race ended before this player finished

# shapes chosen by the player
SHAPE_CIRCLE, SHAPE_SQUARE, SHAPE_TRIANGLE = 0, 1, 2
SHAPE_NAMES = ("circle", "square", "triangle")

# CMD codes
CMD_READY = 1                # arg 0/1
CMD_START = 2                # host only, arg ignored
CMD_SHAPE = 3                # arg 0..2, lobby only

# JOIN_REJECT reasons / DISCONNECT reasons
REJ_FULL, REJ_IN_PROGRESS, REJ_VERSION, REJ_BAD_REQUEST = 1, 2, 3, 4
DISC_TIMEOUT, DISC_UNKNOWN_SESSION, DISC_SHUTDOWN, DISC_KICKED = 1, 2, 3, 4
DISC_LEFT = 5                # the player quit (LEAVE) or rejoined from the same address
REASON_TEXT = {REJ_FULL: "room is full", REJ_IN_PROGRESS: "race in progress",
               REJ_VERSION: "protocol version mismatch", REJ_BAD_REQUEST: "bad request"}
DISC_TEXT = {DISC_TIMEOUT: "timed out", DISC_UNKNOWN_SESSION: "unknown session",
             DISC_SHUTDOWN: "server shut down", DISC_KICKED: "removed by server",
             DISC_LEFT: "left the game"}

# game events (reliable-by-resend, see module docstring)
EV_PLAYER_JOIN, EV_PLAYER_LEAVE, EV_RACE_START, EV_RACE_END = 1, 2, 3, 4
EV_HIT, EV_CHECKPOINT, EV_LAP, EV_FINISH, EV_WRECK, EV_RESPAWN = 5, 6, 7, 8, 9, 10
EVENT_NAMES = {1: "PLAYER_JOIN", 2: "PLAYER_LEAVE", 3: "RACE_START", 4: "RACE_END",
               5: "HIT", 6: "CHECKPOINT", 7: "LAP", 8: "FINISH", 9: "WRECK",
               10: "RESPAWN"}
# Event field meaning (a: int16, b: int32):
#   HIT        a=damage            b=obstacle id
#   CHECKPOINT a=checkpoint number b=race time ms
#   LAP        a=lap completed     b=lap time ms
#   FINISH     a=place             b=total race time ms
#   PLAYER_LEAVE a=reason (DISC_*)


class ProtocolError(ValueError):
    """Raised for malformed, truncated or oversized datagrams."""


# ---------------------------------------------------------------- seq helpers
def seq_gt(a: int, b: int) -> bool:
    """True if 16-bit sequence number ``a`` is newer than ``b`` (wrap safe)."""
    return a != b and ((a - b) & 0xFFFF) < 0x8000


def seq_diff(a: int, b: int) -> int:
    """Signed distance a-b on the 16-bit circle, in -32768..32767."""
    d = (a - b) & 0xFFFF
    return d - 0x10000 if d >= 0x8000 else d


def seq_next(a: int) -> int:
    return (a + 1) & 0xFFFF


def ms32(seconds: float) -> int:
    """Seconds -> uint32 millisecond timestamp (wraps after ~49 days)."""
    return int(seconds * 1000.0) & 0xFFFFFFFF


def ts_diff(a: int, b: int) -> int:
    """Signed difference of two uint32 millisecond timestamps."""
    d = (a - b) & 0xFFFFFFFF
    return d - 0x100000000 if d >= 0x80000000 else d


class SeqTracker:
    """Receiver-side bookkeeping of one peer's packet sequence numbers.

    ``update(seq)`` classifies each arriving packet and keeps loss statistics:
      NEW        newer than anything seen so far (the normal case)
      REORDERED  older than the newest but never seen before (late arrival)
      DUPLICATE  already seen (network duplicate or replay)
      TOO_OLD    older than the 64-packet window, cannot be classified
    """
    NEW, REORDERED, DUPLICATE, TOO_OLD = 0, 1, 2, 3
    WINDOW = 64
    _MASK = (1 << 64) - 1

    def __init__(self) -> None:
        self.started = False
        self.highest = 0          # newest 16-bit seq
        self.ext_highest = 0      # unwrapped newest seq
        self.base = 0             # unwrapped first seq
        self.mask = 0             # bit i set <=> seq (highest - i) was received
        self.received = 0         # unique packets accepted
        self.reordered = 0
        self.duplicates = 0
        self.too_old = 0

    def update(self, seq: int) -> int:
        if not self.started:
            self.started = True
            self.highest = self.ext_highest = self.base = seq
            self.mask = 1
            self.received = 1
            return self.NEW
        d = seq_diff(seq, self.highest)
        if d > 0:
            self.ext_highest += d
            self.highest = seq
            self.mask = 1 if d >= self.WINDOW else ((self.mask << d) | 1) & self._MASK
            self.received += 1
            return self.NEW
        if d == 0:
            self.duplicates += 1
            return self.DUPLICATE
        age = -d
        if age >= self.WINDOW:
            self.too_old += 1
            return self.TOO_OLD
        bit = 1 << age
        if self.mask & bit:
            self.duplicates += 1
            return self.DUPLICATE
        self.mask |= bit
        self.received += 1
        self.reordered += 1
        return self.REORDERED

    @property
    def expected(self) -> int:
        return (self.ext_highest - self.base + 1) if self.started else 0

    @property
    def lost(self) -> int:
        """Packets that never arrived (late ones are un-counted on arrival)."""
        return max(0, self.expected - self.received)

    @property
    def loss_ratio(self) -> float:
        e = self.expected
        return self.lost / e if e else 0.0


# ---------------------------------------------------------------- header
_HEADER = struct.Struct("!HBBHHII")
HEADER_SIZE = _HEADER.size


@dataclass
class Header:
    msg_type: int
    seq: int
    ack: int
    session: int
    ts: int
    version: int = PROTOCOL_VERSION


def encode(msg_type: int, seq: int, ack: int, session: int, ts: int,
           payload: bytes = b"") -> bytes:
    data = _HEADER.pack(MAGIC, PROTOCOL_VERSION, msg_type, seq & 0xFFFF,
                        ack & 0xFFFF, session & 0xFFFFFFFF, ts & 0xFFFFFFFF) + payload
    if len(data) > MAX_DATAGRAM:
        raise ProtocolError("datagram too large: %d bytes" % len(data))
    return data


def decode(data: bytes) -> Tuple[Header, bytes]:
    """Split a datagram into (Header, payload).  Raises ProtocolError.

    The version is *not* enforced here so the server can answer a JOIN from an
    incompatible client with JOIN_REJECT(REJ_VERSION); callers check it.
    """
    if len(data) < HEADER_SIZE:
        raise ProtocolError("short datagram (%d bytes)" % len(data))
    if len(data) > MAX_DATAGRAM:
        raise ProtocolError("oversized datagram (%d bytes)" % len(data))
    magic, ver, mtype, seq, ack, session, ts = _HEADER.unpack_from(data)
    if magic != MAGIC:
        raise ProtocolError("bad magic 0x%04x" % magic)
    if mtype not in MSG_NAMES:
        raise ProtocolError("unknown message type %d" % mtype)
    return Header(mtype, seq, ack, session, ts, ver), data[HEADER_SIZE:]


# ---------------------------------------------------------------- small helpers
def _clamp(v: int, lo: int, hi: int) -> int:
    return lo if v < lo else hi if v > hi else v


def quantize_axis(x: float) -> int:
    """Control axis -1..1 -> int8."""
    return _clamp(int(round(x * 127.0)), -127, 127)


def dequantize_axis(i: int) -> float:
    return _clamp(i, -127, 127) / 127.0


def angle_to_u16(a: float) -> int:
    return int((a % TAU) / TAU * 65536.0) & 0xFFFF


def u16_to_angle(v: int) -> float:
    return v * TAU / 65536.0


def pack_name(name: str) -> bytes:
    raw = name.encode("utf-8")[:NAME_BYTES]
    raw = raw.decode("utf-8", "ignore").encode("utf-8")      # no cut multibyte char
    return raw.ljust(NAME_BYTES, b"\0")


def unpack_name(raw: bytes) -> str:
    s = raw.split(b"\0", 1)[0].decode("utf-8", "replace")
    s = "".join(ch for ch in s if ch.isprintable()).strip()
    return s or "Player"


def _need(payload: bytes, size: int, what: str) -> None:
    if len(payload) < size:
        raise ProtocolError("%s payload too short (%d < %d)" % (what, len(payload), size))


# ---------------------------------------------------------------- JOIN family
_JOIN = struct.Struct("!I16sB")
_JOIN_ACK = struct.Struct("!IBBBBBBBII")
_JOIN_REJECT = struct.Struct("!IB")


def pack_join(nonce: int, name: str, shape: int) -> bytes:
    return _JOIN.pack(nonce & 0xFFFFFFFF, pack_name(name), _clamp(shape, 0, 255))


def unpack_join(p: bytes) -> Tuple[int, str, int]:
    _need(p, _JOIN.size, "JOIN")
    nonce, name, shape = _JOIN.unpack_from(p)
    if shape > SHAPE_TRIANGLE:
        raise ProtocolError("bad shape %d" % shape)
    return nonce, unpack_name(name), shape


@dataclass
class JoinAck:
    nonce: int
    player_id: int
    slot: int
    tick_rate: int
    snapshot_rate: int
    max_players: int
    laps: int
    track_id: int
    track_seed: int
    track_hash: int


def pack_join_ack(a: JoinAck) -> bytes:
    return _JOIN_ACK.pack(a.nonce, a.player_id, a.slot, a.tick_rate, a.snapshot_rate,
                          a.max_players, a.laps, a.track_id, a.track_seed, a.track_hash)


def unpack_join_ack(p: bytes) -> JoinAck:
    _need(p, _JOIN_ACK.size, "JOIN_ACK")
    return JoinAck(*_JOIN_ACK.unpack_from(p))


def pack_join_reject(nonce: int, reason: int) -> bytes:
    return _JOIN_REJECT.pack(nonce & 0xFFFFFFFF, reason)


def unpack_join_reject(p: bytes) -> Tuple[int, int]:
    _need(p, _JOIN_REJECT.size, "JOIN_REJECT")
    return _JOIN_REJECT.unpack_from(p)


# ---------------------------------------------------------------- INPUT / CMD / PING
_INPUT = struct.Struct("!bbBH")
_CMD = struct.Struct("!BB")
_U16 = struct.Struct("!H")
_U8 = struct.Struct("!B")


def pack_input(throttle: float, steer: float, buttons: int, ack_event: int) -> bytes:
    return _INPUT.pack(quantize_axis(throttle), quantize_axis(steer),
                       buttons & 0xFF, ack_event & 0xFFFF)


def unpack_input(p: bytes) -> Tuple[float, float, int, int]:
    _need(p, _INPUT.size, "INPUT")
    t, s, buttons, ack_event = _INPUT.unpack_from(p)
    return dequantize_axis(t), dequantize_axis(s), buttons, ack_event


def pack_cmd(cmd: int, arg: int = 0) -> bytes:
    return _CMD.pack(cmd & 0xFF, arg & 0xFF)


def unpack_cmd(p: bytes) -> Tuple[int, int]:
    _need(p, _CMD.size, "CMD")
    return _CMD.unpack_from(p)


def pack_ping(ping_id: int) -> bytes:       # PING and PONG share this payload
    return _U16.pack(ping_id & 0xFFFF)


def unpack_ping(p: bytes) -> int:
    _need(p, 2, "PING/PONG")
    return _U16.unpack_from(p)[0]


def pack_reason(reason: int) -> bytes:      # DISCONNECT payload
    return _U8.pack(reason & 0xFF)


def unpack_reason(p: bytes) -> int:
    _need(p, 1, "DISCONNECT")
    return p[0]


# ---------------------------------------------------------------- ROSTER
_ROSTER_HEAD = struct.Struct("!BB")
_ROSTER_ENTRY = struct.Struct("!BBBB16s")


@dataclass
class RosterEntry:
    player_id: int
    slot: int
    shape: int
    ready: bool
    host: bool
    name: str


def pack_roster(version: int, entries: List[RosterEntry]) -> bytes:
    out = [_ROSTER_HEAD.pack(version & 0xFF, len(entries))]
    for e in entries:
        flags = (1 if e.ready else 0) | (2 if e.host else 0)
        out.append(_ROSTER_ENTRY.pack(e.player_id, e.slot, e.shape, flags, pack_name(e.name)))
    return b"".join(out)


def unpack_roster(p: bytes) -> Tuple[int, List[RosterEntry]]:
    _need(p, _ROSTER_HEAD.size, "ROSTER")
    version, n = _ROSTER_HEAD.unpack_from(p)
    if n > MAX_PLAYERS_HARD:
        raise ProtocolError("roster too large (%d)" % n)
    _need(p, _ROSTER_HEAD.size + n * _ROSTER_ENTRY.size, "ROSTER")
    entries, off = [], _ROSTER_HEAD.size
    for _ in range(n):
        pid, slot, shape, flags, name = _ROSTER_ENTRY.unpack_from(p, off)
        off += _ROSTER_ENTRY.size
        entries.append(RosterEntry(pid, slot, min(shape, SHAPE_TRIANGLE),
                                   bool(flags & 1), bool(flags & 2), unpack_name(name)))
    return version, entries


# ---------------------------------------------------------------- SNAPSHOT
_SNAP_HEAD = struct.Struct("!IBBBHIBB")
_PSTATE = struct.Struct("!BffHhBBBBBf")
_EVENT = struct.Struct("!HBBhi")


@dataclass
class PlayerState:
    id: int
    x: float
    y: float
    heading: float          # radians
    speed: float            # world units / s (negative = reversing)
    health: int
    lap: int                # laps completed
    cp: int                 # checkpoints passed in the current lap
    status: int
    place: int              # finishing place, 0 while racing
    progress: float         # unwrapped distance along the track, units


@dataclass
class Event:
    id: int
    type: int
    player: int
    a: int = 0
    b: int = 0


@dataclass
class Snapshot:
    tick: int
    state: int
    flags: int
    roster_ver: int
    countdown_ms: int
    race_time_ms: int
    players: List[PlayerState] = field(default_factory=list)
    events: List[Event] = field(default_factory=list)


def pack_players(players: List[PlayerState]) -> bytes:
    """Encode the per-player block once; it is shared by all recipients."""
    out = []
    for p in players:
        out.append(_PSTATE.pack(
            p.id & 0xFF, p.x, p.y, angle_to_u16(p.heading),
            _clamp(int(round(p.speed * 10.0)), -32768, 32767),
            _clamp(int(p.health), 0, 255), _clamp(p.lap, 0, 255), _clamp(p.cp, 0, 255),
            p.status & 0xFF, _clamp(p.place, 0, 255), p.progress))
    return b"".join(out)


def pack_events(events: List[Event]) -> bytes:
    return b"".join(_EVENT.pack(e.id & 0xFFFF, e.type & 0xFF, e.player & 0xFF,
                                _clamp(e.a, -32768, 32767),
                                _clamp(e.b, -2147483648, 2147483647)) for e in events)


def pack_snapshot_parts(tick: int, state: int, flags: int, roster_ver: int,
                        countdown_ms: int, race_time_ms: int, n_players: int,
                        players_blob: bytes, events: List[Event]) -> bytes:
    head = _SNAP_HEAD.pack(tick & 0xFFFFFFFF, state, flags, roster_ver & 0xFF,
                           _clamp(countdown_ms, 0, 0xFFFF), race_time_ms & 0xFFFFFFFF,
                           n_players, len(events))
    return head + players_blob + pack_events(events)


def pack_snapshot(s: Snapshot) -> bytes:
    return pack_snapshot_parts(s.tick, s.state, s.flags, s.roster_ver, s.countdown_ms,
                               s.race_time_ms, len(s.players), pack_players(s.players),
                               s.events)


def unpack_snapshot(p: bytes) -> Snapshot:
    _need(p, _SNAP_HEAD.size, "SNAPSHOT")
    tick, state, flags, rver, cd, rt, n, m = _SNAP_HEAD.unpack_from(p)
    if state > ST_FINISHED:
        raise ProtocolError("bad state %d" % state)
    if n > MAX_PLAYERS_HARD or m > EVENTS_PER_SNAPSHOT:
        raise ProtocolError("snapshot counts out of range (%d,%d)" % (n, m))
    _need(p, _SNAP_HEAD.size + n * _PSTATE.size + m * _EVENT.size, "SNAPSHOT")
    off, players, events = _SNAP_HEAD.size, [], []
    for _ in range(n):
        pid, x, y, hdg, spd, hp, lap, cp, st, place, prog = _PSTATE.unpack_from(p, off)
        off += _PSTATE.size
        if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(prog)):
            raise ProtocolError("non-finite value in snapshot")
        players.append(PlayerState(pid, x, y, u16_to_angle(hdg), spd / 10.0, hp, lap, cp,
                                   st, place, prog))
    for _ in range(m):
        eid, et, ep, ea, eb = _EVENT.unpack_from(p, off)
        off += _EVENT.size
        events.append(Event(eid, et, ep, ea, eb))
    return Snapshot(tick, state, flags, rver, cd, rt, players, events)


def snapshot_wire_size(n_players: int, n_events: int = 0) -> int:
    """Total bytes of one SNAPSHOT datagram (header included)."""
    return HEADER_SIZE + _SNAP_HEAD.size + n_players * _PSTATE.size + n_events * _EVENT.size


INPUT_WIRE_SIZE = HEADER_SIZE + _INPUT.size
