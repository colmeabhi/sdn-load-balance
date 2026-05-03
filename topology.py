#!/usr/bin/python3
"""
topology.py — Mininet topology for SDN load balancing lab

Layout:
    Clients: h1 (10.0.0.1), h2 (10.0.0.2), h3 (10.0.0.3)
    Servers: h4 (10.0.0.4), h5 (10.0.0.5), h6 (10.0.0.6), h7 (10.0.0.7)
    Switch:  s1 (OVS, OpenFlow 1.3)
    Controller: remote RYU on 127.0.0.1:6633

Run:
    sudo python3 topology.py
"""

from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.cli import CLI
from mininet.log import setLogLevel, info
import time


# ─── IP configuration ────────────────────────────────────────────────────────

CLIENT_HOSTS = [
    ("h1", "10.0.0.1"),
    ("h2", "10.0.0.2"),
    ("h3", "10.0.0.3"),
]

SERVER_HOSTS = [
    ("h4", "10.0.0.4"),
    ("h5", "10.0.0.5"),
    ("h6", "10.0.0.6"),
    ("h7", "10.0.0.7"),
]

CONTROLLER_IP   = "127.0.0.1"
CONTROLLER_PORT = 6633


# ─── Topology ────────────────────────────────────────────────────────────────

def build_topology():
    net = Mininet(controller=RemoteController, switch=OVSSwitch, autoSetMacs=True)

    # Remote RYU controller
    info("*** Adding controller\n")
    c0 = net.addController(
        "c0",
        controller=RemoteController,
        ip=CONTROLLER_IP,
        port=CONTROLLER_PORT,
    )

    # Single OpenFlow 1.3 switch
    info("*** Adding switch\n")
    s1 = net.addSwitch("s1", protocols="OpenFlow13")

    # Client hosts
    info("*** Adding client hosts\n")
    clients = []
    for name, ip in CLIENT_HOSTS:
        h = net.addHost(name, ip=ip)
        clients.append(h)

    # Server hosts
    info("*** Adding server hosts\n")
    servers = []
    for name, ip in SERVER_HOSTS:
        h = net.addHost(name, ip=ip)
        servers.append(h)

    # Star topology: every host connects to s1
    info("*** Adding links\n")
    for host in clients + servers:
        net.addLink(host, s1)

    # ── Start network ────────────────────────────────────────────────────────
    info("*** Starting network\n")
    net.build()
    c0.start()
    s1.start([c0])

    # Ensure OF 1.3 is set before the controller handshake completes.
    s1.cmd("ovs-vsctl set Bridge s1 protocols=OpenFlow13")
    time.sleep(1)

    # Serve a simple HTTP page on each server so curl/locust have something to hit
    info("*** Starting HTTP servers on server hosts\n")
    for host in servers:
        host.cmd(
            f'echo "<h1>Server {host.name} ({host.IP()})</h1>" > /tmp/index.html && '
            f'cd /tmp && python3 -m http.server 80 &> /tmp/{host.name}_http.log &'
        )

    info("*** Network ready — VIP is 10.0.0.100\n")
    info("    Clients: " + ", ".join(f"{n}={ip}" for n, ip in CLIENT_HOSTS) + "\n")
    info("    Servers: " + ", ".join(f"{n}={ip}" for n, ip in SERVER_HOSTS) + "\n")

    CLI(net)

    info("*** Stopping network\n")
    net.stop()


if __name__ == "__main__":
    setLogLevel("info")
    build_topology()
