"""NetClient tests, part 2: behaviour once joined (INPUT, snapshots, events, RTT, commands, timeouts).

The test plays the server over a fake socket and a fake clock (see nethelpers.py)."""
import unittest

from app import protocol as P
from app.netclient import DISCONNECTED, JOINED, LEFT, LOST
from tests.nethelpers import SERVER, SESSION, Env, car, ev, make_ack


def me_entry(**over):
    fields = dict(player_id=3, slot=2, shape=P.SHAPE_SQUARE, ready=False, host=True, name="Tester")
    fields.update(over)
    return P.RosterEntry(**fields)


class RunningTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.client = self.env.accept()
        self.env.clear()

    def last_input(self):
        return P.unpack_input(self.env.payloads(P.MSG_INPUT)[-1])

    def assert_controls(self, throttle, steer):
        got = self.last_input()
        self.assertAlmostEqual(got[0], throttle, delta=0.01)
        self.assertAlmostEqual(got[1], steer, delta=0.01)

    # ---- traffic counters
    def test_traffic_counters_match_what_crossed_the_socket(self):
        env, c = self.env, self.client
        before = (c.packets_in, c.bytes_in, c.packets_out, c.bytes_out)
        for tick in range(5):
            env.snapshot(tick + 1, state=P.ST_RACING)
        env.deliver(P.MSG_PONG, b"\x00")                           # too short to decode: still a datagram that arrived
        env.deliver(P.MSG_SNAPSHOT, b"", addr=("10.9.9.9", 5005))   # not from the server: not ours, not counted
        arriving = [len(data) for data, addr in env.sock.inbox if addr == SERVER]
        env.run(1.0, keepalive=False)                               # (the helper's own keepalive PONG would add one)
        self.assertEqual(c.packets_in - before[0], len(arriving))
        self.assertEqual(c.bytes_in - before[1], sum(arriving))
        sent = [len(data) for _t, data, _addr in env.sock.sent]
        self.assertGreater(len(sent), 20)                           # INPUT 30 per second and a PING
        self.assertEqual(c.packets_out - before[2], len(sent))
        self.assertEqual(c.bytes_out - before[3], sum(sent))
        s = c.summary()
        self.assertEqual((s["rx_packets"], s["rx_bytes"], s["tx_packets"], s["tx_bytes"]),
                         (c.packets_in, c.bytes_in, c.packets_out, c.bytes_out))

    def test_snapshot_rate_is_the_average_over_the_time_snapshots_arrived(self):
        env, c = self.env, self.client
        self.assertEqual(c.snapshot_hz(), 0.0)
        env.snapshot(1, state=P.ST_RACING)
        env.run(0.01)
        self.assertEqual(c.snapshot_hz(), 0.0)                      # one snapshot is no rate yet
        for tick in range(2, 42):                                   # 40 more, one every 50 ms
            env.run(0.05)
            env.snapshot(tick, state=P.ST_RACING)
        env.run(0.01)
        self.assertAlmostEqual(c.snapshot_hz(), 20.0, delta=0.6)
        self.assertAlmostEqual(c.summary()["snapshot_hz"], c.snapshot_hz())

    # ---- INPUT
    def test_input_rate_is_low_in_the_lobby_and_30_hz_in_a_race(self):
        env = self.env
        env.run(2.0)
        lobby = len(env.sent(P.MSG_INPUT))
        self.assertTrue(9 <= lobby <= 11, lobby)
        env.snapshot(1, state=P.ST_RACING)
        env.run(0.5)                                   # let the slower schedule run out
        env.clear()
        env.run(1.0, step=0.005)
        racing = len(env.sent(P.MSG_INPUT))
        self.assertTrue(28 <= racing <= 32, racing)

    def test_input_carries_the_controls_and_the_last_event_id(self):
        env = self.env
        env.snapshot(1, events=[ev(1), ev(2)])
        self.client.set_controls(0.5, -1.0)
        env.run(0.5)
        self.assert_controls(0.5, -1.0)
        self.assertEqual(self.last_input()[3], 2)

    def test_controls_are_clamped_and_nan_means_stop(self):
        env, c = self.env, self.client
        c.set_controls(9.0, -9.0)
        env.run(0.5)
        self.assert_controls(1.0, -1.0)
        for bad in (float("nan"), float("inf")):
            c.set_controls(bad, 0.7)
            env.run(0.5)
            self.assert_controls(0.0, 0.0)

    # ---- SNAPSHOT
    def test_a_snapshot_updates_what_the_client_knows(self):
        env, c = self.env, self.client
        self.assertIsNone(c.me)
        env.snapshot(30, [car(3, 10.0, 20.0, health=80, lap=1, cp=2, progress=1234.5), car(4, 50.0, 60.0)],
                     flags=P.SNAPF_HOST, roster_ver=2, race_time_ms=1500)
        env.run(0.02)
        self.assertEqual((c.game_state, c.is_host, c.race_time_ms), (P.ST_RACING, True, 1500))
        self.assertEqual(sorted(c.players), [3, 4])
        self.assertEqual((c.me.health, c.me.lap, c.me.cp, c.me.progress), (80, 1, 2, 1234.5))
        self.assertEqual((c.snapshot_count, len(c.buffer)), (1, 1))
        env.snapshot(33, [car(3)], state=P.ST_FINISHED)            # the race is over, no longer host
        env.run(0.02)
        self.assertEqual((c.game_state, c.is_host), (P.ST_FINISHED, False))

    def test_old_and_repeated_snapshots_are_ignored(self):
        env, c = self.env, self.client
        env.snapshot(10, seq=10)
        env.snapshot(18, seq=12)
        env.snapshot(14, seq=11)                    # arrives late
        env.snapshot(18, seq=12)                    # network duplicate
        env.run(0.05)
        self.assertEqual((c.snapshot_count, c.snapshot.tick), (2, 18))
        self.assertEqual((c.tracker.reordered, c.tracker.duplicates), (1, 1))

    def test_a_repeated_join_ack_is_harmless(self):
        env, c = self.env, self.client
        env.deliver(P.MSG_JOIN_ACK, P.pack_join_ack(make_ack()))      # the server re-sent its cached answer
        env.run(0.05)
        self.assertEqual((c.state, c.session, c.player_id), (JOINED, SESSION, 3))

    # ---- events
    def test_events_are_delivered_once_and_in_order(self):
        env = self.env
        env.snapshot(1, events=[ev(1), ev(2), ev(3)])
        env.snapshot(4, events=[ev(2), ev(3), ev(4)])       # re-sent by the server until we acknowledge
        env.run(0.05)
        self.assertEqual([e.id for e in env.events], [1, 2, 3, 4])
        self.assertEqual([e.id for e in self.client.events], [1, 2, 3, 4])
        self.assertEqual(self.client.event_count, 4)

    def test_event_ids_wrap_from_65535_to_1(self):
        env = self.env
        env.snapshot(1, events=[ev(65534), ev(65535)])
        env.snapshot(4, events=[ev(65535), ev(1), ev(2)])
        env.snapshot(7, events=[ev(1), ev(2)])
        env.run(0.5)
        self.assertEqual([e.id for e in env.events], [65534, 65535, 1, 2])
        self.assertEqual(self.last_input()[3], 2)           # and 2 is what we acknowledge

    # ---- RTT, loss, rates
    def roundtrip(self, delay_ms):
        """Wait for the client's next PING, answer it after ``delay_ms`` and pump."""
        env = self.env
        env.clear()
        for _ in range(300):
            if env.sent(P.MSG_PING):
                break
            env.run(0.01)
        ping_id = P.unpack_ping(env.payloads(P.MSG_PING)[-1])
        env.clock.advance(delay_ms / 1000.0)
        env.pong(ping_id)
        self.client.pump()

    def test_ping_and_pong_measure_the_rtt(self):
        self.roundtrip(40)
        self.assertEqual(len(self.client.rtt_ms), 1)
        self.assertAlmostEqual(self.client.rtt_ms[0], 40.0, delta=1.0)

    def test_a_pong_nobody_asked_for_is_ignored(self):
        self.env.pong(0x1234)
        self.env.run(0.05)
        self.assertEqual(len(self.client.rtt_ms), 0)

    def test_summary_carries_the_numbers_for_the_experiments(self):
        for delay in (10, 20, 30, 40, 50):
            self.roundtrip(delay)
        s = self.client.summary()
        self.assertEqual(s["rtt_n"], 5)
        self.assertAlmostEqual(s["rtt_mean_ms"], 30.0, delta=0.5)
        self.assertAlmostEqual(s["rtt_p95_ms"], 48.0, delta=1.0)
        self.assertAlmostEqual(s["rtt_max_ms"], 50.0, delta=0.5)

    def test_loss_is_counted_from_sequence_gaps(self):
        env = self.env
        for seq in (1, 2, 3, 5, 6, 8, 9, 10):               # 4 and 7 never arrive
            env.snapshot(seq * 3, seq=seq)
        env.run(0.05)
        s = self.client.summary()
        self.assertEqual((s["rx_expected"], s["rx_lost"], s["snapshots"]), (11, 2, 8))
        self.assertAlmostEqual(s["rx_loss_pct"], 100.0 * 2 / 11, places=3)

    def test_update_rate_is_measured(self):
        env = self.env
        for i in range(20):                                 # a snapshot every 50 ms
            env.snapshot(i * 3)
            env.run(0.05)
        self.assertAlmostEqual(self.client.updates_per_s(), 20.0, delta=1.5)

    # ---- ROSTER
    def test_roster_is_requested_when_its_version_changes(self):
        env, c = self.env, self.client
        env.run(1.0)
        self.assertEqual(env.sent(P.MSG_ROSTER_REQ), [])             # no snapshot yet: nothing to compare
        env.snapshot(1, roster_ver=1)
        env.run(2.0)                                                  # the roster never comes: ask again, not too fast
        asks = [t for t, _, _ in env.sent(P.MSG_ROSTER_REQ)]
        self.assertTrue(4 <= len(asks) <= 5, asks)
        self.assertTrue(all(b - a >= 0.45 for a, b in zip(asks, asks[1:])), asks)
        env.roster(1, [me_entry()])
        env.run(0.05)
        self.assertEqual((c.roster_ver, c.roster[3].name), (1, "Tester"))
        env.clear()
        env.run(1.5)
        self.assertEqual(env.sent(P.MSG_ROSTER_REQ), [])             # up to date
        env.snapshot(30, roster_ver=2)
        env.run(0.05)
        self.assertEqual(len(env.sent(P.MSG_ROSTER_REQ)), 1)         # the version moved on: ask again

    # ---- CMD
    def test_ready_is_repeated_until_the_roster_shows_it(self):
        env = self.env
        self.client.send_cmd(P.CMD_READY, 1)
        env.run(1.0)
        cmds = env.payloads(P.MSG_CMD)
        self.assertTrue(3 <= len(cmds) <= 5, len(cmds))
        self.assertEqual(P.unpack_cmd(cmds[0]), (P.CMD_READY, 1))
        env.roster(1, [me_entry(ready=True)])
        env.run(0.05)
        env.clear()
        env.run(1.0)
        self.assertEqual(env.payloads(P.MSG_CMD), [])

    def test_a_shape_change_is_repeated_until_the_roster_shows_it(self):
        env = self.env
        self.client.send_cmd(P.CMD_SHAPE, P.SHAPE_TRIANGLE)
        env.run(0.6)
        self.assertEqual(P.unpack_cmd(env.payloads(P.MSG_CMD)[0]), (P.CMD_SHAPE, P.SHAPE_TRIANGLE))
        env.roster(1, [me_entry(shape=P.SHAPE_TRIANGLE)])
        env.run(0.05)
        env.clear()
        env.run(1.0)
        self.assertEqual(env.payloads(P.MSG_CMD), [])

    def test_start_is_repeated_until_the_race_begins(self):
        env, c = self.env, self.client
        env.snapshot(1, state=P.ST_LOBBY, flags=P.SNAPF_HOST)
        env.run(0.05)
        c.send_cmd(P.CMD_START)
        env.run(0.8)
        self.assertTrue(3 <= len(env.payloads(P.MSG_CMD)) <= 4)
        env.snapshot(60, state=P.ST_COUNTDOWN, flags=P.SNAPF_HOST, countdown_ms=2000)
        env.run(0.05)
        env.clear()
        env.run(1.0)
        self.assertEqual(env.payloads(P.MSG_CMD), [])

    def test_start_from_a_player_who_is_not_host_is_not_sent(self):
        self.client.send_cmd(P.CMD_START)
        self.env.run(1.0)
        self.assertEqual(self.env.payloads(P.MSG_CMD), [])

    def test_a_command_nobody_answers_is_dropped_after_20_tries(self):
        env = self.env
        self.client.send_cmd(P.CMD_SHAPE, P.SHAPE_TRIANGLE)
        env.run(8.0)                                         # the keepalive stops it looking like a dead server
        self.assertEqual(len(env.payloads(P.MSG_CMD)), P.CMD_MAX_TRIES)

    def test_commands_need_a_session(self):
        env = Env()
        env.client.start()
        env.client.send_cmd(P.CMD_READY, 1)
        env.run(1.0, keepalive=False)
        self.assertEqual(env.sent(P.MSG_CMD), [])

    # ---- timeouts and disconnects
    def test_a_silent_server_is_reported_as_lost(self):
        env, c = self.env, self.client
        env.run(4.5, keepalive=False)
        self.assertEqual(c.state, JOINED)
        env.snapshot(1)                                      # any packet restarts the clock
        env.run(4.5, keepalive=False)
        self.assertEqual(c.state, JOINED)
        env.run(1.0, keepalive=False)
        self.assertEqual(c.state, LOST)
        self.assertIn("stopped answering", c.error)
        env.clear()
        env.run(1.0, keepalive=False)
        self.assertEqual(env.sent(), [])                     # a lost client says nothing more

    def test_disconnect_from_the_server(self):
        env, c = self.env, self.client
        env.deliver(P.MSG_DISCONNECT, P.pack_reason(P.DISC_TIMEOUT))
        env.run(0.02)
        self.assertEqual((c.state, c.disconnect_reason), (DISCONNECTED, P.DISC_TIMEOUT))
        self.assertEqual(c.error, P.DISC_TEXT[P.DISC_TIMEOUT])

    def test_unknown_session_reply_from_a_restarted_server(self):
        env, c = self.env, self.client
        env.deliver(P.MSG_DISCONNECT, P.pack_reason(P.DISC_UNKNOWN_SESSION), session=0)
        env.run(0.02)
        self.assertEqual((c.state, c.disconnect_reason), (DISCONNECTED, P.DISC_UNKNOWN_SESSION))

    def test_packets_with_a_wrong_session_are_ignored(self):
        env, c = self.env, self.client
        env.snapshot(1, session=SESSION + 1)
        env.deliver(P.MSG_DISCONNECT, P.pack_reason(P.DISC_KICKED), session=SESSION + 1)
        env.run(0.05)
        self.assertEqual((c.snapshot_count, c.state), (0, JOINED))

    def test_packets_from_another_address_are_ignored(self):
        env, c = self.env, self.client
        env.snapshot(1, addr=("10.9.9.9", SERVER[1]))
        env.run(0.05)
        self.assertEqual(c.snapshot_count, 0)

    # ---- leaving
    def test_leave_sends_three_copies_and_goes_quiet(self):
        env, c = self.env, self.client
        c.leave()
        leaves = env.sent(P.MSG_LEAVE)
        self.assertEqual(len(leaves), 3)
        self.assertTrue(all(h.session == SESSION for _, h, _ in leaves))
        self.assertEqual((c.state, c.active), (LEFT, False))
        c.leave()
        self.assertEqual(len(env.sent(P.MSG_LEAVE)), 3)       # a second call sends nothing
        env.clear()
        env.run(1.0)
        self.assertEqual(env.sent(), [])

    def test_leave_before_the_join_is_answered_sends_nothing(self):
        env = Env()
        env.client.start()
        env.clear()
        env.client.leave()
        self.assertEqual((env.client.state, env.sent()), (LEFT, []))


if __name__ == "__main__":
    unittest.main()
