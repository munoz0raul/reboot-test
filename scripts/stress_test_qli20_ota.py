#!/usr/bin/env python3
"""
qli-2.0 OTA + boot reliability stress test.

Ground truth = serial TTY (P1 at 115200).
SSH is only used to send commands.

Test sequence:
  PHASE 1  (one time):  OTA v1 → v2, watch serial through reboot, verify v2
  PHASE 2  (N cycles):  plain reboot, watch serial, verify version unchanged

Purple alert when serial is SILENT >STUCK_SILENCE s OR
data is flowing but login never appears within BOOT_TIMEOUT s.

Serial logs per-phase in /tmp/stress-logs/
"""

import os, sys, time, threading, subprocess, serial as pyserial
from datetime import datetime

# ── config ────────────────────────────────────────────────────────────────
SERIAL_PORT   = '/dev/cu.usbserial-NNNUP457006P1'
BAUD          = 115200
DEVICE_IP     = '192.168.15.86'
PLAIN_REBOOTS = 8       # plain-reboot cycles after OTA
BOOT_TIMEOUT  = 150     # s: give up waiting for login
STUCK_SILENCE = 60      # s: serial silence → purple alert
LOG_DIR       = '/tmp/stress-logs'

SSH_BASE = ['sshpass', '-p', 'oelinux123', 'ssh',
            '-o', 'StrictHostKeyChecking=no',
            '-o', 'ConnectTimeout=15',
            f'root@{DEVICE_IP}']

# ── ANSI ──────────────────────────────────────────────────────────────────
PURPLE = '\033[95m\033[1m'
GREEN  = '\033[92m\033[1m'
RED    = '\033[91m\033[1m'
YELLOW = '\033[93m'
CYAN   = '\033[96m'
DIM    = '\033[2m'
RESET  = '\033[0m'

os.makedirs(LOG_DIR, exist_ok=True)

# ── shared serial state ───────────────────────────────────────────────────
_lock         = threading.Lock()
_buf          = bytearray()
_last_rx      = [time.time()]
_cur_log      = [None]      # open binary file
_ser          = [None]


def ts():
    return datetime.now().strftime('%H:%M:%S')

def log(msg, color=CYAN):
    print(f"{color}[{ts()}] {msg}{RESET}", flush=True)

def purple_alert(msg):
    print(f"\n{PURPLE}{'━'*60}", flush=True)
    print(f"  {msg}", flush=True)
    print(f"{'━'*60}{RESET}\n", flush=True)


# ── serial reader thread ──────────────────────────────────────────────────
def _reader():
    ser = _ser[0]
    while True:
        try:
            d = ser.read(512)
            if d:
                with _lock:
                    _last_rx[0] = time.time()
                    _buf.extend(d)
                    if _cur_log[0]:
                        _cur_log[0].write(d)
                        _cur_log[0].flush()
                sys.stdout.buffer.write(d)
                sys.stdout.flush()
        except Exception:
            time.sleep(0.1)


def buf_text():
    with _lock:
        return _buf.decode('utf-8', errors='replace')

def clear_buf():
    with _lock:
        _buf.clear()
        _last_rx[0] = time.time()

def serial_silence():
    with _lock:
        return time.time() - _last_rx[0]

def set_logfile(path):
    with _lock:
        if _cur_log[0]:
            _cur_log[0].close()
        f = open(path, 'wb')
        _cur_log[0] = f
    log(f"Serial log → {path}", DIM)


# ── wait_for_login ────────────────────────────────────────────────────────
def wait_for_login(label):
    """
    Wait for 'login:' on serial.
    - If silent > STUCK_SILENCE → purple alert (ask for power cycle), reset timer
    - If no login within BOOT_TIMEOUT → mark stuck, return False
    Returns True on success.
    """
    log(f"Watching serial for login prompt  [{label}]", CYAN)
    start      = time.time()
    alerted    = False

    while True:
        if 'login:' in buf_text():
            elapsed = time.time() - start
            log(f"LOGIN PROMPT  [{label}]  ({elapsed:.0f}s)", GREEN)
            return True

        elapsed = time.time() - start
        silence = serial_silence()

        # Purple alert: silent too long
        if silence > STUCK_SILENCE:
            alerted = True
            purple_alert(
                f"NO SERIAL OUTPUT for {silence:.0f}s  [{label}]\n"
                f"  >>> PLEASE POWER CYCLE THE DEVICE (off → on) <<<"
            )
            with _lock:
                _last_rx[0] = time.time()   # reset so we don't spam

        # Timeout: data may be flowing but login never came
        if elapsed > BOOT_TIMEOUT:
            if not alerted:
                purple_alert(
                    f"BOOT TIMEOUT {BOOT_TIMEOUT}s — login never appeared  [{label}]\n"
                    f"  >>> PLEASE POWER CYCLE THE DEVICE (off → on) <<<"
                )
            log(f"STUCK [{label}] — timeout {BOOT_TIMEOUT}s, login not seen", RED)
            return False

        time.sleep(0.5)


