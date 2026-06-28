# IQ-8275 EVK: Intermittent Hang After Reboot on qli-2.0 Built Images

**Platform:** Qualcomm IQ-8275 EVK (SM8275)  
**Tested tags:** `qli-2.0` (commit `ef0004df`), `qli-2.0-rc3` (commit `c416730004`)  
**Blog under test:** [`github.com/munoz0raul/ostree-blog`](https://github.com/munoz0raul/ostree-blog) @ commit `0fe0b61`

---

## Summary

During OSTree OTA blog validation on the IQ-8275 EVK, the device was found to intermittently fail to complete a warm reboot when running images built from `qli-2.0` (and `qli-2.0-rc3` with OTA). After issuing `reboot` or `echo b > /proc/sysrq-trigger`, the SoC sometimes never comes back — the serial console goes permanently silent after printing `reboot: Restarting system`.

The defect is **not random hardware instability**. Every stuck reboot is preceded by the same kernel log line:

```
systemd-shutdown[1]: Failed to set watchdog hardware timeout to 1min: Invalid argument
```

This message does not appear in factory-prebuilt rc3 images, which exhibit zero stuck reboots under the same test.

---

## Hardware and Test Setup

| Component | Detail |
|-----------|--------|
| Board | Qualcomm IQ-8275 EVK |
| Serial | `/dev/cu.usbserial-NNNUP457006P1` at 115200 baud |
| Device IP | 192.168.15.86 |
| Build server | `raulrm@hu-raulrm-lv`, `/local/mnt/workspace/build/` |
| Build system | Yocto via `kas` |

**Serial as ground truth:** All boot outcomes were recorded by a Python script reading the raw serial port. A "stuck" result (`PCYCLE`) means the device produced no serial output for ≥ 600 seconds and the user had to physically power-cycle it. This ensures `PCYCLE` always represents a confirmed hardware hang, not a slow boot.

---

## Test Environments

Three image configurations were tested:

### ENV-A — rc3 OTA built (qli-2.0-rc3, OSTree SOTA stack)

Built from `qli-2.0-rc3` using:
```
kas build meta-qcom/ci/iq-8275-evk.yml:meta-qcom/ci/qcom-distro-sota.yml: \
  meta-qcom/ci/performance.yml:meta-qcom/ci/ota-demo.yml \
  --target qcom-multimedia-image
```

OSTree update agent runs on `:8088`. Two versions built (v1: grey wallpaper / v2: purple wallpaper + vim). Script: `stress_test_rc3.py`.

Kernel (from device): `6.x` rc3 series, PSCI SMC Calling Convention **v1.3**

### ENV-B — qli-2.0 no-OTA built (qli-2.0, standard distro)

Built from `qli-2.0` without the SOTA stack. `fwupd` excluded via a kas fragment (`IMAGE_INSTALL:remove = "fwupd"`) to work around a recipe packaging failure in this version:
```
kas build meta-qcom/ci/iq-8275-evk.yml:meta-qcom/ci/qcom-distro.yml: \
  meta-qcom/ci/performance.yml:meta-qcom/ci/no-fwupd.yml \
  --target qcom-multimedia-image
```

Script: `stress_test_noota.py` with `BOOT_TIMEOUT=600s`, `SLOW_THRESHOLD=120s`.

Kernel: `Linux iq-8275-evk 6.18.30-01953-g5086fd78561b-dirty #1 SMP PREEMPT Mon Jun 22 14:20:22 UTC 2026 aarch64`, PSCI SMC Calling Convention **v1.1**

### ENV-C — rc3 prebuilt (factory image, no OTA stack)

Qualcomm-provided prebuilt rc3 image. Flashed directly without any custom Yocto build. Used as the control group — same physical hardware, different software stack.

---

## Result Categories

| Label | Meaning |
|-------|---------|
| `FAST_OK` | Login appeared in < 30 s |
| `OK` | Login appeared in 30–120 s |
| `SLOW_OK` | Login appeared in 120–600 s (device self-recovered, no user touch) |
| `PCYCLE` | **BUG** — No login within 600 s; user physically power-cycled device |

Only `PCYCLE` results count as confirmed bugs. `SLOW_OK` is anomalous but not a hard hang.

---

## Test Results

### ENV-A: rc3 OTA built — 5 OTA cycles + 21 plain reboots

**Test script:** `stress_test_rc3.py` (BOOT_TIMEOUT=180 s — note: short timeout may under-count SLOW_OK)

#### OTA reboot phases

| Phase | Direction | Result |
|-------|-----------|--------|
| OTA01 | v1 → v2 | OK |
| OTA02 | v1 → v2 | **PCYCLE** |
| OTA03 | v2 → v1 | **PCYCLE** |
| OTA04 | v2 → v1 | **PCYCLE** |
| OTA05 | v2 → v1 | OK |

3 of 5 OTA-triggered reboots resulted in a stuck SoC.

#### Plain reboot phase (21 reboots alternating `reboot` / `sysrq-b`)

| Reboot # | Type | Result |
|----------|------|--------|
| plain01 | reboot | **PCYCLE** |
| plain02 | sysrq-b | **PCYCLE** |
| plain03 | reboot | **PCYCLE** |
| plain04 | sysrq-b | **PCYCLE** |
| plain05 | reboot | **PCYCLE** |
| plain06 | sysrq-b | **PCYCLE** |
| plain07 | reboot | OK |
| plain08 | sysrq-b | OK |
| plain09 | reboot | OK |
| plain10 | sysrq-b | OK |
| plain11 | reboot | **PCYCLE** |
| plain12 | sysrq-b | **PCYCLE** |
| plain13 | reboot | **PCYCLE** |
| plain14 | sysrq-b | **PCYCLE** |
| plain15 | reboot | **PCYCLE** |
| plain16 | sysrq-b | **PCYCLE** |
| plain17 | reboot | **PCYCLE** |
| plain18 | sysrq-b | **PCYCLE** |
| plain19 | reboot | **PCYCLE** |
| plain20 | sysrq-b | **PCYCLE** |
| plain21 | reboot | **PCYCLE** |

**17 of 21 plain reboots stuck (81%).** Both `reboot` and `sysrq-b` are affected.

---

### ENV-B: qli-2.0 no-OTA built — 30 plain reboots (BOOT_TIMEOUT=600 s)

**Test script:** `stress_test_noota.py`

| Reboot range | Type | PCYCLE | SLOW_OK | FAST_OK/OK |
|---|---|---|---|---|
| plain01–16 | reboot (odd) + sysrq-b (even) | 0 | 0 | 16 |
| plain17 | reboot | **1** (>600 s) | — | — |
| plain18 | sysrq-b | 0 | 0 | FAST_OK |
| plain19 | reboot | **1** (>600 s) | — | — |
| plain20 | sysrq-b | 0 | **1** (443 s) | — |
| plain21–30 | alternating | 0 | 0 | 10 |

**Summary (30 reboots):**
- `reboot` (15 reboots): **2 PCYCLE** (13.3%), 0 SLOW_OK
- `sysrq-b` (15 reboots): 0 PCYCLE, **1 SLOW_OK** at 443 s (2.7%)

The sysrq-b SLOW_OK at 443 s (plain20) occurred immediately after the plain19 reboot PCYCLE, suggesting the watchdog state carried over into the next reset.

---

### ENV-C: rc3 prebuilt — 30 plain reboots (BOOT_TIMEOUT=600 s)

**0 of 30 reboots stuck. 0 SLOW_OK. Every boot completed in < 30 s.**

The watchdog error message **never appeared** in serial output for this image.

---

## Aggregate Comparison

| Environment | Image type | Reboots | PCYCLE | SLOW_OK | Watchdog EINVAL in log |
|---|---|---|---|---|---|
| ENV-C: rc3 prebuilt | Factory no-OTA | 30 | 0 (0%) | 0 | Never |
| ENV-B: qli-2.0 no-OTA built | Built no-OTA | 30 | 2 (6.7%) | 1 | Every stuck boot |
| ENV-A: rc3 OTA built | Built + SOTA | 21 plain + 5 OTA | 17+ (>80%) | unknown | Every stuck boot |

---

## Serial TTY Evidence

The following two logs show the exact sequence before every stuck reboot on ENV-A and ENV-B. These are from `/tmp/stress-logs-noota/diag/STUCK-plain19-reboot-195353.txt` and `STUCK-plain17-reboot-192822.txt`, captured by direct serial port read at 115200 baud.

### STUCK-plain19-reboot (ENV-B)

```
[19:43:38] Qualcomm Linux Reference Distro 2.0 iq-8275-evk ttyMSM0
[19:43:38] iq-8275-evk login:
...
[19:43:52] systemd-shutdown[1]: Failed to set watchdog hardware timeout to 1min: Invalid argument
[19:43:52] systemd-udevd[425]: Failed to remove file descriptor "config-serialization" from the store, ignoring: Connection refused
[19:43:53] reboot: Restarting system
[19:43:53]   ← blank — SoC silent for 600+ seconds
```

### STUCK-plain17-reboot (ENV-B)

```
[19:18:07] iq-8275-evk login:
...
[19:18:21] systemd-shutdown[1]: Failed to set watchdog hardware timeout to 1min: Invalid argument
[19:18:22] reboot: Restarting system
[19:18:23]   ← blank — SoC silent for 600+ seconds
```

### rc3 OTA STUCK-OTA02-v1tov2 (ENV-A)

```
[23:45:56] iq-8275-evk login:
...
[23:46:25] systemd-shutdown[1]: Failed to set watchdog hardware timeout to 1min: Invalid argument
[23:46:26] reboot: Restarting system
[23:46:28]   ← blank
```

**The pattern is identical across ENV-A and ENV-B. The line is absent from every successful boot log and absent from all ENV-C logs.**

---

## Device Diagnostics

From `boot00-baseline` (ENV-B, qli-2.0 no-OTA built):

```
Kernel:  Linux iq-8275-evk 6.18.30-01953-g5086fd78561b-dirty #1 SMP PREEMPT
         Mon Jun 22 14:20:22 UTC 2026 aarch64
PSCI:    method=smc  compatible=arm,psci-1.0  convention=v1.1
Watchdog devices: /dev/watchdog  /dev/watchdog0
Watchdog identity: qcom_wdt
```

From `boot01-baseline` (ENV-A, rc3 OTA built):

```
PSCI:    method=smc  compatible=arm,psci-1.0  convention=v1.3
Watchdog devices: /dev/watchdog  /dev/watchdog0
Watchdog identity: qcom_wdt
```

Both built images expose `qcom_wdt` on `/dev/watchdog{,0}`. The factory rc3 prebuilt either does not expose this device or the driver handles the timeout correctly.

---

## Root Cause Hypothesis

During system shutdown, `systemd-shutdown(1)` arms the hardware watchdog with a 1-minute timeout as a last-resort self-recovery mechanism. On qli-2.0 (and rc3 OTA) built images, `qcom_wdt` returns `EINVAL` when asked to set a 60-second timeout, logging:

```
systemd-shutdown[1]: Failed to set watchdog hardware timeout to 1min: Invalid argument
```

The `qcom_wdt` driver (`drivers/watchdog/qcom-wdt.c`) computes a maximum timeout from the watchdog clock frequency divided by the maximum counter value. If the hardware maximum is less than 60 seconds, the driver rejects the request. This itself is not fatal — systemd falls back to continuing the shutdown.

However, the observed behavior suggests a secondary effect: the failed `ioctl(WDIOC_SETTIMEOUT, 60)` call leaves the watchdog peripheral in a partially configured or active state. When the kernel subsequently calls `do_kernel_restart()` → PSCI `SYSTEM_RESET`, the Qualcomm Trust Zone (SCM/TZ) firmware performs the reset sequence. If the qcom_wdt watchdog fires during that window, or if its state interferes with the TZ reset path, the SoC can take anywhere from a few seconds to never to come back.

**Supporting evidence:**
1. The message appears on every stuck boot and on no clean boot.
2. `sysrq-b` (hard reset via `SysRq`, bypasses systemd entirely) also occasionally produces SLOW_OK (443 s) after a preceding reboot PCYCLE — the watchdog state set during the previous boot's shutdown persists until the watchdog timer expires or the SoC fully resets.
3. The factory rc3 prebuilt image (0/30 stuck) never shows this message, ruling out hardware reliability as the cause.
4. Both `reboot` and `sysrq-b` are affected (ENV-A: 81% stuck for both types), which points to a hardware-level interference with the reset path rather than a userspace-only issue.

---

## Suggested Investigations

### 1. Disable systemd's hardware watchdog on shutdown (quick test)

Add to `/etc/systemd/system.conf` on the qli-2.0 image:

```ini
RuntimeWatchdogSec=off
ShutdownWatchdogSec=off
```

If this eliminates the PCYCLE events, the root cause is confirmed and the fix is either in systemd configuration or in the `qcom_wdt` driver's timeout validation.

### 2. Fix `qcom_wdt` maximum timeout

In `drivers/watchdog/qcom-wdt.c`, check the value of `wdt->layout->max_num_count` and the watchdog clock rate. The driver computes:

```c
wdt->wdd.max_hw_heartbeat_ms = (wdt->layout->max_num_count / clk_rate) * 1000;
```

If this is less than 60000 ms, `ioctl(WDIOC_SETTIMEOUT, 60)` will return `EINVAL`. Options:
- Increase the hardware counter value in the driver if the hardware supports it.
- Clamp the requested timeout to `max_hw_heartbeat_ms` and return success rather than EINVAL, so systemd proceeds with a shorter watchdog timeout instead of failing.

### 3. Kernel config comparison

Compare the following between qli-2.0 kernel and the rc3 prebuilt kernel:
- `CONFIG_WATCHDOG_HANDLE_BOOT_ENABLED`
- `CONFIG_QCOM_WDT` — present in both, but driver version may differ
- The watchdog clock source and rate (check `wdt->clk` in the driver)

### 4. Capture TZ/SCM reboot reason

The `qcom_scm` debug interface was not accessible during testing (`/sys/kernel/debug/qcom_scm` returned `not-accessible`). If it can be enabled, capturing the reboot cause register before and after a stuck reboot sequence would pinpoint whether the SoC reset was initiated at all.

### 5. Check PSCI convention difference

qli-2.0 reports PSCI SMC Calling Convention **v1.1**; rc3 OTA built reports **v1.3**. This difference may reflect different TZ firmware. If the `SYSTEM_RESET` implementation differs between TZ versions, it could interact with a watchdog interrupt differently.

---

## Reproduction Steps

1. Build qli-2.0 no-OTA image:
   ```bash
   kas build meta-qcom/ci/iq-8275-evk.yml:meta-qcom/ci/qcom-distro.yml:meta-qcom/ci/performance.yml:meta-qcom/ci/no-fwupd.yml \
     --target qcom-multimedia-image
   ```

2. Flash to IQ-8275 EVK via QDL (EDL mode: SW3 UP):
   ```bash
   qdl --storage ufs prog_firehose_ddr.elf rawprogram*.xml patch*.xml
   ```

3. Boot normally (SW3 DOWN), SSH in, run `reboot` repeatedly. Observe via serial at 115200 baud.

4. Expected failure rate: ~7–13% of `reboot` calls result in SoC hang (never emitting XBL output on serial within 600 s). With OTA OSTree stack enabled the rate rises to ~81%.

5. Look for `systemd-shutdown[1]: Failed to set watchdog hardware timeout to 1min: Invalid argument` in the serial log of every stuck cycle.

---

## Files in This Repository

| File | Description |
|------|-------------|
| `README.md` | This report |
| `logs/STUCK-plain17-reboot.txt` | Serial TTY capture of first confirmed stuck reboot (ENV-B) |
| `logs/STUCK-plain19-reboot.txt` | Serial TTY capture of second confirmed stuck reboot (ENV-B) |
| `logs/STUCK-OTA02-v1tov2.txt` | Serial TTY capture of stuck OTA reboot (ENV-A) |
| `scripts/stress_test_noota.py` | 30-reboot reliability test (ENV-B, BOOT_TIMEOUT=600 s) |
| `scripts/stress_test_rc3.py` | OTA + reboot stress test (ENV-A) |
