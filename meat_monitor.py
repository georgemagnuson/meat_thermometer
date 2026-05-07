"""
meat_monitor.py — Food safety temperature monitoring
EMAX Smart I45778 Bluetooth thermometer + DHT-11 ambient sensor (stub)

Food safety cooling requirements (HACCP):
  Phase 1 (COOLING): food must drop from 60°C to 21°C in ≤ 2 hours
  Phase 2 (FRIDGE):  food must drop from 21°C to  5°C in ≤ 4 hours

Usage:
  sudo python3 meat_monitor.py

Commands (type + Enter while running):
  h  — food is now off heat  (start monitoring)
  f  — food is now in fridge (manual override)
  q  — quit and save session log
"""

import asyncio
import csv
import signal
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from pathlib import Path
from typing import Callable, NamedTuple

try:
    from bleak import BleakClient
except ImportError:
    BleakClient = None  # allows unit tests to run without BLE hardware

# ---------------------------------------------------------------------------
# EMAX thermometer constants
# ---------------------------------------------------------------------------

EMAX_ADDRESS     = "18:93:D7:1C:C9:0B"
EMAX_WRITE_CHAR  = "0000ffb1-0000-1000-8000-00805f9b34fb"
EMAX_NOTIFY_CHAR = "0000ffb2-0000-1000-8000-00805f9b34fb"
EMAX_START_CMD   = b'\x0b\x01'

# ---------------------------------------------------------------------------
# Food safety thresholds
# ---------------------------------------------------------------------------

THRESHOLD_START_COOLING  = 60   # °C — start 2-hour cooling timer
THRESHOLD_TO_FRIDGE      = 21   # °C — move to fridge (or ambient if cooler)
THRESHOLD_FRIDGE_SAFE    =  5   # °C — safe, monitoring complete

COOLING_TIME_LIMIT_HOURS =  2
FRIDGE_TIME_LIMIT_HOURS  =  4
WARNING_MINUTES_REMAINING = 30  # alert when this many minutes remain on a timer

# ---------------------------------------------------------------------------
# Sessions directory
# ---------------------------------------------------------------------------

SESSIONS_DIR = Path(__file__).parent / "sessions"

# ---------------------------------------------------------------------------
# DHT-11 ambient temperature (STUB)
# ---------------------------------------------------------------------------
# TODO: When DHT-11 sensor is wired to the Pi:
#   1. Install library:  pip install adafruit-circuitpython-dht
#   2. Set DHT11_GPIO_PIN to the correct GPIO pin number (e.g. 4 for GPIO4)
#   3. Uncomment the block below and remove the stub function.
#
# DHT11_GPIO_PIN = 4   # TODO: confirm GPIO pin with physical wiring
#
# import board
# import adafruit_dht
# _dht = adafruit_dht.DHT11(getattr(board, f"D{DHT11_GPIO_PIN}"))
#
# def get_ambient_temp() -> float | None:
#     try:
#         return float(_dht.temperature)
#     except RuntimeError:
#         return None   # DHT-11 occasionally fails one read — caller retries

def get_ambient_temp() -> float | None:
    """Stub: returns None until DHT-11 is physically connected."""
    # TODO: replace with real DHT-11 reading (see comments above)
    return None

# ---------------------------------------------------------------------------
# EMAX BT alarm (EXPERIMENTAL — protocol unconfirmed)
# ---------------------------------------------------------------------------
# TODO: Capture BLE traffic from the phone app using nRF Connect or a BLE
#   proxy to find the exact alarm-set command bytes.
#   Key question: does the EMAX support decreasing-temp alarms (needed for
#   cooling) or only increasing-temp alarms (for cooking)?
#
# async def set_emax_alarm(client: BleakClient, target_temp_c: int) -> None:
#     candidates = [
#         bytes([0x0C, target_temp_c]),
#         bytes([0x05, target_temp_c, 0x00]),
#         bytes([0x01, 0x01, target_temp_c & 0xFF, (target_temp_c * 10) >> 8]),
#     ]
#     for cmd in candidates:
#         await client.write_gatt_char(EMAX_WRITE_CHAR, cmd, response=False)
#         await asyncio.sleep(0.5)

# ---------------------------------------------------------------------------
# Cooking phase targets (STUB — food type not yet configured)
# ---------------------------------------------------------------------------
# TODO: Add target internal temperatures and hold times per food type.
# USDA safe internal temps:
#   Poultry (whole / ground):  74°C (165°F) — no hold time
#   Ground beef / pork:        71°C (160°F) — no hold time
#   Whole beef / pork / lamb:  63°C (145°F) + 3 min rest
#   Fish / shellfish:          63°C (145°F)
#   Eggs (fully cooked):       72°C (160°F)
#
# COOKING_TARGET_TEMP_C    = None   # e.g. 74
# COOKING_TARGET_HOLD_SECS = None   # e.g. 0  (or 180 for 3-min rest)

# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------

def decode_ascii_safe(data: bytearray) -> str:
    try:
        return data.rstrip(b'\x00').decode("ascii")
    except Exception:
        return ""


def parse_emax_payload(data: bytearray) -> dict | None:
    """Parse EMAX notification. Format: OKZ{temp3}-{unit}-{flag}-{probes}"""
    s = decode_ascii_safe(data)
    if not s.startswith("OKZ"):
        return None
    try:
        parts = s.split("-")
        if len(parts) < 4:          # truncated packet — discard
            return None
        temp_c = int(parts[0][3:])
        unit   = parts[1]
        probes = parts[3]
        return {"temp_c": temp_c, "unit": unit, "probes": probes, "raw": s}
    except (ValueError, IndexError):
        return None


def calculate_cooling_target(ambient_c: float | None) -> float:
    """Return the lower of 21°C and ambient — whichever is cooler."""
    if ambient_c is None:
        return float(THRESHOLD_TO_FRIDGE)
    return min(float(THRESHOLD_TO_FRIDGE), ambient_c)


def format_duration(seconds: float) -> str:
    """Format a duration in seconds as H:MM:SS."""
    total = int(seconds)
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h}:{m:02d}:{s:02d}"

# ---------------------------------------------------------------------------
# Phase state machine
# ---------------------------------------------------------------------------

class Phase(Enum):
    STANDBY  = auto()
    COOKING  = auto()   # optional cook-phase monitoring (see TODO above)
    OFF_HEAT = auto()   # food off heat, watching for 60°C on the way down
    COOLING  = auto()   # 60°C → 21°C, 2-hour timer active
    FRIDGE   = auto()   # 21°C → 5°C, 4-hour timer active
    COMPLETE = auto()   # ≤5°C reached within time limits — PASS
    FAILED   = auto()   # timer expired before target — FAIL


PHASE_LABELS = {
    Phase.STANDBY:  "STANDBY   — waiting to start",
    Phase.COOKING:  "COOKING   — monitoring cook temperature",
    Phase.OFF_HEAT: "OFF HEAT  — waiting for 60°C",
    Phase.COOLING:  f"COOLING   — 60°C → 21°C  ({COOLING_TIME_LIMIT_HOURS}hr limit)",
    Phase.FRIDGE:   f"IN FRIDGE — 21°C → 5°C   ({FRIDGE_TIME_LIMIT_HOURS}hr limit)",
    Phase.COMPLETE: "COMPLETE  ✓ — food safely cooled",
    Phase.FAILED:   "FAILED    ✗ — timer expired before target temperature",
}


class PhaseTransition(NamedTuple):
    from_phase: Phase
    to_phase:   Phase
    reason:     str
    temp_c:     float
    timestamp:  datetime


@dataclass
class TimerStatus:
    phase:             Phase
    started_at:        datetime
    limit_seconds:     float
    elapsed_seconds:   float
    remaining_seconds: float
    is_expired:        bool
    warn:              bool


