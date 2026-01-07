import argparse
import asyncio
import json
import platform
import re
import subprocess
from pathlib import Path

from bleak import BleakScanner, BleakClient
from bleak.exc import BleakDeviceNotFoundError


PWR_CHAR = "c6d6dc0d-07f5-47ef-9b59-630622b01fd3"
CFG_PATH = Path.home() / ".config" / "theriverman" / "megaboom" / "ue_megaboom.json"
MAC_RE = re.compile(r"(?i)\b([0-9a-f]{2}(?::[0-9a-f]{2}){5})\b")


def mac_to_bytes(mac: str) -> bytes:
    mac = mac.replace(":", "").replace("-", "").strip()
    if len(mac) != 12:
        raise ValueError(f"Bad MAC format: {mac!r}")
    return bytes.fromhex(mac)


def detect_macos_bluetooth_mac() -> str | None:
    # noinspection PyBroadException
    try:
        res = subprocess.run(
            ["system_profiler", "SPBluetoothDataType"],
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception:
        return None

    preferred_hits: list[str] = []
    any_hits: list[str] = []

    for line in res.stdout.splitlines():
        m = MAC_RE.search(line)
        if not m:
            continue
        mac = m.group(1).upper()
        any_hits.append(mac)
        if "bluetooth controller" in line.lower() or "address" in line.lower():
            preferred_hits.append(mac)

    return preferred_hits[0] if preferred_hits else (any_hits[0] if any_hits else None)


def _migrate_cfg(cfg: dict) -> dict:
    cfg = cfg if isinstance(cfg, dict) else {}
    devices_in = cfg.get("devices") if isinstance(cfg.get("devices"), dict) else {}
    devices: dict[str, dict] = {}
    for label, entry in devices_in.items():
        if not isinstance(entry, dict):
            continue
        lbl = str(label)
        ble_id = entry.get("ble_id")
        name_hint = entry.get("name_hint")
        if ble_id or name_hint:
            devices[lbl] = {}
            if ble_id:
                devices[lbl]["ble_id"] = ble_id
            if name_hint:
                devices[lbl]["name_hint"] = name_hint

    default_device = cfg.get("default_device") if isinstance(cfg.get("default_device"), str) else None

    # Legacy flat keys
    if "ble_id" in cfg or "name_hint" in cfg:
        legacy_label = default_device or "default"
        entry = devices.get(legacy_label, {})
        if cfg.get("ble_id"):
            entry.setdefault("ble_id", cfg.get("ble_id"))
        if cfg.get("name_hint"):
            entry.setdefault("name_hint", cfg.get("name_hint"))
        if entry:
            devices[legacy_label] = entry
            default_device = default_device or legacy_label

    if default_device and default_device not in devices:
        default_device = None
    if default_device is None and devices:
        default_device = next(iter(devices))

    cfg["devices"] = devices
    cfg["default_device"] = default_device
    cfg.pop("ble_id", None)
    cfg.pop("name_hint", None)
    return cfg


def load_cfg() -> dict:
    if CFG_PATH.exists():
        raw = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    else:
        raw = {}
    return _migrate_cfg(raw)


def save_cfg(cfg: dict) -> None:
    cfg = _migrate_cfg(cfg)
    CFG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CFG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def remember_device(cfg: dict, label: str, ble_id: str, name_hint: str | None, set_default: bool) -> None:
    label = label.strip() if label else "default"
    cfg = cfg if cfg else {}
    cfg.setdefault("devices", {})
    cfg["devices"][label] = {"ble_id": ble_id}
    if name_hint:
        cfg["devices"][label]["name_hint"] = name_hint
    if set_default or not cfg.get("default_device"):
        cfg["default_device"] = label
    save_cfg(cfg)
    target = " and set as default" if cfg.get("default_device") == label else ""
    print(f"Saved ble_id={ble_id} under '{label}' in {CFG_PATH}{target}")


def get_default_device(cfg: dict):
    label = cfg.get("default_device")
    if not label:
        return None, None
    device = (cfg.get("devices") or {}).get(label)
    if not device:
        return None, None
    return label, device


def derive_label(explicit_label: str | None, name_hint: str | None, device) -> str:
    if explicit_label:
        return explicit_label
    if name_hint:
        return name_hint
    if isinstance(device, str):
        return device
    if getattr(device, "name", None):
        return device.name
    return getattr(device, "address", "default")


def get_rssi(device, adv=None):
    rssi = getattr(device, "rssi", None)
    if rssi is None and adv is not None:
        rssi = getattr(adv, "rssi", None)
    if rssi is None:
        meta = getattr(device, "metadata", None)
        if isinstance(meta, dict):
            rssi = meta.get("rssi")
    return rssi


async def scan_devices(timeout: float):
    """
    Always returns: list of (BLEDevice, AdvertisementData|None)
    """
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)

    if isinstance(found, dict):
        # Bleak >=0.22 returns dict[address -> (BLEDevice, AdvertisementData)]
        return list(found.values())

    if isinstance(found, tuple) and len(found) == 2 and isinstance(found[0], (list, tuple)):
        # Older bleak versions sometimes return (devices, adv_map)
        devices, adv_map = found
        normalized = []
        for idx, dev in enumerate(devices):
            adv = None
            if isinstance(adv_map, dict):
                adv = adv_map.get(getattr(dev, "address", None))
            elif isinstance(adv_map, (list, tuple)) and idx < len(adv_map):
                adv = adv_map[idx]
            normalized.append((dev, adv))
        return normalized

    # Fallback: assume iterable of devices or (device, adv) tuples
    normalized = []
    for item in found or []:
        if isinstance(item, tuple) and len(item) >= 2:
            normalized.append((item[0], item[1]))
        else:
            normalized.append((item, None))
    return normalized


