import argparse
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import charge_watch as tool


def field(raw):
    return {"raw": str(raw) + "\n"}


def sample(second, status="Charging", health="Good", ceiling=4380000,
           pair=(65, 70), counter=6000000, connected=1):
    fields = {key: field(0) for key in tool.FIELDS}
    fields.update(status=field(status), health=field(health), voltage_max=field(ceiling),
                  voltage_now=field(4031955), voltage_ocv=field(4043775),
                  current_now=field(-428381), charge_counter=field(counter), charge_now=field(counter),
                  charge_control_start_threshold=field(pair[0]), charge_control_end_threshold=field(pair[1]))
    return {"time": {"realtime_ns": (1000 + second) * 10**9, "boottime_ns": second * 10**9,
                     "monotonic_ns": second * 10**9}, "identity": {"boot_id": field("boot-a"), "image": field("test")},
            "battery": fields, "external_power": {"usb": {"online": field(connected)}}}


class SummaryTests(unittest.TestCase):
    def test_delayed_fault_and_actual_thresholds(self):
        records = [sample(0), sample(1, "Not charging")]
        records.extend(sample(t, "Not charging") for t in range(2, 286))
        records.append(sample(286, "Not charging", "Over voltage", 4035000))
        result = tool.summarize_records(records)
        stop = result["stop_observations"][0]
        self.assertTrue(stop["qualifies_online_unchanged_pair"])
        self.assertEqual(stop["thresholds"], [65, 70])
        self.assertEqual(stop["observation_after_stop_seconds"], 285)
        self.assertEqual(stop["first_health_change_after_stop"]["seconds_after_stop"], 285)
        self.assertEqual(stop["first_voltage_max_change_after_stop"]["current"], 4035000)
        self.assertEqual(result["first_unhealthy_observation"]["raw"], field("Over voltage"))
        self.assertEqual(records[-1]["battery"]["voltage_now"], field(4031955))

    def test_baseline_noncharging_is_not_a_transition(self):
        result = tool.summarize_records([sample(0, "Not charging"), sample(285, "Not charging", "Over voltage")])
        self.assertEqual(result["stop_observations"], [])
        self.assertIsNotNone(result["first_unhealthy_observation"])

    def test_missing_status_leaves_uncertain_transition(self):
        for unavailable in ({"error": "unavailable"}, field("Unknown"), field("")):
            missing = sample(1)
            missing["battery"]["status"] = unavailable
            result = tool.summarize_records([sample(0), missing, sample(2, "Full")])
            stop = result["stop_observations"][0]
            self.assertFalse(stop["qualifies_online_unchanged_pair"])
            self.assertIn("intervening unavailable status", stop["uncertainty"])
            self.assertEqual(result["unavailable_field_counts"]["status"], 1)

    def test_followup_restart_or_disconnect_is_explicit(self):
        for changed in (sample(2), sample(2, "Not charging", connected=0),
                        sample(2, "Not charging", pair=(60, 70))):
            result = tool.summarize_records([sample(0), sample(1, "Not charging"), changed,
                                            sample(286, "Charging", "Over voltage")])
            stop = result["stop_observations"][0]
            self.assertEqual(stop["first_followup_interruption"]["seconds_after_stop"], 1)
            self.assertEqual(stop["uninterrupted_followup_seconds"], 0)
            self.assertEqual(stop["observation_after_stop_seconds"], 285)

    def test_changed_thresholds_or_offline_do_not_qualify(self):
        for changed in (sample(1, "Not charging", pair=(60, 70)), sample(1, "Not charging", connected=0)):
            self.assertFalse(tool.summarize_records([sample(0), changed])["stop_observations"][0]["qualifies_online_unchanged_pair"])

    def test_identity_and_clock_breaks_block_deltas_and_transitions(self):
        for second in (sample(1, "Full", counter=999), sample(0, "Full", counter=999)):
            if second["time"]["boottime_ns"]:
                second["identity"]["boot_id"] = field("boot-b")
            result = tool.summarize_records([sample(0), second])
            self.assertIsNone(result["observation_seconds"])
            self.assertEqual(result["stop_observations"], [])
            self.assertEqual(result["counter_changes"]["charge_counter"]["valid_adjacent_pairs"], 0)

    def test_missing_data_and_raw_counter_scaling(self):
        first = sample(0, counter=7055064)
        last = sample(180, counter=7045792)
        last["battery"]["health"] = {"error": "permission denied", "errno": 13}
        last["battery"]["voltage_max"] = {"error": "missing", "errno": 2}
        result = tool.summarize_records([first, last])
        self.assertEqual(result["counter_changes"]["charge_counter"]["adjacent_delta_sum_raw"], -9272)
        self.assertIsNone(result["first_health_change"])
        self.assertIsNone(result["first_voltage_max_change"])
        self.assertEqual(result["unavailable_field_counts"]["health"], 1)
        self.assertIn("physical scaling and calibration are unverified", " ".join(result["limits"]))

    def test_wall_clock_jump_does_not_change_elapsed_time(self):
        first, last = sample(0), sample(3)
        last["time"]["realtime_ns"] = 1
        self.assertEqual(tool.summarize_records([first, last])["observation_seconds"], 3)


