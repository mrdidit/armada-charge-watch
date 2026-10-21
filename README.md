# Armada Charge Watch

Read-only charging diagnostics for Armada Linux. Records battery health, charge thresholds, voltage, current and charge counters before and after a charging limit is reached. Python 3 standard library only; no installation, root access or image rebuild is needed for capture.

## Why this exists

Development of a separate **Armada Charging Modes** implementation for Normal, Bypass and percentage-limit policies was halted after a delayed `Over voltage` battery-health flag appeared during native charge-limit testing. Charging could stop normally at the selected percentage, then show the fault several minutes later.

The same symptom reproduced with Armada's native Steam charge-limit integration, with the separate Charging Modes service and kernel backend absent. This tool preserves comparable evidence across implementations, KPFE units and other device models: actual start/stop thresholds, the charging-stop transition, subsequent health changes and the reported voltage ceiling. A successful stop alone does not cover the following hold period.

The watcher only observes the selected charging configuration. It does not implement bypass, set limits, reset the gauge or clear faults.

## KPFE findings

Investigation snapshot: **21 October 2026**, KONKR Pocket FIT Elite, Snapdragon 8 Elite / SM8750.

### Delayed charge-limit fault

| Test | Actual start/stop thresholds | Result after charging stopped |
| --- | --- | --- |
| Earlier native-limit test during Charging Modes development, policy service stopped | 60% / 70% | About 4m 45s later: `Over voltage`; reported `voltage_max` fell from 4.380 V to 4.035 V. |
| Native Steam limit, Armada `20261021.659cfa4`, Linux `7.2.6`; separate Charging Modes implementation absent | 52% / 55% | 7m 12s later: `Over voltage`; reported `voltage_max` fell from 4.380 V to 3.929 V. |
| Native Steam limit, same image after reboot | 52% / 70% | 5m 24s later: `Over voltage`; reported `voltage_max` fell from 4.380 V to 4.068 V. |

The 55% test retained 1,090 samples at one-second intervals with no sample gaps. Status changed from `Charging` to `Full` with USB power still online. At the fault, reported instantaneous voltage was **3.924130 V**, below the contemporaneous reported ceiling; voltage, current and temperature were unchanged from the preceding healthy sample. Unplugging restored the 4.380 V ceiling, while the health flag remained until reboot.

The separate 70% test retained **1,275 samples**, also without gaps. Status changed to `Full` at **10:28:37 Dublin**, followed by the fault at **10:34:01**. The actual **52/70** thresholds stayed unchanged and USB remained online. Instantaneous voltage was **4.063290 V**, again below the new reported ceiling and unchanged from the preceding healthy sample. After disconnection, the ceiling returned to 4.380 V while `Over voltage` remained set.