class PhaseMonitor:
    def __init__(self):
        self.phase = Phase.STANDBY
        self._cooling_started_at: datetime | None = None
        self._fridge_started_at:  datetime | None = None

    def start_off_heat(self, now: datetime) -> PhaseTransition | None:
        if self.phase != Phase.STANDBY:
            return None
        old = self.phase
        self.phase = Phase.OFF_HEAT
        return PhaseTransition(old, Phase.OFF_HEAT, "manual: food off heat", 0.0, now)

    def start_fridge(self, temp_c: float, now: datetime) -> PhaseTransition | None:
        """Manual override to enter FRIDGE phase."""
        if self.phase not in (Phase.COOLING, Phase.OFF_HEAT):
            return None
        old = self.phase
        self._fridge_started_at = now
        self.phase = Phase.FRIDGE
        return PhaseTransition(old, Phase.FRIDGE, "manual: food in fridge", temp_c, now)

    def on_temperature(
        self,
        temp_c: float,
        ambient_c: float | None,
        now: datetime,
    ) -> list[PhaseTransition]:
        transitions: list[PhaseTransition] = []

        if self.phase == Phase.STANDBY:
            return transitions

        cooling_target = calculate_cooling_target(ambient_c)

        if self.phase == Phase.OFF_HEAT:
            if temp_c <= THRESHOLD_START_COOLING:
                self._cooling_started_at = now
                self.phase = Phase.COOLING
                transitions.append(PhaseTransition(
                    Phase.OFF_HEAT, Phase.COOLING,
                    f"temp ≤ {THRESHOLD_START_COOLING}°C — cooling timer started",
                    temp_c, now,
                ))

        if self.phase == Phase.COOLING:
            elapsed = (now - self._cooling_started_at).total_seconds()
            limit   = COOLING_TIME_LIMIT_HOURS * 3600
            if elapsed > limit:
                self.phase = Phase.FAILED
                transitions.append(PhaseTransition(
                    Phase.COOLING, Phase.FAILED,
                    f"cooling timer expired ({format_duration(elapsed)} elapsed)",
                    temp_c, now,
                ))
            elif temp_c <= cooling_target:
                self._fridge_started_at = now
                self.phase = Phase.FRIDGE
                transitions.append(PhaseTransition(
                    Phase.COOLING, Phase.FRIDGE,
                    f"temp ≤ {cooling_target}°C — place food in fridge",
                    temp_c, now,
                ))

        if self.phase == Phase.FRIDGE:
            elapsed = (now - self._fridge_started_at).total_seconds()
            limit   = FRIDGE_TIME_LIMIT_HOURS * 3600
            if elapsed > limit:
                self.phase = Phase.FAILED
                transitions.append(PhaseTransition(
                    Phase.FRIDGE, Phase.FAILED,
                    f"fridge timer expired ({format_duration(elapsed)} elapsed)",
                    temp_c, now,
                ))
            elif temp_c <= THRESHOLD_FRIDGE_SAFE:
                self.phase = Phase.COMPLETE
                transitions.append(PhaseTransition(
                    Phase.FRIDGE, Phase.COMPLETE,
                    f"temp ≤ {THRESHOLD_FRIDGE_SAFE}°C — food safely cooled",
                    temp_c, now,
                ))

        return transitions

    def get_timer_status(self, now: datetime) -> TimerStatus | None:
        if self.phase == Phase.COOLING and self._cooling_started_at:
            return self._make_status(Phase.COOLING, self._cooling_started_at,
                                     COOLING_TIME_LIMIT_HOURS * 3600, now)
        if self.phase == Phase.FRIDGE and self._fridge_started_at:
            return self._make_status(Phase.FRIDGE, self._fridge_started_at,
                                     FRIDGE_TIME_LIMIT_HOURS * 3600, now)
        return None

    def _make_status(self, phase, started_at, limit_seconds, now) -> TimerStatus:
        elapsed   = (now - started_at).total_seconds()
        remaining = limit_seconds - elapsed
        return TimerStatus(
            phase=phase,
            started_at=started_at,
            limit_seconds=limit_seconds,
            elapsed_seconds=elapsed,
            remaining_seconds=remaining,
            is_expired=remaining <= 0,
            warn=0 < remaining <= WARNING_MINUTES_REMAINING * 60,
        )

# ---------------------------------------------------------------------------
# Session logger
# ---------------------------------------------------------------------------

class SessionLogger:
    COLUMNS = [
        "timestamp", "phase", "temp_c", "temp_f",
        "ambient_c", "elapsed_session", "timer_elapsed",
        "timer_remaining", "event",
    ]

    def __init__(self, session_name: str, sessions_dir: Path):
        sessions_dir.mkdir(parents=True, exist_ok=True)
        date_str  = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._path = sessions_dir / f"{session_name}_{date_str}.csv"
        self._file = open(self._path, "w", newline="")
        self._writer = csv.DictWriter(self._file, fieldnames=self.COLUMNS)
        self._writer.writeheader()

    @property
    def path(self) -> Path:
        return self._path

    def log_reading(
        self,
        now: datetime,
        phase: Phase,
        temp_c: int,
        ambient_c: float | None,
        session_start: datetime,
        timer_status: TimerStatus | None,
        event: str | None,
    ) -> None:
        self._writer.writerow({
            "timestamp":       now.isoformat(timespec="seconds"),
            "phase":           phase.name,
            "temp_c":          temp_c,
            "temp_f":          f"{temp_c * 9 / 5 + 32:.1f}",
            "ambient_c":       "" if ambient_c is None else f"{ambient_c:.1f}",
            "elapsed_session": f"{(now - session_start).total_seconds():.0f}",
            "timer_elapsed":   "" if timer_status is None else f"{timer_status.elapsed_seconds:.0f}",
            "timer_remaining": "" if timer_status is None else f"{timer_status.remaining_seconds:.0f}",
            "event":           event or "",
        })
        self._file.flush()

    def close(self) -> None:
        self._file.close()

# ---------------------------------------------------------------------------
# EMAX BLE reader
# ---------------------------------------------------------------------------