class CaptureTests(unittest.TestCase):
    def test_healthy_capture_finishes_with_progress_and_exact_thresholds(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "capture"
            record = sample(0, pair=(52, 55))
            record["time"] = tool.timestamp()
            args = argparse.Namespace(output=output, duration=1, interval=1, battery=None,
                                      firmware=None, progress_interval=30)
            with mock.patch.object(tool, "select_battery", return_value="BAT0"), mock.patch.object(tool, "metadata", return_value={"identity": {}}), mock.patch.object(tool, "take_sample", return_value=record), contextlib.redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(tool.capture(args), 0)
            finish = tool.load_json(output / "finished.json")
            self.assertEqual(finish["reason"], "duration_limit")
            self.assertGreaterEqual(finish["records"], 1)
            self.assertIn("start/stop=52/55%", stdout.getvalue())
            self.assertIn("voltage_max=4380000 uV", stdout.getvalue())

    def test_missing_monitoring_stops_without_fault_claim_or_design_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "capture"
            missing = sample(0)
            missing["battery"]["voltage_max"] = {"error": "missing"}
            missing["battery"]["voltage_max_design"] = field(4400000)
            args = argparse.Namespace(output=output, duration=30, interval=1, battery=None,
                                      firmware=None, progress_interval=30)
            with mock.patch.object(tool, "select_battery", return_value="BAT0"), mock.patch.object(tool, "metadata", return_value={"identity": {}}), mock.patch.object(tool, "take_sample", return_value=missing) as read, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(tool.capture(args), 3)
            self.assertEqual(read.call_count, 1)
            finish = tool.load_json(output / "finished.json")
            self.assertEqual(finish["reason"], "monitoring_unavailable")
            self.assertEqual(finish["alerts"], [])
            self.assertIn("not a detected battery fault", stderr.getvalue())
            self.assertNotIn("MANUALLY UNPLUG", stderr.getvalue())

    def test_missing_thresholds_do_not_disable_monitoring(self):
        record = sample(0)
        for key in ("charge_control_start_threshold", "charge_control_end_threshold"):
            record["battery"][key] = {"error": "missing"}
        self.assertEqual(tool.monitoring_issues_for(record), [])
        self.assertIsNone(tool.thresholds(record))
        with contextlib.redirect_stdout(io.StringIO()) as progress:
            tool.print_progress(record, 0)
        self.assertIn("start/stop=unavailable", progress.getvalue())

    def test_alerts_preserve_both_faults_and_missing_monitoring(self):
        fault = sample(1, "Not charging", "Over voltage", 4035000)
        alerts = tool.alerts_for(fault, 4380000)
        self.assertEqual(len(alerts), 2)
        missing = copy.deepcopy(fault)
        missing["battery"]["health"] = {"error": "missing"}
        missing["battery"]["voltage_max"] = {"error": "missing"}
        self.assertEqual(tool.alerts_for(missing, 4380000), [])
        self.assertEqual(len(tool.monitoring_issues_for(missing)), 2)

    def test_online_requires_online_property(self):
        record = sample(0, connected=0)
        record["external_power"]["usb"]["voltage_now"] = field(998000)
        self.assertIs(tool.online(record), False)
        record["external_power"]["usb"]["online"] = {"error": "missing"}
        self.assertIsNone(tool.online(record))

    def test_record_and_byte_limits_never_write_partial_record(self):
        record = sample(0)
        size = len(tool.encoded(record))
        for byte_limit, record_limit in ((size, 100), (size * 10, 1)):
            stream = io.BytesIO()
            writer = tool.SampleWriter(stream, byte_limit, record_limit)
            self.assertTrue(writer.append(record))
            self.assertFalse(writer.append(record))
            self.assertEqual(len(stream.getvalue()), size)
            self.assertEqual(json.loads(stream.getvalue()), record)

    def test_capture_stops_on_first_alert_and_saves_finish(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "capture"
            fault = sample(0, health="Over voltage")
            meta = {"identity": fault["identity"]}
            args = argparse.Namespace(output=output, duration=30, interval=1, battery="battery", firmware=None, progress_interval=30)
            with mock.patch.object(tool, "select_battery", return_value="battery"), mock.patch.object(tool, "metadata", return_value=meta), mock.patch.object(tool, "take_sample", return_value=fault) as read, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(tool.capture(args), 2)
            self.assertEqual(read.call_count, 1)
            self.assertIn("MANUALLY UNPLUG", stderr.getvalue())
            self.assertEqual(tool.load_json(output / "finished.json")["reason"], "alert")
            self.assertEqual(len(list(tool.load_samples(output / "samples.jsonl"))), 1)

    def test_interruption_saves_completed_records(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "capture"
            args = argparse.Namespace(output=output, duration=30, interval=1, battery="battery", firmware=None, progress_interval=30)
            with mock.patch.object(tool, "select_battery", return_value="battery"), mock.patch.object(tool, "metadata", return_value={"identity": {}}), mock.patch.object(tool, "take_sample", side_effect=KeyboardInterrupt), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(tool.capture(args), 130)
            self.assertEqual(tool.load_json(output / "finished.json")["reason"], "interrupted")

    def test_missing_and_oversized_reads_are_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "field"
            self.assertEqual(tool.read_value(path)["errno"], 2)
            path.write_text("123456")
            self.assertEqual(tool.read_value(path, 3)["raw_prefix"], "123")
            self.assertIn("error", tool.read_value(path, 3))

    def test_sample_collection_only_opens_fixture_properties_for_reading(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "battery").mkdir()
            (root / "usb").mkdir()
            for path, raw in ((root / "battery" / "health", "Good\n"),
                              (root / "usb" / "type", "USB\n"),
                              (root / "usb" / "online", "1\n")):
                path.write_text(raw)
                path.chmod(0o444)
            real_open = Path.open
            modes = []

            def checked_open(path, mode="r", *args, **kwargs):
                modes.append(mode)
                return real_open(path, mode, *args, **kwargs)

            with mock.patch.object(Path, "open", checked_open):
                result = tool.take_sample({"boot_id": field("fixture")}, root=root)
            self.assertTrue(modes)
            self.assertEqual(set(modes), {"rb"})
            self.assertEqual(result["battery"]["health"], field("Good"))
            self.assertEqual(result["battery"]["voltage_max"]["errno"], 2)
            self.assertTrue(tool.online(result))

    def test_subprocess_output_is_bounded_and_timeout_visible(self):
        result = tool.bounded_command([tool.sys.executable, "-c", "import sys; sys.stdout.write('x' * 100000)"], stdout_limit=100)
        self.assertEqual(len(result["stdout"]), 100)
        self.assertTrue(result["truncated"])
        result = tool.bounded_command([tool.sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.05)
        self.assertEqual(result["error"], "command timeout")

    def test_journal_uses_current_account_and_preserves_permission_failures(self):
        for returncode, stdout, stderr in ((0, "kernel fixture\n", ""), (1, "", "Permission denied\n")):
            with self.subTest(returncode=returncode), tempfile.TemporaryDirectory() as directory:
                capture = Path(directory)
                tool.write_json(capture / "metadata.json", {
                    "identity": {"boot_id": field("00000000-0000-4000-8000-000000000001")},
                    "started": {"realtime_ns": 1000 * 10**9}})
                tool.write_json(capture / "finished.json", {"finished": {"realtime_ns": 1005 * 10**9}})
                result = {"returncode": returncode, "stdout": stdout, "stderr": stderr, "truncated": False}
                with mock.patch.object(tool.os, "geteuid", return_value=1000), mock.patch.object(tool, "bounded_command", return_value=result) as query, contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(tool.journal(argparse.Namespace(capture=capture)), returncode)
                query.assert_called_once_with(
                    ["journalctl", "-k", "--boot=00000000000040008000000000000001", "--since=@1000.0", "--until=@1005.0",
                     "--no-pager", "--output=short-monotonic"],
                    stdout_limit=tool.MAX_JOURNAL_BYTES, timeout=15)
                status = tool.load_json(capture / "journal-status.json")
                self.assertEqual(status["returncode"], returncode)
                self.assertEqual(status["stderr"], stderr)
                self.assertEqual((capture / "kernel-journal.txt").read_text(), stdout)
                self.assertEqual(tool.value(tool.load_json(capture / "metadata.json")["identity"]["boot_id"]),
                                 "00000000-0000-4000-8000-000000000001")

    def test_journal_rejects_invalid_boot_uuid_before_query(self):
        with tempfile.TemporaryDirectory() as directory:
            capture = Path(directory)
            tool.write_json(capture / "metadata.json", {
                "identity": {"boot_id": field("invalid-boot-id")}, "started": {"realtime_ns": 1000 * 10**9}})
            tool.write_json(capture / "finished.json", {"finished": {"realtime_ns": 1005 * 10**9}})
            with mock.patch.object(tool, "bounded_command") as query, self.assertRaises(ValueError):
                tool.journal(argparse.Namespace(capture=capture))
            query.assert_not_called()

    def test_numeric_arguments_reject_nonfinite_or_out_of_range(self):
        for raw in ("nan", "inf", "-1", "0", "3601"):
            with self.assertRaises(argparse.ArgumentTypeError):
                tool.bounded_number(1, 3600)(raw)

    def test_sample_input_limit_checked_before_json_parsing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "samples.jsonl"
            path.write_bytes(b"x" * (tool.MAX_LINE_BYTES + 1))
            with self.assertRaisesRegex(ValueError, "limit"):
                list(tool.load_samples(path))

    def test_malformed_sample_structure_has_explicit_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "samples.jsonl"
            for key in ("time", "external_power", "identity"):
                record = sample(0)
                record[key] = None
                path.write_bytes(tool.encoded(record))
                with self.assertRaises(ValueError):
                    list(tool.load_samples(path))
            self.assertIsNone(tool.value({"raw": None}))


class PortableTests(unittest.TestCase):
    def supply(self, root, name, kind="Battery", scope=None):
        path = root / name
        path.mkdir()
        (path / "type").write_text(kind + "\n")
        if scope is not None:
            (path / "scope").write_text(scope + "\n")
        return path

    def test_battery_detection_excludes_peripheral_and_accepts_missing_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.supply(root, "controller", scope="Device")
            self.supply(root, "ac", kind="Mains")
            self.supply(root, "BAT0")
            self.assertEqual(tool.select_battery(root=root), "BAT0")
            with self.assertRaisesRegex(ValueError, "scope=Device"):
                tool.select_battery("controller", root)
            with self.assertRaisesRegex(ValueError, "not an accessible"):
                tool.select_battery("missing", root)

    def test_ambiguous_batteries_require_explicit_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.supply(root, "BAT0", scope="System")
            self.supply(root, "BAT1", scope="System")
            with self.assertRaisesRegex(ValueError, "BAT0, BAT1"):
                tool.select_battery(root=root)
            self.assertEqual(tool.select_battery("BAT1", root), "BAT1")

    def test_no_battery_and_unsafe_names_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.supply(root, "controller", scope="Device")
            with self.assertRaisesRegex(ValueError, "no system Battery"):
                tool.select_battery(root=root)
            for name in ("", ".", "..", "../battery", "/sys/battery"):
                with self.assertRaisesRegex(ValueError, "directory name"):
                    tool.select_battery(name, root)

    def test_alternate_device_metadata_has_no_hostname_or_default_firmware(self):
        uname = argparse.Namespace(sysname="Linux", nodename="PRIVATE-HOST", release="6.12-test",
                                   version="PRIVATE-BUILDER", machine="aarch64")
        reads = []

        def read(path, limit=256):
            reads.append(str(path))
            if str(path) == "/sys/firmware/devicetree/base/model":
                return field("Alternate ARM handheld\0")
            return {"error": "not available", "errno": 2}

        with mock.patch.object(tool, "read_value", side_effect=read), mock.patch.object(tool.os, "uname", return_value=uname), mock.patch.object(tool, "digest", return_value={"path": "/home/PRIVATE-USER/charge_watch.py", "sha256": "fixture"}) as hashed, mock.patch.object(tool, "bounded_command") as command:
            meta = tool.metadata("BAT0")
        self.assertEqual(meta["identity"]["kernel"]["release"], "6.12-test")
        self.assertIn("Alternate ARM handheld", meta["identity"]["model"]["device_tree"]["raw"])
        self.assertIn("error", meta["identity"]["image"])
        self.assertEqual(meta["firmware"], {"requested": False})
        self.assertEqual(hashed.call_count, 1)
        command.assert_not_called()
        encoded = json.dumps(meta)
        for forbidden in ("PRIVATE-HOST", "PRIVATE-BUILDER", "PRIVATE-USER", "serial", "adsp.mbn"):
            self.assertNotIn(forbidden, encoded)
        self.assertFalse(any("serial" in path for path in reads))

    def test_os_release_only_retains_distribution_fields(self):
        with mock.patch.object(tool, "read_value", return_value={"raw": 'ID=armada\nVERSION_ID="42"\nPRIVATE_HOST=secret\nNAME="Armada Linux"\n'}):
            self.assertEqual(tool.os_release(), {"ID": "armada", "VERSION_ID": '"42"', "NAME": '"Armada Linux"'})

    def test_explicit_firmware_is_hashed_not_assumed(self):
        with tempfile.TemporaryDirectory() as directory:
            firmware = Path(directory) / "firmware.bin"
            firmware.write_bytes(b"synthetic firmware")
            meta = tool.metadata("BAT0", firmware)
            self.assertEqual(meta["firmware"]["bytes"], 18)
            self.assertEqual(meta["firmware"]["sha256"], tool.hashlib.sha256(b"synthetic firmware").hexdigest())
            missing = tool.metadata("BAT0", Path(directory) / "absent.bin")
            self.assertIn("error", missing["firmware"])


if __name__ == "__main__":
    unittest.main()
