import asyncio
import struct
from bleak import BleakScanner, BleakClient

# Known thermometer name fragments (case-insensitive)
THERMO_NAMES = ["ibbq", "smartthermo", "tp3", "tp2", "meater", "govee", "inkbird", "ibbt", "emax"]

# iBBQ service/characteristic UUIDs (Inkbird/iBBQ protocol)
IBBQ_SERVICE       = "0000fff0-0000-1000-8000-00805f9b34fb"
IBBQ_NOTIFY_CHAR   = "0000fff4-0000-1000-8000-00805f9b34fb"
IBBQ_WRITE_CHAR    = "0000fff5-0000-1000-8000-00805f9b34fb"
IBBQ_ACCOUNT_WRITE = b'\x21\x07\x06\x05\x04\x03\x02\x01\xb8\x22\x00\x00\x00\x00\x00'
IBBQ_SUBSCRIBE     = b'\x01\x00'

SCAN_TIMEOUT = 15.0  # seconds


def is_thermometer(name: str | None) -> bool:
    if not name:
        return False
    low = name.lower()
    return any(t in low for t in THERMO_NAMES)


def decode_ibbq_temp(data: bytearray) -> list[float | None]:
    """Parse iBBQ notification payload into Celsius readings per probe."""
    if len(data) < 2:
        return []
    probe_count = (len(data) - 2) // 2
    temps = []
    for i in range(probe_count):
        raw = struct.unpack_from("<H", data, 2 + i * 2)[0]
        if raw == 0xFFFF:  # probe not inserted
            temps.append(None)
        else:
            temps.append(raw / 10.0)
    return temps


async def on_ibbq_notify(sender, data: bytearray):
    temps = decode_ibbq_temp(data)
    for i, t in enumerate(temps):
        if t is None:
            print(f"  Probe {i+1}: not inserted")
        else:
            print(f"  Probe {i+1}: {t:.1f} °C  ({t * 9/5 + 32:.1f} °F)")


EMAX_WRITE_CHAR  = "0000ffb1-0000-1000-8000-00805f9b34fb"
EMAX_NOTIFY_CHAR = "0000ffb2-0000-1000-8000-00805f9b34fb"

# Sent to ffb1 to initialise/subscribe temperature streaming
EMAX_START_CMD = b'\x0b\x01'


def decode_ascii_safe(data: bytearray) -> str:
    try:
        return data.rstrip(b'\x00').decode("ascii")
    except Exception:
        return ""


def parse_emax_payload(data: bytearray) -> dict | None:
    """Parse EMAX notification. Format: OKZ{temp3}-{unit}-{flag}-{probes}
    Example: OKZ022-UC-UG-ZIII  →  22 °C
    """
    s = decode_ascii_safe(data)
    if not s.startswith("OKZ"):
        return None
    try:
        parts = s.split("-")
        temp_c = int(parts[0][3:])          # strip 'OKZ', parse integer
        unit   = parts[1] if len(parts) > 1 else "UC"
        probes = parts[3] if len(parts) > 3 else ""
        return {"temp_c": temp_c, "unit": unit, "probes": probes, "raw": s}
    except (ValueError, IndexError):
        return None


async def connect_emax(address: str):
    """EMAX Smart thermometer — subscribe and stream temperature."""
    print(f"\nConnecting to {address} (EMAX protocol) ...")
    async with BleakClient(address) as client:
        print(f"Connected: {client.is_connected}")

        def on_emax_notify(sender, data: bytearray):
            parsed = parse_emax_payload(data)
            if parsed:
                temp_c = parsed["temp_c"]
                temp_f = temp_c * 9 / 5 + 32
                probes = parsed["probes"]
                print(f"  Temperature: {temp_c} °C  ({temp_f:.1f} °F)   probes:{probes}")
            else:
                print(f"  Raw: {data.hex()}  {decode_ascii_safe(data)!r}")

        await client.start_notify(EMAX_NOTIFY_CHAR, on_emax_notify)

        # Trigger streaming
        await client.write_gatt_char(EMAX_WRITE_CHAR, EMAX_START_CMD, response=False)

        print("Receiving temperature (Ctrl-C to stop)...\n")
        try:
            while True:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            await client.stop_notify(EMAX_NOTIFY_CHAR)