class EMAXReader:
    def __init__(self, address: str = EMAX_ADDRESS):
        self._address = address
        self._stop    = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def stream(self, on_reading: Callable[[int], None]) -> None:
        print(f"Connecting to EMAX Smart {self._address} ...")
        async with BleakClient(self._address) as client:
            print(f"Connected.\n")

            def on_notify(sender, data: bytearray):
                parsed = parse_emax_payload(data)
                if parsed:
                    on_reading(parsed["temp_c"])

            await client.start_notify(EMAX_NOTIFY_CHAR, on_notify)
            await client.write_gatt_char(EMAX_WRITE_CHAR, EMAX_START_CMD, response=False)
            await self._stop.wait()
            await client.stop_notify(EMAX_NOTIFY_CHAR)

# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

BANNER = "=" * 60

def print_banner(text: str) -> None:
    print(f"\n{BANNER}\n  {text}\n{BANNER}")

def print_transition(t: PhaseTransition) -> None:
    print_banner(f"PHASE → {t.to_phase.name}  |  {t.reason}")

def print_reading(
    now: datetime,
    phase: Phase,
    temp_c: int,
    ambient_c: float | None,
    timer: TimerStatus | None,
    events: list[str],
) -> None:
    temp_f   = temp_c * 9 / 5 + 32
    ts       = now.strftime("%H:%M:%S")
    amb_str  = f"  ambient:{ambient_c:.1f}°C" if ambient_c is not None else ""
    phase_str = phase.name

    if timer:
        remaining = format_duration(max(0, timer.remaining_seconds))
        warn_str  = "  *** TIME WARNING ***" if timer.warn else ""
        timer_str = f"  [{remaining} remaining{warn_str}]"
    else:
        timer_str = ""

    print(f"  {ts}  {phase_str:<9}  {temp_c:>3}°C ({temp_f:>5.1f}°F){amb_str}{timer_str}")

    for event in events:
        print(f"          ↳ {event}")

# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------

async def main() -> None:
    session_name = input("Session name (e.g. chicken_roast): ").strip() or "session"

    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    logger       = SessionLogger(session_name, SESSIONS_DIR)
    monitor      = PhaseMonitor()
    reader       = EMAXReader()
    session_start = datetime.now()
    last_temp: int | None = None

    print_banner(f"Session: {session_name}")
    print(f"  Log: {logger.path}")
    print(f"\n  Commands:  h = off heat   f = in fridge   q = quit\n")
    print(f"  {'Time':>8}  {'Phase':<9}  {'Temp':>8}          {'Timer'}")
    print(f"  {'-'*55}")

    def on_reading(temp_c: int) -> None:
        nonlocal last_temp
        last_temp = temp_c

    # Keyboard input in a background thread
    loop = asyncio.get_running_loop()
    cmd_queue: asyncio.Queue[str] = asyncio.Queue()

    def read_stdin():
        while True:
            try:
                line = sys.stdin.readline().strip().lower()
                if line:
                    loop.call_soon_threadsafe(cmd_queue.put_nowait, line)
            except Exception:
                break

    threading.Thread(target=read_stdin, daemon=True).start()

    # Start BLE in background
    ble_task = asyncio.create_task(reader.stream(on_reading))

    async def run_loop():
        nonlocal session_start
        while True:
            await asyncio.sleep(2)

            # Process keyboard commands
            while not cmd_queue.empty():
                cmd = await cmd_queue.get()
                now = datetime.now()
                if cmd == "q":
                    reader.stop()
                    return
                elif cmd == "h":
                    t = monitor.start_off_heat(now)
                    if t:
                        print_transition(t)
                        logger.log_reading(now, monitor.phase, last_temp or 0,
                                           get_ambient_temp(), session_start,
                                           monitor.get_timer_status(now),
                                           "manual: food off heat")
                elif cmd == "f":
                    t = monitor.start_fridge(last_temp or 0, now)
                    if t:
                        print_transition(t)
                        logger.log_reading(now, monitor.phase, last_temp or 0,
                                           get_ambient_temp(), session_start,
                                           monitor.get_timer_status(now),
                                           "manual: food in fridge")

            if last_temp is None:
                continue

            now       = datetime.now()
            ambient   = get_ambient_temp()
            timer     = monitor.get_timer_status(now)
            events: list[str] = []

            transitions = monitor.on_temperature(last_temp, ambient, now)
            for t in transitions:
                print_transition(t)
                events.append(t.reason)
                timer = monitor.get_timer_status(now)

            if timer and timer.warn and not transitions:
                events.append(f"WARNING: {format_duration(timer.remaining_seconds)} remaining")

            print_reading(now, monitor.phase, last_temp, ambient, timer, events)

            event_str = "; ".join(events) if events else None
            logger.log_reading(now, monitor.phase, last_temp, ambient,
                               session_start, timer, event_str)

            if monitor.phase in (Phase.COMPLETE, Phase.FAILED):
                reader.stop()
                return

    try:
        await run_loop()
    finally:
        ble_task.cancel()
        logger.close()
        print_banner(f"Session ended — log saved to {logger.path}")


if __name__ == "__main__":
    asyncio.run(main())
