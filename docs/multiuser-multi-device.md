# Multi-user + multi-device relay — design investigation

**Date:** 2026-11-05 · **Status:** design investigation (no code yet)
**Scope:** one relay daemon serving (A) multiple users of one OpenClaw gateway and
(B) multiple devices on multiple USB ports across different web tabs.

## 1. Current singleton choke points (all verified in source)

| choke point | location | today |
|---|---|---|
| One RFC2217 PortManager slot | `rfc2217_client` line 1495 | `if self.pm is not None: refuse new client` |
| One browser WS link | `browser_handler` line 1269 | `"second browser link — replacing"` — kills the previous tab's port link |
| One `browser_ready` Event | `Relay.__init__` line 1196 | global gate on ALL payload forwarding |
| One phase state | `self.phase` line 1202 | single esptool phase for every device |
| One capture ring | `self.cap` line 1214 | one 60k-chunk ring for all traffic |
| One monitor log mirror | `self.mon_lines` line 1199 | one shared (ts, dir, text) stream |
| One serial_link status | `status_snapshot` line 1478 | `self.ws is not None` — single device boolean |
| Tap tagging | `monitor_tap` lines 1280/1326/1546 | direction only, no session/device label |
| Device identity | no identity read anywhere | `navigator.serial.requestPort()` only; no VID/PID/serialNumber read |

Consequences to design against:

- **Serial lines are physically single-slot per device.** Two users flashing the
  same board concurrently would interleave SLIP bytes and corrupt the flash.
  Exclusivity per device is non-negotiable; concurrency must be per-device or
  queued per device.
- **A web tab holds exactly one Web Serial port at a time** (browser-enforced).
  Multi-device therefore = multiple tabs, each with its own port. The relay must
  track a *link per device*, not replace a global link.

## 2. Target model: device registry + exclusive-use claims

### DeviceSession

Each device gets its own session object, replacing today's scattered singletons:

```
DeviceSession {
  id            # stable identity: vid/pid + serialNumber (or vid/pid + short hash if serialNumber empty)
  label         # friendly name (board model + serial suffix; user-renameable)
  ws            # this tab's /relay WebSocket (one per device)
  browserReady  # Event, per device
  pm, pmWriter  # PortManager for its own RFC2217 slot
  rfcPort       # assigned RFC2217 port (see allocation)
  phase, pct, phaseSrc
  cap           # own capture ring
  counts, monLines, monBuf
  listeners     # console WS subscribers filtered to this device
  ownerClaim    # current exclusive-use claim (id, ownerLabel, since)
}
```

### Link binding (tabs → devices)

