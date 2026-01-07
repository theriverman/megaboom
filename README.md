# Megaboom BLE Power Tool (macOS only)

Control a UE MEGABOOM 3 (and similar UE speakers) over Bluetooth Low Energy by writing to its hidden power characteristic. This project is macOS-first: it auto-detects your local Bluetooth MAC via `system_profiler` and **does not support or test Linux/Windows**.

## Project contents
- `megaboom.py` — CLI for scanning BLE advertisers and toggling power via GATT.
- `pyproject.toml` — Python package metadata (Python ≥3.13, depends on `bleak>=2.1.1`).
- `uv.lock` — locked dependency set (includes PyObjC for macOS).

## Install / upgrade the CLI
Install or refresh from local source (rebuilds the wheel and replaces the shim in `~/.local/bin`):
```bash
uv tool install --force --reinstall --no-cache .
```
Versioning is derived from git tags via setuptools-scm; make sure your working tree has tags when building.
During development (live edits without reinstalls):
```bash
uv tool install --force --editable --no-cache .
```
Verify: `megaboom --help`. Make sure `~/.local/bin` is on your `PATH`.

## Config file (location, schema, behaviour)
- Path: `~/.config/theriverman/megaboom/ue_megaboom.json`
- Schema:
  ```json
  {
    "devices": {
      "livingroom": {
        "ble_id": "A7F24F0B-D4D8-0F63-3852-18D36B714156"
      }
    },
    "default_device": "livingroom"
  }
  ```
- `devices` is a map of labels → `{ble_id}`.
- `default_device` chooses which entry is used when `--ble-id/--name` are omitted.
- Remembering a device without an existing default will set it as default unless you opt out.
- Show the location any time: `megaboom config-path`.

## Command overview
`megaboom scan` — List advertisers; optionally remember a found device.
```
megaboom scan
megaboom scan --name MEGABOOM --remember --remember-as livingroom --set-default
```
- `--remember` requires `--name` (used to pick the match).
- Labels default to name/substring/address unless `--remember-as` is provided; only the BLE id is stored.
- `--set-default` marks the label as favourite.

`megaboom power-id` — Power on/off by explicit BLE id (from scan output).
```
megaboom power-id A7F2...4156 on
megaboom power-id --remember --remember-as livingroom --set-default A7F2...4156 off
```

`megaboom power` — Power on/off using defaults or provided selectors.
```
# Uses default_device if present
megaboom power on

# Override with explicit id or name substring
megaboom power --ble-id A7F2...4156 off
megaboom power --name MEGABOOM on
```
Rules: If no default/favourite is configured you must pass `--ble-id` or `--name`; otherwise it errors. macOS auto-detects your own Bluetooth MAC; override with `--my-mac AA:BB:CC:DD:EE:FF` if needed.

`megaboom config-path` — Print the config file location (and whether it exists).

`megaboom version` — Print the installed package version (set from git tags via setuptools-scm; falls back to `unknown` if no version metadata is available).

## Device discovery tips (macOS BLE)
- Pairing mode: hold the speaker’s Bluetooth button; it should broadcast its name instead of `<no name>`.
- RSSI: move the speaker next to the Mac; pick the strongest RSSI (closest to 0).
- Manufacturer ID: UE/Logitech uses company ID `0x0047` (decimal 71). The scan output prints `mfg_ids=[...]` to help disambiguate unnamed advertisers.
- Trial-and-error: if still unnamed, use `power-id` against the strong candidate and, once it works, remember it with `--remember`.

## Typical setup
1) Place the speaker near the Mac; enable pairing mode if possible.
2) Discover: `megaboom scan --timeout 12` (optionally `--name MEGABOOM`).
3) Save default: `megaboom scan --name MEGABOOM --remember --remember-as livingroom --set-default`
   - For unnamed devices: try `megaboom power-id <BLE-ID> on`, then add `--remember --set-default`.
4) Daily use: `megaboom power on` / `megaboom power off` (uses the saved default).

## Platform notes and limitations
- macOS only; Linux/Windows are not supported/tested.
- Needs BLE access and an existing paired device MAC (auto-detected on macOS; override with `--my-mac`).
- Bleak return shapes are normalized internally; the CLI tolerates `<no name>` advertisers and prints `mfg_ids`.

## Uninstall / reinstall
- Reinstall from local source: `uv tool install --force --reinstall --no-cache .`
- Remove the tool: `uv tool uninstall megaboom`