async def explore_device(address: str):
    """Enumerate all GATT services and characteristics, attempt reads and notifications."""
    print(f"\nConnecting to {address} for exploration ...")
    async with BleakClient(address) as client:
        print(f"Connected: {client.is_connected}\n")

        notify_chars = []

        for service in client.services:
            print(f"Service: {service.uuid}  ({service.description})")
            for char in service.characteristics:
                props = ", ".join(char.properties)
                print(f"  Char: {char.uuid}  [{props}]")

                if "read" in char.properties:
                    try:
                        val = await client.read_gatt_char(char.uuid)
                        ascii_str = decode_ascii_safe(val)
                        tag = f"  ascii: '{ascii_str}'" if ascii_str else ""
                        print(f"    read -> {val.hex()}{tag}")
                    except Exception as e:
                        print(f"    read -> ERROR: {e}")

                if "notify" in char.properties or "indicate" in char.properties:
                    notify_chars.append(char.uuid)

        if notify_chars:
            print(f"\nSubscribing to {len(notify_chars)} notify/indicate characteristic(s)...")

            def make_handler(uuid):
                def handler(sender, data: bytearray):
                    ascii_str = decode_ascii_safe(data)
                    tag = f"  ascii: '{ascii_str}'" if ascii_str else ""
                    print(f"  NOTIFY {uuid}: {data.hex()}{tag}")
                return handler

            for uuid in notify_chars:
                try:
                    await client.start_notify(uuid, make_handler(uuid))
                except Exception as e:
                    print(f"  start_notify {uuid} -> ERROR: {e}")

            print("Listening for 30 seconds (Ctrl-C to stop early)...\n")
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                pass
            finally:
                for uuid in notify_chars:
                    try:
                        await client.stop_notify(uuid)
                    except Exception:
                        pass


async def connect_ibbq(address: str):
    print(f"\nConnecting to {address} ...")
    async with BleakClient(address) as client:
        print(f"Connected: {client.is_connected}")

        # Authenticate / enable notifications
        await client.write_gatt_char(IBBQ_WRITE_CHAR, IBBQ_ACCOUNT_WRITE, response=True)
        await client.start_notify(IBBQ_NOTIFY_CHAR, on_ibbq_notify)

        print("Receiving temperature data (Ctrl-C to stop)...\n")
        try:
            while True:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            await client.stop_notify(IBBQ_NOTIFY_CHAR)


async def scan() -> list:
    print(f"Scanning for BLE devices ({SCAN_TIMEOUT}s)...\n")
    found = []
    devices = await BleakScanner.discover(timeout=SCAN_TIMEOUT, return_adv=True)
    for address, (device, adv) in devices.items():
        name = device.name or adv.local_name
        rssi = adv.rssi
        svc_uuids = [str(u) for u in adv.service_uuids]
        mfr = adv.manufacturer_data

        thermo = is_thermometer(name)
        tag = " <-- THERMOMETER" if thermo else ""
        print(f"  {name or '(unknown)':30s}  {address}  RSSI:{rssi:4d}{tag}")
        if svc_uuids:
            print(f"    services: {', '.join(svc_uuids)}")
        if mfr:
            for k, v in mfr.items():
                print(f"    mfr [{k:#06x}]: {v.hex()}")
        if thermo:
            found.append((address, name, svc_uuids, mfr))

    return found


async def main():
    thermometers = await scan()

    if not thermometers:
        print("\nNo thermometers detected. Is the device powered on and in range?")
        return

    print(f"\nFound {len(thermometers)} thermometer(s):")
    for i, (addr, name, svcs, mfr) in enumerate(thermometers):
        print(f"  [{i}] {name} @ {addr}")
        if svcs:
            print(f"       Services: {', '.join(svcs)}")
        if mfr:
            for k, v in mfr.items():
                print(f"       Mfr data [{k:#06x}]: {v.hex()}")

    # Auto-select if only one, otherwise prompt
    if len(thermometers) == 1:
        choice = 0
    else:
        choice = int(input("\nSelect device index to connect: "))

    address, name, svcs, _ = thermometers[choice]

    # Route to the right handler based on service UUIDs
    if IBBQ_SERVICE in svcs:
        await connect_ibbq(address)
    elif "0000ffb0-0000-1000-8000-00805f9b34fb" in svcs:
        await connect_emax(address)
    else:
        await explore_device(address)


if __name__ == "__main__":
    asyncio.run(main())
