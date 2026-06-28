#!/usr/bin/env python3
"""
rc3 OTA bidirectional + 30-reboot stress test.

No reflash. OTA direction switching via:
  v1→v2: POST /update  (ota-agent pulls from server)
  v2→v1: ostree admin deploy <v1-commit> + reboot

Serial TTY = ground truth. SSH = commands only.
Purple alert on stuck (silent >60s OR no login within 150s).

Sequence:
  Phase 1: 5 OTA cycles  (v1→v2→v1→v2→v1→v2 alternating)
  Phase 2: 30 plain reboots  (alternating reboot / sysrq-b)

Diagnostics collected every boot → /tmp/stress-logs-rc3/diag/
"""

import os, sys, time, threading, subprocess, json
import serial as pyserial
from datetime import datetime

SERIAL_PORT   = '/dev/cu.usbserial-NNNUP457006P1'
BAUD          = 115200
DEVICE_IP     = '192.168.15.86'
OTA_CYCLES    = 5     # v1→v2 counted as one cycle, v2→v1 as another
PLAIN_REBOOTS = 30
BOOT_TIMEOUT  = 180
STUCK_SILENCE = 60
LOG_DIR       = '/tmp/stress-logs-rc3'
DIAG_DIR      = f'{LOG_DIR}/diag'

SSH_BASE = ['sshpass', '-p', 'oelinux123', 'ssh',
            '-o', 'StrictHostKeyChecking=no',
            '-o', 'ConnectTimeout=15',
            f'root@{DEVICE_IP}']

PURPLE = '\033[95m\033[1m'
GREEN  = '\033[92m\033[1m'
RED    = '\033[91m\033[1m'
YELLOW = '\033[93m'
CYAN   = '\033[96m'
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

def last_lines(n=20):
    with _lock:
        return list(_all_lines[-n:])

def save_stuck_info(label):
    lines = last_lines(20)
    path  = f"{DIAG_DIR}/STUCK-{label}-{datetime.now().strftime('%H%M%S')}.txt"
    with open(path, 'w') as f:
        f.write(f"STUCK: [{label}]\nLast serial lines before silence:\n\n")
        for t, l in lines:
            f.write(f"  [{datetime.fromtimestamp(t).strftime('%H:%M:%S.%f')}] {l}\n")
    log(f"Stuck info → {path}", RED)

def wait_for_login(label):
    log(f"Serial: waiting for login  [{label}]", CYAN)
    start   = time.time()
    alerted = False
    while True:
        if 'login:' in buf_text():
            log(f"LOGIN  [{label}]  ({time.time()-start:.0f}s)", GREEN)
            return True
        elapsed = time.time() - start
        sil     = silence()
        if sil > STUCK_SILENCE:
            alerted = True
            save_stuck_info(label)
            purple_alert(
                f"NO SERIAL OUTPUT for {sil:.0f}s  [{label}]\n"
                f">>> PLEASE POWER CYCLE THE DEVICE (off → on) <<<"
            )
            with _lock:
                _last_rx[0] = time.time()
        if elapsed > BOOT_TIMEOUT:
            if not alerted:
                save_stuck_info(label)
                purple_alert(
                    f"BOOT TIMEOUT {BOOT_TIMEOUT}s — login never appeared  [{label}]\n"
                    f">>> PLEASE POWER CYCLE THE DEVICE (off → on) <<<"
                )
            log(f"STUCK [{label}]", RED)
            return False
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

def get_version():
    out, rc = ssh('cat /etc/ota-demo-version')
    return out.strip() if rc == 0 else '?'

def trigger_ota_v1_to_v2():
    log("OTA v1→v2: POST /update ...", YELLOW)
    try:
        ssh('curl -s -X POST http://localhost:8088/update', timeout=120)
    except Exception:
        pass

