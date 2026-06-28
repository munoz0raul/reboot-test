#!/usr/bin/env python3
"""
rc3 prebuilt no-OTA image — 30-reboot reliability test.

Ground truth = serial TTY.  SSH = commands only.

Key design:
  - BOOT_TIMEOUT = 600s (10 min): gives slow-recovery boots time to come back
    WITHOUT prompting for a power cycle.
  - Only prompts for user power cycle when device truly hasn't come back in 600s.
  - When it does ask, waits for user to TYPE "done" before continuing.
    This guarantees that "PCYCLE" results = user physically intervened.
  - Slow recoveries (came back on their own but took >SLOW_THRESHOLD) → "SLOW_OK"

Result categories:
  FAST_OK   — booted in <30s
  OK        — booted in 30–120s
  SLOW_OK   — booted in 120–600s without user touch
  PCYCLE    — BUG: device did not recover in 600s; user had to power cycle
"""

import os, sys, time, threading, subprocess, json, select
import serial as pyserial
from datetime import datetime

SERIAL_PORT     = '/dev/cu.usbserial-NNNUP457006P1'
BAUD            = 115200
DEVICE_IP       = '192.168.15.86'
PLAIN_REBOOTS   = 30
BOOT_TIMEOUT    = 600     # s — wait this long before declaring stuck
SLOW_THRESHOLD  = 120     # s — log as SLOW_OK if recovery takes longer than this
LOG_DIR         = '/tmp/stress-logs-noota'
RESUME_FROM     = 18      # set >1 to resume mid-run (skip already-completed reboots)
DIAG_DIR        = f'{LOG_DIR}/diag'

SSH_BASE = ['sshpass', '-p', 'oelinux123', 'ssh',
            '-o', 'StrictHostKeyChecking=no',
            '-o', 'ConnectTimeout=15',
            f'root@{DEVICE_IP}']

PURPLE = '\033[95m\033[1m'
GREEN  = '\033[92m\033[1m'
RED    = '\033[91m\033[1m'
YELLOW = '\033[93m'
CYAN   = '\033[96m'
BLUE   = '\033[94m\033[1m'
DIM    = '\033[2m'
RESET  = '\033[0m'

os.makedirs(DIAG_DIR, exist_ok=True)

_lock      = threading.Lock()
_buf       = bytearray()
_last_rx   = [time.time()]
_cur_log   = [None]
_ser       = [None]
_all_lines = []


def ts():
    return datetime.now().strftime('%H:%M:%S')

def log(msg, color=CYAN):
    print(f"{color}[{ts()}] {msg}{RESET}", flush=True)

def purple_alert(msg):
    print(f"\n{PURPLE}{'━'*64}", flush=True)
    for line in msg.splitlines():
        print(f"  {line}", flush=True)
    print(f"{'━'*64}{RESET}\n", flush=True)

def _reader():
    ser = _ser[0]
    while True:
        try:
            d = ser.read(512)
            if d:
                now = time.time()
                with _lock:
                    _last_rx[0] = now
                    _buf.extend(d)
                    if _cur_log[0]:
                        _cur_log[0].write(d)
                        _cur_log[0].flush()
                    for line in d.decode('utf-8', errors='replace').splitlines():
                        if line.strip():
                            _all_lines.append((now, line))
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

def silence():
    with _lock:
        return time.time() - _last_rx[0]

def set_log(path):
    with _lock:
        if _cur_log[0]:
            _cur_log[0].close()
        _cur_log[0] = open(path, 'wb')
    log(f"→ {path}", DIM)

def last_lines(n=30):
    with _lock:
        return list(_all_lines[-n:])

def save_stuck_info(label):
    lines = last_lines(30)
    path  = f"{DIAG_DIR}/STUCK-{label}-{datetime.now().strftime('%H%M%S')}.txt"
    with open(path, 'w') as f:
        f.write(f"STUCK: [{label}]\nLast serial lines before going silent:\n\n")
        for t, l in lines:
            f.write(f"  [{datetime.fromtimestamp(t).strftime('%H:%M:%S.%f')}] {l}\n")
    log(f"Stuck info → {path}", RED)
    return path


def wait_for_user_done(prompt_msg):
    """Print purple alert, then wait for serial activity (auto-detects power cycle)."""
    purple_alert(prompt_msg + "\n\nWaiting for serial activity after your power cycle ...")
    # Reset silence timer so we wait for NEW activity
    with _lock:
        _last_rx[0] = time.time()
    # Wait until serial data arrives (power cycle restarts XBL which outputs immediately)
    deadline = time.time() + 600
    while time.time() < deadline:
        if silence() < 5 and time.time() - (_last_rx[0] - silence()) > 2:
            # received something recently
            break
        with _lock:
            last = _last_rx[0]
        if time.time() - last < 30:
            break
        time.sleep(1)


