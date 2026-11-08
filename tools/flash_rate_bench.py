import serial, time, subprocess, sys, struct

def pm_open(baud=115200):
    return serial.serial_for_url("rfc2217://127.0.0.1:4003", baudrate=baud, timeout=1)

def dance_to_115200():
    """Guarantee the tab's UART is at 115200: force it away, then back."""
    s = pm_open(115200); time.sleep(1)
    s.baudrate = 921600; time.sleep(1.5)
    s.baudrate = 115200; time.sleep(1.5)
    s.close()

def reset_to_app():
    s = pm_open(115200); time.sleep(0.5); s.read(32768)
    s.setDTR(False); s.setRTS(False); time.sleep(0.3)
    s.setDTR(True);  s.setRTS(False); time.sleep(0.1)
    s.setRTS(True);  time.sleep(0.25)
    s.setRTS(False); time.sleep(0.1)
    s.setDTR(False)
    buf = b""; end = time.time() + 10
    while time.time() < end: buf += s.read(16384)
    s.close()
    return b"liminal" in buf.lower()

def kick():
    dance_to_115200()
    if not reset_to_app(): reset_to_app()
    time.sleep(1.5)
    s = pm_open(115200); time.sleep(0.4); s.read(32768)
    s.write(b"KICK\n"); time.sleep(3)
    buf = b""; end = time.time() + 3
    while time.time() < end: buf += s.read(8192)
    s.close()
    return b"waiting for download" in buf

def flashonce(baud):
    if not kick():
        print(f"baud {baud:>7}: entry failed", flush=True); return
    t0 = time.time()
    p = subprocess.run(
        [sys.executable, "-m", "esptool", "--port", "rfc2217://127.0.0.1:4003",
         "--chip", "esp32s3", "-b", str(baud), "--before", "no_reset", "--after", "no_reset",
         "write_flash", "--flash_mode", "dio", "--flash_size", "16MB", "--flash_freq", "80m",
         "0x10000", "/tmp/liminal-clock.bin"],
        capture_output=True, text=True, timeout=900)
    dt = time.time() - t0
    out = (p.stdout + p.stderr).strip()
    ver = "hash verified" in out.lower()
    print(f"baud {baud:>7} (stub): {dt:6.1f}s  verified={ver}", flush=True)
    for line in out.splitlines()[-6:]: print("  ", line.strip()[:110], flush=True)
    dance_to_115200()
    print("  ...tab danced back to 115200", flush=True)

for b in [230400, 460800, 921600]:
    flashonce(b)
print("=== final: app back? ===", flush=True)
dance_to_115200()
print("app booted:", reset_to_app(), flush=True)
