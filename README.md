# Hikvision Backup Recordings

A Python toolkit to browse and download recordings from Hikvision NVR/DVR devices over ISAPI (HTTP).  
Includes a desktop **GUI** and an interactive **CLI** — no extra dependencies beyond the Python standard library (plus `ffmpeg` for MP4 conversion).

---

## Features

- 🖥️ **Desktop GUI** (`hikvision_gui.py`) — connect, browse, and download with a point-and-click interface
- 💻 **Interactive CLI** (`hikvision_backup_interactive.py`) — scriptable, great for scheduled/automated backups
- 📅 **Available dates browser** — scan the last 30 days and see which dates have recordings per channel
- 📆 **Date range search** — search across multiple days in one go
- ⏩ **Resume support** — interrupted downloads pick up where they left off (HTTP Range)
- 📦 **MP4 conversion** — optional ffmpeg remux from `.ps` to `.mp4` after download (two-pass: audio then no-audio fallback)
- 🚦 **Bandwidth limiting** — cap download speed to avoid saturating the network
- ✅ **Multi-channel** — search and download several channels at once
- 🔁 **Retry on network error** — 3 attempts with a 20-second countdown between each
- ⏹️ **Ctrl-C skip/stop** — interrupt a download mid-file; choose to skip to the next file or stop the whole run
- 📋 **Export to CSV** — save the recording list as a spreadsheet (GUI)
- 📝 **Session log file** — each run writes a timestamped `.log` file to the save folder

---

## Requirements

- Python 3.10+
- [`pure_hik_client.py`](https://github.com/crazy3tr/OpenHik-DVR) — place in the same folder as these scripts
- `ffmpeg` in PATH — only needed for MP4 conversion

### Installing ffmpeg

| OS | Command |
|---|---|
| Windows | `winget install ffmpeg` or download from [gyan.dev](https://www.gyan.dev/ffmpeg/builds/) |
| macOS | `brew install ffmpeg` |
| Linux | `sudo apt install ffmpeg` |

> **Note:** ffmpeg 9.x or newer is recommended. Older builds may fail to read `.ps` files due to a demuxer name change (`mpegps` → `mpeg`).

---

## Quick Start

1. Clone or download this repo.
2. Copy `pure_hik_client.py` from [OpenHik-DVR](https://github.com/crazy3tr/OpenHik-DVR) into the same folder.
3. Run the GUI:

```bash
python hikvision_gui.py
```

Or use the CLI:

```bash
python hikvision_backup_interactive.py
```

---

## Saved Profiles (GUI)

The GUI can save connection profiles (IP, port, username, password) to `hik_profiles.json` in the script folder for quick reconnect.

> ⚠️ This file contains plaintext credentials. It is listed in `.gitignore` and should **never** be committed to version control.

---

## GUI Overview

| Section | What it does |
|---|---|
| **Device Connection** | Enter NVR IP, HTTP port, username, password → Connect |
| **Search** | Set start/end date, channels, click **Search Recordings** |
| **📅 List Available Dates** | Scans last 30 days and shows a date picker — click a date to auto-fill |
| **Recordings table** | Tick/untick segments to select; shows channel, timestamps, duration, size |
| **Download Options** | Choose save folder, MP4 conversion, bandwidth limit |
| **Export CSV** | Save the current recording list to a `.csv` file |
| **Progress** | Per-file and overall progress bars |
| **Log** | Live status with colour-coded entries; also written to a `.log` file in the save folder |

---

## Folder Layout

Downloaded files are organized automatically:

```
NVR-Backup/
  2024-10-12/
    ch2/
      ch2_2024-10-12_08-00-00.ps
      ch2_2024-10-12_08-00-00.mp4   ← if MP4 conversion is enabled
    ch3/
      ch3_2024-10-12_08-00-00.ps
  hik_backup_20241012_080000.log
```

> `.ps` files can be played directly in **VLC** without conversion.

---

## NVR Compatibility

Tested on Hikvision NVRs with ISAPI over HTTP port 8080.  
Should work with any Hikvision device that supports:
- `/ISAPI/ContentMgmt/search`
- `/ISAPI/ContentMgmt/download` or `/PSIA/Custom/SelfExt/ContentMgmt/download`

> HTTPS is not currently supported.

---

## Credits

- Uses [`OpenHik-DVR`](https://github.com/crazy3tr/OpenHik-DVR) (`pure_hik_client.py`) for ISAPI communication and Digest Auth.

---

## License

MIT
