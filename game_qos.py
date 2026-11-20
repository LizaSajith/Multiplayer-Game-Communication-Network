"""
Project 17 - Ryu controller: learning switch + dynamic game-traffic prioritisation (Role 2).

Run (from the repo root, in the Ryu Python environment):
    ryu-manager controller/game_qos.py --ofp-tcp-listen-port 6653 --wsapi-port 8080

Modes (env GAME_QOS_MODE, or POST /game/mode at runtime):
    off      - baseline: game traffic shares queue 0 with everything else
    static   - game traffic always in queue 1
    dynamic  - (default) queue 1 only while the bottleneck is congested

Which traffic counts as game traffic (env GAME_MATCH):
    registered - (default) UDP to/from port 5005 of players registered via POST /game/register
    port       - any UDP to/from port 5005 (no registration needed)
The game port is 5005 (Role 1's app/protocol.py DEFAULT_PORT); change with env GAME_UDP_PORT.

OpenFlow pipeline (OpenFlow 1.3, two tables):
    table 0  priority 100  game flow of a registered player -> set_queue(1), goto table 1   (only when priority is ON)
             priority 0    anything                          -> goto table 1
    table 1  priority 1    learned eth_src/eth_dst/in_port   -> output(port)
             priority 0    table miss                        -> controller (MAC learning)

REST API (hosts reach it at http://10.0.0.254:8080 via the root-namespace link):
    POST /game/register    {"ip": "10.0.0.2", "port": 40000}   -> player's game flows get priority
    POST /game/unregister  {"ip": "10.0.0.2"}
    POST /game/mode        {"mode": "off" | "static" | "dynamic"}
    GET  /game/status

Logs (env GAME_QOS_LOG_DIR, default ./results/controller):
    link_stats.csv  every poll: time, dpid, port, tx_mbps, util, tx_dropped_delta, priority_on, mode
    events.csv      time, event, detail   (mode changes, priority on/off, player register/unregister)

Check every Ryu call against the Ryu docs and ryu/app/simple_switch_13.py for your installed version.
"""

import csv
import json
import os
import time

from ryu.app.wsgi import ControllerBase, WSGIApplication, route
from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, DEAD_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.lib import hub
from ryu.lib.packet import ether_types, ethernet, packet
from ryu.ofproto import ofproto_v1_3
from webob import Response

# ---------------------------------------------------------------- settings
GAME_UDP_PORT = int(os.environ.get("GAME_UDP_PORT", 5005))         # Role 1 game server port
GAME_MATCH = os.environ.get("GAME_MATCH", "registered")           # "registered" players only, or any "port" 5005 traffic
LINK_MBPS = float(os.environ.get("GAME_LINK_MBPS", 10))          # must match setup_qos.sh
POLL_SECONDS = float(os.environ.get("GAME_POLL_SECONDS", 2))
HIGH_UTIL = float(os.environ.get("GAME_HIGH_UTIL", 0.70))        # priority ON above this
LOW_UTIL = float(os.environ.get("GAME_LOW_UTIL", 0.30))          # priority OFF below this...
LOW_SAMPLES = int(os.environ.get("GAME_LOW_SAMPLES", 3))         # ...for this many polls in a row
START_MODE = os.environ.get("GAME_QOS_MODE", "dynamic")
LOG_DIR = os.environ.get("GAME_QOS_LOG_DIR", os.path.join("results", "controller"))

BOTTLENECK_PORTS = {1: 1, 2: 1}     # dpid -> port number of the s1-s2 link (see topo/game_topo.py)
GAME_QUEUE = 1
TABLE_CLASSIFY = 0
TABLE_FORWARD = 1
GAME_PRIORITY = 100
GAME_COOKIE = 0x6A3E                 # marks game flows so they can be deleted together
MODES = ("off", "static", "dynamic")

API_INSTANCE = "game_qos_app"