def deploy_v1_via_ostree():
    """Switch back to v1 by deploying the rollback commit, then reboot."""
    log("OTA v2→v1: deploying rollback commit via ostree ...", YELLOW)
    # find the non-active deployment (v1)
    out, _ = ssh(
        "ostree admin status | grep -v '^\*' | grep 'nodistro' | "
        "awk '{print $2}' | head -1 | cut -d. -f1"
    )
    commit = out.strip()
    if not commit or len(commit) < 10:
        log(f"Could not find v1 rollback commit: {commit!r}", RED)
        return False
    log(f"Deploying v1 commit {commit[:16]}...", DIM)
    try:
        ssh(f"ostree admin deploy nodistro:{commit} && sleep 1 && reboot", timeout=20)
    except Exception:
        pass
    return True

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

DIAG_CMD = r"""
echo '=== VERSION ===' && cat /etc/ota-demo-version
echo '=== UPTIME ===' && uptime && date -u
echo '=== CMDLINE ===' && cat /proc/cmdline
echo '=== OSTREE ===' && ostree admin status
echo '=== PSCI ===' && printf 'method: ' && cat /proc/device-tree/psci/method 2>/dev/null && echo '' && printf 'compatible: ' && cat /proc/device-tree/psci/compatible 2>/dev/null && echo '' || echo none
echo '=== WATCHDOGS ===' && ls /dev/watchdog* 2>/dev/null || echo none
echo '=== WATCHDOG ID ===' && cat /sys/class/watchdog/watchdog0/identity 2>/dev/null || echo none
echo '=== REBOOT REASON ===' && cat /sys/kernel/debug/qcom_scm 2>/dev/null || echo not-accessible
echo '=== REBOOT PARAM ===' && cat /sys/module/kernel/parameters/reboot 2>/dev/null || echo not-set
echo '=== DMESG RESET/WATCH/PSCI ===' && dmesg | grep -iE 'watchdog|psci|scm|reboot|reset|panic|oops' | grep -v 'spmi spmi-0: disallowed\|cpufreq\|PM_DT_PARSING' | tail -20
echo '=== DMESG ERRORS ===' && dmesg -l err,warn | grep -v 'spmi spmi-0: disallowed\|cpufreq\|PM_DT_PARSING' | tail -20
echo '=== PREV BOOT ERRORS ===' && journalctl -b -1 --no-pager -p err 2>/dev/null | tail -20 || echo no-prev-boot
echo '=== PREV BOOT SHUTDOWN ===' && journalctl -b -1 --no-pager 2>/dev/null | grep -iE 'shutdown|systemd-shutdown|watchdog|reboot.*system|Reached target.*[Ss]hut' | tail -20 || echo no-prev-boot
echo '=== SYSRQ ===' && cat /proc/sys/kernel/sysrq
"""

def collect_diagnostics(label):
    log(f"Collecting diagnostics [{label}] ...", DIM)
    out, _ = ssh(DIAG_CMD, timeout=50)
    path   = f"{DIAG_DIR}/{label}-{datetime.now().strftime('%H%M%S')}.txt"
    with open(path, 'w') as f:
        f.write(out)
    log(f"Saved: {path}", DIM)
    return out


