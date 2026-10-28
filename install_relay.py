#!/usr/bin/env python3
"""ESP serial relay — installer.

Encodes the field-verified install procedure: dependencies, LaunchAgent with
re-homed paths, and the optional tailscale serve route that publishes the
relay dashboard tailnet-wide.

Usage:
  python3 install_relay.py [--dry-run]
  python3 install_relay.py --skip-serve      # relay only, no ts.net route
  python3 install_relay.py --call-period MS  # (room node firmware knob; see skills)

What it does:
  1. Verifies python3 + pyserial (+ websockets for the relay daemon)
  2. Writes a LaunchAgent (app.esp-serial-relay) with THIS machine's paths
  3. Bootstraps it and verifies the 4001/4002 listeners
  4. Adds `tailscale serve --https=8444 -> 127.0.0.1:4002` via the Tailscale.app
     binary (the CLI is usually NOT on PATH on macOS)

Idempotent: safe to re-run; it bootouts any existing agent before bootstrap.
"""
import argparse
import os
import plistlib
import subprocess
import sys

LABEL = "app.esp-serial-relay"
LEGACY_LABELS = ["app.clawkie.serial-relay"]  # cleaned up when present
RELAY_PORTS = (4001, 4002)
SERVE_PORT = "8444"
HOME = os.path.expanduser("~")


def run(cmd, check=True, capture=False):
    printable = " ".join(cmd) if isinstance(cmd, list) else cmd
    print(f"  $ {printable}")
    result = subprocess.run(cmd, capture_output=True, text=True,
                            shell=isinstance(cmd, str))
    if check and result.returncode != 0:
        print(f"  ! failed ({result.returncode}): {result.stderr.strip()[:300]}")
        if check:
            sys.exit(1)
    out = (result.stdout or "").strip()
    if out and not capture:
        print(f"    {out[:400]}")
    return out


def find_relay_script():
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, "serial_relay.py"),
        os.path.join(HOME, ".openclaw/workspace/serial-relay/serial_relay.py"),
        os.path.join(HOME, "serial-relay/serial_relay.py"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    print("serial_relay.py not found next to the installer nor in known paths")
    sys.exit(1)


def find_tailscale_cli():
    candidates = [
        os.environ.get("TAILSCALE_CLI"),
        "/opt/homebrew/bin/tailscale",
        "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
        os.path.join(HOME, "Applications/Tailscale.app/Contents/MacOS/Tailscale"),
    ]
    for c in candidates:
        if c and os.access(c, os.X_OK):
            # the .app binary self-identifies oddly sometimes; confirm it runs
            probe = subprocess.run([c, "version"], capture_output=True, text=True)
            if probe.returncode == 0 or "tailscale" in (probe.stdout + probe.stderr).lower():
                return c
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-serve", action="store_true")
    ap.add_argument("--uid", type=int, default=os.getuid())
    args = ap.parse_args()
    dry = args.dry_run
    gui_uid = args.uid

    relay = find_relay_script()
    log_out = os.path.join(HOME, "Library/Logs/serial-relay.out.log")
    log_err = os.path.join(HOME, "Library/Logs/serial-relay.err.log")
    print(f"relay script: {relay}")

    # 1 --- dependencies
    print("\n[1/4] python dependencies")
    run([sys.executable, "-m", "pip", "install", "--user", "pyserial", "--quiet"],
        check=False)
    for mod in ("serial.rfc2217", "websockets"):
        r = run([sys.executable, "-c", f"import {mod}; print('ok {mod}')"],
                check=False, capture=True)
        if "ok" not in r:
            print(f"  ! dependency {mod} still missing — install it manually")
            sys.exit(1)

    # syntax gate (the manifest-verification rule from 2026-10-28)
    r = run([sys.executable, "-c",
             f"import ast; ast.parse(open('{relay}').read()); print('syntax ok')"],
            check=False, capture=True)
    if "syntax ok" not in r:
        print("  ! relay script does not parse — refusing to install")
        sys.exit(1)

    # 2 --- LaunchAgent with re-homed paths
    print("\n[2/4] LaunchAgent")
    plist_path = os.path.join(HOME, f"Library/LaunchAgents/{LABEL}.plist")
    plist = {
        "Label": LABEL,
        "ProgramArguments": [sys.executable, relay],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 5,
        "StandardOutPath": log_out,
        "StandardErrorPath": log_err,
        "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"},
    }
    if not dry:
        os.makedirs(os.path.dirname(plist_path), exist_ok=True)
        os.makedirs(os.path.dirname(log_out), exist_ok=True)
        with open(plist_path, "wb") as f:
            plistlib.dump(plist, f)
        run(["plutil", "-lint", plist_path])
        run(["launchctl", "bootout", f"gui/{gui_uid}/{LABEL}"], check=False)
        # clean up legacy labels from earlier distributions (harmless if absent)
        for legacy in LEGACY_LABELS:
            run(["launchctl", "bootout", f"gui/{gui_uid}/{legacy}"], check=False)
        run(["launchctl", "bootstrap", f"gui/{gui_uid}", plist_path])

    # 3 --- listener verification
    print("\n[3/4] listeners")
    import time
    if not dry:
        time.sleep(4)
    ls = run(["lsof", "-nP", "-iTCP:4001", "-iTCP:4002", "-sTCP:LISTEN"],
             check=False) if not dry else "(dry-run: skipped)"
    ok = ("4001" in ls and "4002" in ls) if not dry else True
    print(f"  {'OK' if ok else 'FAILED'}: {'both listeners up' if ok else 'missing'}")
    if not ok:
        err = os.path.join(HOME, "Library/Logs/serial-relay.err.log")
        print(f"  check the daemon log: {err}")
        sys.exit(1)

    # 4 --- tailscale serve route
    print("\n[4/4] tailscale serve route")
    if args.skip_serve:
        print("  skipped (--skip-serve)")
        return
    ts = find_tailscale_cli()
    if ts is None:
        print("  ! no tailscale CLI found (.app binary or PATH) — route not added")
        print(f"    add manually: tailscale serve --bg --https={SERVE_PORT} http://127.0.0.1:4002")
        return
    print(f"  cli: {ts}")
    if not dry:
        run([ts, "serve", "--bg", f"--https={SERVE_PORT}", "http://127.0.0.1:4002"],
            check=False)
        ts_name = run([ts, "status", "--json"], check=False, capture=True)
        print(f"  dashboard: https://<your-host>.tail<net>.ts.net:{SERVE_PORT}/")
        print("  done — verify with: curl -sk https://<host>:8444/ (expect the relay page)")


if __name__ == "__main__":
    main()