async def cmd_scan(
    name_substring: str | None,
    timeout: float,
    remember: bool,
    remember_as: str | None,
    set_default: bool,
):
    found = await scan_devices(timeout)

    # Print a usable list for you to identify the speaker (especially if it advertises with no name)
    print("Discovered BLE devices:")
    for d, adv in found:
        nm = d.name or "<no name>"
        rssi = get_rssi(d, adv)
        extra = ""
        if adv:
            su = list(getattr(adv, "service_uuids", []) or [])
            md = getattr(adv, "manufacturer_data", None)
            mfg_ids = list((adv.manufacturer_data or {}).keys())
            if su:
                extra += f" services={len(su)}"
            if md:
                extra += f" mfg={len(md)}"
            if mfg_ids:
                extra += f" mfg_ids={mfg_ids}"
        rssi_display = rssi if rssi is not None else "?"
        print(f"- {nm}  id={d.address}  rssi={rssi_display}{extra}")

    if not name_substring:
        return

    matches = [(d, adv, get_rssi(d, adv)) for d, adv in found if d.name and name_substring.lower() in d.name.lower()]
    if not matches:
        raise RuntimeError(f"No BLE device with name containing {name_substring!r} found in scan output.")

    # Pick strongest
    matches.sort(key=lambda x: (x[2] is not None, x[2]), reverse=True)
    dev = matches[0][0]

    if remember:
        cfg = load_cfg()
        label = derive_label(remember_as, name_substring, dev)
        remember_device(cfg, label, dev.address, name_substring, set_default)


async def find_device(name_substring: str, timeout: float):
    found = await scan_devices(timeout)
    matches = [(d, adv, get_rssi(d, adv)) for d, adv in found if d.name and name_substring.lower() in d.name.lower()]
    if not matches:
        seen = sorted({(d.name or "<no name>") for d, _adv in found})
        raise RuntimeError(f"No device matching {name_substring!r} found. Seen: {seen}")
    matches.sort(key=lambda x: (x[2] is not None, x[2]), reverse=True)
    return matches[0][0]


