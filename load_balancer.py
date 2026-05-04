"""
load_balancer.py — RYU SDN load balancer (OpenFlow 1.3)

Architecture:
  - Non-VIP traffic:  behaves exactly like simple_switch_13 — installs per-flow
                      rules so the switch handles future packets without the controller.
  - VIP first packet: controller selects a server, installs bidirectional rewrite
                      rules (client→VIP → client→server, and the reverse).
  - VIP subsequent:   handled entirely by the switch flow rules.

Change ALGORITHM at the top to switch balancing strategies.

Run:
    source ~/ryu-env/bin/activate
    ryu-manager load_balancer.py
"""

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ether_types, ipv4, arp

import random
import hashlib


# ─── Algorithm Selection ─────────────────────────────────────────────────────
# Options: round_robin | random | ip_hash | least_connections | weighted
ALGORITHM = "weighted"

# ─── Virtual IP ──────────────────────────────────────────────────────────────
VIRTUAL_IP  = "10.0.0.100"
VIRTUAL_MAC = "00:00:00:00:00:fe"

# ─── Backend Server Pool ─────────────────────────────────────────────────────
SERVER_IPS = ["10.0.0.4", "10.0.0.5", "10.0.0.6", "10.0.0.7"]

# Weights for the "weighted" algorithm (index matches SERVER_IPS)
SERVER_WEIGHTS = [1, 1, 2, 2]

# Seconds before an idle load-balancer flow is removed from the switch
FLOW_IDLE_TIMEOUT = 30


# ─── App ─────────────────────────────────────────────────────────────────────