def classify(elapsed):
    if elapsed < 30:
        return 'FAST_OK'
    elif elapsed < SLOW_THRESHOLD:
        return 'OK'
    else:
        return 'SLOW_OK'


def wait_for_login(label):
    """
    Wait for 'login:' on serial up to BOOT_TIMEOUT seconds.
    Returns (result_str, elapsed_seconds).

    result_str:
      'FAST_OK'  — booted < 30s
      'OK'       — booted 30–SLOW_THRESHOLD s
      'SLOW_OK'  — booted SLOW_THRESHOLD–BOOT_TIMEOUT s (NO user needed)
      'PCYCLE'   — did not boot within BOOT_TIMEOUT; user was prompted & confirmed power cycle
    """
    log(f"Serial: waiting for login  [{label}]  (timeout={BOOT_TIMEOUT}s)", CYAN)
    start     = time.time()
    slow_noted = False

    while True:
        if 'login:' in buf_text():
            elapsed = time.time() - start
            result  = classify(elapsed)
            log(f"LOGIN  [{label}]  {elapsed:.0f}s  → {result}", GREEN)
            return result, elapsed

        elapsed = time.time() - start
        sil     = silence()

        # Just log when going slow — no alert, no user interrupt
        if not slow_noted and sil > SLOW_THRESHOLD:
            slow_noted = True
            log(f"[{label}] No serial for {sil:.0f}s — waiting (may be slow recovery) ...", YELLOW)

        if elapsed >= BOOT_TIMEOUT:
            # Device truly didn't come back on its own
            stuck_path = save_stuck_info(label)
            wait_for_user_done(
                f"DEVICE DID NOT BOOT in {BOOT_TIMEOUT}s  [{label}]\n"
                f"Last serial lines saved to: {stuck_path}\n\n"
                f">>> POWER CYCLE the device (off → on) <<<"
            )
            # Wait for it to come back after user power cycle
            log(f"User power cycled — waiting for login after recovery ...", CYAN)
            clear_buf()
            pcycle_start = time.time()
            while time.time() - pcycle_start < 120:
                if 'login:' in buf_text():
                    log(f"LOGIN after user power cycle  [{label}]", GREEN)
                    return 'PCYCLE', elapsed
                time.sleep(0.5)
            log(f"Still no login 120s after power cycle — giving up [{label}]", RED)
            return 'PCYCLE', elapsed

        time.sleep(0.5)


