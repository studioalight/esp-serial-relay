# esp-serial-relay

**A generic browser→RFC2217 serial-port relay for OpenClaw agents** — ESP32 is the
primary use case, not a requirement.

## The problem it addresses

An OpenClaw agent lives on a gateway host (a VM, a Mac, a Linux box) — but the
**development boards it needs to work with are plugged into a *different* machine**:
a laptop on a desk, a bench Mac in another room, a colleague's desktop. USB is
physical; agents are not.

Without this bridge, every embedded-development turn hits the same wall:

- **The agent can't see the board.** Flash, monitor, log-capture — all need a serial
  port on a machine the agent may not control, or SSH keys the operator won't hand out.
- **The workarounds are brittle.** ssh-piped socat/rsync chains, USB-over-network
  daemons, or "email me the log file" loops — each adds a failure mode, none give the
  agent live read/write, and none survive a wedged connection.
- **The port is a single-slot resource.** Two consumers opening it concurrently wedge
  each other; wedged holders require human intervention at the far end.
- **Diagnostics lose the clock.** Cross-correlating a board's log with agent-side
  events needs timestamps on a shared clock; raw serial streams come with none.

## What it takes on (the design answers)

- **Browser WebSerial as the local port-holder.** The USB-host Mac needs zero
  software installation: a tab opens, the user grants port access, done. The browser
  is the USB daemon — sandboxed, permission-gated, and reload-to-recover by design.
- **One standard protocol on the tool side.** The relay speaks RFC2217 (pyserial's
  own), so every existing serial tool — `esptool`, `idf.py`, `picotool`, miniterm,
  anything — works against the remote device unchanged and locally.
- **Wedging is survivable, not fatal.** Tab reloads recover the port; the relay
  releases cleanly and waits; a single-instance-lock watcher prevents orphan holders
  from fencing the port forever.
- **The stream stays honest.** In-page ◆ annotations (which survive tab reloads),
  millisecond wall-clock stamps injected by the capture watcher, and serial-line-rate
  discipline — because >5 lines/s of board output both starves the attached tasks
  and invalidates every latency measurement taken over the link.
- **Install is a procedure, not a project.** The verified installer encodes the whole
  path — dependencies, a LaunchAgent with re-homed per-host paths, a listener gate,
  and an optional tailscale serve route — so a new host is minutes, not an afternoon.
- **Agents operate it, not just humans.** Shipped as an OpenClaw tool-plugin:
  `esp_relay_status` reports bridge state in any session; `esp_relay_install` runs
  the installer. The install/distribute skill ships inside the payload.

Known limits, honestly: the RFC2217 listener binds loopback (tools run on the same
host as the relay — tunnel or run them there); the browser tab must stay open — a
lost tab drops the serial lane until reconnected; and only one consumer may hold the
port at a time, by design.

## What it actually is (the generic core)

The relay daemon (`serial_relay.py`) is chip-agnostic. Its topology:

```
[browser tab on the USB-host Mac]  --WebSerial over WSS-->  [relay daemon]
        |  local USB serial ("grant port access")                |  RFC2217 127.0.0.1:4001
        |                                                        |  dashboard :4002
        v                                                        v
  ANY serial device                                         local serial tools
```

Any OpenClaw agent can then drive the remote serial port as if it were local:
`esptool`, `picotool`, `idf.py monitor`, miniterm, or a plain `pyserial` script —
the RFC2217 listener speaks standard pyserial `rfc2217://127.0.0.1:4001`. The
browser tab grants access to whichever port the user picks (macOS exposes
usbmodem/cu.* names for any USB serial device).

Also included: ◆ in-page annotations (survive tab reloads, land in the relay log
with server-side ms timestamps), wall-clock-stamped capture output, a link-down
Easy-mode recovery message, and an optional tailscale serve route so the dashboard
is reachable tailnet-wide (`https://<host>.tail<net>.ts.net:8444/`).

## What is RFC2217? (the protocol this runs on)

