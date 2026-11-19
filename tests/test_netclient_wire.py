"""NetClient against the real GameServer, joined by in-memory sockets (tests/nethelpers.py::Wire).

There is no network and the clock is fake, so every run is identical.  The lossy test drops and
duplicates datagrams on the way and checks that the race, the events and the statistics still come out
right; the real-UDP version of that is tests/test_loopback.py."""
import unittest

from app import protocol as P
from app.netclient import DISCONNECTED, JOINED, REJECTED
from tests.nethelpers import Wire


class WireTests(unittest.TestCase):
    def join_all(self, w, *clients, seconds=6.0):
        self.assertTrue(w.run_until(lambda: all(c.state == JOINED for c in clients), seconds))

    def start_race(self, w, host, *others, seconds=6.0):
        """The host starts the race once it knows it is the host; everybody ends up RACING."""
        self.assertTrue(w.run_until(lambda: host.is_host, seconds))
        host.send_cmd(P.CMD_START)
        racers = (host,) + others
        self.assertTrue(w.run_until(lambda: all(c.game_state == P.ST_RACING for c in racers), seconds))

    # ------------------------------------------------------------------ a normal race
    def test_two_players_race_over_the_wire(self):
        w = Wire(laps=1)
        a, b = w.add("alice", P.SHAPE_CIRCLE), w.add("bob", P.SHAPE_TRIANGLE)
        self.join_all(w, a, b)
        self.assertEqual((a.player_id, b.player_id), (1, 2))
        self.assertEqual(a.track.hash, w.server.game.track.hash)
        self.assertTrue(w.run_until(lambda: len(a.roster) == 2 and len(b.roster) == 2, 3.0))
        self.assertEqual({e.name: e.shape for e in a.roster.values()},
                         {"alice": P.SHAPE_CIRCLE, "bob": P.SHAPE_TRIANGLE})
        self.start_race(w, a, b)
        self.assertEqual((a.is_host, b.is_host), (True, False))
        # alice drives straight ahead until she hits an obstacle; bob stays on the grid
        a.set_controls(1.0, 0.0)
        self.assertTrue(w.run_until(lambda: a.me.health < 100, 8.0))
        w.run(0.3)
        # everyone sees everyone: position, health, and the hit itself
        self.assertEqual(sorted(b.players), [1, 2])
        self.assertGreater(a.me.speed, 50)
        self.assertGreater(b.players[1].progress, b.players[2].progress)
        self.assertEqual((b.players[1].health, b.players[1].progress),
                         (a.players[1].health, a.players[1].progress))
        self.assertLess(b.players[1].health, 100)
        hits = [e for e in w.events["bob"] if e.type == P.EV_HIT]
        self.assertTrue(hits and hits[0].player == 1)
        # one RACE_START each; alice saw bob arrive, bob never sees what happened before he joined
        for name in ("alice", "bob"):
            self.assertEqual([e.type for e in w.events[name]].count(P.EV_RACE_START), 1)
        first = w.events["alice"][0]
        self.assertEqual((first.id, first.type, first.player), (2, P.EV_PLAYER_JOIN, 2))
        self.assertFalse([e for e in w.events["bob"] if e.type == P.EV_PLAYER_JOIN])
        since_start = lambda name: [(e.id, e.type) for e in w.events[name] if e.id >= 3]
        self.assertEqual(since_start("alice"), since_start("bob"))
        # a graceful leave reaches the server at once and the others hear about it
        b.leave()
        w.run(0.2)
        self.assertEqual(len(w.server.clients), 1)
        leaves = [(e.player, e.a) for e in w.events["alice"] if e.type == P.EV_PLAYER_LEAVE]
        self.assertEqual(leaves, [(2, P.DISC_LEFT)])

    def test_lobby_commands_reach_everyone(self):
        w = Wire()
        a, b = w.add("alice"), w.add("bob")
        self.join_all(w, a, b)
        b.send_cmd(P.CMD_READY, 1)
        b.send_cmd(P.CMD_SHAPE, P.SHAPE_SQUARE)
        both_see = lambda: all(c.roster.get(2) is not None and c.roster[2].ready
                               and c.roster[2].shape == P.SHAPE_SQUARE for c in (a, b))
        self.assertTrue(w.run_until(both_see, 3.0))
        self.assertEqual(w.server.game.players[2].shape, P.SHAPE_SQUARE)

    # ------------------------------------------------------------------ a bad network
    def test_race_survives_loss_and_duplicates(self):
        down = lambda i, d: 0 if i % 3 == 1 else (2 if i % 5 == 0 else 1)     # lose a third, double a fifth
        up = lambda i, d: 0 if i % 4 == 3 else 1                              # lose a quarter of the client packets
        w = Wire(laps=1, down_filter=down, up_filter=up)
        a, b = w.add("alice"), w.add("bob")
        self.join_all(w, a, b)           # JOIN is repeated and the server re-sends its cached JOIN_ACK
        self.start_race(w, a, b)
        a.set_controls(1.0, 0.0)
        w.run(1.0)
        before = a.me.progress
        w.run(2.0)
        self.assertGreater(a.me.progress, before)
        self.assertEqual(sorted(b.players), [1, 2])
        self.assertEqual(sorted(a.buffer.sample(w.clock())), [1, 2])
        for name in ("alice", "bob"):
            ids = [e.id for e in w.events[name]]
            self.assertEqual(ids, sorted(set(ids)), name)                     # in order, none twice
            self.assertEqual([e.type for e in w.events[name]].count(P.EV_RACE_START), 1, name)
        self.assertGreater(a.summary()["rx_loss_pct"], 5.0)
        self.assertGreater(a.tracker.duplicates, 0)

    # ------------------------------------------------------------------ the server's side of a dead client
    def test_a_client_that_stops_talking_is_removed_and_told(self):
        w = Wire()
        a, b = w.add("alice"), w.add("bob")
        self.join_all(w, a, b)
        for _ in range(int(6.5 / w.server.game.dt)):         # bob's program freezes; alice carries on
            w.step(skip=(b,))
        self.assertEqual(len(w.server.clients), 1)
        b.pump()                                             # he wakes up: the DISCONNECT is waiting for him
        self.assertEqual((b.state, b.disconnect_reason), (DISCONNECTED, P.DISC_TIMEOUT))
        leaves = [(e.player, e.a) for e in w.events["alice"] if e.type == P.EV_PLAYER_LEAVE]
        self.assertEqual(leaves, [(2, P.DISC_TIMEOUT)])

    def test_a_restarted_server_is_noticed_and_a_new_start_rejoins(self):
        w = Wire()
        a = w.add("alice")
        self.join_all(w, a)
        w.server = w.new_server()                            # a fresh server: it knows no sessions
        w.run(1.0)
        self.assertEqual((a.state, a.disconnect_reason), (DISCONNECTED, P.DISC_UNKNOWN_SESSION))
        a.start()                                            # reconnect: a brand new session
        self.join_all(w, a, seconds=3.0)
        self.assertTrue(w.run_until(lambda: a.snapshot_count > 0, 3.0))
        self.assertEqual(a.player_id, 1)

    # ------------------------------------------------------------------ refusals
    def test_joining_a_running_race_is_refused(self):
        w = Wire()
        a = w.add("alice")
        self.join_all(w, a)
        self.start_race(w, a)                                # the host can start alone
        late = w.add("late")
        self.assertTrue(w.run_until(lambda: late.state == REJECTED, 3.0))
        self.assertEqual(late.reject_reason, P.REJ_IN_PROGRESS)
        self.assertEqual(late.error, P.REASON_TEXT[P.REJ_IN_PROGRESS])

    def test_a_full_room_is_refused(self):
        w = Wire(max_players=2)
        a, b, c = w.add("alice"), w.add("bob"), w.add("carol")
        self.assertTrue(w.run_until(lambda: c.state == REJECTED, 3.0))
        self.assertEqual((a.state, b.state), (JOINED, JOINED))
        self.assertEqual((c.reject_reason, c.error), (P.REJ_FULL, P.REASON_TEXT[P.REJ_FULL]))


if __name__ == "__main__":
    unittest.main()
