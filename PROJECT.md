# SDN Load Balancer — Complete Project Reference

## Table of Contents

1. [What This Project Does](#1-what-this-project-does)
2. [Background Theory](#2-background-theory)
   - 2.1 [Traditional Networking vs SDN](#21-traditional-networking-vs-sdn)
   - 2.2 [The OpenFlow Protocol](#22-the-openflow-protocol)
   - 2.3 [Flow Tables and the Packet Pipeline](#23-flow-tables-and-the-packet-pipeline)
   - 2.4 [The Controller–Switch Conversation](#24-the-controllerswitch-conversation)
3. [Load Balancing Theory](#3-load-balancing-theory)
   - 3.1 [Why Load Balance?](#31-why-load-balance)
   - 3.2 [Virtual IP Pattern](#32-virtual-ip-pattern)
   - 3.3 [The Five Algorithms](#33-the-five-algorithms)
4. [Tools and Their Roles](#4-tools-and-their-roles)
5. [Topology](#5-topology)
6. [Code Walkthrough](#6-code-walkthrough)
   - 6.1 [topology.py](#61-topologypy)
   - 6.2 [load_balancer.py](#62-load_balancerpy)
   - 6.3 [benchmark/locustfile.py](#63-benchmarklocustfilepy)
   - 6.4 [analysis.py](#64-analysispy)
7. [Packet Flow: End-to-End Trace](#7-packet-flow-end-to-end-trace)
8. [Key Code Nuances](#8-key-code-nuances)
9. [Running the Project](#9-running-the-project)
10. [Benchmarking](#10-benchmarking)

---

## 1. What This Project Does

This project builds a **software-defined load balancer** that runs entirely inside a virtual network. Clients send HTTP requests to a single Virtual IP address (`10.0.0.100`). An SDN controller intercepts the first packet of each connection, picks a backend server using one of five algorithms, and installs rewrite rules directly into the network switch. After that, the switch forwards all subsequent packets in the same flow at line rate — the controller is not involved again until the flow expires.

```
  h1 ──┐                      ┌── h4 (10.0.0.4)  server
  h2 ──┤                      ├── h5 (10.0.0.5)  server
  h3 ──┤── s1 (OVS switch) ───┼── h6 (10.0.0.6)  server
       │         │             └── h7 (10.0.0.7)  server
       │         │ OpenFlow 1.3
       │    RYU controller
       │    (load_balancer.py)
       │
  clients hit 10.0.0.100 (VIP)
  controller rewrites → real server IP
```

---

## 2. Background Theory

### 2.1 Traditional Networking vs SDN

In a **traditional network**, each switch contains both a *control plane* (decides where packets go) and a *data plane* (actually moves packets). Every device makes forwarding decisions independently using protocols like STP, OSPF, or BGP. Changing network behaviour means logging into each device separately.

In **Software-Defined Networking (SDN)**, these two planes are separated:

| Plane | Where it lives | What it does |
|-------|---------------|--------------|
| Control plane | Centralised controller (RYU) | Decides forwarding rules |
| Data plane | Switch (OVS) | Executes forwarding rules at hardware speed |

The controller has a global view of the entire network. It installs *flow rules* into switches via a standardised protocol. This makes the network programmable: you can implement complex policies (load balancing, firewalling, traffic engineering) in software, without touching switch firmware.

### 2.2 The OpenFlow Protocol

OpenFlow is the protocol through which a controller tells a switch what to do. This project uses **OpenFlow 1.3** (the most widely deployed version).

The handshake between a switch and controller works like this:

```
Switch                          Controller (RYU)
  |                                    |
  |── HELLO (version=1.3) ────────────>|
  |<── HELLO (version=1.3) ────────────|
  |<── FEATURES_REQUEST ───────────────|
  |── FEATURES_REPLY (dpid, ports) ───>|
  |                                    |  EventOFPSwitchFeatures fires
  |                                    |  → switch_features_handler()
  |<── FLOW_MOD (table-miss rule) ─────|
  |                                    |
  [network traffic begins]
  |── PACKET_IN (unmatched packet) ───>|  EventOFPPacketIn fires
  |                                    |  → packet_in_handler()
  |<── FLOW_MOD (forward rule) ────────|
  |<── PACKET_OUT (send this now) ─────|
  |                                    |
  [switch handles all future packets in this flow internally]
```

Key message types:

- **HELLO**: version negotiation. Both sides advertise the highest version they support; they settle on the minimum.
- **FEATURES_REQUEST/REPLY**: controller asks for the switch's datapath ID (dpid) and port list.
- **PACKET_IN**: switch sends an unmatched packet to the controller.
- **FLOW_MOD**: controller installs, modifies, or deletes a rule in the switch's flow table.
- **PACKET_OUT**: controller tells the switch to forward a specific packet out a port.
- **FLOW_REMOVED**: switch tells the controller a flow expired (used for connection counting).

### 2.3 Flow Tables and the Packet Pipeline

An OpenFlow switch maintains one or more **flow tables**. Each entry has three parts:

```
┌──────────────────────┬──────────────────────┬──────────────────────┐
│      Match fields    │       Actions        │      Counters        │
│  in_port, eth_type,  │  output(port),       │  packet_count,       │
│  ipv4_src, ipv4_dst, │  set_field(ip_dst),  │  byte_count,         │
│  eth_src, eth_dst,   │  set_field(ip_src),  │  duration            │
│  ...                 │  ...                 │                      │
└──────────────────────┴──────────────────────┴──────────────────────┘
```

When a packet arrives, the switch checks each rule from **highest priority to lowest**. The first matching rule wins. If no rule matches, the **table-miss** rule (priority 0, match-all) applies.

This project uses three priority levels:

| Priority | Rule | Purpose |
|----------|------|---------|
| 0 | Match everything → send to controller | Table-miss: catch unlearned flows |
| 1 | Match eth_dst + in_port → forward | L2 learned flows (non-VIP traffic) |
| 10 | Match src/dst IP → rewrite + forward | VIP load-balancer flows |

Higher-priority rules shadow lower-priority ones for matching traffic. Once a priority-10 rule exists for a VIP connection, the priority-0 table-miss never sees those packets again.

**Idle timeout**: a flow is deleted from the switch after `idle_timeout` seconds of inactivity. When it is deleted, the switch sends a `FLOW_REMOVED` message to the controller. This project sets `idle_timeout=30` for VIP flows, which is how the connection counter would decrement (hook not wired in the current version — extension point).

### 2.4 The Controller–Switch Conversation

RYU maps the OpenFlow state machine to Python event handlers via decorators:

```python
@set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
def switch_features_handler(self, ev):
    ...
```

`CONFIG_DISPATCHER` means "fire this handler when a switch first connects and sends its features". `MAIN_DISPATCHER` means "fire this handler for all subsequent in-session messages". Registering a handler in the wrong dispatcher state is a silent failure — the method exists but is never called.

RYU uses **eventlet** for concurrency: a cooperative green-thread library. All packet handlers run in a single thread and must not block. Long operations (database writes, sleeps) must be offloaded with `hub.spawn`.

---

## 3. Load Balancing Theory

### 3.1 Why Load Balance?

A single server has finite CPU, memory, and bandwidth. When many clients connect simultaneously, a single server becomes a bottleneck. Load balancing distributes client connections across a pool of servers so that:

- No single server is overwhelmed
- If one server fails, others absorb its traffic
- Total system throughput scales with the number of servers

### 3.2 Virtual IP Pattern

The Virtual IP (VIP) pattern hides the server pool from clients. Clients always connect to one address (`10.0.0.100`). The load balancer intercepts the connection and transparently redirects it to a real server. The server's response is rewritten to look like it came from the VIP. The client never knows a redirect happened.

```
Client sends:   SRC=10.0.0.1   DST=10.0.0.100
                           ↓ controller rewrites
Switch forwards: SRC=10.0.0.1   DST=10.0.0.4   (server h4)

Server replies:  SRC=10.0.0.4   DST=10.0.0.1
                           ↓ controller rewrites
Switch forwards: SRC=10.0.0.100  DST=10.0.0.1   (client sees VIP as source)
```

Both the destination IP and the destination MAC must be rewritten, because the switch's L2 forwarding uses MAC addresses. If only the IP is rewritten, the switch would still try to forward to the VIP's MAC (which is the fake `00:00:00:00:00:fe`), not to the server's real MAC.

### 3.3 The Five Algorithms

#### Round Robin
Requests are assigned to servers in a fixed cycle: s1, s2, s3, s4, s1, s2, ...

```python
server = SERVER_IPS[self._rr_index % len(SERVER_IPS)]
self._rr_index += 1
```

**Good for**: homogeneous servers with similar request costs.  
**Bad for**: heterogeneous workloads where some requests are much heavier than others.

#### Random
Each new connection picks a server uniformly at random.

```python
server = random.choice(SERVER_IPS)
```

**Good for**: simple, stateless — no counter to maintain. Converges to uniform distribution at scale.  
**Bad for**: small numbers of connections (high variance).

#### IP Hash
The client's IP address is hashed to deterministically select a server. The same client always hits the same server as long as the pool size doesn't change.

```python
idx = int(hashlib.md5(client_ip.encode()).hexdigest(), 16) % len(SERVER_IPS)
server = SERVER_IPS[idx]
```

**Good for**: applications that maintain server-side session state (e.g., shopping carts) — client always hits the server that has its session.  
**Bad for**: changing pool size invalidates all existing assignments. MD5 is not cryptographically secure but is fast enough for this purpose.

#### Least Connections
The server with the fewest currently active connections is selected. Active connections are tracked in `self.connections` and incremented when a new VIP flow is created.

```python
server = min(self.connections, key=self.connections.get)
```

**Good for**: heterogeneous request durations — long-lived connections are naturally accounted for.  
**Bad for**: requires accurate connection tracking. In this implementation, decrement on flow expiry is not yet wired (extension point).

#### Weighted Round Robin
Servers with higher weights receive proportionally more traffic. This is implemented by pre-expanding the weight list into a flat sequence and cycling through it.

```python
# weights [1, 1, 2, 2] → ['10.0.0.4', '10.0.0.5', '10.0.0.6', '10.0.0.6', '10.0.0.7', '10.0.0.7']
def _build_weighted_sequence(self):
    seq = []
    for ip, w in zip(SERVER_IPS, SERVER_WEIGHTS):
        seq.extend([ip] * w)
    return seq
```

**Good for**: heterogeneous servers — a server with 4× the RAM can be given 4× the weight.  
**Bad for**: weight changes require rebuilding the sequence and restarting the cursor.

---

## 4. Tools and Their Roles

### Mininet
Mininet creates a **virtual network entirely within a single Linux machine** using Linux network namespaces and virtual Ethernet pairs (veth). Each host is a network namespace with its own IP stack. The switch is Open vSwitch (OVS). From inside a Mininet host, you can run any Linux networking tool (curl, ping, iperf) and it behaves as if it were a real machine on a real network.

Mininet version used: **2.3.0**, installed at `/usr/bin/mn`, Python package at `/usr/lib/python3/dist-packages/mininet/`.  
**Must be run with `sudo`** because creating network namespaces and veth pairs requires root.

### Open vSwitch (OVS)
OVS is the software switch inside Mininet. It speaks OpenFlow and is configured by Mininet to connect to the RYU controller. OVS maintains the flow table and applies rules at kernel speed via the `openvswitch` kernel module.

### RYU
RYU is a Python-based SDN controller framework. It handles the OpenFlow TCP connection, deserialises OF messages into Python objects, and dispatches them as events to your application. Your application (`load_balancer.py`) registers handlers for specific event types using the `@set_ev_cls` decorator.

RYU version: **4.34**, running under **Python 3.8** (Python 3.10 is incompatible — see `setup-challenges.md`).

### Locust
Locust is a Python load-testing framework. It spawns a configurable number of virtual users, each independently sending HTTP requests to the target, and measures throughput, latency percentiles, and failure rate. In this project, Locust users hit the VIP (`http://10.0.0.100`) and the load balancer distributes the resulting TCP connections across the server pool.

---

## 5. Topology

```
                         ┌──────────────────────────────────┐
                         │           s1 (OVS, OF 1.3)       │
                         │  port1 port2 port3 port4 port5 port6 port7 │
                         └──┬────┬────┬────┬────┬────┬────┬──┘
                            │    │    │    │    │    │    │
                           h1   h2   h3   h4   h5   h6   h7
                       10.0.0.1 .2  .3   .4   .5   .6   .7
                       [clients]     [servers — run HTTP on port 80]

                         s1 ←── OpenFlow 1.3 TCP ──→ RYU (127.0.0.1:6633)
                                                      load_balancer.py
                                                      VIP: 10.0.0.100
```

Port assignment (Mininet assigns ports in order of `addLink` calls):

| Host | Switch port | IP |
|------|------------|-----|
| h1 | 1 | 10.0.0.1 |
| h2 | 2 | 10.0.0.2 |
| h3 | 3 | 10.0.0.3 |
| h4 | 4 | 10.0.0.4 |
| h5 | 5 | 10.0.0.5 |
| h6 | 6 | 10.0.0.6 |
| h7 | 7 | 10.0.0.7 |

The VIP `10.0.0.100` has no host. It only exists as a MAC entry in the ARP reply the controller generates on demand (`00:00:00:00:00:fe`).

---

## 6. Code Walkthrough

### 6.1 topology.py

**Runtime**: `sudo /usr/bin/python3 topology.py`  
**Interpreter**: system Python 3.10 — the only Python that has Mininet in its site-packages (`/usr/lib/python3/dist-packages/mininet/`). The ryu-env Python 3.8 does NOT have Mininet.

#### Key decisions

```python
net = Mininet(controller=RemoteController, switch=OVSSwitch, autoSetMacs=True)
```

- `RemoteController`: Mininet will not start a bundled controller; the switch connects to RYU running in a separate process.
- `OVSSwitch`: use Open vSwitch as the switch implementation (supports OpenFlow 1.3).
- `autoSetMacs=True`: assigns deterministic MACs (`00:00:00:00:00:01` for h1, etc.) so ARP is predictable.

```python
s1 = net.addSwitch("s1", protocols="OpenFlow13")
```

Tells OVS to only negotiate OpenFlow 1.3. Without this, OVS defaults to OF 1.0, and RYU (configured for 1.3 only) would reject the handshake.

```python
net.build()
c0.start()
s1.start([c0])
s1.cmd("ovs-vsctl set Bridge s1 protocols=OpenFlow13")
time.sleep(1)
```

`s1.start([c0])` configures the OVS bridge and points it at the RYU controller. The extra `ovs-vsctl` command re-asserts the protocol after start, ensuring it is set before the TCP handshake completes in case of a timing race.

```python
host.cmd(
    f'echo "<h1>Server {host.name} ({host.IP()})</h1>" > /tmp/index.html && '
    f'cd /tmp && python3 -m http.server 80 &> /tmp/{host.name}_http.log &'
)
```

Each server host runs Python's built-in HTTP server on port 80. `host.cmd()` runs inside the host's network namespace, so the server is visible only from within the Mininet network. The `&` backgrounds the process so `CLI(net)` can proceed.

---

### 6.2 load_balancer.py

**Runtime**: `source ~/ryu-env/bin/activate && ryu-manager load_balancer.py`

#### Class declaration and version pin

```python
class LoadBalancer(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
```

`OFP_VERSIONS` tells RYU's OF protocol handler which versions this app supports. If the switch connects with a different version, RYU will reject it and log a negotiation error. Setting this to 1.3 only means the app can safely use OF 1.3-specific features like `OFPActionSetField`.

#### `switch_features_handler`

```python
@set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
def switch_features_handler(self, ev):
    match   = parser.OFPMatch()
    actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                      ofproto.OFPCML_NO_BUFFER)]
    self.add_flow(datapath, 0, match, actions)
```

This is the first thing that runs after a switch connects. `OFPMatch()` with no arguments is a wildcard — it matches every packet. Priority 0 is the lowest possible, so any other rule takes precedence. `OFPCML_NO_BUFFER` means "send the entire packet to the controller, do not buffer it in the switch". This is important: without it, OVS truncates the packet to 128 bytes and assigns a buffer ID. Truncated packets cannot be correctly parsed or forwarded.

The table-miss rule is the foundation of the whole system. Without it, the switch drops all unmatched packets (in secure fail-mode) or handles them itself without the controller (in standalone mode).

#### `add_flow` and the buffer_id nuance

```python
def add_flow(self, datapath, priority, match, actions,
             buffer_id=None, idle_timeout=0, hard_timeout=0):
    inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
    if buffer_id and buffer_id != ofproto.OFP_NO_BUFFER:
        mod = parser.OFPFlowMod(datapath=datapath, buffer_id=buffer_id, ...)
    else:
        mod = parser.OFPFlowMod(datapath=datapath, ...)
    datapath.send_msg(mod)
```

When a PacketIn arrives and the switch has buffered the packet (assigned a `buffer_id`), there are two ways to forward it:

1. **PacketOut with buffer_id**: tell the switch to apply actions to the buffered packet.
2. **FlowMod with buffer_id**: install a flow rule AND simultaneously apply the rule to the buffered packet.

Option 2 is used here because it installs the flow rule and forward the first packet in a single round trip. If you also send a PacketOut with the same buffer_id, the switch would try to forward the buffered packet twice — causing duplicate delivery and potential errors. This is the bug that was in the original version of this code.

The correct pattern (mirrored from `simple_switch_13`):
```python
if msg.buffer_id != ofproto.OFP_NO_BUFFER:
    self.add_flow(datapath, 1, match, actions, msg.buffer_id)
    return   # ← critical: no PacketOut, the FlowMod handles the buffered packet
else:
    self.add_flow(datapath, 1, match, actions)
# fall through to PacketOut (only when buffer_id == OFP_NO_BUFFER)
```

#### `packet_in_handler` — the decision tree

Every unmatched packet arrives here. The handler processes it in this order:

```
PacketIn
  │
  ├─ LLDP? → discard (link-layer discovery, not our concern)
  │
  ├─ ARP?
  │   ├─ ARP request for VIP (10.0.0.100)? → generate ARP reply, return
  │   └─ Other ARP → fall through to L2 forwarding
  │
  ├─ IPv4 to VIP (10.0.0.100)? → _handle_vip(), return
  │
  └─ Everything else → L2 forwarding (simple_switch_13 logic)
        ├─ Known dst MAC → install flow rule (priority=1), forward
        └─ Unknown dst MAC → flood
```

Non-VIP traffic uses `simple_switch_13`'s exact algorithm. This is deliberate: `simple_switch_13` is known to work correctly on this OVS version. Attempting to replace it with a custom PacketOut-only approach (no flow rule installation) caused 100% packet loss in testing because the controller was overwhelmed handling every single packet in a pingall across 7 hosts.

#### `_send_arp_reply`

When a client ARPs for `10.0.0.100`, it expects a MAC address back. The controller synthesises an ARP reply with the fake MAC `00:00:00:00:00:fe`. The client caches this and uses it as the Ethernet destination for all subsequent packets to the VIP. The switch then matches on that MAC and sends those packets to the controller via the table-miss rule, where the load balancing decision happens.

```python
reply.add_protocol(arp.arp(
    opcode=arp.ARP_REPLY,
    src_mac=VIRTUAL_MAC,   # fake MAC for VIP
    src_ip=VIRTUAL_IP,
    dst_mac=arp_pkt.src_mac,
    dst_ip=arp_pkt.src_ip,
))
```

The reply is sent as a `PacketOut` from `OFPP_CONTROLLER` (the controller port) out through the port the ARP request came in on.

#### `_handle_vip` — the load balancing core

```python
def _handle_vip(self, datapath, in_port, eth_pkt, ip_pkt, msg):
```

Step 1 — **Server selection** (sticky per client):

```python
if client_ip not in self.client_to_server:
    server_ip = self._select_server(client_ip)
    self.client_to_server[client_ip] = server_ip
```

Once a client is mapped to a server, all its flows go to the same server. This avoids the situation where TCP packets from the same connection get split across different servers (which would break TCP).

Step 2 — **Forward flow installation**:

```python
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
```

This rule matches IP packets from the client heading to the VIP. The actions rewrite **both** the destination MAC and destination IP, then forward to the server's port.

Why rewrite the MAC? Because the switch makes final forwarding decisions using MAC addresses. If only the IP is rewritten but the MAC is still `00:00:00:00:00:fe` (the VIP's fake MAC), the switch has no port for that MAC and will either drop the packet or flood.

Why must MAC be rewritten before IP? `OFPActionSetField` actions are applied in order. The MAC must resolve to the server's real MAC so the switch can actually forward the packet.

Step 3 — **Reverse flow installation**:

```python
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
```

This is the return path. When the server replies, its source IP is its real IP. The client is only aware of the VIP, so the source IP must be rewritten back to `10.0.0.100` before the packet reaches the client. The MAC is rewritten to the VIP's fake MAC for the same reason.

Without the reverse flow, the TCP three-way handshake would fail: the client sends SYN to 10.0.0.100, gets SYN-ACK from 10.0.0.4 (the server's real IP), sees a packet from an unexpected address, and resets the connection.

Step 4 — **Forward the triggering packet**:

```python
data = msg.data if msg.buffer_id == ofproto.OFP_NO_BUFFER else None
out  = parser.OFPPacketOut(...)
datapath.send_msg(out)
```

The flow rules cover future packets. The packet that triggered the PacketIn must be forwarded manually now, before the flow rule takes effect.

---

### 6.3 benchmark/locustfile.py

```python
class WebClient(HttpUser):
    host      = HOST          # http://10.0.0.100
    wait_time = between(0.5, 2)

    @task(3)
    def get_index(self):
        self.client.get("/", name="GET /index")

    @task(1)
    def get_health(self):
        self.client.get("/index.html", name="GET /index.html")
```

`@task(3)` and `@task(1)` set the relative weight of each task. For every 4 requests, 3 go to `/` and 1 goes to `/index.html`.

`wait_time = between(0.5, 2)` adds a random 0.5–2 second pause between requests per user, simulating human think time and preventing an artificial thundering herd.

Locust must be run **from inside the Mininet network** (from a client host's shell) because `10.0.0.100` only exists within the virtual network topology:

```
mininet> h1 ~/ryu-env/bin/locust -f ~/sdn-load-balance/benchmark/locustfile.py \
    --headless -u 100 -r 10 -t 60s --csv=~/sdn-load-balance/results/rr_100
```

The `quitting` hook at the bottom writes a summary CSV automatically when the test ends, without needing manual data collection.

---

### 6.4 analysis.py

Reads Locust's `*_stats.csv` output files and prints a comparison table:

```
File                                Reqs   Fails    Avg ms    p50    p95    p99      RPS
--------------------------------------------------------------------------------------------
rr_100_stats                         5820       0 ( 0.0%)     12.3     11     18     31  97.0
rr_200_stats                        11240      12 ( 0.1%)     18.1     16     28     55  187.3
```

This lets you compare algorithm performance across different concurrency levels in a single view.

---

## 7. Packet Flow: End-to-End Trace

Here is what happens when client `h1` makes an HTTP request to the VIP for the first time, with `ALGORITHM = "round_robin"` and no previous flows installed.

```
Step 1 — h1 ARPs for 10.0.0.100
  h1 broadcasts: "who has 10.0.0.100? Tell 10.0.0.1"
  Switch has no flow for ARP broadcasts → table-miss fires → PACKET_IN to RYU
  RYU: _handle_arp() sees ARP_REQUEST for VIRTUAL_IP
  RYU: _send_arp_reply() → crafts ARP REPLY: "10.0.0.100 is at 00:00:00:00:00:fe"
  RYU: sends PACKET_OUT → switch delivers ARP reply to h1 on port 1
  h1 caches: 10.0.0.100 → 00:00:00:00:00:fe

Step 2 — h1 sends TCP SYN
  h1 sends: SRC_IP=10.0.0.1 DST_IP=10.0.0.100 DST_MAC=00:00:00:00:00:fe (SYN)
  Switch: no matching flow (the fake MAC has no port) → table-miss → PACKET_IN

Step 3 — RYU selects server
  _handle_vip() called
  client_ip = "10.0.0.1" → not in client_to_server
  _select_server("10.0.0.1") → round_robin → "10.0.0.4" (h4)
  client_to_server["10.0.0.1"] = "10.0.0.4"
  connections["10.0.0.4"] = 1
  LOG: [round_robin] 10.0.0.1 → 10.0.0.4

Step 4 — RYU installs flow rules
  FLOW_MOD (priority=10, idle=30s):
    MATCH: in_port=1, eth_type=IP, ipv4_src=10.0.0.1, ipv4_dst=10.0.0.100
    ACTION: set eth_dst=00:00:00:00:00:04, set ipv4_dst=10.0.0.4, output(port 4)

  FLOW_MOD (priority=10, idle=30s):
    MATCH: in_port=4, eth_type=IP, ipv4_src=10.0.0.4, ipv4_dst=10.0.0.1
    ACTION: set eth_src=00:00:00:00:00:fe, set ipv4_src=10.0.0.100, output(port 1)

Step 5 — RYU forwards the SYN
  PACKET_OUT: apply same actions as forward flow, send SYN to h4
  h4 receives: SRC_IP=10.0.0.1 DST_IP=10.0.0.4 (SYN) ← correctly rewritten

Step 6 — h4 sends SYN-ACK
  h4: SRC_IP=10.0.0.4 DST_IP=10.0.0.1 (SYN-ACK)
  Switch matches reverse flow (priority=10, in_port=4, src=10.0.0.4, dst=10.0.0.1)
  Switch applies: set eth_src=00:00:00:00:00:fe, set ipv4_src=10.0.0.100, output(port 1)
  h1 receives: SRC_IP=10.0.0.100 DST_IP=10.0.0.1 ← looks like it came from VIP ✓

Step 7 — TCP ACK and all subsequent packets
  Handled entirely by switch flow rules at line rate.
  Controller not involved until flow expires after 30s idle.
```

---

## 8. Key Code Nuances

### Why `OFPCML_NO_BUFFER` matters

OVS has a known bug (fixed in v2.1.0, but relevant for old Mininet installs): when `max_len` in the controller output action is set to a small value (like 128 bytes), OVS sends a PacketIn with an invalid buffer_id and truncated data. Subsequent PacketOut using that buffer_id fails silently. Setting `OFPCML_NO_BUFFER` (value `0xFFFF`) tells OVS to send all bytes and not buffer — making the controller authoritative for that packet.

### Why non-VIP traffic installs flow rules

If the controller handles every packet via PacketOut without installing flow rules:
- Mininet's `pingall` generates 42 simultaneous ping sessions (7×6 pairs)
- Each ping involves ARP + ICMP request + ICMP reply = 126+ PacketIn events
- All arrive in milliseconds on RYU's single eventlet thread
- The queue overflows or processing latency causes packet timeout
- Result: 100% packet loss

Installing priority-1 flow rules offloads learned traffic to the switch. After the first packet in each pair, subsequent packets never reach the controller. This is the `simple_switch_13` pattern that this code is built on.

### Why both MAC and IP must be rewritten

A packet's path through a switch is:
```
Ingress port → L2 lookup (dst MAC) → output port → egress
```

If only the IP is rewritten, the Ethernet frame still has `DST_MAC=00:00:00:00:00:fe`. The switch's MAC table has no entry for this MAC (it's a fake address). The switch either drops the frame (secure mode) or floods it. Flooding to all 7 ports would work but is wasteful and would confuse hosts.

Rewriting both IP and MAC ensures the frame is correctly delivered to the specific server port.

### Why ARP for the VIP must be intercepted

If the ARP request for `10.0.0.100` is flooded (the default for unknown ARPs), every host receives it and none of them know their IP is `10.0.0.100`, so no one replies. The client's ARP times out, and no TCP connection is ever made. The controller must respond to this ARP directly.

### Session stickiness

```python
if client_ip not in self.client_to_server:
    server_ip = self._select_server(client_ip)
    self.client_to_server[client_ip] = server_ip
```

TCP requires all packets in a session to go to the same server. If the SYN goes to h4 but the ACK goes to h5, neither server has a complete TCP state machine and both will send RST. The sticky mapping ensures that once a client is assigned to a server, all its packets (even future TCP sessions, until the mapping is cleared) go there. In a production system, stickiness would be per TCP 4-tuple (src_ip, src_port, dst_ip, dst_port), not per source IP.

---

## 9. Running the Project

### Prerequisites

```
~/ryu-env/          Python 3.8 venv with RYU 4.34 + locust 2.25.0
~/miniforge3/       conda install (provides the Python 3.8 binary)
/usr/bin/mn         Mininet 2.3.0 (system, requires sudo)
```

### Start the controller (Terminal 1)

```bash
source ~/ryu-env/bin/activate
ryu-manager ~/sdn-load-balance/load_balancer.py
```

Expected output:
```
loading app load_balancer.py
LoadBalancer ready | algorithm=round_robin | VIP=10.0.0.100 | ...
Switch 1 connected — table-miss installed      ← appears when Mininet starts
```

### Start the topology (Terminal 2)

```bash
sudo /usr/bin/python3 ~/sdn-load-balance/topology.py
```

**Must use `/usr/bin/python3`** (system Python 3.10 with Mininet).  
Do not use `python3` from the ryu-env — it does not have Mininet.

### Verify basic connectivity

```
mininet> pingall
# Expected: 0% dropped
```

### Verify load balancing

```
mininet> h1 curl -s http://10.0.0.100
# Should return: <h1>Server h4 (10.0.0.4)</h1>
mininet> h2 curl -s http://10.0.0.100
# Should return: <h1>Server h5 (10.0.0.5)</h1>   (round_robin)
```

Terminal 1 should log: `[round_robin] 10.0.0.1 → 10.0.0.4`

### Switch algorithm

In `load_balancer.py`, line 32:
```python
ALGORITHM = "least_connections"   # change here, restart ryu-manager
```

---

## 10. Benchmarking

Run Locust from inside a Mininet client host so it can reach the VIP:

```
mininet> h1 ~/ryu-env/bin/locust \
    -f ~/sdn-load-balance/benchmark/locustfile.py \
    --headless -u 100 -r 10 -t 60s \
    --csv=~/sdn-load-balance/results/rr_100
```

Repeat for each user count to build a comparison dataset:

```
mininet> h1 bash -c 'for u in 100 200 400 600 800; do \
    ~/ryu-env/bin/locust \
        -f ~/sdn-load-balance/benchmark/locustfile.py \
        --headless -u $u -r 20 -t 60s \
        --csv=~/sdn-load-balance/results/rr_$u; \
done'
```

Then compare algorithms:
```bash
# Change ALGORITHM in load_balancer.py, restart, re-run benchmarks
# Then analyse all results together:
python3 ~/sdn-load-balance/analysis.py ~/sdn-load-balance/results/
```

### What to measure

| Metric | Meaning |
|--------|---------|
| Requests/s (RPS) | Throughput — higher is better |
| Average response time | Mean latency |
| p95 / p99 | Tail latency — what the slowest 5% / 1% of users experience |
| Failure % | 0% expected; non-zero means servers or the controller are overwhelmed |

The load balancer adds one RTT of latency for the first packet of each connection (the PacketIn → FlowMod round trip). All subsequent packets in the same flow are unaffected. At high concurrency, the bottleneck shifts from controller processing to the HTTP server's capacity.