# ── SSH helper ────────────────────────────────────────────────────────────
def ssh(cmd, timeout=25):
    subprocess.run('ssh-keygen -R 192.168.15.86 2>/dev/null', shell=True)
    try:
        r = subprocess.run(SSH_BASE + [cmd],
                           capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip(), r.returncode
    except Exception as e:
        return f'ERR:{e}', 1

def get_version():
    out, rc = ssh('cat /etc/ota-demo-version')
    return out.strip() if rc == 0 else '?'

def trigger_ota():
    log("Triggering OTA update (curl POST) ...", YELLOW)
    try:
        ssh('curl -s -X POST http://localhost:8088/update', timeout=90)
    except Exception:
        pass   # connection closes when device reboots

def plain_reboot():
    log("Sending reboot ...", YELLOW)
    try:
        ssh('reboot', timeout=8)
    except Exception:
        pass


# ── main ─────────────────────────────────────────────────────────────────
def run():
    log("=" * 60, PURPLE)
    log("  qli-2.0 OTA + Boot Reliability Stress Test", PURPLE)
    log(f"  OTA phase x1, then {PLAIN_REBOOTS} plain-reboot cycles", PURPLE)
    log(f"  Serial ground truth: {SERIAL_PORT}", PURPLE)
    log(f"  Purple = STUCK — power cycle needed", PURPLE)
    log("=" * 60, PURPLE)
    print(flush=True)

    # kill anything holding the port
    subprocess.run('pkill -9 -f "serial_capture|stress_test|serial-qli20|serial-rc3"',
                   shell=True, capture_output=True)
    time.sleep(1)

    _ser[0] = pyserial.Serial(SERIAL_PORT, BAUD, timeout=0.1)
    threading.Thread(target=_reader, daemon=True).start()

    results = []

    # ── PHASE 1: OTA v1 → v2 ────────────────────────────────────────────
    log("PHASE 1: OTA v1 → v2", YELLOW)
    ver_before = get_version()
    log(f"Current version: {ver_before}", CYAN)

    set_logfile(f"{LOG_DIR}/phase0-v1boot-{datetime.now().strftime('%H%M%S')}.log")
    clear_buf()

    if ver_before != '1':
        purple_alert(
            f"Device is on version {ver_before} or unreachable.\n"
            f"Waiting for v1 to boot via serial..."
        )
        log("Waiting for v1 login prompt via serial ...", CYAN)
        ok = wait_for_login("v1-initial-boot")
        if not ok:
            clear_buf()
            wait_for_login("v1-initial-boot-after-powercycle")
        time.sleep(8)
        ver_before = get_version()
        log(f"Version after waiting: {ver_before}", GREEN if ver_before == '1' else RED)

    log(f"Starting OTA phase with version={ver_before}", CYAN)
    set_logfile(f"{LOG_DIR}/phase1-OTA-{datetime.now().strftime('%H%M%S')}.log")
    clear_buf()
    trigger_ota()
    time.sleep(3)

    ok = wait_for_login("OTA-reboot")
    if not ok:
        # User power-cycled — wait again
        clear_buf()
        ok = wait_for_login("OTA-reboot-after-powercycle")

    time.sleep(8)
    ver = get_version()
    log(f"Post-OTA version: {ver}", GREEN if ver == '2' else RED)
    results.append({'phase': 'OTA', 'stuck': not ok, 'ver': ver})

    if ver != '2':
        purple_alert(
            f"Expected version=2 after OTA, got {ver}\n"
            f"Something is wrong — check device manually."
        )

    # ── PHASE 2: plain reboot cycles ─────────────────────────────────────
    for i in range(1, PLAIN_REBOOTS + 1):
        log(f"\nPHASE 2 — plain reboot {i}/{PLAIN_REBOOTS}", YELLOW)
        set_logfile(f"{LOG_DIR}/phase2-reboot{i:02d}-{datetime.now().strftime('%H%M%S')}.log")
        clear_buf()
        plain_reboot()
        time.sleep(3)

        ok = wait_for_login(f"reboot-{i}")
        if not ok:
            clear_buf()
            ok = wait_for_login(f"reboot-{i}-after-powercycle")

        time.sleep(6)
        ver = get_version()
        log(f"Reboot {i} post-version: {ver}  ({'OK' if ok else 'NEEDED POWERCYCLE'})",
            GREEN if ok else RED)
        results.append({'phase': f'reboot-{i}', 'stuck': not ok, 'ver': ver})

    # ── summary ───────────────────────────────────────────────────────────
    print(f"\n{PURPLE}{'═'*60}{RESET}", flush=True)
    log("STRESS TEST COMPLETE", PURPLE)
    print(f"{PURPLE}{'═'*60}{RESET}", flush=True)
    stuck_total = 0
    for r in results:
        stuck = r['stuck']
        stuck_total += int(stuck)
        c = RED if stuck else GREEN
        flag = 'STUCK/POWERCYCLE' if stuck else 'auto-booted'
        print(f"{c}  {r['phase']:30s}  ver={r['ver']}  {flag}{RESET}", flush=True)
    print()
    log(f"Needed power cycle: {stuck_total}/{len(results)}", RED if stuck_total else GREEN)
    log(f"Serial logs: {LOG_DIR}/", DIM)


if __name__ == '__main__':
    run()