The newer test image includes [Armada PR #758](https://github.com/armada-os/armada/pull/758), which integrates native charge limits with Steam. The repeated symptom does not establish that PR as the cause: the earlier test predates it, and the image base and threshold pair differ. These readings establish a firmware-reported health fault, not physical overcharging or its cause.

### Battery capacity and reporting

| Item | Finding |
| --- | --- |
| Confirmed battery in the tested KPFE | Two **4,100 mAh cells in series (2S)**: **4,100 mAh pack capacity** at twice the cell voltage, equivalent in energy to **8,200 mAh at single-cell voltage**. |
| Other shipped battery variant | Two **4,200 mAh cells in series**: 4,200 mAh pack capacity, equivalent to 8,400 mAh at single-cell voltage. This variant has not been tested here. |
| Firmware model name | `BELOVED_8200MAH_37W_CXD2273_KR02` |
| Reported design capacity | `charge_full_design=8400000` µAh: **8,400 mAh**, despite the confirmed 4,100 mAh cells and the `8200MAH` name. |
| Reported full-capacity estimate | **9,272 mAh** in the earlier snapshot; **9,261 mAh** in the 21 October capture. These are gauge/firmware estimates, not established physical capacity. |
| Voltage and current | Voltage is exposed on a single-cell scale. Actual pack-current versus normalized-current scaling remains unresolved. |

The audited `qcom_battmgr` driver forwards the design/full-capacity values from Qualcomm firmware; no hardcoded 8,400 value or driver-side 2S conversion was found. The installed ADSP binary contains an 8,400 capacity-like field beside the exact battery model and separate likely charging-policy tables containing 8,400 and 4,380 values. This supports a possible profile mismatch, but active table selection and effective charging parameters remain unverified.

The design discrepancy is approximately **2.44%**. It does not by itself explain the earlier 9,272 mAh estimate, which is **10.38% above the configured design** and **13.07% above the 8,200 mAh energy-equivalent capacity**. Whether that estimate was learned, initialized or restored remains unresolved. A causal connection between the profile discrepancy and the `Over voltage` flag is unproven.

The earlier `power_now=48942505` reading also implies 48.94 W in standard sysfs units, while reported voltage × current gives about 1.99 W. Doubling voltage would give about 3.98 W only if current is actual series-pack current. The tool retains these raw fields without assuming a conversion or treating `power_now` as verified instantaneous consumption.

## Run a capture

1. Download and extract [armada-charge-watch-v0.1.1.zip](https://github.com/mrdidit/armada-charge-watch/releases/download/v0.1.1/armada-charge-watch-v0.1.1.zip).
2. Open a terminal in the extracted folder and inspect the selected battery:

   ```sh
   python3 charge_watch.py inspect
   ```

3. Confirm the expected device and battery, health `Good`, a readable positive `voltage_max`, and the configured thresholds. Start below the intended limit with the charger disconnected, then run:

   ```sh
   python3 charge_watch.py capture --duration 3600 --output captures/test-01
   ```

4. Connect the charger and keep the device awake and attended. Allow the capture to cover charging and the period after the limit is reached; the observed faults occurred several minutes after stopping. Keep the configured thresholds unchanged during the comparison. **If an alert appears, unplug the charger and retain the capture.** `Ctrl+C` ends sampling while preserving completed records.
5. Generate the summary:

   ```sh
   python3 charge_watch.py summarize captures/test-01 > captures/test-01/summary.json
   ```

Use a new output directory for each run. Capture never changes charging state, including when sampling ends.

### Device requirements and limits

- Linux and Python 3.8 or newer. No `pip` packages are required.
- A unique system battery is selected automatically. Batteries with `scope=Device` are excluded. If several candidates exist, add `--battery NAME` to `inspect` and `capture`. Check the selection manually when the driver omits `scope`.
- Guarded capture requires readable **`health` and `voltage_max`**. Missing/unknown health or an unavailable/invalid voltage ceiling stops capture with exit code 3; this means monitoring is unsupported, not that a battery fault was detected. Other missing fields are recorded. Missing threshold or external-power information limits the stop analysis.
- Unhealthy status or a decrease in reported `voltage_max` stops capture with exit code 2 and a manual-unplug message. A ceiling decrease alone is a conservative stop condition, not proof of physical overvoltage; another device's normal policy may change that ceiling.
- Sampling defaults to once per second, progress every 30 seconds and a 30-minute duration. Maximum duration is one hour, with at most 3,601 samples and 16 MiB of sample data. Suspend counts against the deadline; the tool does not prevent sleep. A stalled kernel property read can delay observation.
- Files retain raw sysfs values and errors. Reads are sequential, not an atomic hardware snapshot. Zero exit status means capture completed without a detected alert; it does not establish a charging fix, battery isolation or long-term stability. Check `finished.json` for the actual stop reason and duration.

## Share results

The output contains `metadata.json`, `samples.jsonl` and `finished.json`; the command above adds `summary.json`. Share the capture with the device model, image/kernel version, selected limit, charger details and observed behaviour. Keep runs from different images or configurations separate.

Optional kernel diagnostics, limited to the capture's boot and time window:

```sh
python3 charge_watch.py journal captures/test-01
```

This uses the current account's journal access, with a 15-second query timeout and 2 MiB output limit. Permission failures are recorded. An optional `--firmware /path/to/file` on `inspect` or `capture` records that file's SHA-256, not its contents.

Nothing is uploaded automatically. Default metadata omits hostname and battery serial numbers, but includes a boot UUID, timestamps, hardware model and software versions. Optional kernel logs can contain identifying information, and an optional firmware hash includes the supplied file path. Review diagnostic files before publishing them. Hardware logs and firmware binaries are not included in this repository.

## Validation

Thirty offline tests cover battery selection, missing properties, delayed faults, stop/hold analysis, clock discontinuities, read-only collection and output bounds. The portable summarizer also reproduced the archived KPFE 55% and 70% capture results. The original collector captured the hardware observations above; live capture with this portable release and hardware testing on other models remain outstanding.

```sh
python3 -m unittest -v test_charge_watch.py
```

Maintainer: **mrdidit**. MIT license. AI-assisted development with OpenAI Codex.
