#!/usr/bin/env bash
# Project 17 - is this machine ready for the experiments?  (Role 3)
#
#   bash scripts/check_env.sh            # checks only
#   bash scripts/check_env.sh --tests    # also runs the unit tests (about 30 s)
#
# Exit code 0 = ready, 1 = something required is missing.  Run it on the Ubuntu VM that has Mininet.
cd "$(dirname "$0")/.." || exit 1
FAIL=0
ok()   { printf '  [ OK ]  %s\n' "$1"; }
bad()  { printf '  [FAIL]  %s\n          -> %s\n' "$1" "$2"; FAIL=1; }
warn() { printf '  [warn]  %s\n          -> %s\n' "$1" "$2"; }

echo "== system"
[ "$(uname -s)" = "Linux" ] && ok "Linux" || bad "not Linux" "Mininet needs the Ubuntu VM"
PYV=$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null)
python3 -c 'import sys;sys.exit(0 if sys.version_info>=(3,9) else 1)' 2>/dev/null && ok "python3 $PYV" || bad "python3 >= 3.9 missing (found: ${PYV:-none})" "sudo apt install python3"
[ "$(id -u)" = "0" ] && ok "running as root" || warn "not root" "the experiments need: sudo python3 scripts/run_experiment.py ..."
CPUS=$(nproc 2>/dev/null || echo 1)
[ "$CPUS" -ge 2 ] && ok "$CPUS CPU cores" || warn "only $CPUS CPU core" "12 bots + server on one core distort the timings; give the VM 2+ cores"

echo "== network tools"
for t in mn ovs-vsctl ovs-ofctl tc iperf curl; do
  command -v "$t" >/dev/null 2>&1 && ok "$t" || bad "$t not found" "bash scripts/install_role2.sh   (or: sudo apt install mininet openvswitch-switch iperf curl iproute2)"
done
if command -v ovs-vsctl >/dev/null 2>&1; then
  if timeout 5 ovs-vsctl show >/dev/null 2>&1; then ok "Open vSwitch is running"
  else bad "Open vSwitch daemon not running" "sudo systemctl start openvswitch-switch"; fi
fi
python3 -c 'import mininet' 2>/dev/null && ok "python can import mininet" || bad "python cannot import mininet" "sudo apt install mininet  (use the system python3, not a venv)"

echo "== controller (Ryu)"
RYU=""
for c in "${RYU_MANAGER:-}" "${SUDO_USER:+/home/$SUDO_USER/ryu-env/bin/ryu-manager}" "$HOME/ryu-env/bin/ryu-manager" "$(command -v ryu-manager 2>/dev/null)"; do
  [ -n "$c" ] && [ -x "$c" ] && RYU="$c" && break
done
[ -n "$RYU" ] && ok "ryu-manager: $RYU" || bad "ryu-manager not found" "bash scripts/install_role2.sh   (installs ~/ryu-env)"
for port in 6653 8080; do
  if (exec 3<>/dev/tcp/127.0.0.1/$port) 2>/dev/null; then warn "TCP port $port is in use" "fine if you started Ryu yourself (--ryu external); otherwise: sudo pkill -f ryu-manager"
  else ok "TCP port $port is free"; fi
done

echo "== analysis"
python3 -c 'import openpyxl' 2>/dev/null && ok "openpyxl (Excel results file)" || warn "openpyxl missing (only needed for results/project17_results.xlsx; the CSV files are written anyway)" "pip install openpyxl   or   sudo apt install python3-openpyxl"
python3 -c 'import matplotlib' 2>/dev/null && ok "matplotlib" || warn "matplotlib missing (only needed for graphs)" "pip install matplotlib   or   sudo apt install python3-matplotlib"

echo "== project files"
for f in app/server.py app/bot.py app/sdn_client.py scripts/demo.py controller/game_qos.py topo/game_topo.py scripts/setup_qos.sh scripts/run_experiment.py analysis/plot.py; do
  [ -f "$f" ] && ok "$f" || bad "$f missing" "git pull / unzip the full repository"
done

if [ "${1:-}" = "--tests" ]; then
  echo "== unit tests"
  if python3 -I -W error::ResourceWarning -m unittest discover -s tests -t . >/tmp/p17_tests.log 2>&1; then ok "$(tail -3 /tmp/p17_tests.log | head -1)"
  else bad "unit tests failed" "see /tmp/p17_tests.log"; fi
fi
echo
[ $FAIL -eq 0 ] && echo "READY: next step is  sudo python3 scripts/demo.py --prepare --resume   (then: python3 scripts/demo.py)" || echo "NOT READY: fix the [FAIL] lines above, then run this script again."
exit $FAIL
