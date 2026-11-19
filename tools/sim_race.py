"""Headless race: N autopilot bots drive a full race through the Game class.

No sockets and no pygame - this checks the rules engine (laps, checkpoints,
obstacles, wrecks, results) on its own.

    python tools/sim_race.py                 # 4 bots, 3 laps
    python tools/sim_race.py --bots 8 --laps 2
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app import protocol as P
from app.autopilot import Autopilot
from app.game import Game, GameConfig


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bots", type=int, default=4)
    ap.add_argument("--laps", type=int, default=3)
    ap.add_argument("--seed", type=int, default=17)
    ap.add_argument("--max-seconds", type=int, default=400, help="game-time safety limit")
    args = ap.parse_args()

    game = Game(GameConfig(laps=args.laps, max_players=max(16, args.bots), track_seed=args.seed))
    shapes = [P.SHAPE_CIRCLE, P.SHAPE_SQUARE, P.SHAPE_TRIANGLE]
    pilots = {}
    for i in range(args.bots):
        pid = i + 1
        game.add_player(pid, "bot%d" % pid, shapes[i % 3], i)
        pilots[pid] = Autopilot(game.track, skill=max(0.5, 1.0 - 0.08 * i))

    print("track %.1f m, %d checkpoints, %d obstacles, %d laps, %d bots" % (
        game.track.meters(game.track.length), len(game.track.checkpoints),
        len(game.track.obstacles), args.laps, args.bots))
    if not game.begin_countdown():
        sys.exit("could not start the countdown")

    started = time.time()
    ticks = 0
    limit = args.max_seconds * game.cfg.tick_rate
    while game.state != P.ST_FINISHED and ticks < limit:
        if game.state == P.ST_RACING:
            for pid, p in game.players.items():
                throttle, steer = pilots[pid].control(p.x, p.y, p.heading, p.speed, game.race_time)
                game.set_input(pid, throttle, steer)
        game.step()
        ticks += 1

    print("state %s after %.1f s of game time (%.2f s wall)" % (
        P.STATE_NAMES[game.state], ticks / game.cfg.tick_rate, time.time() - started))
    for r in game.last_results:
        print("  place %d  %-6s %-8s finish %s  health %3d  hits %2d  wrecks %d  best lap %s" % (
            r["place"], r["name"], r["shape"],
            "%.1fs" % (r["finish_ms"] / 1000.0) if r["finished"] else "DNF",
            r["health"], r["hits"], r["wrecks"],
            "%.1fs" % (r["best_lap_ms"] / 1000.0) if r["best_lap_ms"] else "-"))
    counts = {}
    for etype, _pid, _a, _b in game.drain_events():
        name = P.EVENT_NAMES.get(etype, str(etype))
        counts[name] = counts.get(name, 0) + 1
    print("events:", counts)


if __name__ == "__main__":
    main()
