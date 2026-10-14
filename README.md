# Hikvision Backup Recordings

A Python toolkit to browse and download recordings from Hikvision NVR/DVR devices over ISAPI (HTTP).  
Includes a desktop **GUI** and an interactive **CLI** — no extra dependencies beyond the Python standard library.

---

## Features

- 🖥️ **Desktop GUI** (`hikvision_gui.py`) — connect, browse, and download with a point-and-click interface
- 💻 **Interactive CLI** (`hikvision_backup_interactive.py`) — scriptable, great for scheduled/automated backups
- 📅 **Available dates browser** — scan the last 30 days and see which dates have recordings per channel
- 📆 **Date range search** — search across multiple days in one go
- ⏩ **Resume support** — interrupted downloads pick up where they left off (HTTP Range)
- 📦 **MP4 conversion** — optional ffmpeg remux from `.ps` to `.mp4` after download
- 🚦 **Bandwidth limiting** — cap download speed to avoid saturating the network
- ✅ **Multi-channel** — search and download several channels at once

---

## Requirements

- Python 3.8+
- [`pure_hik_client.py`](https://github.com/crazy3tr/OpenHik-DVR) — place in the same folder
- `ffmpeg` in PATH — only needed for MP4 conversion

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

## GUI Overview

| Section | What it does |
|---|---|
| **Device Connection** | Enter NVR IP, HTTP port, username, password → Connect |
| **Search** | Set start/end date, channels, click **Search Recordings** |
| **📅 List Available Dates** | Scans last 30 days and shows a date picker — click a date to auto-fill |
| **Recordings table** | Tick/untick segments to select; shows channel, timestamps, duration, size |
| **Download Options** | Choose save folder, MP4 conversion, bandwidth limit |
| **Progress** | Per-file and overall progress bars |
| **Log** | Live status with colour-coded entries |

---

## Folder Layout

Downloaded files are organized automatically:

```
NVR-Backup/
  2024-10-12/
    ch2/
      ch2_2024-10-12_08-00-00.ps
    ch3/
      ch3_2024-10-12_08-00-00.ps
  2024-10-13/
    ...
```

---

## NVR Compatibility

Tested on Hikvision NVRs with ISAPI over HTTP port 8080.  
Should work with any Hikvision device that supports:
- `/ISAPI/ContentMgmt/search`
- `/ISAPI/ContentMgmt/download` or `/PSIA/Custom/SelfExt/ContentMgmt/download`

---

## Credits

- Uses [`OpenHik-DVR`](https://github.com/crazy3tr/OpenHik-DVR) (`pure_hik_client.py`) for ISAPI communication and Digest Auth.

---

## License

MIT