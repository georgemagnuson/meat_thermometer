"""
meat_thermometer_v2.py — EMAX Smart I45778 direct reader
Connects straight to the known device, streams temperature until Ctrl-C.
Logs every reading to emax_log_{timestamp}.csv alongside the live display.
"""

import asyncio
import csv
import sys
from datetime import datetime
from pathlib import Path
from bleak import BleakClient, BleakError

EMAX_ADDRESS     = "18:93:D7:1C:C9:0B"
EMAX_WRITE_CHAR  = "0000ffb1-0000-1000-8000-00805f9b34fb"
EMAX_NOTIFY_CHAR = "0000ffb2-0000-1000-8000-00805f9b34fb"
EMAX_START_CMD   = b'\x0b\x01'
CONNECT_TIMEOUT  = 15.0  # seconds before giving up


def log_path() -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path(__file__).parent / f"emax_log_{ts}.csv"


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
        if len(parts) < 4:
            return None
        temp_c = int(parts[0][3:])
        return {"temp_c": temp_c, "unit": parts[1], "probes": parts[3], "raw": s}
    except (ValueError, IndexError):
        return None


class EMAXReader:
    def __init__(self):
        self._stop         = asyncio.Event()
        self._prev_temp: int | None = None
        self._csv_writer   = None
        self._csv_file     = None
        self._reading_count = 0
        self._log_path     = log_path()

    def open_log(self):
        self._csv_file = open(self._log_path, "w", newline="")
        self._csv_writer = csv.writer(self._csv_file)
        self._csv_writer.writerow(["timestamp", "temp_c", "temp_f", "delta_c", "probes", "raw"])
        print(f"Logging to {self._log_path}\n")

    def close_log(self):
        if self._csv_file:
            self._csv_file.close()

    def on_notify(self, sender, data: bytearray):
        parsed = parse_emax_payload(data)
        if not parsed:
            print(f"  [raw] {data.hex()}")
            return

        now    = datetime.now()
        temp_c = parsed["temp_c"]
        temp_f = temp_c * 9 / 5 + 32
        probes = parsed["probes"]
        delta  = temp_c - self._prev_temp if self._prev_temp is not None else 0
        self._prev_temp = temp_c
        self._reading_count += 1

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

        print(f"Connecting to EMAX Smart {EMAX_ADDRESS} (timeout {CONNECT_TIMEOUT:.0f}s) ...")
        try:
            async with BleakClient(EMAX_ADDRESS, timeout=CONNECT_TIMEOUT) as client:
                print(f"Connected. Reading temperature — press Ctrl-C to stop.\n")
                print(f"  {'Time':>8}  {'°C':>3}       {'°F':>6}    probes")
                print(f"  {'-'*50}")

                await client.start_notify(EMAX_NOTIFY_CHAR, self.on_notify)
                await client.write_gatt_char(EMAX_WRITE_CHAR, EMAX_START_CMD, response=False)
                await self._stop.wait()
                await client.stop_notify(EMAX_NOTIFY_CHAR)

        except (BleakError, asyncio.TimeoutError) as e:
            print(f"\n  Could not connect to EMAX Smart {EMAX_ADDRESS}")
            print(f"  {e}")
            print(f"  Is the thermometer powered on and in range?")
            self.close_log()
            self._log_path.unlink(missing_ok=True)  # remove empty log
            sys.exit(1)

        self.close_log()
        print(f"\n  {'-'*50}")
        print(f"  Stopped. {self._reading_count} readings recorded to {self._log_path}")


async def main():
    reader = EMAXReader()
    try:
        await reader.run()
    except (KeyboardInterrupt, asyncio.CancelledError):
        reader.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