async def send_power(ble_id: str | None, name_substring: str, my_mac: str, cmd: int, timeout: float):
    if ble_id:
        dev_address = ble_id
    else:
        dev = await find_device(name_substring, timeout)
        dev_address = dev.address

    payload = bytearray(mac_to_bytes(my_mac))
    payload.append(cmd)  # 1=on, 2=off

    async with BleakClient(dev_address) as client:
        await client.write_gatt_char(PWR_CHAR, payload, response=True)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="Scan BLE advertisements; optionally remember device BLE id")
    scan.add_argument("--name", default=None, help="Substring of BLE device name to match (optional)")
    scan.add_argument("--timeout", type=float, default=8.0, help="Scan timeout seconds (default: 8)")
    scan.add_argument("--remember", action="store_true", help=f"Store matched BLE id to {CFG_PATH}")
    scan.add_argument("--remember-as", default=None, help="Label to store this device under (default: name/substring/address)")
    scan.add_argument("--set-default", action="store_true", help="Set this device as the default/favourite")

    pwr = sub.add_parser("power", help="Send power on/off via BLE GATT write")
    pwr.add_argument("--name", default=None, help="Substring of BLE device name (optional if default is configured)")
    pwr.add_argument(
        "--ble-id",
        default=None,
        help="BLE identifier to connect to (macOS often uses UUID-like id from scan). "
             f"If omitted, uses the default device in {CFG_PATH} or falls back to --name.",
    )
    pwr.add_argument(
        "--my-mac",
        default=None,
        help="Bluetooth MAC of a device already paired with the speaker. "
             "On macOS, if omitted, best-effort auto-detect is attempted.",
    )
    pwr.add_argument("--timeout", type=float, default=8.0, help="BLE scan timeout seconds (default: 8)")
    pwr.add_argument("action", choices=["on", "off"])

    pwr_id = sub.add_parser("power-id", help="Send power on/off to a specific BLE id (from scan output)")
    pwr_id.add_argument("ble_id", help="BLE identifier to connect to, e.g. from scan output")
    pwr_id.add_argument(
        "--my-mac",
        default=None,
        help="Bluetooth MAC of a device already paired with the speaker. "
             "On macOS, if omitted, best-effort auto-detect is attempted.",
    )
    pwr_id.add_argument("--timeout", type=float, default=8.0, help="BLE connection timeout seconds (default: 8)")
    pwr_id.add_argument("--remember", action="store_true", help=f"Store this BLE id to {CFG_PATH}")
    pwr_id.add_argument("--remember-as", default=None, help="Label to store this device under (default: remember label/name/address)")
    pwr_id.add_argument("--set-default", action="store_true", help="Set this device as the default/favourite")
    pwr_id.add_argument("action", choices=["on", "off"])

    args = ap.parse_args()

    if args.command == "scan":
        if args.remember and not args.name:
            raise SystemExit("--remember requires --name to select which device to store")
        asyncio.run(cmd_scan(args.name, args.timeout, args.remember, args.remember_as, args.set_default))
        return

    if args.command == "power-id":
        my_mac = args.my_mac
        if not my_mac and platform.system() == "Darwin":
            my_mac = detect_macos_bluetooth_mac()
        if not my_mac:
            raise SystemExit("Could not determine --my-mac. Provide: --my-mac 'AA:BB:CC:DD:EE:FF'")

        cmd = 1 if args.action == "on" else 2
        try:
            asyncio.run(send_power(args.ble_id, "", my_mac, cmd, args.timeout))
        except BleakDeviceNotFoundError as e:
            print(e)
            return
        if args.remember:
            cfg = load_cfg()
            label = derive_label(args.remember_as, None, args.ble_id)
            remember_device(cfg, label, args.ble_id, None, args.set_default)
        return

    # power command
    cfg = load_cfg()
    default_label, default_entry = get_default_device(cfg)

    ble_id = args.ble_id
    name_hint = args.name
    if not ble_id and not name_hint and default_entry:
        ble_id = default_entry.get("ble_id") or ble_id
        name_hint = default_entry.get("name_hint") or name_hint

    if not ble_id and not name_hint:
        raise SystemExit(
            "No default/favourite device configured. Provide --ble-id or --name, "
            f"or set a default via scan --remember or power-id --remember (config: {CFG_PATH})."
        )

    my_mac = args.my_mac
    if not my_mac and platform.system() == "Darwin":
        my_mac = detect_macos_bluetooth_mac()
    if not my_mac:
        raise SystemExit("Could not determine --my-mac. Provide: --my-mac 'AA:BB:CC:DD:EE:FF'")

    cmd = 1 if args.action == "on" else 2
    try:
        asyncio.run(send_power(ble_id, name_hint, my_mac, cmd, args.timeout))
    except BleakDeviceNotFoundError as e:
        print(e)


if __name__ == "__main__":
    main()
