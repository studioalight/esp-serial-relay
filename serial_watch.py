import serial, time, os, sys

# Default: the gateway serial-relay (browser-piped). The old bridge-Mac
# server (rfc2217://100.87.66.78:4001) is retired 2026-10-15.
URL = os.environ.get("CLAWKIE_SERIAL_URL", "rfc2217://127.0.0.1:4001")
LOG = "/tmp/amoled_serial_watch.log"

while True:
    try:
        ser = serial.serial_for_url(URL, 115200, timeout=1)
        with open(LOG, "ab") as f:
            f.write(f"\n=== watcher connect {time.strftime('%H:%M:%S')} via {URL} ===\n".encode())
            f.flush()
        # Boot pulse (relay path): open alone leaves the S3 in download mode;
        # the classic RTS reset boots the app (proven 2026-10-15 16:36).
        try:
            ser.dtr = False
            ser.rts = False
            time.sleep(0.2)
            ser.rts = True
            time.sleep(0.1)
            ser.rts = False
            time.sleep(0.1)
            ser.dtr = True
        except Exception:
            pass
        while True:
            chunk = ser.read(4096)
            if chunk:
                with open(LOG, "ab") as f:
                    f.write(chunk); f.flush()
    except Exception as e:
        with open(LOG, "ab") as f:
            f.write(f"\n=== watcher error {time.strftime('%H:%M:%S')}: {type(e).__name__}: {e} — reconnecting ===\n".encode())
            f.flush()
        try:
            if 'ser' in dir(): ser.close()
        except Exception: pass
        time.sleep(3)