[RFC 2217](https://www.rfc-editor.org/info/rfc2217) — *Telnet Com Port Control
Option* (IETF, October 1997) — is the standard that makes a remote serial port behave
like a local one: a server listens on TCP, and a small option channel inside the
session carries the serial-line control traffic (baud rate, data bits, parity, stop
bits, flow control, DTR/RTS modem lines) between client and server.

pyserial speaks it natively on both ends:

```python
# on the machine with the device — a plain server, no relay needed for simple cases:
serial.serial_for_url("rfc2217://host:4001", ...)   # client: any pyserial tool
```

The practical contract: the **client** sets line parameters, the **server** applies
them to the real port, and every DTR/RTS toggle is a network roundtrip — which is why
board resets over high-latency links have their own failure modes and pacing rules
(see the traps section and the flash skill). What RFC2217 does *not* cover is the
USB-plumbing side: claiming a port, surviving hotplug, recovering from a dead holder —
which is exactly the part the browser-tab topology and this relay add.

## Why the name says "esp"

This distribution grew out of the Studio Alight room-node project, so the
*payload around the core* is ESP-focused: the skills
(`skills/esp-serial-relay-install/`) carry ESP-IDF/esptool field lessons
(download-mode kicks, flash_args offsets, watchdog fingerprints), the companion
scripts target our boards, and the installer's verified path was an ESP32
Waveshare AMOLED room node. None of that gates the core — for another device
family, reuse the daemon + installer and skip the ESP skills.

## Layout

| file | role |
|---|---|
| `serial_relay.py` | the relay daemon (RFC2217 :4001 + dashboard :4002) — generic |
| `install_relay.py` | verified self-installer: deps → LaunchAgent → listener gate → tailscale serve :8444 |
| `serial_watch.py` | durable board-log watcher (wall-clock stamps, single-instance flock) |
| `monitor_sample.py` | JSONL sampler for HTTP-reporting boards |
| `utun5_rate.py` | macOS wire-rate sampler via netstat deltas (BPF-free) |
| `skills/esp-serial-relay-install/` | install/distribute skill (ESP field lessons included) |
| `openclaw.plugin.json` + `src/index.ts` | the OpenClaw tool-plugin wrapper (`esp_relay_status`, `esp_relay_install`) |

## Install

```bash
python3 install_relay.py            # deps + LaunchAgent + listeners + serve route
python3 install_relay.py --dry-run  # walkthrough
```

As an OpenClaw plugin: `openclaw plugins install npm-pack:<tgz>` (or `--link <repo>`
for development, `clawhub:` once published). The installer is also exposed as the
`esp_relay_install` agent tool when the plugin is enabled.

## Traps the skill encodes (all field-caught)

- The tailscale CLI is the `.app` binary on macOS — not on `$PATH`
- Verify the tarball manifest before shipping (a missing skill sailed through once)
- Gateway 443 (managed serve) vs relay 8444 (added route) are separate mechanisms
- `--after no_reset` leaves ESP32 boards in download mode — kick with a hard reset
- A zombie watcher can hold RFC2217 port 4001 — kill the orphan, then re-bootstrap
- Zero bytes at :4001 means the browser tab lost the port — reload the tab
- Flash sizes: read offsets from the project's `build/flash_args`, never memory

## The design journey (how it got here)

This tool wasn't designed on paper — it was beaten into shape by failures, each one
leaving a rule behind. The honest arc:

**0. The bridge is born (2026-03).** The lineage starts months earlier than the
tool itself, in three sibling repos that still exist:

- [`studioalight/esp32-bridge`](https://github.com/studioalight/esp32-bridge) — v2.0
  established the core moves this relay still uses: serial monitor + flashing over
  WebSocket, USB hotplug detection, **smart reconnection tracking devices by
  hardware ID**, chip-ID auto-detection, batch flash in one esptool invocation,
  and tailscale auto-discovery for remote reach.
- [`studioalight/esp-idf-project-builder`](https://github.com/studioalight/esp-idf-project-builder)
  — the config-free workflow: flash addresses read from ESP-IDF build artifacts,
  target verification before flashing, multi-target (S2/S3/C3/C6/P4). The October
  rule "offsets from `build/flash_args`, never memory" was this repo's principle,
  rediscovered.
- [`studioalight/esp32-p4-skill`](https://github.com/studioalight/esp32-p4-skill) —
  the topology this whole family shares: **container/agent → tailscale →
  bridge-on-Mac → USB → board**. The agent was always on the far side of the wire;
  only the middle has been rewritten.

**1. The bridge-Mac era (2026-10-12–15).** The October generation ran flashes over a
WAN hop to a pyserial RFC2217 server on a remote Mac — and died to a latency wall
nobody had named: every DTR/RTS control command is a network roundtrip, and esptool's
reset timing breaks mid-flash on anything but a LAN. The measured pacing table
(connection setup costs as much as a 1.2 MB flash — batch everything into one write)
is still in the flash skill. The relay itself started as Fredrik's redesign on
2026-10-15: *the gateway owns both endpoints* — the RFC2217 listener AND the
WebSocket the browser pipes serial through — "the daemon never holds a half-open USB
lease on a remote box."

**2. The wedge kills (2026-10-15–24).** The bridge-Mac server wedged on dead clients
"one too many times" — hence the browser-tab topology: the wedge class becomes
"reload one tab." But wedging found new ways in: a zombie watcher from a LaunchAgent
respawn race holding the port for hours («client already active — refusing» in the
relay log became the diagnostic), and `--after no_reset` leaving boards stuck in
download mode needing a hard-reset kick. Each got a rule and a guard
(single-instance flock, the kick command in the skill).

**3. Packaging (2026-10-28).** The bridge went to a second OpenClaw instance
as a file drop, and the receiver caught what the sender missed (a missing skill in
the tarball — verify the manifest before shipping). Installing it converted the
procedure into the self-installer, then into this plugin with install/status tools.
Both shores' dashboards answer 200 side-by-side on their tailscale serve routes.

The thread through all of it: **every failure became an instrument, every instrument
became a rule, and every rule ships in the skills** — so the next board starts from
the map instead of the minefield.

## Build (plugin wrapper)

```bash
npm install
npm run plugin:build
npm run plugin:validate
```