class GameQoS(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    _CONTEXTS = {"wsgi": WSGIApplication}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mac_to_port = {}           # dpid -> {mac: port}
        self.datapaths = {}             # dpid -> datapath
        self.players = {}               # ip -> {"port": udp_port, "since": t}
        self.mode = START_MODE if START_MODE in MODES else "dynamic"
        self.priority_on = False
        self.low_count = 0
        self.last_tx = {}               # (dpid, port) -> (time, tx_bytes, tx_dropped)
        self.util = {}                  # (dpid, port) -> latest utilisation 0..1

        os.makedirs(LOG_DIR, exist_ok=True)
        self.stats_file = open(os.path.join(LOG_DIR, "link_stats.csv"), "a", newline="")
        self.stats_csv = csv.writer(self.stats_file)
        self.events_file = open(os.path.join(LOG_DIR, "events.csv"), "a", newline="")
        self.events_csv = csv.writer(self.events_file)
        if self.stats_file.tell() == 0:
            self.stats_csv.writerow(["time", "dpid", "port", "tx_mbps", "util",
                                     "tx_dropped_delta", "priority_on", "mode"])
        if self.events_file.tell() == 0:
            self.events_csv.writerow(["time", "event", "detail"])

        kwargs["wsgi"].register(GameRestController, {API_INSTANCE: self})
        self.log_event("start", "mode=%s match=%s port=%s link=%sMbps high=%s low=%s"
                       % (self.mode, GAME_MATCH, GAME_UDP_PORT, LINK_MBPS, HIGH_UTIL, LOW_UTIL))
        self.monitor_thread = hub.spawn(self._monitor)

    # ------------------------------------------------------------ logging
    def log_event(self, event, detail=""):
        self.events_csv.writerow(["%.3f" % time.time(), event, detail])
        self.events_file.flush()
        self.logger.info("[game_qos] %s %s", event, detail)

    # ------------------------------------------------------------ flow helpers
    def add_flow(self, dp, table, priority, match, actions=None, goto=None, cookie=0):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        inst = []
        if actions:
            inst.append(parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions))
        if goto is not None:
            inst.append(parser.OFPInstructionGotoTable(goto))
        dp.send_msg(parser.OFPFlowMod(datapath=dp, table_id=table, priority=priority,
                                      match=match, instructions=inst, cookie=cookie))

    def delete_game_flows(self, dp, match=None):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        dp.send_msg(parser.OFPFlowMod(
            datapath=dp, table_id=TABLE_CLASSIFY, command=ofp.OFPFC_DELETE,
            cookie=GAME_COOKIE, cookie_mask=0xFFFFFFFFFFFFFFFF,
            out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY,
            match=match if match is not None else parser.OFPMatch()))

    def game_matches(self, parser, ip=None):
        """Both directions of the game traffic: of one player (ip given) or of anyone (ip=None)."""
        src = {"ipv4_src": ip} if ip else {}
        dst = {"ipv4_dst": ip} if ip else {}
        to_server = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP, ip_proto=17,
                                    udp_dst=GAME_UDP_PORT, **src)
        to_player = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP, ip_proto=17,
                                    udp_src=GAME_UDP_PORT, **dst)
        return [to_server, to_player]

    def install_game_rules(self, dp):
        """Called when priority switches on (or a switch connects while it is on)."""
        if GAME_MATCH == "port":
            self.install_player(dp, None)
        else:
            for ip in self.players:
                self.install_player(dp, ip)

    def install_player(self, dp, ip):
        parser = dp.ofproto_parser
        for m in self.game_matches(parser, ip):
            self.add_flow(dp, TABLE_CLASSIFY, GAME_PRIORITY, m,
                          actions=[parser.OFPActionSetQueue(GAME_QUEUE)],
                          goto=TABLE_FORWARD, cookie=GAME_COOKIE)

    def remove_player(self, dp, ip):
        for m in self.game_matches(dp.ofproto_parser, ip):
            self.delete_game_flows(dp, m)

    def set_priority(self, on, reason):
        if on == self.priority_on:
            return
        self.priority_on = on
        for dp in self.datapaths.values():
            if on:
                self.install_game_rules(dp)
            else:
                self.delete_game_flows(dp)
        self.log_event("priority_on" if on else "priority_off", reason)
        if on and GAME_MATCH != "port" and not self.players:
            self.log_event("warning", "priority is on but no players are registered yet")

    def set_mode(self, mode):
        self.mode = mode
        self.low_count = 0
        self.log_event("mode", mode)
        if mode == "off":
            self.set_priority(False, "mode=off")
        elif mode == "static":
            self.set_priority(True, "mode=static")
        # dynamic: the monitor decides on the next poll

    # ------------------------------------------------------------ switch connect / disconnect
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        dp = ev.msg.datapath
        ofp, parser = dp.ofproto, dp.ofproto_parser
        self.datapaths[dp.id] = dp

        # table 0: default -> table 1 ; table 1: miss -> controller
        self.add_flow(dp, TABLE_CLASSIFY, 0, parser.OFPMatch(), goto=TABLE_FORWARD)
        self.add_flow(dp, TABLE_FORWARD, 0, parser.OFPMatch(),
                      actions=[parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)])

        if self.mode == "static":
            self.priority_on = True
        if self.priority_on:
            self.install_game_rules(dp)
        self.log_event("switch_up", "dpid=%s" % dp.id)

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def state_change_handler(self, ev):
        dp = ev.datapath
        if ev.state == DEAD_DISPATCHER and dp.id in self.datapaths:
            del self.datapaths[dp.id]
            self.log_event("switch_down", "dpid=%s" % dp.id)

    # ------------------------------------------------------------ learning switch (table 1)
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg = ev.msg
        dp = msg.datapath
        ofp, parser = dp.ofproto, dp.ofproto_parser
        in_port = msg.match["in_port"]

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocols(ethernet.ethernet)[0]
        if eth.ethertype in (ether_types.ETH_TYPE_LLDP, ether_types.ETH_TYPE_IPV6):
            return

        table = self.mac_to_port.setdefault(dp.id, {})
        table[eth.src] = in_port
        out_port = table.get(eth.dst, ofp.OFPP_FLOOD)
        actions = [parser.OFPActionOutput(out_port)]

        if out_port != ofp.OFPP_FLOOD:
            match = parser.OFPMatch(in_port=in_port, eth_src=eth.src, eth_dst=eth.dst)
            self.add_flow(dp, TABLE_FORWARD, 1, match, actions=actions)

        data = msg.data if msg.buffer_id == ofp.OFP_NO_BUFFER else None
        dp.send_msg(parser.OFPPacketOut(datapath=dp, buffer_id=msg.buffer_id,
                                        in_port=in_port, actions=actions, data=data))

    # ------------------------------------------------------------ congestion monitor
    def _monitor(self):
        while True:
            for dp in list(self.datapaths.values()):
                parser = dp.ofproto_parser
                dp.send_msg(parser.OFPPortStatsRequest(dp, 0, dp.ofproto.OFPP_ANY))
            hub.sleep(POLL_SECONDS)

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def port_stats_reply_handler(self, ev):
        dpid = ev.msg.datapath.id
        watch_port = BOTTLENECK_PORTS.get(dpid)
        now = time.time()
        for st in ev.msg.body:
            if st.port_no != watch_port:
                continue
            key = (dpid, st.port_no)
            prev = self.last_tx.get(key)
            self.last_tx[key] = (now, st.tx_bytes, st.tx_dropped)
            if prev is None or now <= prev[0] or st.tx_bytes < prev[1]:
                continue                     # first sample, or counters were reset
            mbps = (st.tx_bytes - prev[1]) * 8 / (now - prev[0]) / 1e6
            util = mbps / LINK_MBPS
            dropped = st.tx_dropped - prev[2]
            self.util[key] = util
            self.stats_csv.writerow(["%.3f" % now, dpid, st.port_no, "%.3f" % mbps, "%.3f" % util,
                                     dropped, int(self.priority_on), self.mode])
            self.stats_file.flush()
        self._decide()

    def _decide(self):
        if self.mode != "dynamic" or not self.util:
            return
        worst = max(self.util.values())
        if worst >= HIGH_UTIL:
            self.low_count = 0
            self.set_priority(True, "util=%.2f >= %.2f" % (worst, HIGH_UTIL))
        elif self.priority_on and worst <= LOW_UTIL:
            self.low_count += 1
            if self.low_count >= LOW_SAMPLES:
                self.low_count = 0
                self.set_priority(False, "util=%.2f <= %.2f for %d polls" % (worst, LOW_UTIL, LOW_SAMPLES))
        else:
            self.low_count = 0

    # ------------------------------------------------------------ used by the REST API
    def register_player(self, ip, port):
        new = ip not in self.players
        self.players[ip] = {"port": port, "since": time.time()}
        if new and self.priority_on and GAME_MATCH != "port":
            for dp in self.datapaths.values():
                self.install_player(dp, ip)
        self.log_event("register", "%s:%s" % (ip, port))

    def unregister_player(self, ip):
        if self.players.pop(ip, None) is None:
            return False
        for dp in self.datapaths.values():
            self.remove_player(dp, ip)
        self.log_event("unregister", ip)
        return True

    def status(self):
        return {
            "mode": self.mode,
            "match": GAME_MATCH,
            "game_port": GAME_UDP_PORT,
            "priority_on": self.priority_on,
            "link_mbps": LINK_MBPS,
            "util": {"s%d-port%d" % k: round(v, 3) for k, v in self.util.items()},
            "players": sorted(self.players),
            "switches": sorted(self.datapaths),
        }


