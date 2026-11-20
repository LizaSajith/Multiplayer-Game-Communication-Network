#!/usr/bin/env bash
# Project 17 - OVS QoS queues on the bottleneck link (Role 2).
#
#   sudo bash scripts/setup_qos.sh setup [BW_MBPS] [QUEUE_PKTS]   # queues on s1-eth1 and s2-eth1 (default 10 Mbps, 100 packets)
#   sudo bash scripts/setup_qos.sh show              # show queues + tc counters (are packets hitting queue 1?)
#   sudo bash scripts/setup_qos.sh clear             # remove all QoS / queue records
#
# Queue 0 = default (everything else, e.g. iperf).  Queue 1 = game traffic.
# OVS maps queue N to Linux HTB class 1:(N+1): queue 0 -> class 1:1, queue 1 -> class 1:2.
# The controller (controller/game_qos.py) decides which packets go into queue 1.
#
# Verify the option names against the Open vSwitch docs (ovs-vswitchd.conf.db, "QoS" and
# "Queue" tables) for your installed version.

set -u
ACTION="${1:-setup}"
BW_MBPS="${2:-10}"
QUEUE_PKTS="${3:-100}"          # packets each queue can hold: 100 x 1500 B at 10 Mbps = ~120 ms of delay
PORTS=("s1-eth1" "s2-eth1")

BW=$((BW_MBPS * 1000000))
GAME_MIN=$((BW * 6 / 10))      # game queue guaranteed 60% of the link
OTHER_MIN=$((BW * 1 / 10))     # everything else guaranteed 10%

case "$ACTION" in
  setup)
    # Start clean so repeated runs don't pile up orphan records.
    for p in "${PORTS[@]}"; do ovs-vsctl clear port "$p" qos 2>/dev/null; done
    ovs-vsctl -- --all destroy qos -- --all destroy queue >/dev/null 2>&1

    ovs-vsctl \
      -- set port "${PORTS[0]}" qos=@newqos \
      -- set port "${PORTS[1]}" qos=@newqos \
      -- --id=@newqos create qos type=linux-htb other-config:max-rate=$BW \
           queues:0=@q0 queues:1=@q1 \
      -- --id=@q0 create queue other-config:min-rate=$OTHER_MIN other-config:max-rate=$BW other-config:priority=1 \
      -- --id=@q1 create queue other-config:min-rate=$GAME_MIN other-config:max-rate=$BW other-config:priority=0 \
      >/dev/null

    # Fix the size of each queue (OVS leaves the Linux default, which can be 1000 packets = 1.2 s).
    sleep 1
    for p in "${PORTS[@]}"; do
      tc qdisc replace dev "$p" parent 1:1 handle 11: pfifo limit "$QUEUE_PKTS" 2>/dev/null || true
      tc qdisc replace dev "$p" parent 1:2 handle 12: pfifo limit "$QUEUE_PKTS" 2>/dev/null || true
    done

    echo "QoS ready on ${PORTS[*]}: link ${BW_MBPS} Mbps, queue ${QUEUE_PKTS} packets, q0 (other) min $((OTHER_MIN/1000000)) Mbps, q1 (game) min $((GAME_MIN/1000000)) Mbps"
    ;;
  show)
    ovs-vsctl list qos
    ovs-vsctl list queue
    for p in "${PORTS[@]}"; do
      echo "=== tc classes on $p (watch 'Sent' grow on 1:2 for game traffic) ==="
      tc -s class show dev "$p"
      echo "--- queue sizes on $p ---"
      tc qdisc show dev "$p"
    done
    ;;
  clear)
    for p in "${PORTS[@]}"; do ovs-vsctl clear port "$p" qos 2>/dev/null; done
    ovs-vsctl -- --all destroy qos -- --all destroy queue >/dev/null 2>&1
    echo "QoS cleared"
    ;;
  *)
    echo "usage: $0 setup [BW_MBPS] | show | clear" >&2
    exit 1
    ;;
esac
