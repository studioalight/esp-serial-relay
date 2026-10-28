# esp-serial-relay

**A generic browser→RFC2217 serial-port relay for OpenClaw agents** — ESP32 is the
primary use case, not a requirement.

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