`/relay` hello message extends from `{t:"hello", baud}` to also carry
`{device: {vendorId, productId, serialNumber, label?}}` read via `port.getInfo()`
on the page. Relay get-or-creates the session by that identity; `ws` replace
happens **per device** (same tab re-opening = replace that device's ws; a second
tab for the *same* device follows today's replace rule scoped to the session).

Device identity fallback when `serialNumber` is empty: `vid/pid + a stable
short label assigned on first sight` (persisted in a small device-registry JSON
so labels survive restarts).

### RFC2217 port allocation

- **Device ports are assigned**, not user-chosen: base 4001 + 2·index
  (4001, 4003, 4005, …) configurable base.
- **v65 PORT DISCIPLINE:** infrastructure ports are reserved — never assign a
  device to **4002** (page/WS + Tailscale-serve's persistent upstream: the
  proxy's connection gets accepted as an rfc2217 client — the boot-time
  "ghost connector", field-proven 2026-11-08) or to a port claimed by another
  device. `registry_rename` rejects 4001/4002; the boot auto-heal moves any
  registry row squatting on the infrastructure ports. New device ports are
  safest at **4004+**.
- The **first/only device keeps 4001** — backward compatible with all current
  flash tooling (`rfc2217://127.0.0.1:4001`).
- Assignment is persisted in the device-registry JSON (device → rfcPort) so a
  board's port survives daemon restarts.

### Exclusive-use claims (multi-user on one gateway)

Serial lines still exclusive per device → users *claim* a device's slot:

- `POST /claims` (page/WS) — body `{deviceId, ownerLabel, ownerKey?}` →
  `{claimId, port, ttl}`. esptool then runs against the assigned port.
- The rfc2217 client for that port is bound to the claim; payload only forwards
  while the claim is active. Disconnect/expire → auto-release; queue per device
  with ETA for waiters; `force-release` admin action.
- **Identity is per-gateway-session when driven by an agent.** The plugin tools
  (`esp_relay_install`) can carry the claim automatically: the tool context's
  session key becomes the `ownerLabel` (each agent session of the gateway gets
  its own claim). No trust in TCP peers (everything is loopback anyway).
- Legacy behavior: unclaimed direct clients to :4001 use the device's public
  exclusive slot (today's behavior — first-come exclusive, no queue).

## 3. Per-device everything else

- **Phase decoding:** decoder state moves per session; console frames tagged
  `{t:"phase", device:id, phase, pct}` and fanned out only to that session's
  listeners (or to `all` subscribers).
- **Capture:** ring per device; `/capture/dump.txt` gains `?device=<id>` (or a
  combined dump with a `device` column for cross-device correlation).
- **Monitor mirror:** `mon_lines` per session; `/console` subscribers can pass
  `?device=` to filter (default all, with device labels in frames).
- **Status:** `/status.json` gains `devices:[{id,label,link,rfcPort,phase,
  counts,listeners,ownerClaim}]`; legacy top-level fields kept for the
  default/first device for backward compat.

## 4. UI changes

- **Relay control page (`/`):** per-tab, scoped to that tab's own device
  (hello handshake identity). Toolbar gains the device label + assigned port.
- **Dashboard (`/dashboard`):** device selector or per-device cards — link
  state, rfcPort, phase chips + progress bar, listeners, owner claim. One
  board per card; no cross-talk.
- **Claims page (new, `/claims`):** current owners + per-device queues with
  ETA, `force-release` admin, history (last few claims). Needed for
  multiuser operation to be observable.
- **Settings (`/settings`):** base port + wait time global; device labels
  editable per session (reassign port button).

## 5. Migration phases (backward compatible at each step)

| phase | ships | compat guarantee |
|---|---|---|
| **P1 — device registry + per-device ports** | sessions, link binding, assigned ports, status.json devices list | single device ⇒ exactly today's behavior; :4001 keeps first device |
| **P2 — per-device phase/capture/mirror** | decoder state per session, tagged frames, per-device dump/filter | single device ⇒ same panels, same data shapes |
| **P3 — claims + multiuser** | claim API, exclusive slots with queue/ETA, force-release, claims page | unclaimed :4001 keeps legacy exclusive behavior |
| **P4 — tests + field trial** | two boards on two tabs + two gateway sessions claiming different devices; conflict test on the same board | golden-capture replay stays green |

Non-goals for this design (kept deliberately out): TLS/auth on the WS endpoints
(unchanged trust model — tailnet-only), cross-machine device pooling (one
gateway per host; each host runs its own relay), arbitrary concurrent flash on
the same board (physically impossible).

## 6. Risks and open questions

1. **USB serialNumber absence** on some boards: fallback labels must be stable
   across un/replug. If two identical boards have no serial, the registry has to
   key on a persisted per-board label instead of true identity.
2. **Port exhaustion / drift:** assigned ports persist; reassigning requires
   care if a flash is in flight (guard reassignment while a claim is active).
3. **Phase-state bleed across devices:** a naive refactor risks one device's
   decoder seeing another's frames; the tap must attach device labels at the
   source (three current tap points must all route by session).
4. **Claim ownership on agent-driven flashes:** session keys must not be
   guessable by a local unauthenticated user on the gateway host — the claim
   token should be a random key issued by the relay, not a session label alone.

## 7. Recommendation

Build P1+P2 first (registry + per-device state) — the biggest UX unlock
(multiple boards on multiple tabs without replacing each other). Claims (P3)
are a smaller additive layer once the registry exists; they turn the "one
gateway, many users" case from undefined-behavior into an observable, queued,
exclusive-slot protocol.

The alternative (no registry, port-per-user claim-first) would leave
multi-device tabs fighting over the global ws singleton — structurally worse.
The registry is the right primitive; claims ride on it.
