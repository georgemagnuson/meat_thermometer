"""
meat_thermometer_v2.py — EMAX Smart I45778 direct reader
Connects straight to the known device, streams temperature until Ctrl-C.
Logs every reading to emax_log.csv alongside the live display.
"""

import asyncio
import csv
import signal
import sys
from datetime import datetime
from pathlib import Path
from bleak import BleakClient

EMAX_ADDRESS     = "18:93:D7:1C:C9:0B"
EMAX_WRITE_CHAR  = "0000ffb1-0000-1000-8000-00805f9b34fb"
EMAX_NOTIFY_CHAR = "0000ffb2-0000-1000-8000-00805f9b34fb"
EMAX_START_CMD   = b'\x0b\x01'

LOG_FILE = Path(__file__).parent / "emax_log.csv"


def decode_ascii_safe(data: bytearray) -> str:
    try:
        return data.rstrip(b'\x00').decode("ascii")
    except Exception:
        return ""


def parse_emax_payload(data: bytearray) -> dict | None:
    """Parse OKZ{temp}-{unit}-{flag}-{probes}  e.g. OKZ022-UC-UG-ZIII"""
    s = decode_ascii_safe(data)
    if not s.startswith("OKZ"):
        return None
    try:
        parts = s.split("-")
        temp_c = int(parts[0][3:])
        unit   = parts[1] if len(parts) > 1 else "UC"
        probes = parts[3] if len(parts) > 3 else ""
        return {"temp_c": temp_c, "unit": unit, "probes": probes, "raw": s}
    except (ValueError, IndexError):
        return None


class EMAXReader:
    def __init__(self):
        self._stop = asyncio.Event()
        self._prev_temp: int | None = None
        self._csv_writer = None
        self._csv_file = None
        self._reading_count = 0

    def open_log(self):
        self._csv_file = open(LOG_FILE, "w", newline="")
        self._csv_writer = csv.writer(self._csv_file)
        self._csv_writer.writerow(["timestamp", "temp_c", "temp_f", "delta_c", "probes", "raw"])
        print(f"Logging to {LOG_FILE}\n")

    def close_log(self):
        if self._csv_file:
            self._csv_file.close()

    def on_notify(self, sender, data: bytearray):
        parsed = parse_emax_payload(data)
        if not parsed:
            print(f"  [raw] {data.hex()}")
            return

        now      = datetime.now()
        temp_c   = parsed["temp_c"]
        temp_f   = temp_c * 9 / 5 + 32
        probes   = parsed["probes"]
        delta    = temp_c - self._prev_temp if self._prev_temp is not None else 0
        self._prev_temp = temp_c
        self._reading_count += 1

        # Delta indicator
        if delta > 0:
            arrow = f"  ▲+{delta}"
        elif delta < 0:
            arrow = f"  ▼{delta}"
        else:
            arrow = ""

        ts = now.strftime("%H:%M:%S")
        print(f"  {ts}  {temp_c:>3d} °C  ({temp_f:>6.1f} °F)  probes:{probes}{arrow}")

        if self._csv_writer:
            self._csv_writer.writerow([
                now.isoformat(timespec="seconds"),
                temp_c, f"{temp_f:.1f}", delta, probes, parsed["raw"]
            ])
            self._csv_file.flush()

    def stop(self):
        self._stop.set()

    async def run(self):
        self.open_log()

        print(f"Connecting to EMAX Smart {EMAX_ADDRESS} ...")
        async with BleakClient(EMAX_ADDRESS) as client:
            print(f"Connected. Reading temperature — press Ctrl-C to stop.\n")
            print(f"  {'Time':>8}  {'°C':>3}       {'°F':>6}    probes")
            print(f"  {'-'*50}")

            await client.start_notify(EMAX_NOTIFY_CHAR, self.on_notify)
            await client.write_gatt_char(EMAX_WRITE_CHAR, EMAX_START_CMD, response=False)

            await self._stop.wait()

            await client.stop_notify(EMAX_NOTIFY_CHAR)

        self.close_log()
        print(f"\n  {'-'*50}")
        print(f"  Stopped. {self._reading_count} readings recorded to {LOG_FILE}")


async def main():
    reader = EMAXReader()

    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT,  reader.stop)
    loop.add_signal_handler(signal.SIGTERM, reader.stop)

    await reader.run()


if __name__ == "__main__":
    asyncio.run(main())
