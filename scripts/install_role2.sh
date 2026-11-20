#!/usr/bin/env bash
# Project 17 - Role 2 setup for Ubuntu (20.04, 22.04 or 24.04).
# Usage:  bash scripts/install_role2.sh
#
# Mininet + Open vSwitch run with the system Python (via sudo).
# Ryu runs in its own Python 3.8 environment (~/ryu-env), created with uv, because Ryu 4.34
# (the last release) does not work on newer Python. The pins below are the fixes that
# worked when this was tested on Python 3.8.
set -e

sudo apt-get update
sudo apt-get install -y mininet openvswitch-switch iperf iperf3 curl git net-tools ethtool

# uv: downloads a private Python 3.8 just for Ryu (does not touch the system Python)
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"

uv venv -p 3.8 ~/ryu-env
# shellcheck disable=SC1090
source ~/ryu-env/bin/activate
uv pip install "setuptools==57.5.0" wheel pbr      # fix: 'easy_install has no attribute get_script_args'
uv pip install --no-build-isolation ryu
uv pip install "eventlet==0.30.2"                  # fix: 'cannot import name ALREADY_HANDLED'

echo
echo "== checks =="
ryu-manager --version
sudo ovs-vsctl --version | head -1
sudo mn --version
echo
echo "Done. Next: sudo mn --test pingall"