def ssh(cmd, timeout=30):
    subprocess.run('ssh-keygen -R 192.168.15.86 2>/dev/null',
                   shell=True, capture_output=True)
    try:
        r = subprocess.run(SSH_BASE + [cmd],
                           capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip(), r.returncode
    except Exception as e:
        return f'ERR:{e}', 1


DIAG_CMD = r"""
echo '=== UPTIME ===' && uptime && date -u
echo '=== UNAME ===' && uname -a
echo '=== CMDLINE ===' && cat /proc/cmdline
echo '=== PSCI ===' && printf 'method: ' && cat /proc/device-tree/psci/method 2>/dev/null && echo '' && printf 'compatible: ' && cat /proc/device-tree/psci/compatible 2>/dev/null && echo '' || echo none
echo '=== WATCHDOGS ===' && ls /dev/watchdog* 2>/dev/null || echo none
echo '=== WATCHDOG ID ===' && cat /sys/class/watchdog/watchdog0/identity 2>/dev/null || echo none
echo '=== REBOOT REASON ===' && cat /sys/kernel/debug/qcom_scm 2>/dev/null || echo not-accessible
echo '=== DMESG RESET/WATCH/PSCI ===' && dmesg | grep -iE 'watchdog|psci|scm|reboot|reset|panic|oops' | grep -v 'spmi spmi-0: disallowed\|cpufreq\|PM_DT_PARSING' | tail -20
echo '=== DMESG ERRORS ===' && dmesg -l err,warn | grep -v 'spmi spmi-0: disallowed\|cpufreq\|PM_DT_PARSING' | tail -20
echo '=== PREV BOOT SHUTDOWN ===' && journalctl -b -1 --no-pager 2>/dev/null | grep -iE 'shutdown|systemd-shutdown|watchdog|reboot.*system|Reached target.*[Ss]hut' | tail -20 || echo no-prev-boot
"""

def collect_diagnostics(label):
    log(f"Collecting diagnostics [{label}] ...", DIM)
    out, _ = ssh(DIAG_CMD, timeout=50)
    path   = f"{DIAG_DIR}/{label}-{datetime.now().strftime('%H%M%S')}.txt"
    with open(path, 'w') as f:
        f.write(out)
    log(f"Saved: {path}", DIM)

def do_reboot(sysrq=False):
    if sysrq:
        log("sysrq-b: hard kernel reboot (no userspace shutdown) ...", YELLOW)
        try:
            ssh('echo b > /proc/sysrq-trigger', timeout=8)
        except Exception:
            pass
    else:
        log("reboot: normal systemd reboot ...", YELLOW)
        try:
            ssh('reboot', timeout=8)
        except Exception:
            pass


def run():
    log("=" * 64, PURPLE)
    log("  rc3 prebuilt no-OTA — 30-reboot reliability test", PURPLE)
    log(f"  {PLAIN_REBOOTS} reboots alternating  reboot / sysrq-b", PURPLE)
    log(f"  BOOT_TIMEOUT={BOOT_TIMEOUT}s  SLOW_THRESHOLD={SLOW_THRESHOLD}s", PURPLE)
    log(f"  PCYCLE = BUG: device required user intervention", PURPLE)
    log("=" * 64, PURPLE)

    subprocess.run('pkill -9 -f "serial_capture|stress_test|serial-qli|serial-rc3"',
                   shell=True, capture_output=True)
    time.sleep(1)

    _ser[0] = pyserial.Serial(SERIAL_PORT, BAUD, timeout=0.1)
    threading.Thread(target=_reader, daemon=True).start()

    results  = []
    boot_num = [RESUME_FROM - 1]  # offset so log files continue numbering

    def next_boot(label):
        boot_num[0] += 1
        set_log(f"{LOG_DIR}/boot{boot_num[0]:02d}-{label}.log")
        clear_buf()

    # ── wait for initial boot ────────────────────────────────────────────────
    next_boot("initial")
    # If device is already up (login prompt already came and went), SSH works immediately
    log("Checking if device is already up via SSH ...", YELLOW)
    out, rc = ssh('uname -a', timeout=10)
    if rc == 0:
        log(f"Device already up: {out[:80]}", GREEN)
    else:
        log("Device not yet reachable — waiting for login via serial ...", YELLOW)
        result, elapsed = wait_for_login("initial")
        time.sleep(6)
        out, _ = ssh('uname -a')
        log(f"Device up: {out[:80]}", GREEN)
    collect_diagnostics("boot00-baseline")

    # ── 30 plain reboots ─────────────────────────────────────────────────────
    log(f"\n{'─'*64}", CYAN)
    log(f"Starting {PLAIN_REBOOTS} reboots  (odd=reboot  even=sysrq-b)", YELLOW)
    log(f"{'─'*64}", CYAN)

    for i in range(RESUME_FROM, PLAIN_REBOOTS + 1):
        use_sysrq = (i % 2 == 0)
        rtype     = 'sysrq-b' if use_sysrq else 'reboot'
        label     = f"plain{i:02d}-{rtype}"
        log(f"\n--- Reboot {i:02d}/{PLAIN_REBOOTS}  [{rtype}] ---", BLUE)
        next_boot(label)
        do_reboot(sysrq=use_sysrq)
        time.sleep(3)

        result, elapsed = wait_for_login(label)
        time.sleep(6)
        collect_diagnostics(f"boot{boot_num[0]:02d}-{label}")

        color = GREEN if result != 'PCYCLE' else RED
        log(f"  [{label}]  {result}  ({elapsed:.0f}s)", color)
        results.append({'label': label, 'result': result,
                        'elapsed': round(elapsed, 1), 'type': rtype,
                        'boot': boot_num[0]})

    # ── summary ───────────────────────────────────────────────────────────────
    print(f"\n{PURPLE}{'═'*64}{RESET}", flush=True)
    log("TEST COMPLETE", PURPLE)
    print(f"{PURPLE}{'═'*64}{RESET}", flush=True)

    pcycles = 0
    for r in results:
        pcycles += int(r['result'] == 'PCYCLE')
        c    = RED if r['result'] == 'PCYCLE' else (YELLOW if r['result'] == 'SLOW_OK' else GREEN)
        print(f"{c}  boot{r['boot']:02d}  {r['result']:10s}  {r['elapsed']:5.0f}s  [{r['type']:7s}]  {r['label']}{RESET}",
              flush=True)

    print(flush=True)
    log(f"Reboots needing user power cycle (BUGS): {pcycles}/{len(results)}", RED if pcycles else GREEN)

    for rtype in ('reboot', 'sysrq-b'):
        s = [r for r in results if r['type'] == rtype]
        if s:
            p = sum(1 for r in s if r['result'] == 'PCYCLE')
            slow = sum(1 for r in s if r['result'] == 'SLOW_OK')
            log(f"  {rtype:8s}: {p}/{len(s)} PCYCLE  {slow}/{len(s)} SLOW_OK", RED if p else GREEN)

    with open(f"{LOG_DIR}/summary.json", 'w') as f:
        json.dump({'ts': datetime.now().isoformat(), 'results': results,
                   'pcycles': pcycles, 'total': len(results)}, f, indent=2)
    log(f"Logs   : {LOG_DIR}/", DIM)
    log(f"Diag   : {DIAG_DIR}/", DIM)
    log(f"Summary: {LOG_DIR}/summary.json", DIM)


if __name__ == '__main__':
    run()
