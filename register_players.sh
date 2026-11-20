#!/usr/bin/env bash
# Project 17 - register every player host with the controller in one go (Role 2).
# Run it from a Mininet host, because only hosts can reach the controller at 10.0.0.254:
#
#   mininet> h1 bash scripts/register_players.sh 4        # registers h2..h5 (10.0.0.2 .. 10.0.0.5)
#
# Until Role 3's sdn_client.py does this automatically when a player joins, this is how the real
# game's players get into the VIP queue. (Or start Ryu with GAME_MATCH=port to skip registration.)
set -u
N="${1:-4}"
URL="${2:-http://10.0.0.254:8080}"
for i in $(seq 2 $((N + 1))); do
  ip="10.0.0.$i"
  echo -n "$ip: "
  curl -s --noproxy '*' -m 3 -X POST -d "{\"ip\": \"$ip\", \"port\": 0}" "$URL/game/register" || echo "failed"
  echo
done
