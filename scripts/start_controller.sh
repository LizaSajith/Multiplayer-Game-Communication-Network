#!/usr/bin/env bash
# Project 17 - start the Ryu controller with one command (Role 2).
#   bash scripts/start_controller.sh                 # controller in dynamic mode
#   GAME_QOS_MODE=off bash scripts/start_controller.sh
cd "$(dirname "$0")/.." || exit 1
if [ ! -f "$HOME/ryu-env/bin/activate" ]; then
  echo "Ryu environment not found at ~/ryu-env. Run: bash scripts/install_role2.sh" >&2
  exit 1
fi
# shellcheck disable=SC1091
source "$HOME/ryu-env/bin/activate"
exec ryu-manager controller/game_qos.py --ofp-tcp-listen-port 6653 --wsapi-port 8080