class LoadBalancer(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(LoadBalancer, self).__init__(*args, **kwargs)
        self.mac_to_port     = {}          # dpid → {mac → port}
        self.ip_to_mac       = {}          # ip  → mac  (learned from ARP)
        self.connections     = {ip: 0 for ip in SERVER_IPS}
        self._rr_index       = 0
        self._weighted_seq   = self._build_weighted_sequence()
        self._weighted_idx   = 0
        self.client_to_server = {}         # client_ip → server_ip (session sticky)
        self.logger.info(
            "LoadBalancer ready | algorithm=%s | VIP=%s | servers=%s",
            ALGORITHM, VIRTUAL_IP, SERVER_IPS,
        )

    # ── Algorithm helpers ─────────────────────────────────────────────────────

    def _build_weighted_sequence(self):
        seq = []
        for ip, w in zip(SERVER_IPS, SERVER_WEIGHTS):
            seq.extend([ip] * w)
        return seq

    def _select_server(self, client_ip):
        if ALGORITHM == "round_robin":
            server = SERVER_IPS[self._rr_index % len(SERVER_IPS)]
            self._rr_index += 1
        elif ALGORITHM == "random":
            server = random.choice(SERVER_IPS)
        elif ALGORITHM == "ip_hash":
            idx = int(hashlib.md5(client_ip.encode()).hexdigest(), 16) % len(SERVER_IPS)
            server = SERVER_IPS[idx]
        elif ALGORITHM == "least_connections":
            server = min(self.connections, key=self.connections.get)
        elif ALGORITHM == "weighted":
            server = self._weighted_seq[self._weighted_idx % len(self._weighted_seq)]
            self._weighted_idx += 1
        else:
            self.logger.warning("Unknown algorithm '%s', using round_robin", ALGORITHM)
            server = SERVER_IPS[self._rr_index % len(SERVER_IPS)]
            self._rr_index += 1
        return server

    # ── OpenFlow helpers ──────────────────────────────────────────────────────

    def add_flow(self, datapath, priority, match, actions,
                 buffer_id=None, idle_timeout=0, hard_timeout=0):
        """
        Mirrors simple_switch_13's add_flow signature exactly.
        When buffer_id is provided, the FlowMod also forwards the buffered packet
        so no separate PacketOut is needed.
        """
        ofproto = datapath.ofproto
        parser  = datapath.ofproto_parser
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        if buffer_id and buffer_id != ofproto.OFP_NO_BUFFER:
            mod = parser.OFPFlowMod(
                datapath=datapath, buffer_id=buffer_id,
                priority=priority, match=match,
                idle_timeout=idle_timeout, hard_timeout=hard_timeout,
                instructions=inst,
            )
        else:
            mod = parser.OFPFlowMod(
                datapath=datapath, priority=priority,
                match=match,
                idle_timeout=idle_timeout, hard_timeout=hard_timeout,
                instructions=inst,
            )
        datapath.send_msg(mod)

    # ── Switch handshake ──────────────────────────────────────────────────────

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        ofproto  = datapath.ofproto
        parser   = datapath.ofproto_parser

        # Wipe stale flows from prior sessions so MAC/port learning starts clean.
        # Old L2 rules would intercept ARP replies before the controller sees them,
        # preventing ip_to_mac and mac_to_port from being populated.
        datapath.send_msg(parser.OFPFlowMod(
            datapath=datapath,
            command=ofproto.OFPFC_DELETE,
            out_port=ofproto.OFPP_ANY,
            out_group=ofproto.OFPG_ANY,
            match=parser.OFPMatch(),
        ))

        match   = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                          ofproto.OFPCML_NO_BUFFER)]
        self.add_flow(datapath, 0, match, actions)
        self.logger.info("Switch %s connected — flows cleared, table-miss installed", datapath.id)

    # ── Main packet handler ───────────────────────────────────────────────────

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        if ev.msg.msg_len < ev.msg.total_len:
            self.logger.debug("packet truncated: only %s of %s bytes",
                              ev.msg.msg_len, ev.msg.total_len)

        msg      = ev.msg
        datapath = msg.datapath
        ofproto  = datapath.ofproto
        parser   = datapath.ofproto_parser
        in_port  = msg.match["in_port"]
        dpid     = datapath.id

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocols(ethernet.ethernet)[0]

        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        src = eth.src
        dst = eth.dst

        self.mac_to_port.setdefault(dpid, {})
        self.mac_to_port[dpid][src] = in_port   # MAC learning

        # ── ARP ──────────────────────────────────────────────────────────────
        arp_pkt = pkt.get_protocol(arp.arp)
        if arp_pkt:
            self.ip_to_mac[arp_pkt.src_ip] = arp_pkt.src_mac
            if arp_pkt.opcode == arp.ARP_REQUEST and arp_pkt.dst_ip == VIRTUAL_IP:
                self._send_arp_reply(datapath, in_port, arp_pkt)
                return
            # All other ARP falls through to standard L2 forwarding below

        # ── IPv4: learn IP→MAC, then check for VIP ───────────────────────────
        ip_pkt = pkt.get_protocol(ipv4.ipv4)
        if ip_pkt:
            # Populate ip_to_mac from every IP packet, not just ARP.
            # This ensures warmup ICMP replies from backends (which arrive before
            # any L2 flow is installed) still teach the controller their MACs.
            self.ip_to_mac[ip_pkt.src] = src

        if ip_pkt and ip_pkt.dst == VIRTUAL_IP:
            self._handle_vip(datapath, in_port, eth, ip_pkt, msg)
            return

        # ── Everything else: standard L2 forwarding (verbatim simple_switch_13)
        out_port = self.mac_to_port[dpid].get(dst, ofproto.OFPP_FLOOD)
        actions  = [parser.OFPActionOutput(out_port)]

        if out_port != ofproto.OFPP_FLOOD:
            match = parser.OFPMatch(in_port=in_port, eth_dst=dst, eth_src=src)
            if msg.buffer_id != ofproto.OFP_NO_BUFFER:
                self.add_flow(datapath, 1, match, actions, msg.buffer_id)
                return
            else:
                self.add_flow(datapath, 1, match, actions)

        data = msg.data if msg.buffer_id == ofproto.OFP_NO_BUFFER else None
        out  = parser.OFPPacketOut(
            datapath=datapath, buffer_id=msg.buffer_id,
            in_port=in_port, actions=actions, data=data,
        )
        datapath.send_msg(out)

    # ── ARP reply for VIP ─────────────────────────────────────────────────────

    def _send_arp_reply(self, datapath, in_port, arp_pkt):
        ofproto = datapath.ofproto
        parser  = datapath.ofproto_parser
        reply   = packet.Packet()
        reply.add_protocol(ethernet.ethernet(
            ethertype=0x0806, dst=arp_pkt.src_mac, src=VIRTUAL_MAC,
        ))
        reply.add_protocol(arp.arp(
            opcode=arp.ARP_REPLY,
            src_mac=VIRTUAL_MAC, src_ip=VIRTUAL_IP,
            dst_mac=arp_pkt.src_mac, dst_ip=arp_pkt.src_ip,
        ))
        reply.serialize()
        out = parser.OFPPacketOut(
            datapath=datapath,
            buffer_id=ofproto.OFP_NO_BUFFER,
            in_port=ofproto.OFPP_CONTROLLER,
            actions=[parser.OFPActionOutput(in_port)],
            data=reply.data,
        )
        datapath.send_msg(out)
        self.logger.debug("ARP reply: VIP %s → %s", VIRTUAL_IP, arp_pkt.src_ip)

    # ── VIP load balancing ────────────────────────────────────────────────────

    def _handle_vip(self, datapath, in_port, eth_pkt, ip_pkt, msg):
        ofproto    = datapath.ofproto
        parser     = datapath.ofproto_parser
        dpid       = datapath.id
        client_ip  = ip_pkt.src
        client_mac = eth_pkt.src

        # Pick a server (sticky: same client always hits the same server)
        if client_ip not in self.client_to_server:
            server_ip = self._select_server(client_ip)
            self.client_to_server[client_ip] = server_ip
            self.connections[server_ip] += 1
            self.logger.info(
                "[%s] %s → %s  (connections: %s)",
                ALGORITHM, client_ip, server_ip, dict(self.connections),
            )
        else:
            server_ip = self.client_to_server[client_ip]

        server_mac  = self.ip_to_mac.get(server_ip)
        client_port = self.mac_to_port[dpid].get(client_mac)
        server_port = self.mac_to_port[dpid].get(server_mac)

        if not server_mac or not server_port:
            # Server MAC not yet learned — emit ARP probe and drop this packet.
            # The TCP client will retransmit the SYN; by then the ARP reply will
            # have arrived and the controller will know the server's port.
            self.logger.debug("Server %s MAC unknown — sending ARP probe", server_ip)
            self._arp_probe(datapath, server_ip, client_ip, client_mac, in_port)
            return

        # ── Forward flow: client→VIP  →  client→server ───────────────────────
        match_fwd = parser.OFPMatch(
            in_port=in_port,
            eth_type=ether_types.ETH_TYPE_IP,
            ipv4_src=client_ip,
            ipv4_dst=VIRTUAL_IP,
        )
        actions_fwd = [
            parser.OFPActionSetField(eth_dst=server_mac),
            parser.OFPActionSetField(ipv4_dst=server_ip),
            parser.OFPActionOutput(server_port),
        ]
        self.add_flow(datapath, 10, match_fwd, actions_fwd,
                      idle_timeout=FLOW_IDLE_TIMEOUT)

        # ── Reverse flow: server→client  →  VIP→client ───────────────────────
        match_rev = parser.OFPMatch(
            in_port=server_port,
            eth_type=ether_types.ETH_TYPE_IP,
            ipv4_src=server_ip,
            ipv4_dst=client_ip,
        )
        actions_rev = [
            parser.OFPActionSetField(eth_src=VIRTUAL_MAC),
            parser.OFPActionSetField(ipv4_src=VIRTUAL_IP),
            parser.OFPActionOutput(client_port),
        ]
        self.add_flow(datapath, 10, match_rev, actions_rev,
                      idle_timeout=FLOW_IDLE_TIMEOUT)

        # Forward the triggering packet now (flow rule covers future ones)
        data = msg.data if msg.buffer_id == ofproto.OFP_NO_BUFFER else None
        out  = parser.OFPPacketOut(
            datapath=datapath,
            buffer_id=msg.buffer_id,
            in_port=in_port,
            actions=actions_fwd,
            data=data,
        )
        datapath.send_msg(out)

    # ── ARP probe ─────────────────────────────────────────────────────────────

    def _arp_probe(self, datapath, target_ip, src_ip, src_mac, out_port):
        """Broadcast an ARP-who-has for target_ip to learn its MAC and port."""
        ofproto = datapath.ofproto
        parser  = datapath.ofproto_parser
        probe   = packet.Packet()
        probe.add_protocol(ethernet.ethernet(
            ethertype=0x0806,
            dst="ff:ff:ff:ff:ff:ff",
            src=src_mac,
        ))
        probe.add_protocol(arp.arp(
            opcode=arp.ARP_REQUEST,
            src_mac=src_mac, src_ip=src_ip,
            dst_mac="00:00:00:00:00:00", dst_ip=target_ip,
        ))
        probe.serialize()
        out = parser.OFPPacketOut(
            datapath=datapath,
            buffer_id=ofproto.OFP_NO_BUFFER,
            in_port=ofproto.OFPP_CONTROLLER,
            actions=[parser.OFPActionOutput(ofproto.OFPP_FLOOD)],
            data=probe.data,
        )
        datapath.send_msg(out)
