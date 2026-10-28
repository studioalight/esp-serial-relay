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

## Build (plugin wrapper)

```bash
npm install
npm run plugin:build
npm run plugin:validate
```