def run():
    log("=" * 64, PURPLE)
    log("  rc3 OTA Bidirectional + 30-Reboot Stress Test", PURPLE)
    log(f"  Phase 1: {OTA_CYCLES} OTA cycles (v1↔v2 alternating)", PURPLE)
    log(f"  Phase 2: {PLAIN_REBOOTS} plain reboots (reboot / sysrq-b alternating)", PURPLE)
    log(f"  Serial ground truth  |  diagnostics every boot", PURPLE)
    log("=" * 64, PURPLE)

    subprocess.run('pkill -9 -f "serial_capture|stress_test|serial-qli|serial-rc3"',
                   shell=True, capture_output=True)
    time.sleep(1)

    _ser[0] = pyserial.Serial(SERIAL_PORT, BAUD, timeout=0.1)
    threading.Thread(target=_reader, daemon=True).start()

    results  = []
    boot_num = [0]

    def next_boot(label):
        boot_num[0] += 1
        set_log(f"{LOG_DIR}/boot{boot_num[0]:02d}-{label}.log")
        clear_buf()

    def after_reboot(label, rtype):
        ok = wait_for_login(label)
        if not ok:
            clear_buf()
            ok = wait_for_login(f"{label}-pcycle")
        time.sleep(8)
        ver = get_version()
        collect_diagnostics(f"boot{boot_num[0]:02d}-{label}")
        status = 'OK' if ok else 'STUCK'
        log(f"  [{label}]  ver={ver}  {status}", GREEN if ok else RED)
        results.append({'label': label, 'ok': ok, 'ver': ver, 'type': rtype,
                        'boot': boot_num[0]})
        return ok, ver

    # ── wait for initial boot ────────────────────────────────────────────
    next_boot("initial")
    ver = get_version()
    if ver not in ('1', '2'):
        log("Waiting for device via serial ...", YELLOW)
        wait_for_login("initial")
        time.sleep(8)
        ver = get_version()
    log(f"Starting on version={ver}", CYAN)
    collect_diagnostics(f"boot{boot_num[0]:02d}-baseline")

    # ── Phase 1: OTA cycles v1↔v2 ────────────────────────────────────────
    log(f"\n{'─'*64}", CYAN)
    log(f"PHASE 1: {OTA_CYCLES} OTA cycles (v1↔v2)", YELLOW)
    log(f"{'─'*64}", CYAN)

    current_ver = ver
    for cycle in range(1, OTA_CYCLES + 1):
        if current_ver == '1':
            label = f"OTA{cycle:02d}-v1tov2"
            next_boot(label)
            trigger_ota_v1_to_v2()
            time.sleep(3)
            ok, current_ver = after_reboot(label, 'ota-v1→v2')
        else:
            label = f"OTA{cycle:02d}-v2tov1"
            next_boot(label)
            ok = deploy_v1_via_ostree()
            time.sleep(3)
            ok2, current_ver = after_reboot(label, 'ota-v2→v1')

    # ── Phase 2: 30 plain reboots ─────────────────────────────────────────
    log(f"\n{'─'*64}", CYAN)
    log(f"PHASE 2: {PLAIN_REBOOTS} plain reboots", YELLOW)
    log(f"{'─'*64}", CYAN)

    for i in range(1, PLAIN_REBOOTS + 1):
        use_sysrq = (i % 2 == 0)
        rtype     = 'sysrq-b' if use_sysrq else 'reboot'
        label     = f"plain{i:02d}-{rtype}"
        next_boot(label)
        do_reboot(sysrq=use_sysrq)
        time.sleep(3)
        after_reboot(label, rtype)

    # ── summary ───────────────────────────────────────────────────────────
    print(f"\n{PURPLE}{'═'*64}{RESET}", flush=True)
    log("STRESS TEST COMPLETE", PURPLE)
    print(f"{PURPLE}{'═'*64}{RESET}", flush=True)
    stuck = 0
    for r in results:
        ok    = r['ok']
        stuck += int(not ok)
        c     = GREEN if ok else RED
        flag  = 'OK   ' if ok else 'STUCK'
        print(f"{c}  boot{r['boot']:02d}  {flag}  [{r['type']:12s}]  {r['label']:40s}  ver={r['ver']}{RESET}",
              flush=True)
    print(flush=True)
    log(f"Total stuck: {stuck}/{len(results)}", RED if stuck else GREEN)

    for rtype in ('reboot', 'sysrq-b', 'ota-v1→v2', 'ota-v2→v1'):
        s = [r for r in results if r['type'] == rtype]
        if s:
            n = sum(1 for r in s if not r['ok'])
            log(f"  {rtype:14s}: {n}/{len(s)} stuck", RED if n else GREEN)

    with open(f"{LOG_DIR}/summary.json", 'w') as f:
        json.dump({'ts': datetime.now().isoformat(), 'results': results,
                   'stuck': stuck, 'total': len(results)}, f, indent=2)
    log(f"Logs      : {LOG_DIR}/", DIM)
    log(f"Diag      : {DIAG_DIR}/", DIM)
    log(f"Summary   : {LOG_DIR}/summary.json", DIM)


if __name__ == '__main__':
    run()
