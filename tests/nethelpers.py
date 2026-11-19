"""Helpers for the client tests (this is not a test module).

* ``FakeClock`` / ``FakeSock``  a clock and a datagram socket the tests control.
* ``Env``   one NetClient whose server is played by the test, packet by packet.
* ``Wire``  the real GameServer and real NetClients joined by in-memory sockets on one fake clock,
            with optional packet loss and duplication.  No network, fully deterministic.
"""
from __future__ import annotations

from app import protocol as P
from app.game import GameConfig
from app.netclient import NetClient
from app.server import GameServer
from app.track import build_track

SERVER = ("10.0.0.1", 5005)
NONCE = 4242
SESSION = 0x0A0B0C0D
TRACK = build_track(0, 17)       # the GameConfig defaults (track_id 0, seed 17)


def car(pid, x=0.0, y=0.0, heading=0.0, speed=0.0, *, health=100, lap=0, cp=0, status=0, place=0,
        progress=0.0):
    return P.PlayerState(pid, x, y, heading, speed, health, lap, cp, status, place, progress)


def ev(eid, etype=P.EV_HIT, player=3, a=0, b=0):
    return P.Event(eid, etype, player, a, b)


def make_ack(**over):
    fields = dict(nonce=NONCE, player_id=3, slot=2, tick_rate=60, snapshot_rate=20, max_players=16,
                  laps=2, track_id=0, track_seed=17, track_hash=TRACK.hash)
    fields.update(over)
    return P.JoinAck(**fields)


class FakeClock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class FakeSock:
    """What the client sends piles up in ``sent`` (stamped with the clock); ``inbox`` feeds recvfrom."""

    def __init__(self, clock):
        self.clock = clock
        self.sent = []          # (time, data, addr)
        self.inbox = []         # (data, addr)
        self.closed = False

    def sendto(self, data, addr):
        self.sent.append((self.clock(), data, addr))

    def recvfrom(self, size):
        if not self.inbox:
            raise BlockingIOError
        return self.inbox.pop(0)

    def close(self):
        self.closed = True


class Env:
    """A NetClient on a fake socket and clock; the test plays the server."""

    def __init__(self, **client_kwargs):
        self.clock = FakeClock()
        self.sock = FakeSock(self.clock)
        self.events = []
        self.client = NetClient(SERVER, "Tester", P.SHAPE_SQUARE, clock=self.clock, sock=self.sock,
                                on_event=self.events.append, nonce=NONCE, **client_kwargs)
        self.seq = 0                    # next server -> client sequence number
        self._quiet_since = self.clock.t

    # ---- the server's side
    def deliver(self, mtype, payload=b"", *, session=SESSION, seq=None, addr=SERVER):
        use = self.seq if seq is None else seq
        if P.seq_gt(P.seq_next(use), self.seq):
            self.seq = P.seq_next(use)
        self._quiet_since = self.clock.t
        self.sock.inbox.append((P.encode(mtype, use, 0, session, P.ms32(self.clock.t), payload), addr))

    def accept(self, **over):
        """Start the client, answer its JOIN, and return it JOINED."""
        self.client.start()
        self.deliver(P.MSG_JOIN_ACK, P.pack_join_ack(make_ack(**over)))
        self.client.pump()
        return self.client

    def snapshot(self, tick, players=None, *, state=P.ST_RACING, flags=0, roster_ver=0, countdown_ms=0,
                 race_time_ms=0, events=(), **kw):
        if players is None:
            players = [car(self.client.player_id or 3)]
        snap = P.Snapshot(tick, state, flags, roster_ver, countdown_ms, race_time_ms, list(players),
                          list(events))
        self.deliver(P.MSG_SNAPSHOT, P.pack_snapshot(snap), **kw)

    def roster(self, version, entries, **kw):
        self.deliver(P.MSG_ROSTER, P.pack_roster(version, entries), **kw)

    def pong(self, ping_id, **kw):
        self.deliver(P.MSG_PONG, P.pack_ping(ping_id), **kw)

    # ---- time
    def run(self, seconds, step=0.01, keepalive=True):
        """Advance the fake clock in small steps, pumping the client each time.  With ``keepalive`` a
        harmless PONG arrives after every silent second, so the client does not think the server died."""
        for _ in range(int(round(seconds / step))):
            self.clock.advance(step)
            if keepalive and self.clock.t - self._quiet_since >= 1.0:
                self.pong(0xFFFF)
            self.client.pump()

    # ---- what the client sent
    def sent(self, mtype=None):
        out = []
        for t, data, _addr in self.sock.sent:
            header, payload = P.decode(data)
            if mtype is None or header.msg_type == mtype:
                out.append((t, header, payload))
        return out

    def payloads(self, mtype):
        return [p for _, _, p in self.sent(mtype)]

    def clear(self):
        self.sock.sent.clear()