def _json(body, status=200):
    return Response(status=status, content_type="application/json", charset="utf-8",
                    body=json.dumps(body).encode("utf-8"))


class GameRestController(ControllerBase):
    def __init__(self, req, link, data, **config):
        super().__init__(req, link, data, **config)
        self.app = data[API_INSTANCE]

    def _body(self, req):
        try:
            return json.loads(req.body.decode("utf-8") or "{}")
        except ValueError:
            return None

    @route("game", "/game/status", methods=["GET"])
    def get_status(self, req, **kwargs):
        return _json(self.app.status())

    @route("game", "/game/register", methods=["POST"])
    def post_register(self, req, **kwargs):
        body = self._body(req)
        if not body or "ip" not in body:
            return _json({"error": "send JSON {\"ip\": ..., \"port\": ...}"}, 400)
        self.app.register_player(str(body["ip"]), int(body.get("port", 0)))
        return _json({"ok": True, "priority_on": self.app.priority_on})

    @route("game", "/game/unregister", methods=["POST"])
    def post_unregister(self, req, **kwargs):
        body = self._body(req)
        if not body or "ip" not in body:
            return _json({"error": "send JSON {\"ip\": ...}"}, 400)
        return _json({"ok": self.app.unregister_player(str(body["ip"]))})

    @route("game", "/game/mode", methods=["POST"])
    def post_mode(self, req, **kwargs):
        body = self._body(req)
        if not body or body.get("mode") not in MODES:
            return _json({"error": "mode must be one of %s" % (MODES,)}, 400)
        self.app.set_mode(body["mode"])
        return _json(self.app.status())
