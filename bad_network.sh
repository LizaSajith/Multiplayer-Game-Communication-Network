#!/usr/bin/env bash
# Project 17 - make this VM's real network card slow and lossy for the two-laptop demo (Part 2).
#   sudo bash scripts/bad_network.sh on      # add 100 ms (+-20 ms) delay and 5 % loss
#   sudo bash scripts/bad_network.sh off     # remove it again (do not forget!)
#   sudo bash scripts/bad_network.sh status
#   sudo bash scripts/bad_network.sh on enp0s8   # choose the card yourself
# By default it picks the card with the first address that `hostname -I` shows (the hotspot one).
set -u
DEV="${2:-$(ip -o -4 addr show scope global | awk 'NR==1 {print $2}')}"
if [ -z "$DEV" ]; then echo "No network card found." >&2; exit 1; fi
case "${1:-status}" in
  on)
    tc qdisc replace dev "$DEV" root netem delay 100ms 20ms loss 5%
    echo "Bad network ON on $DEV: 100 ms delay (+-20 ms), 5 % loss" ;;
  off)
    tc qdisc del dev "$DEV" root 2>/dev/null
    echo "Bad network OFF on $DEV" ;;
  status)
    echo "Card: $DEV"; tc qdisc show dev "$DEV" ;;
  *)
    echo "usage: sudo bash $0 on | off | status" >&2; exit 1 ;;
esac
