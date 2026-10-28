---
name: esp-serial-relay-install
description: "Install, verify, and distribute the ESP serial relay on any shore: deps, LaunchAgent with re-homed paths, tailscale serve route, end-to-end curl proof, and the traps (manifest verification, CLI location, log paths, watcher re-homing). The second-shore install ran live 2026-10-28 (VM 3)."
---

# ESP Serial Relay — install & distribute

The relay turns a browser tab's Web Serial port (on the Mac holding the ESP32's USB)
into an RFC2217 endpoint any local tool can drive: `esptool rfc2217://127.0.0.1:4001`,
with a dashboard on :4002 (◆ annotations, wall-clock stamps, link-state Easy mode).

## Install on this machine

```sh
python3 install_relay.py            # full: deps + LaunchAgent + listeners + serve route
python3 install_relay.py --dry-run  # walkthrough without changes
python3 install_relay.py --skip-serve
```

The installer encodes the field-verified install procedure (first executed cross-host, 2026-10-28):
1. `pyserial --user` (websockets is usually preinstalled)
2. LaunchAgent `app.esp-serial-relay` with THIS host's paths (legacy `app.clawkie.serial-relay` cleaned up when present) (script found beside
   the installer or in known workspace layouts)
3. Listener gate: 4001 (RFC2217) + 4002 (dashboard) must both be listening
4. `tailscale serve --bg --https=8444 http://127.0.0.1:4002` — CLI found via
   `/Applications/Tailscale.app/Contents/MacOS/Tailscale` or PATH
5. Verify: `curl -sk https://<host>.tail<net>.ts.net:8444/` → 200 with the dashboard
   signature; then curl the ORIGINAL shore the same way for the symmetry proof

## Traps (all field-caught)

- **Verify the tarball manifest before shipping** — `remote-flash-rfc2217` was silently
  missing from the first cross-shore pack because it lived in `workspace/skills/` while
  the packer read `workshop-skills/`. Receiver-side caught it.
- **The tailscale CLI is the .app binary** on macOS shores — `/Applications/Tailscale.app/
  Contents/MacOS/Tailscale` — not `$PATH`.
- **Gateway 443 ≠ relay 8444**: the gateway's managed serve (`mode: serve`) and your
  added `serve --bg` route are separate mechanisms; the route survives gateway restarts
  only if the CLI route was added with `--bg`.
- **Log paths are per-install** — this installer writes `~/Library/Logs/serial-relay.err.log`; earlier hand-rolled installs may have used different names. Know which log you're grepping.
- **`--after no_reset` leaves the board in download mode** — kick out with
  `esptool --before no_reset --after hard_reset chip_id` before the watcher re-reads.
- **A zombie watcher holds :4001** after LaunchAgent respawn races — the relay logs
  `client already active — refusing`; kill the orphan, then bootout/bootstrap the watcher.
- **Zero bytes at :4001 = the tab lost the Web Serial port**, not a daemon failure —
  reload the tab on the USB-host Mac.
- **Flash size answers what the chip says, not your layout** — take offsets from the
  project's `build/flash_args`, never memory (ours: chip says 32 MB, layout is 16 MB).

## Distribution as an OpenClaw plugin

This repo IS the plugin payload: `openclaw.plugin.json` (id `esp-serial-relay`,
disabled by default, `onStartup: false` — install on demand) pointing at the manifest's
`extra.espRelay` map (script/installer/watcher paths, ports, topology note). To publish:
serve or commit the repo; on the target shore clone + run the installer; the skill ships
with it. The watcher plist (`com.openclaw.serialwatch.plist`) is per-host — edit its
paths before use (single-instance flock is built in).

## Companion scripts

- `serial_watch.py` — durable board-log watcher (wall-clock `[HH:MM:SS.mmm]` stamps,
  single-instance flock; pairs with the plist)
- `monitor_sample.py` — JSONL sampler for HTTP-reporting boards (5 s cadence, wall stamps)
- `utun5_rate.py` — macOS wire-rate sampler via `netstat -id` deltas (BPF is root-only;
  rename the interface per host)