class WireSock:
    """A client's socket inside a Wire."""

    def __init__(self, addr):
        self.addr = addr
        self.outbox = []        # datagrams the client sent
        self.inbox = []         # (data, addr) waiting for the client
        self.closed = False

    def sendto(self, data, addr):
        self.outbox.append(data)

    def recvfrom(self, size):
        if not self.inbox:
            raise BlockingIOError
        return self.inbox.pop(0)

    def close(self):
        self.closed = True


class ServerSock:
    def __init__(self):
        self.sent = []

    def sendto(self, data, addr):
        self.sent.append((data, addr))

    def getsockname(self):
        return SERVER

    def close(self):
        pass


class Wire:
    """The real GameServer and any number of real NetClients, joined by in-memory sockets.

    ``step()`` advances the shared fake clock by one server tick: the clients pump, their datagrams
    reach the server, the server ticks, and what it sent is queued for the clients (so the one-way
    delay is one tick).  ``down_filter`` / ``up_filter`` get ``(index, datagram)`` for every datagram
    towards the clients / the server and return how many copies get through (0 drops it, 2 duplicates).
    """

    def __init__(self, laps=1, down_filter=None, up_filter=None, **cfg):
        self.clock = FakeClock()
        self.server_sock = ServerSock()
        self.cfg = GameConfig(laps=laps, countdown_s=1.0, **cfg)
        self.server = self.new_server()
        self.clients, self.socks, self.events = [], {}, {}
        self.down_filter, self.up_filter = down_filter, up_filter
        self.down_count = self.up_count = 0

    def new_server(self):
        return GameServer(self.cfg, sock=self.server_sock, clock=self.clock, verbose=False)

    def add(self, name, shape=P.SHAPE_CIRCLE):
        n = len(self.clients)
        sock = WireSock(("10.0.1.%d" % (n + 1), 6000 + n))
        log = []
        client = NetClient(SERVER, name, shape, clock=self.clock, sock=sock, on_event=log.append,
                           nonce=7000 + n)
        self.clients.append(client)
        self.socks[sock.addr] = sock
        self.events[name] = log
        client.start()
        return client

    def step(self, skip=()):
        self.clock.advance(self.server.game.dt)
        for client in self.clients:
            if client not in skip:
                client.pump()
        for sock in self.socks.values():
            for data in sock.outbox:
                copies = 1 if self.up_filter is None else self.up_filter(self.up_count, data)
                self.up_count += 1
                for _ in range(copies):
                    self.server.handle_datagram(data, sock.addr)
            sock.outbox.clear()
        self.server.tick()
        for data, addr in self.server_sock.sent:
            copies = 1 if self.down_filter is None else self.down_filter(self.down_count, data)
            self.down_count += 1
            sock = self.socks.get(addr)
            for _ in range(copies if sock is not None else 0):
                sock.inbox.append((data, SERVER))
        self.server_sock.sent.clear()

    def run(self, seconds, **kw):
        for _ in range(int(round(seconds / self.server.game.dt))):
            self.step(**kw)

    def run_until(self, condition, seconds=10.0):
        for _ in range(int(round(seconds / self.server.game.dt))):
            if condition():
                return True
            self.step()
        return condition()
