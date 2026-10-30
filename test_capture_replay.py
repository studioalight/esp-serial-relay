#!/usr/bin/env python3
"""Replay a relay capture dump through the wire phase decoder.

Usage: python3 test_capture_replay.py capture.txt [capture2.txt ...]
Dump format (from GET /capture/dump.txt): "HH:MM:SS dir hexchunk" per line.

Prints the decoded phase timeline and final state. Exit 0 = replay clean.
The point: a real flash capture becomes a permanent regression test — no board needed.
"""
import sys, json, importlib.util

spec = importlib.util.spec_from_file_location("serial_relay", "serial_relay.py")
S = importlib.util.module_from_spec(spec)
spec.loader.exec_module(S)


class R:
    phase = None
    phase_src = None
    _flash_total = None
    _flash_done = 0
    _phase_scan = bytearray()
    _wire_phase_frame = None


def replay(path):
    r = R()
    timeline = []
    n_lines = 0
    with open(path) as f:
        for line in f:
            parts = line.split()
            if len(parts) != 3:
                continue
            ts, d, hx = parts
            try:
                chunk = bytes.fromhex(hx)
            except ValueError:
                continue
            n_lines += 1
            S.wire_decode_phase(r, chunk, d)
            p = r.phase
            state = None if p is None else (p["phase"] + ("" if p.get("pct") is None else f" {p['pct']}%"))
            if state and (not timeline or timeline[-1][1] != state):
                timeline.append((ts, state))
    print(f"== {path}: {n_lines} chunks")
    for ts, st in timeline:
        print(f"   {ts}  {st}")
    print(f"   final: {r.phase} (src={r.phase_src}, flash_total={r._flash_total})")
    return r


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    for p in sys.argv[1:]:
        replay(p)
