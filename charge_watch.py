#!/usr/bin/env python3
"""Bounded, read-only Linux charging evidence collection. Python 3 standard library."""

import argparse
from collections import Counter
import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import selectors
import subprocess
import sys
import time
import uuid


SCHEMA = 2
MAX_DURATION = 3600
MAX_SAMPLES = 3601
MAX_SAMPLE_BYTES = 16 * 1024 * 1024
MAX_LINE_BYTES = 128 * 1024
MAX_JOURNAL_BYTES = 2 * 1024 * 1024
MAX_METADATA_BYTES = 256 * 1024
SUPPLY_ROOT = Path("/sys/class/power_supply")
FIELDS = (
    "status", "health", "capacity", "current_now", "voltage_now", "voltage_ocv",
    "voltage_max", "temp", "charge_control_start_threshold",
    "charge_control_end_threshold", "charge_now", "charge_counter", "charge_full",
    "charge_full_design", "constant_charge_current", "constant_charge_current_max",
    "charge_behaviour", "power_now", "energy_now", "energy_full", "energy_full_design",
)


def read_value(path, limit=256):
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            return {"error": "read limit exceeded", "raw_prefix": raw[:limit].decode(errors="replace")}
        return {"raw": raw.decode(errors="replace")}
    except OSError as error:
        return {"error": str(error), "errno": error.errno}


def value(field):
    raw = field.get("raw") if isinstance(field, dict) else None
    return raw.strip() if isinstance(raw, str) else None


def number(field):
    raw = value(field)
    try:
        return int(raw) if raw is not None else None
    except ValueError:
        return None


def timestamp():
    return {
        "realtime_ns": time.time_ns(),
        "utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "monotonic_ns": time.monotonic_ns(),
        "boottime_ns": time.clock_gettime_ns(time.CLOCK_BOOTTIME),
    }


def digest(path, limit):
    try:
        sha = hashlib.sha256()
        total = 0
        with Path(path).open("rb") as stream:
            while True:
                chunk = stream.read(min(65536, limit - total + 1))
                total += len(chunk)
                if total > limit:
                    return {"path": str(path), "error": "hash read limit exceeded", "limit_bytes": limit}
                if not chunk:
                    break
                sha.update(chunk)
        return {"path": str(path), "bytes": total, "sha256": sha.hexdigest()}
    except OSError as error:
        return {"path": str(path), "error": str(error), "errno": error.errno}


