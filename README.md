# flashforge-c5-dashboard

A self-hosted, Fluidd/Mainsail-style web dashboard for the **FlashForge Creator 5 / Creator 5 Pro** running **stock firmware in LAN-only mode**, built to serve as OrcaSlicer's **Device** tab.

In LAN-only mode the FlashForge app and Orca's default device view stop working. This dashboard talks to the printer's local HTTP API (port 8898) and camera (port 8080) directly and gives you live status, history, the camera and the controls the firmware exposes, with no cloud and no firmware modification.

> **Status:** in daily use on a Creator 5 Pro (firmware 1.9.9). Monitoring, camera, history, file list, thumbnails, pause and the light are verified on hardware. Some controls are implemented from protocol documentation but **not yet verified on hardware**; see [Tested vs untested](#tested-vs-untested).

## Features

- **Live status** over Server-Sent Events (2 s poll): state, progress, layer, elapsed/remaining time, ETA, current job thumbnail.
- **Camera relay**: one upstream MJPEG connection fanned out to any number of viewers (the printer's camera only serves one client). Resizable view, width presets, fullscreen, snapshot, rotate/flip.
- **Temperatures**: all four toolheads, bed and chamber, with per-heater targets, editable presets (PLA/PETG/ABS/TPU…) and cooldown.
- **History charts** (uPlot): temperatures, targets, fans and speed from 15 minutes to 30 days, plus a daily utilization chart.
- **Toolheads & material station**: per-tool colour/material lanes; edit a slot's material and colour (snapped to the firmware's 24-colour palette).
- **Tune**: speed factor, part-cooling and chamber fans, Z offset. Changes are *staged* and sent together with **Apply**.
- **Sensors & machine info**: chamber temperature, air quality (TVOC), door, fans, lidar, firmware, disk, lifetime stats.
- **Files**: list of files on the printer with thumbnails, print time, weight and colours; start a print with tool→slot mapping, leveling, flow calibration and time-lapse options; drag-and-drop upload.
- **Job history & statistics**, recorded by the dashboard (SQLite).
- **Event console**: status changes, prints and every command sent (with the client address).
- **Notifications**: ntfy or generic JSON webhooks per event, plus optional browser notifications.
- **Prometheus metrics** at `/metrics` (`printer_*`).
- Dark/light theme, collapsible cards, responsive layout. No external requests: the charting library is bundled.

## What it can't do (stock firmware limits)

The Creator 5 series has no G-code/TCP channel on port 8899, so the network API offers **no homing, jogging, raw G-code console, or filament load/unload**. The printer's own time-lapse recordings are also not reachable over the LAN API. Those need shell access to the printer, which stock firmware doesn't provide.

## Requirements

- A small Linux host on the same LAN as the printer (a Proxmox LXC with 512 MB RAM works well)
- Python 3.11+ with `aiohttp` (Debian/Ubuntu: `apt install python3-aiohttp`)
- The printer in **LAN-only mode**, plus its **serial number** and **check code** (shown on the printer's network/LAN-mode settings screen)

## Install

```sh
sudo useradd --system --home /var/lib/printer-dash --shell /usr/sbin/nologin printerdash
sudo install -d -o printerdash -g printerdash -m 750 /var/lib/printer-dash
sudo mkdir -p /opt/printer-dash && sudo cp -r server.py static /opt/printer-dash/

sudo install -d -m 750 -g printerdash /etc/printer-dash
sudo install -m 640 -g printerdash config.example.json /etc/printer-dash/config.json
sudoedit /etc/printer-dash/config.json        # printer IP, serial number, check code, listen port

sudo cp deploy/printer-dash.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now printer-dash
```

Then open `http://<host>/`. Config and data paths can be overridden with the `PRINTER_DASH_CONFIG` and `PRINTER_DASH_DB` environment variables.

## Using it as OrcaSlicer's Device tab

In OrcaSlicer's physical-printer settings, keep the **FlashForge** host type (so sending prints still works) and set the **Device UI** URL to `http://<host>/`.

Asset URLs carry a content-hash version (`?v=…`) and static files are served with `Cache-Control: no-cache`, so Orca's embedded browser always loads matching page and script versions after an update.

## Protocol notes (Creator 5 traps)

The printer's HTTP API is undocumented. These rules are implemented in `server.py`:

- **Temperatures** (`temperatureCtl_cmd`) always include a 4-entry `nozzles` array; `-200` means "leave unchanged". Scalar heaters turn off with `-100`, but inside `nozzles` **only a literal `0` turns a tool off** (`-100` is ignored and the tool keeps heating).
- **`printerCtl_cmd` sets Z offset, speed and both fans at once**, and only while printing, so the dashboard always resends the current values for fields you didn't change.
- The firmware answers **"Success" to unknown `/control` commands**, so a success reply doesn't prove a command exists.
- The API sends **no CORS headers**, which is why a server-side proxy is needed rather than a static page.

## Tested vs untested

| Verified on hardware | Implemented, not yet verified |
|---|---|
| Status, SSE, history, metrics | Resume, cancel |
| Camera relay and snapshot | Temperature targets (tools/bed/chamber) |
| File list and thumbnails | Speed / fan / Z-offset **Apply** |
| Pause | Start print with tool→slot mapping, upload |
| Light on/off | Material-slot editing, camera on/off, "bed cleared" |

## Credits

- Protocol details for the Creator 5 come from [GhostTypes/ff-5mp-api-py](https://github.com/GhostTypes/ff-5mp-api-py) and [GhostTypes/flashforge-emulator-v2](https://github.com/GhostTypes/flashforge-emulator-v2), which document behaviour verified on real hardware.
- Charts by [uPlot](https://github.com/leeoniya/uPlot) (MIT, bundled in `static/vendor/`).

Not affiliated with FlashForge.

## License

[MIT](LICENSE)
