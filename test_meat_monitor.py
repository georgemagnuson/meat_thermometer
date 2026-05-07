"""
test_meat_monitor.py — TDD tests for meat_monitor.py

Run unit tests only (no hardware needed):
  pytest test_meat_monitor.py -v

Run BLE integration tests (thermometer must be powered on and in range):
  pytest test_meat_monitor.py -v -m bluetooth
"""

import asyncio
import csv
import pytest
from datetime import datetime, timedelta
from pathlib import Path

from meat_monitor import (
    parse_emax_payload,
    calculate_cooling_target,
    format_duration,
    Phase,
    PhaseTransition,
    PhaseMonitor,
    SessionLogger,
    THRESHOLD_START_COOLING,
    THRESHOLD_TO_FRIDGE,
    THRESHOLD_FRIDGE_SAFE,
    COOLING_TIME_LIMIT_HOURS,
    FRIDGE_TIME_LIMIT_HOURS,
    WARNING_MINUTES_REMAINING,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

T0 = datetime(2026, 5, 7, 18, 0, 0)


def mins(n: float) -> timedelta:
    return timedelta(minutes=n)


def hrs(n: float) -> timedelta:
    return timedelta(hours=n)


# ---------------------------------------------------------------------------
# parse_emax_payload
# ---------------------------------------------------------------------------

class TestParseEmaxPayload:
    def test_valid_payload_returns_temp(self):
        data = bytearray(b"OKZ023-UC-UG-ZIII\x00\x00\x00")
        result = parse_emax_payload(data)
        assert result is not None
        assert result["temp_c"] == 23

    def test_valid_payload_unit_celsius(self):
        data = bytearray(b"OKZ045-UC-UG-ZIII\x00\x00")
        result = parse_emax_payload(data)
        assert result["unit"] == "UC"

    def test_valid_payload_probes_field(self):
        data = bytearray(b"OKZ031-UC-UG-ZIII\x00")
        result = parse_emax_payload(data)
        assert result["probes"] == "ZIII"

    def test_valid_payload_raw_string_preserved(self):
        data = bytearray(b"OKZ022-UC-UG-ZIII\x00\x00")
        result = parse_emax_payload(data)
        assert result["raw"] == "OKZ022-UC-UG-ZIII"

    def test_wrong_prefix_returns_none(self):
        data = bytearray(b"XYZ022-UC-UG-ZIII\x00")
        assert parse_emax_payload(data) is None

    def test_truncated_packet_returns_none(self):
        # BLE can split packets mid-transmission
        data = bytearray(b"OKZ022-U")
        assert parse_emax_payload(data) is None

    def test_empty_payload_returns_none(self):
        assert parse_emax_payload(bytearray()) is None

    def test_non_numeric_temp_returns_none(self):
        data = bytearray(b"OKZabc-UC-UG-ZIII\x00")
        assert parse_emax_payload(data) is None

    def test_three_digit_temp_100c(self):
        data = bytearray(b"OKZ100-UC-UG-ZIII\x00")
        result = parse_emax_payload(data)
        assert result["temp_c"] == 100

    def test_null_padded_to_20_bytes(self):
        data = bytearray(b"OKZ060-UC-UG-ZIII\x00\x00\x00")
        result = parse_emax_payload(data)
        assert result["temp_c"] == 60


# ---------------------------------------------------------------------------
# calculate_cooling_target
# ---------------------------------------------------------------------------

class TestCalculateCoolingTarget:
    def test_no_ambient_returns_21(self):
        assert calculate_cooling_target(None) == 21.0

    def test_ambient_above_21_returns_21(self):
        assert calculate_cooling_target(25.0) == 21.0

    def test_ambient_below_21_returns_ambient(self):
        assert calculate_cooling_target(18.0) == 18.0

    def test_ambient_exactly_21_returns_21(self):
        assert calculate_cooling_target(21.0) == 21.0

    def test_cold_ambient_returns_ambient(self):
        # e.g. a cold garage
        assert calculate_cooling_target(12.0) == 12.0


# ---------------------------------------------------------------------------
# format_duration
# ---------------------------------------------------------------------------

class TestFormatDuration:
    def test_zero_seconds(self):
        assert format_duration(0) == "0:00:00"

    def test_90_seconds(self):
        assert format_duration(90) == "0:01:30"

    def test_one_hour(self):
        assert format_duration(3600) == "1:00:00"

    def test_two_hours_minus_one_second(self):
        assert format_duration(7199) == "1:59:59"

    def test_fractional_seconds_truncated(self):
        assert format_duration(61.9) == "0:01:01"


# ---------------------------------------------------------------------------
# PhaseMonitor — initial state
# ---------------------------------------------------------------------------

class TestPhaseMonitorInitial:
    def test_initial_phase_is_standby(self):
        pm = PhaseMonitor()
        assert pm.phase == Phase.STANDBY

    def test_no_timer_in_standby(self):
        pm = PhaseMonitor()
        assert pm.get_timer_status(T0) is None

    def test_temperature_readings_ignored_in_standby(self):
        pm = PhaseMonitor()
        transitions = pm.on_temperature(22.0, None, T0)
        assert transitions == []
        assert pm.phase == Phase.STANDBY


# ---------------------------------------------------------------------------
# PhaseMonitor — OFF_HEAT transition
# ---------------------------------------------------------------------------

class TestPhaseMonitorOffHeat:
    def test_start_off_heat_from_standby(self):
        pm = PhaseMonitor()
        t = pm.start_off_heat(T0)
        assert pm.phase == Phase.OFF_HEAT
        assert t.to_phase == Phase.OFF_HEAT

    def test_start_off_heat_not_allowed_from_other_phases(self):
        pm = PhaseMonitor()
        pm.start_off_heat(T0)
        pm.start_off_heat(T0)  # second call
        assert pm.phase == Phase.OFF_HEAT  # still OFF_HEAT, not double-transitioned

    def test_start_off_heat_returns_transition(self):
        pm = PhaseMonitor()
        t = pm.start_off_heat(T0)
        assert isinstance(t, PhaseTransition)
        assert t.from_phase == Phase.STANDBY
        assert t.to_phase == Phase.OFF_HEAT

    def test_no_timer_in_off_heat(self):
        pm = PhaseMonitor()
        pm.start_off_heat(T0)
        assert pm.get_timer_status(T0) is None


# ---------------------------------------------------------------------------
# PhaseMonitor — COOLING phase (60°C trigger)
# ---------------------------------------------------------------------------

class TestPhaseMonitorCooling:
    def _pm_at_off_heat(self):
        pm = PhaseMonitor()
        pm.start_off_heat(T0)
        return pm

    def test_temp_above_60_in_off_heat_no_transition(self):
        pm = self._pm_at_off_heat()
        transitions = pm.on_temperature(65.0, None, T0)
        assert transitions == []
        assert pm.phase == Phase.OFF_HEAT

    def test_temp_exactly_60_triggers_cooling(self):
        pm = self._pm_at_off_heat()
        transitions = pm.on_temperature(60.0, None, T0)
        assert any(t.to_phase == Phase.COOLING for t in transitions)
        assert pm.phase == Phase.COOLING

    def test_temp_below_60_triggers_cooling(self):
        pm = self._pm_at_off_heat()
        transitions = pm.on_temperature(55.0, None, T0)
        assert any(t.to_phase == Phase.COOLING for t in transitions)

    def test_already_below_60_when_off_heat_starts(self):
        # If food is already below 60°C when taken off heat, first reading starts timer
        pm = self._pm_at_off_heat()
        transitions = pm.on_temperature(45.0, None, T0)
        assert pm.phase == Phase.COOLING

    def test_cooling_timer_starts_when_phase_begins(self):
        pm = self._pm_at_off_heat()
        pm.on_temperature(60.0, None, T0)
        status = pm.get_timer_status(T0 + mins(30))
        assert status is not None
        assert status.phase == Phase.COOLING

    def test_cooling_timer_elapsed(self):
        pm = self._pm_at_off_heat()
        pm.on_temperature(60.0, None, T0)
        status = pm.get_timer_status(T0 + mins(45))
        assert abs(status.elapsed_seconds - 45 * 60) < 1

    def test_cooling_timer_remaining(self):
        pm = self._pm_at_off_heat()
        pm.on_temperature(60.0, None, T0)
        status = pm.get_timer_status(T0 + mins(90))
        expected_remaining = (COOLING_TIME_LIMIT_HOURS * 3600) - (90 * 60)
        assert abs(status.remaining_seconds - expected_remaining) < 1

    def test_cooling_warning_at_30_min_remaining(self):
        pm = self._pm_at_off_heat()
        pm.on_temperature(60.0, None, T0)
        warn_time = T0 + hrs(COOLING_TIME_LIMIT_HOURS) - mins(WARNING_MINUTES_REMAINING)
        status = pm.get_timer_status(warn_time)
        assert status.warn is True

    def test_no_cooling_warning_with_plenty_of_time(self):
        pm = self._pm_at_off_heat()
        pm.on_temperature(60.0, None, T0)
        status = pm.get_timer_status(T0 + mins(10))
        assert status.warn is False

    def test_cooling_timer_not_expired_before_limit(self):
        pm = self._pm_at_off_heat()
        pm.on_temperature(60.0, None, T0)
        status = pm.get_timer_status(T0 + hrs(COOLING_TIME_LIMIT_HOURS) - timedelta(seconds=1))
        assert status.is_expired is False

    def test_cooling_timer_expired_at_limit(self):
        pm = self._pm_at_off_heat()
        pm.on_temperature(60.0, None, T0)
        # Advance 1 second past the limit
        too_late = T0 + hrs(COOLING_TIME_LIMIT_HOURS) + timedelta(seconds=1)
        transitions = pm.on_temperature(35.0, None, too_late)
        assert any(t.to_phase == Phase.FAILED for t in transitions)
        assert pm.phase == Phase.FAILED

    def test_failed_phase_has_no_timer(self):
        pm = self._pm_at_off_heat()
        pm.on_temperature(60.0, None, T0)
        too_late = T0 + hrs(COOLING_TIME_LIMIT_HOURS) + timedelta(seconds=1)
        pm.on_temperature(35.0, None, too_late)
        assert pm.get_timer_status(too_late) is None


# ---------------------------------------------------------------------------
# PhaseMonitor — FRIDGE phase (21°C trigger)
# ---------------------------------------------------------------------------

class TestPhaseMonitorFridge:
    def _pm_in_cooling(self, ambient=None):
        pm = PhaseMonitor()
        pm.start_off_heat(T0)
        pm.on_temperature(60.0, ambient, T0)
        return pm

    def test_temp_above_21_in_cooling_no_transition(self):
        pm = self._pm_in_cooling()
        transitions = pm.on_temperature(25.0, None, T0 + mins(30))
        assert not any(t.to_phase == Phase.FRIDGE for t in transitions)

    def test_temp_exactly_21_triggers_fridge(self):
        pm = self._pm_in_cooling()
        transitions = pm.on_temperature(21.0, None, T0 + mins(30))
        assert any(t.to_phase == Phase.FRIDGE for t in transitions)
        assert pm.phase == Phase.FRIDGE

    def test_temp_below_21_triggers_fridge(self):
        pm = self._pm_in_cooling()
        transitions = pm.on_temperature(19.0, None, T0 + mins(30))
        assert any(t.to_phase == Phase.FRIDGE for t in transitions)

    def test_ambient_below_21_used_as_target(self):
        pm = self._pm_in_cooling(ambient=18.0)
        # At 20°C food is not yet at the 18°C ambient target
        transitions = pm.on_temperature(20.0, 18.0, T0 + mins(30))
        assert not any(t.to_phase == Phase.FRIDGE for t in transitions)
        # At 18°C it is
        transitions = pm.on_temperature(18.0, 18.0, T0 + mins(45))
        assert any(t.to_phase == Phase.FRIDGE for t in transitions)

    def test_fridge_timer_starts_at_transition(self):
        pm = self._pm_in_cooling()
        fridge_time = T0 + mins(60)
        pm.on_temperature(21.0, None, fridge_time)
        status = pm.get_timer_status(fridge_time + mins(30))
        assert status is not None
        assert status.phase == Phase.FRIDGE

    def test_fridge_timer_remaining(self):
        pm = self._pm_in_cooling()
        fridge_time = T0 + mins(60)
        pm.on_temperature(21.0, None, fridge_time)
        status = pm.get_timer_status(fridge_time + hrs(2))
        expected = FRIDGE_TIME_LIMIT_HOURS * 3600 - 2 * 3600
        assert abs(status.remaining_seconds - expected) < 1

    def test_fridge_timer_warning(self):
        pm = self._pm_in_cooling()
        fridge_time = T0 + mins(60)
        pm.on_temperature(21.0, None, fridge_time)
        warn_time = fridge_time + hrs(FRIDGE_TIME_LIMIT_HOURS) - mins(WARNING_MINUTES_REMAINING)
        status = pm.get_timer_status(warn_time)
        assert status.warn is True

    def test_fridge_timer_expired_causes_failure(self):
        pm = self._pm_in_cooling()
        fridge_time = T0 + mins(60)
        pm.on_temperature(21.0, None, fridge_time)
        too_late = fridge_time + hrs(FRIDGE_TIME_LIMIT_HOURS) + timedelta(seconds=1)
        transitions = pm.on_temperature(8.0, None, too_late)
        assert any(t.to_phase == Phase.FAILED for t in transitions)

    def test_temp_at_5_triggers_complete(self):
        pm = self._pm_in_cooling()
        pm.on_temperature(21.0, None, T0 + mins(60))
        transitions = pm.on_temperature(5.0, None, T0 + hrs(3))
        assert any(t.to_phase == Phase.COMPLETE for t in transitions)
        assert pm.phase == Phase.COMPLETE

    def test_temp_below_5_triggers_complete(self):
        pm = self._pm_in_cooling()
        pm.on_temperature(21.0, None, T0 + mins(60))
        transitions = pm.on_temperature(3.0, None, T0 + hrs(3))
        assert any(t.to_phase == Phase.COMPLETE for t in transitions)

    def test_complete_phase_has_no_timer(self):
        pm = self._pm_in_cooling()
        pm.on_temperature(21.0, None, T0 + mins(60))
        done_time = T0 + hrs(3)
        pm.on_temperature(5.0, None, done_time)
        assert pm.get_timer_status(done_time) is None

    def test_no_further_transitions_after_complete(self):
        pm = self._pm_in_cooling()
        pm.on_temperature(21.0, None, T0 + mins(60))
        pm.on_temperature(5.0, None, T0 + hrs(3))
        transitions = pm.on_temperature(2.0, None, T0 + hrs(4))
        assert transitions == []


# ---------------------------------------------------------------------------
# PhaseMonitor — full happy path
# ---------------------------------------------------------------------------

class TestPhaseMonitorHappyPath:
    def test_full_cooling_cycle_passes(self):
        pm = PhaseMonitor()
        pm.start_off_heat(T0)

        # Drops through 60°C at +5 min
        pm.on_temperature(60.0, None, T0 + mins(5))
        assert pm.phase == Phase.COOLING

        # Reaches 21°C at +90 min (within 2hr limit)
        pm.on_temperature(21.0, None, T0 + mins(90))
        assert pm.phase == Phase.FRIDGE

        # Reaches 5°C at +90 + 180 min = +270 min (within 4hr fridge limit)
        pm.on_temperature(5.0, None, T0 + mins(270))
        assert pm.phase == Phase.COMPLETE


# ---------------------------------------------------------------------------
# SessionLogger
# ---------------------------------------------------------------------------

class TestSessionLogger:
    def test_creates_csv_file(self, tmp_path):
        logger = SessionLogger("test_session", tmp_path)
        logger.close()
        files = list(tmp_path.glob("*.csv"))
        assert len(files) == 1

    def test_csv_filename_contains_session_name(self, tmp_path):
        logger = SessionLogger("chicken_roast", tmp_path)
        logger.close()
        files = list(tmp_path.glob("*.csv"))
        assert "chicken_roast" in files[0].name

    def test_csv_has_required_headers(self, tmp_path):
        logger = SessionLogger("test", tmp_path)
        logger.close()
        with open(list(tmp_path.glob("*.csv"))[0]) as f:
            reader = csv.DictReader(f)
            headers = reader.fieldnames
        for col in ["timestamp", "phase", "temp_c", "temp_f", "ambient_c", "elapsed_session", "event"]:
            assert col in headers

    def test_log_reading_writes_row(self, tmp_path):
        logger = SessionLogger("test", tmp_path)
        logger.log_reading(
            now=T0,
            phase=Phase.COOLING,
            temp_c=45,
            ambient_c=None,
            session_start=T0 - mins(10),
            timer_status=None,
            event=None,
        )
        logger.close()
        with open(list(tmp_path.glob("*.csv"))[0]) as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 1
        assert int(rows[0]["temp_c"]) == 45

    def test_log_reading_converts_temp_to_fahrenheit(self, tmp_path):
        logger = SessionLogger("test", tmp_path)
        logger.log_reading(
            now=T0,
            phase=Phase.COOLING,
            temp_c=100,
            ambient_c=None,
            session_start=T0,
            timer_status=None,
            event=None,
        )
        logger.close()
        with open(list(tmp_path.glob("*.csv"))[0]) as f:
            rows = list(csv.DictReader(f))
        assert float(rows[0]["temp_f"]) == pytest.approx(212.0)

    def test_log_reading_records_elapsed_time(self, tmp_path):
        logger = SessionLogger("test", tmp_path)
        logger.log_reading(
            now=T0 + mins(30),
            phase=Phase.COOLING,
            temp_c=40,
            ambient_c=None,
            session_start=T0,
            timer_status=None,
            event=None,
        )
        logger.close()
        with open(list(tmp_path.glob("*.csv"))[0]) as f:
            rows = list(csv.DictReader(f))
        assert float(rows[0]["elapsed_session"]) == pytest.approx(1800.0)

    def test_log_reading_records_event(self, tmp_path):
        logger = SessionLogger("test", tmp_path)
        logger.log_reading(
            now=T0,
            phase=Phase.COOLING,
            temp_c=60,
            ambient_c=None,
            session_start=T0,
            timer_status=None,
            event="COOLING timer started",
        )
        logger.close()
        with open(list(tmp_path.glob("*.csv"))[0]) as f:
            rows = list(csv.DictReader(f))
        assert rows[0]["event"] == "COOLING timer started"

    def test_log_reading_records_ambient(self, tmp_path):
        logger = SessionLogger("test", tmp_path)
        logger.log_reading(
            now=T0,
            phase=Phase.FRIDGE,
            temp_c=15,
            ambient_c=20.5,
            session_start=T0,
            timer_status=None,
            event=None,
        )
        logger.close()
        with open(list(tmp_path.glob("*.csv"))[0]) as f:
            rows = list(csv.DictReader(f))
        assert float(rows[0]["ambient_c"]) == pytest.approx(20.5)

    def test_multiple_readings_all_written(self, tmp_path):
        logger = SessionLogger("test", tmp_path)
        for i in range(5):
            logger.log_reading(
                now=T0 + timedelta(seconds=i * 2),
                phase=Phase.COOLING,
                temp_c=50 - i,
                ambient_c=None,
                session_start=T0,
                timer_status=None,
                event=None,
            )
        logger.close()
        with open(list(tmp_path.glob("*.csv"))[0]) as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 5


# ---------------------------------------------------------------------------
# Bluetooth integration tests
# Run with:  pytest test_meat_monitor.py -v -m bluetooth
# ---------------------------------------------------------------------------

try:
    from bleak import BleakScanner, BleakClient
    _BLEAK_AVAILABLE = True
except ImportError:
    _BLEAK_AVAILABLE = False

from meat_monitor import EMAX_ADDRESS, EMAX_WRITE_CHAR, EMAX_NOTIFY_CHAR, EMAX_START_CMD

bluetooth = pytest.mark.skipif(
    not _BLEAK_AVAILABLE,
    reason="bleak not installed — BLE tests require the Pi",
)

SCAN_TIMEOUT    = 15.0  # seconds to scan for the device
CONNECT_TIMEOUT = 15.0  # seconds to wait for a BLE connection
READING_TIMEOUT = 10.0  # seconds to wait for the first temperature notification
BLE_SETTLE_SECS =  2.0  # wait between scan and connect so the BLE stack is free


@pytest.fixture(scope="module")
async def emax_scan():
    """Scan once for the EMAX and share results across all scan-dependent tests.
    Skips the entire module if the device is not found."""
    if not _BLEAK_AVAILABLE:
        pytest.skip("bleak not installed")
    devices = await BleakScanner.discover(timeout=SCAN_TIMEOUT, return_adv=True)
    target = EMAX_ADDRESS.upper()
    match = next((v for k, v in devices.items() if k.upper() == target), None)
    if match is None:
        pytest.skip(f"EMAX {EMAX_ADDRESS} not found in scan — is it powered on?")
    return match  # (BLEDevice, AdvertisementData)


@bluetooth
@pytest.mark.bluetooth
class TestEMAXBluetooth:
    async def test_emax_is_discoverable(self, emax_scan):
        """EMAX appears in a BLE scan by its known address."""
        device, _ = emax_scan
        assert device.address.upper() == EMAX_ADDRESS.upper()

    async def test_emax_advertises_expected_service(self, emax_scan):
        """EMAX advertisement includes the ffb0 vendor service UUID."""
        _, adv = emax_scan
        service_uuids = [str(u).lower() for u in adv.service_uuids]
        assert "0000ffb0-0000-1000-8000-00805f9b34fb" in service_uuids

    async def test_emax_connects(self, emax_scan):
        """BleakClient connects to the EMAX without error."""
        await asyncio.sleep(BLE_SETTLE_SECS)  # let BLE stack settle after scan
        try:
            async with BleakClient(EMAX_ADDRESS, timeout=CONNECT_TIMEOUT) as client:
                assert client.is_connected
        except Exception as e:
            pytest.fail(f"Could not connect to EMAX: {e}")

    async def test_emax_has_expected_characteristics(self):
        """Connected EMAX exposes ffb1 (write+notify) and ffb2 (notify) characteristics."""
        try:
            async with BleakClient(EMAX_ADDRESS, timeout=CONNECT_TIMEOUT) as client:
                uuids = {str(c.uuid).lower() for s in client.services for c in s.characteristics}
        except Exception as e:
            pytest.skip(f"Could not connect to EMAX: {e}")

        assert EMAX_WRITE_CHAR  in uuids, f"ffb1 write char not found"
        assert EMAX_NOTIFY_CHAR in uuids, f"ffb2 notify char not found"

    async def test_emax_streams_temperature(self):
        """After sending start command, EMAX delivers a parseable temperature reading."""
        received: list[dict] = []

        def on_notify(sender, data: bytearray):
            parsed = parse_emax_payload(data)
            if parsed:
                received.append(parsed)

        try:
            async with BleakClient(EMAX_ADDRESS, timeout=CONNECT_TIMEOUT) as client:
                await client.start_notify(EMAX_NOTIFY_CHAR, on_notify)
                await client.write_gatt_char(EMAX_WRITE_CHAR, EMAX_START_CMD, response=False)
                await asyncio.sleep(READING_TIMEOUT)
                await client.stop_notify(EMAX_NOTIFY_CHAR)
        except Exception as e:
            pytest.skip(f"Could not connect to EMAX: {e}")

        assert len(received) > 0, "No temperature notifications received"

    async def test_emax_temperature_in_plausible_range(self):
        """Temperature reading from EMAX is within a plausible range (0–150°C)."""
        received: list[dict] = []

        def on_notify(sender, data: bytearray):
            parsed = parse_emax_payload(data)
            if parsed:
                received.append(parsed)

        try:
            async with BleakClient(EMAX_ADDRESS, timeout=CONNECT_TIMEOUT) as client:
                await client.start_notify(EMAX_NOTIFY_CHAR, on_notify)
                await client.write_gatt_char(EMAX_WRITE_CHAR, EMAX_START_CMD, response=False)
                await asyncio.sleep(READING_TIMEOUT)
                await client.stop_notify(EMAX_NOTIFY_CHAR)
        except Exception as e:
            pytest.skip(f"Could not connect to EMAX: {e}")

        assert len(received) > 0, "No temperature notifications received"
        for reading in received:
            temp = reading["temp_c"]
            assert 0 <= temp <= 150, f"Temperature {temp}°C outside plausible range"