def bounded_command(args, stdout_limit=16384, stderr_limit=8192, timeout=10):
    """Stop the child before pipe output can exceed the memory budget."""
    result = {"argv": args, "timeout_seconds": timeout, "truncated": False}
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    try:
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as error:
        return {**result, "error": str(error), "stdout": "", "stderr": ""}
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, ("stdout", stdout_limit))
            selector.register(process.stderr, selectors.EVENT_READ, ("stderr", stderr_limit))
            deadline = time.monotonic() + timeout
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    result["error"] = "command timeout"
                    break
                events = selector.select(min(remaining, 0.2))
                for key, _ in events:
                    name, limit = key.data
                    chunk = os.read(key.fd, min(65536, limit - len(buffers[name]) + 1))
                    if not chunk:
                        selector.unregister(key.fileobj)
                    elif len(buffers[name]) + len(chunk) > limit:
                        buffers[name].extend(chunk[:limit - len(buffers[name])])
                        result["truncated"] = True
                        result["error"] = "command output limit reached"
                        break
                    else:
                        buffers[name].extend(chunk)
                if "error" in result:
                    break
            if "error" not in result:
                try:
                    process.wait(timeout=max(0.01, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    result["error"] = "command timeout"
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdout.close()
        process.stderr.close()
    return {**result, "returncode": process.returncode,
            **{key: data.decode(errors="replace") for key, data in buffers.items()}}


def select_battery(requested=None, root=SUPPLY_ROOT):
    if requested is not None and (Path(requested).name != requested or requested in ("", ".", "..")):
        raise ValueError("--battery must be a power_supply directory name")
    supplies = sorted(root.iterdir())
    if len(supplies) > 32:
        raise ValueError("power supply count exceeds limit of 32")
    candidates = [path.name for path in supplies
                  if value(read_value(path / "type")) == "Battery"
                  and value(read_value(path / "scope")) != "Device"]
    if requested is not None:
        if requested not in candidates:
            raise ValueError(f"{requested} is not an accessible system Battery supply (scope=Device excluded)")
        return requested
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise ValueError("no system Battery supply found (scope=Device excluded)")
    raise ValueError("multiple system batteries found; select --battery NAME: " + ", ".join(candidates))


def os_release():
    result = read_value("/etc/os-release", 16384)
    if "raw" not in result:
        return result
    allowed = {"ID", "NAME", "VERSION_ID", "PRETTY_NAME", "BUILD_ID", "VARIANT_ID"}
    return {key: raw for line in result["raw"].splitlines() if "=" in line
            for key, raw in [line.split("=", 1)] if key in allowed}


def metadata(battery, firmware=None, root=SUPPLY_ROOT):
    uname = os.uname()
    identity = {
        "boot_id": read_value("/proc/sys/kernel/random/boot_id"),
        "image": read_value("/usr/lib/armada/version", 4096),
        "kernel": {key: getattr(uname, key) for key in ("sysname", "release", "machine")},
        "os": os_release(),
        "model": {"device_tree": read_value("/sys/firmware/devicetree/base/model", 4096),
                  "dmi_product": read_value("/sys/class/dmi/id/product_name", 4096)},
        "battery_name": battery,
    }
    script = digest(Path(__file__).resolve(), 1024 * 1024)
    script["path"] = Path(__file__).name
    return {
        "schema": SCHEMA, "started": timestamp(), "identity": identity,
        "script": script,
        "battery_info": {key: read_value(root / battery / key)
                         for key in ("type", "scope", "model_name", "manufacturer", "technology")},
        "firmware": digest(firmware, 64 * 1024 * 1024) if firmware else {"requested": False},
        "limits": {"duration_seconds": MAX_DURATION, "sample_records": MAX_SAMPLES,
                   "sample_bytes": MAX_SAMPLE_BYTES, "journal_bytes": MAX_JOURNAL_BYTES},
        "units": {"voltage_*": "microvolt", "current_*": "microampere",
                  "constant_charge_current*": "microampere", "charge_*_threshold": "percent",
                  "charge_now/counter/full/full_design": "microampere-hour", "temp": "0.1 degree Celsius",
                  "power_now": "microwatt", "energy_*": "microwatt-hour", "capacity": "percent"},
        "limits_of_evidence": ["Properties are read sequentially, not as an atomic hardware snapshot.",
                               "Raw sysfs units do not establish calibrated physical pack scaling.",
                               "A single capture does not establish a charging fix or long-term stability."],
    }


def take_sample(identity, battery="battery", root=SUPPLY_ROOT):
    # Keep per-record identity small; optional host metadata is stored once.
    identity = {key: identity[key] for key in ("boot_id", "image", "battery_name") if key in identity}
    sample = {"time": timestamp(), "identity": identity,
              "battery": {field: read_value(root / battery / field) for field in FIELDS},
              "external_power": {}}
    try:
        supplies = sorted(root.iterdir())
        if len(supplies) > 32:
            sample["supply_error"] = "power supply count exceeds limit of 32"
        for path in supplies[:32]:
            if path.name == battery:
                continue
            supply_type = read_value(path / "type")
            if value(supply_type) != "Battery":
                sample["external_power"][path.name] = {
                    "type": supply_type, "online": read_value(path / "online")}
    except OSError as error:
        sample["supply_error"] = str(error)
    sample["read_finished_boottime_ns"] = time.clock_gettime_ns(time.CLOCK_BOOTTIME)
    return sample


def online(sample):
    states = [number(supply.get("online")) for supply in sample["external_power"].values()]
    if 1 in states:
        return True
    if not states or sample.get("supply_error") or any(state not in (0, 1) for state in states):
        return None
    return False


def thresholds(sample):
    fields = sample["battery"]
    pair = [number(fields.get("charge_control_start_threshold")),
            number(fields.get("charge_control_end_threshold"))]
    return pair if all(item is not None for item in pair) else None


def status_value(sample):
    status = value(sample["battery"].get("status"))
    return status if status in ("Charging", "Discharging", "Not charging", "Full") else None


def alerts_for(sample, previous_ceiling):
    fields = sample["battery"]
    health = value(fields.get("health"))
    ceiling = number(fields.get("voltage_max"))
    alerts = []
    if health and health.lower() not in ("good", "unknown"):
        alerts.append("Reported battery health is " + health + ".")
    if ceiling is not None and ceiling > 0 and previous_ceiling is not None and ceiling < previous_ceiling:
        alerts.append(f"Reported battery voltage_max dropped from {previous_ceiling} to {ceiling} microvolts; this alone does not establish physical overvoltage.")
    return alerts


def monitoring_issues_for(sample):
    fields = sample["battery"]
    health = value(fields.get("health"))
    ceiling = number(fields.get("voltage_max"))
    issues = []
    if not health or health.lower() == "unknown":
        issues.append("Battery health unavailable or unknown.")
    if ceiling is None or ceiling <= 0:
        issues.append("Battery voltage_max unavailable or invalid; no design-voltage substitute is used.")
    return issues


def encoded(record):
    return (json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n").encode()


def write_json(path, record):
    data = encoded(record)
    if len(data) > MAX_METADATA_BYTES:
        raise ValueError("metadata size limit exceeded")
    with path.open("xb") as stream:
        stream.write(data)


class SampleWriter:
    def __init__(self, stream, byte_limit=MAX_SAMPLE_BYTES, record_limit=MAX_SAMPLES):
        self.stream = stream
        self.byte_limit = byte_limit
        self.record_limit = record_limit
        self.bytes = 0
        self.records = 0

    def append(self, record):
        data = encoded(record)
        if len(data) > MAX_LINE_BYTES or self.bytes + len(data) > self.byte_limit or self.records >= self.record_limit:
            return False
        self.stream.write(data)
        self.stream.flush()
        self.bytes += len(data)
        self.records += 1
        return True


def print_alert(alerts):
    print("\nALERT: MANUALLY UNPLUG THE CHARGER. Capture stopped; no charging controls were changed.",
          file=sys.stderr, flush=True)
    for alert in alerts:
        print("  " + alert, file=sys.stderr, flush=True)


def print_monitoring_issues(issues):
    print("\nMONITORING UNAVAILABLE: capture stopped. Missing monitoring is not a detected battery fault.",
          file=sys.stderr, flush=True)
    for issue in issues:
        print("  " + issue, file=sys.stderr, flush=True)


def print_progress(sample, started):
    fields = sample["battery"]
    elapsed = sample["time"]["boottime_ns"] / 1e9 - started
    pair = thresholds(sample)
    pair_text = "/".join(map(str, pair)) + "%" if pair else "unavailable"
    print(f"{elapsed:.0f}s: capacity={value(fields.get('capacity')) or '?'}% "
          f"status={value(fields.get('status')) or '?'} health={value(fields.get('health')) or '?'} "
          f"voltage_max={value(fields.get('voltage_max')) or '?'} uV "
          f"start/stop={pair_text}", flush=True)


def capture(args):
    battery = select_battery(args.battery)
    directory = args.output
    directory.mkdir(parents=True, exist_ok=False)
    meta = metadata(battery, args.firmware)
    meta["capture"] = {"duration_seconds": args.duration, "interval_seconds": args.interval,
                       "battery": battery, "progress_interval_seconds": args.progress_interval}
    write_json(directory / "metadata.json", meta)
    start = time.clock_gettime(time.CLOCK_BOOTTIME)
    deadline = start + args.duration
    next_sample = start
    next_progress = start
    ceiling = None
    reason = "duration_limit"
    alerts = []
    monitoring_issues = []
    last_time = None
    capture_error = None
    with (directory / "samples.jsonl").open("xb") as stream:
        writer = SampleWriter(stream)
        try:
            while True:
                now = time.clock_gettime(time.CLOCK_BOOTTIME)
                if now > deadline:
                    break
                if now < next_sample:
                    time.sleep(min(next_sample - now, deadline - now, 0.5))
                    continue
                sample = take_sample(meta["identity"], battery)
                sample["index"] = writer.records
                alerts = alerts_for(sample, ceiling)
                sample["alerts"] = alerts
                monitoring_issues = monitoring_issues_for(sample)
                sample["monitoring_issues"] = monitoring_issues
                if not writer.append(sample):
                    reason = "sample_output_limit"
                    # Preserve the final alert reading even if the sample file is full.
                    if alerts or monitoring_issues:
                        write_json(directory / "alert-sample.json", sample)
                    if alerts:
                        print_alert(alerts)
                    if monitoring_issues:
                        print_monitoring_issues(monitoring_issues)
                    break
                last_time = sample["time"]
                if alerts:
                    reason = "alert"
                    print_alert(alerts)
                    break
                if monitoring_issues:
                    reason = "monitoring_unavailable"
                    print_monitoring_issues(monitoring_issues)
                    break
                if now >= next_progress:
                    print_progress(sample, start)
                    next_progress = now + args.progress_interval
                ceiling = number(sample["battery"]["voltage_max"])
                next_sample += args.interval
                now = time.clock_gettime(time.CLOCK_BOOTTIME)
                if next_sample < now:
                    next_sample = now + args.interval
        except KeyboardInterrupt:
            reason = "interrupted"
        except OSError as error:
            reason = "capture_error"
            capture_error = str(error)
        finally:
            stream.flush()
            os.fsync(stream.fileno())
            write_json(directory / "finished.json", {
                "schema": SCHEMA, "reason": reason, "finished": timestamp(), "last_sample": last_time,
                "records": writer.records, "sample_bytes": writer.bytes, "alerts": alerts,
                "monitoring_issues": monitoring_issues,
                "error": capture_error,
                "elapsed_boottime_seconds": time.clock_gettime(time.CLOCK_BOOTTIME) - start,
            })
    print(f"Saved {writer.records} samples to {directory}; reason={reason}", flush=True)
    return 2 if alerts else (3 if monitoring_issues else (1 if capture_error else (130 if reason == "interrupted" else 0)))


def load_json(path, limit=MAX_METADATA_BYTES):
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"input exceeds size limit: {path}")
    return json.loads(data)


def load_samples(path):
    total = 0
    count = 0
    with path.open("rb") as stream:
        while True:
            line = stream.readline(MAX_LINE_BYTES + 1)
            if not line:
                break
            total += len(line)
            count += 1
            if len(line) > MAX_LINE_BYTES or total > MAX_SAMPLE_BYTES or count > MAX_SAMPLES:
                raise ValueError("sample input limit exceeded")
            sample = json.loads(line)
            if not isinstance(sample, dict) or not isinstance(sample.get("battery"), dict):
                raise ValueError(f"invalid sample structure on line {count}")
            if not isinstance(sample.get("external_power"), dict) or not isinstance(sample.get("identity"), dict):
                raise ValueError(f"invalid sample metadata on line {count}")
            if any(not isinstance(supply, dict) for supply in sample["external_power"].values()):
                raise ValueError(f"invalid supply data on line {count}")
            if not isinstance(sample.get("time"), dict):
                raise ValueError(f"invalid timestamp on line {count}")
            for key in ("boottime_ns", "monotonic_ns", "realtime_ns"):
                item = sample.get("time", {}).get(key)
                if not isinstance(item, int) or isinstance(item, bool) or item < 0:
                    raise ValueError(f"invalid {key} on line {count}")
            yield sample


def same_segment(left, right):
    return (left["identity"] == right["identity"] and
            value(left["identity"].get("boot_id")) is not None and
            right["time"]["boottime_ns"] > left["time"]["boottime_ns"] and
            right["time"]["monotonic_ns"] > left["time"]["monotonic_ns"])


def seconds(left, right):
    return (right["time"]["boottime_ns"] - left["time"]["boottime_ns"]) / 1e9


def summarize_records(records, interval=1):
    previous = None
    last_status_sample = None
    first = None
    last = None
    first_health_change = None
    first_ceiling_change = None
    first_unhealthy = None
    stops = []
    errors = Counter()
    statuses = Counter()
    pairs = Counter()
    counters = {key: {"first_raw": None, "last_raw": None, "adjacent_delta_sum_raw": 0,
                      "valid_adjacent_pairs": 0, "changed_adjacent_pairs": 0}
                for key in ("charge_counter", "charge_now")}
    count = 0
    breaks = []
    gaps = []
    segment = 0
    latest_health = None
    latest_ceiling = None
    for sample in records:
        count += 1
        first = first or sample
        last = sample
        if previous and not same_segment(previous, sample):
            breaks.append({"sample": count - 1, "reason": "identity or clock discontinuity"})
            previous = None
            last_status_sample = None
            latest_health = latest_ceiling = None
            segment += 1
        if previous and seconds(previous, sample) > interval * 2:
            gaps.append({"sample": count - 1, "seconds": seconds(previous, sample)})
        fields = sample["battery"]
        for key in FIELDS:
            if value(fields.get(key)) is None or (key not in ("status", "health", "charge_behaviour") and number(fields.get(key)) is None):
                errors[key] += 1
        status = status_value(sample)
        health = value(fields.get("health"))
        ceiling = number(fields.get("voltage_max"))
        if value(fields.get("status")) is not None and status is None:
            errors["status"] += 1
        if health is not None and (not health or health.lower() == "unknown"):
            errors["health"] += 1
        statuses[status or "<unavailable>"] += 1
        pairs[str(thresholds(sample))] += 1
        if online(sample) is None:
            errors["external_power_online"] += 1
        if health and health.lower() not in ("good", "unknown") and first_unhealthy is None:
            first_unhealthy = {"time": sample["time"], "raw": fields["health"]}
        for key, current, prior in (("health", health, latest_health), ("voltage_max", ceiling, latest_ceiling)):
            if current is not None and prior is not None and current != prior[0]:
                change = {"previous_time": prior[1], "time": sample["time"],
                          "previous": prior[0], "current": current}
                if key == "health" and first_health_change is None:
                    first_health_change = change
                if key == "voltage_max" and first_ceiling_change is None:
                    first_ceiling_change = change
        if health is not None:
            latest_health = (health, sample["time"])
        if ceiling is not None:
            latest_ceiling = (ceiling, sample["time"])
        if status in ("Not charging", "Full") and last_status_sample and value(last_status_sample["battery"].get("status")) == "Charging":
            prior = last_status_sample
            uncertainty = []
            if prior is not previous:
                uncertainty.append("intervening unavailable status")
            if seconds(prior, sample) > interval * 2:
                uncertainty.append("sample gap")
            if online(prior) is not True or online(sample) is not True:
                uncertainty.append("external power not confirmed online at both observations")
            pair = thresholds(sample)
            if pair is None or thresholds(prior) != pair:
                uncertainty.append("threshold pair unavailable or changed")
            stops.append({"charging_time": prior["time"], "stop_time": sample["time"],
                          "stop_status": status, "thresholds": pair, "segment": segment,
                          "qualifies_online_unchanged_pair": not uncertainty, "uncertainty": uncertainty,
                          "observation_after_stop_seconds": 0,
                          "uninterrupted_followup_seconds": 0, "first_followup_interruption": None,
                          "first_health_change_after_stop": None, "first_voltage_max_change_after_stop": None,
                          "stop_health": health, "stop_voltage_max": ceiling})
        for stop in stops:
            if stop["segment"] != segment:
                continue
            delay = (sample["time"]["boottime_ns"] - stop["stop_time"]["boottime_ns"]) / 1e9
            stop["observation_after_stop_seconds"] = delay
            if delay > 0 and stop["first_followup_interruption"] is None:
                interruptions = []
                if status not in ("Not charging", "Full"):
                    interruptions.append("status=" + (status or "unavailable"))
                if online(sample) is not True:
                    interruptions.append("external power offline or unavailable")
                if thresholds(sample) is None or thresholds(sample) != stop["thresholds"]:
                    interruptions.append("threshold pair changed or unavailable")
                if previous and seconds(previous, sample) > interval * 2:
                    interruptions.append("sample gap")
                if interruptions:
                    stop["first_followup_interruption"] = {"time": sample["time"],
                        "seconds_after_stop": delay, "reasons": interruptions}
                else:
                    stop["uninterrupted_followup_seconds"] = delay
            for key, current, baseline in (("health", health, stop["stop_health"]),
                                           ("voltage_max", ceiling, stop["stop_voltage_max"])):
                target = "first_" + key + "_change_after_stop"
                if current is not None and baseline is not None and current != baseline and stop[target] is None:
                    stop[target] = {"time": sample["time"], "seconds_after_stop": delay,
                                    "previous": baseline, "current": current}
        for key, stats in counters.items():
            current = number(fields.get(key))
            if current is not None:
                if stats["first_raw"] is None:
                    stats["first_raw"] = current
                stats["last_raw"] = current
                prior = number(previous["battery"].get(key)) if previous else None
                if prior is not None:
                    delta = current - prior
                    stats["adjacent_delta_sum_raw"] += delta
                    stats["valid_adjacent_pairs"] += 1
                    stats["changed_adjacent_pairs"] += int(delta != 0)
        if status is not None:
            last_status_sample = sample
        previous = sample
    return {
        "schema": SCHEMA, "records": count, "first_time": first["time"] if first else None,
        "last_time": last["time"] if last else None,
        "observation_seconds": seconds(first, last) if first and last and not breaks else None,
        "statuses": dict(statuses), "observed_threshold_pairs": dict(pairs),
        "unavailable_field_counts": dict(errors), "identity_or_clock_breaks": breaks, "sample_gaps": gaps,
        "first_unhealthy_observation": first_unhealthy, "first_health_change": first_health_change,
        "first_voltage_max_change": first_ceiling_change, "stop_observations": stops, "counter_changes": counters,
        "limits": ["Stop times are first observed status transitions; exact physical stopping times are unknown.",
                   "Observation after stop is follow-up coverage, not proof that charging remained stopped.",
                   "Flat counters or noncharging status do not independently establish zero charging current.",
                   "Counter deltas use reported microampere-hours; physical scaling and calibration are unverified.",
                   "A single capture does not establish a charging fix or long-term stability."],
    }


def summarize(args):
    meta = load_json(args.capture / "metadata.json")
    result = summarize_records(load_samples(args.capture / "samples.jsonl"), meta["capture"]["interval_seconds"])
    result["capture_finish"] = load_json(args.capture / "finished.json") if (args.capture / "finished.json").exists() else {"error": "finished.json missing; capture incomplete"}
    result["identity"] = meta["identity"]
    print(json.dumps(result, indent=2))
    return 0


def journal(args):
    meta = load_json(args.capture / "metadata.json")
    finish = load_json(args.capture / "finished.json")
    boot_id = value(meta["identity"]["boot_id"])
    if not boot_id:
        raise ValueError("capture boot ID unavailable")
    boot_id = uuid.UUID(boot_id).hex
    start = meta["started"]["realtime_ns"] / 1e9
    end = finish["finished"]["realtime_ns"] / 1e9
    if not math.isfinite(start) or not math.isfinite(end) or not 0 <= end - start <= MAX_DURATION + 120:
        raise ValueError("journal realtime window invalid or exceeds bounded capture window")
    if (args.capture / "kernel-journal.txt").exists() or (args.capture / "journal-status.json").exists():
        raise ValueError("journal output already exists")
    result = bounded_command(["journalctl", "-k", "--boot=" + boot_id,
                              "--since=@" + str(start), "--until=@" + str(end),
                              "--no-pager", "--output=short-monotonic"],
                             stdout_limit=MAX_JOURNAL_BYTES, timeout=15)
    output = result.pop("stdout").encode()
    # Invalid UTF-8 replacement can expand bytes; retain the hard file limit.
    if len(output) > MAX_JOURNAL_BYTES:
        output = output[:MAX_JOURNAL_BYTES]
        result["truncated"] = True
    with (args.capture / "kernel-journal.txt").open("xb") as stream:
        stream.write(output)
    result["bytes"] = len(output)
    result["empty"] = not bool(output.strip())
    write_json(args.capture / "journal-status.json", result)
    print(json.dumps(result, indent=2))
    return 1 if result.get("error") or result.get("returncode") != 0 else 0


def bounded_number(low, high):
    def parse(raw):
        try:
            result = float(raw)
        except ValueError as error:
            raise argparse.ArgumentTypeError("numeric value required") from error
        if not math.isfinite(result) or not low <= result <= high:
            raise argparse.ArgumentTypeError(f"must be finite and between {low} and {high}")
        return result
    return parse


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    inspect_parser = subparsers.add_parser("inspect", help="print identity and one passive sample")
    capture_parser = subparsers.add_parser("capture", help="save bounded passive samples to a new directory")
    for command_parser in (inspect_parser, capture_parser):
        command_parser.add_argument("--battery", help="system battery supply name; autodetected when unique")
        command_parser.add_argument("--firmware", type=Path, help="optional firmware file to hash (maximum 64 MiB)")
    capture_parser.add_argument("--output", type=Path, required=True)
    capture_parser.add_argument("--duration", type=bounded_number(1, MAX_DURATION), default=1800)
    capture_parser.add_argument("--interval", type=bounded_number(1, 60), default=1)
    capture_parser.add_argument("--progress-interval", type=bounded_number(1, 3600), default=30,
                                help="seconds between live progress lines (default: 30)")
    for name in ("summarize", "journal"):
        command_parser = subparsers.add_parser(name)
        command_parser.add_argument("capture", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "inspect":
            battery = select_battery(args.battery)
            meta = metadata(battery, args.firmware)
            sample = take_sample(meta["identity"], battery)
            alerts = alerts_for(sample, None)
            issues = monitoring_issues_for(sample)
            print(json.dumps({"metadata": meta, "sample": sample, "alerts": alerts,
                              "monitoring_issues": issues}, indent=2))
            if alerts:
                print_alert(alerts)
            if issues:
                print_monitoring_issues(issues)
            return 2 if alerts else (3 if issues else 0)
        return {"capture": capture, "summarize": summarize, "journal": journal}[args.command](args)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print("ERROR: " + str(error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
