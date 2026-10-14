#!/usr/bin/env python3
"""
Hikvision NVR/DVR Backup Tool
Interactively prompts for connection details, then lets you:
  1. List recordings available for a channel/date
  2. Download (all or selected) recordings with resume support

Place this file in the same folder as pure_hik_client.py, then run:
    python hikvision_backup_interactive.py
"""

import os
import sys
import re
import time
import logging
import getpass
import urllib.request
import urllib.error
from xml.sax.saxutils import escape
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from pure_hik_client import PureHikClient, convert_to_mp4, DownloadIncompleteError
except ImportError:
    print("ERROR: pure_hik_client.py not found in the same folder as this script.")
    sys.exit(1)


# ── Helpers ───────────────────────────────────────────────────────────────────

def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"{prompt}{suffix}: ").strip()
    return val if val else default

def ask_int(prompt: str, default: int) -> int:
    while True:
        raw = ask(prompt, str(default))
        try:
            return int(raw)
        except ValueError:
            print("  Please enter a number.")

def ask_yn(prompt: str, default: bool = True) -> bool:
    d = "Y/n" if default else "y/N"
    raw = input(f"{prompt} [{d}]: ").strip().lower()
    if not raw:
        return default
    return raw.startswith("y")

def progress(downloaded_bytes: int, speed_mbs: float) -> None:
    mb = downloaded_bytes / (1024 * 1024)
    print(f"\r    {mb:7.1f} MB  @ {speed_mbs:.1f} MB/s    ", end="", flush=True)

def fmt_duration(start_str: str, end_str: str) -> str:
    """Return HH:MM:SS duration string from two ISO-8601 UTC timestamps."""
    try:
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        s = datetime.strptime(start_str, fmt)
        e = datetime.strptime(end_str,   fmt)
        secs = int((e - s).total_seconds())
        return f"{secs//3600:02d}:{(secs%3600)//60:02d}:{secs%60:02d}"
    except Exception:
        return "??:??:??"


# ── Listing ───────────────────────────────────────────────────────────────────

def list_recordings(client: PureHikClient, channels: list[int],
                    start_utc: datetime, end_utc: datetime) -> None:
    """
    Print a table of every recording segment found across the given channels
    for the given UTC time window.
    """
    date_str = start_utc.strftime("%Y-%m-%d")
    print(f"\n{'='*72}")
    print(f"  Recordings on {date_str}  "
          f"({start_utc.strftime('%H:%M:%SZ')} → {end_utc.strftime('%H:%M:%SZ')} UTC)")
    print(f"{'='*72}")

    grand_total_mb   = 0.0
    grand_total_segs = 0

    for ch in channels:
        print(f"\n  Channel {ch}:")
        try:
            segments = client.search_recordings(ch, start_utc, end_utc, max_results=500)
        except Exception as e:
            print(f"    ERROR: {e}")
            continue

        if not segments:
            print("    No recordings found.")
            continue

        # Header
        print(f"    {'#':>4}  {'Start (UTC)':<22}  {'End (UTC)':<22}  {'Duration':>8}  {'Size':>8}")
        print(f"    {'-'*4}  {'-'*22}  {'-'*22}  {'-'*8}  {'-'*8}")

        ch_total_mb = 0.0
        for i, seg in enumerate(segments, 1):
            start = seg.start_time or "?"
            end   = seg.end_time   or "?"
            dur   = fmt_duration(start, end) if (start != "?" and end != "?") else "?"
            size  = f"{seg.size_mb:.1f} MB" if seg.size_mb > 0 else "?"
            print(f"    {i:>4}  {start:<22}  {end:<22}  {dur:>8}  {size:>8}")
            ch_total_mb += seg.size_mb

        print(f"\n    Total: {len(segments)} segment(s)  ~{ch_total_mb:.0f} MB")
        grand_total_segs += len(segments)
        grand_total_mb   += ch_total_mb

    print(f"\n{'─'*72}")
    print(f"  Grand total: {grand_total_segs} segment(s) across {len(channels)} channel(s)"
          f"  ~{grand_total_mb:.0f} MB")
    print(f"{'='*72}\n")


# ── Resume-capable download ───────────────────────────────────────────────────

DOWNLOAD_ENDPOINTS = [
    "/ISAPI/ContentMgmt/download",
    "/PSIA/Custom/SelfExt/ContentMgmt/download",
]

def download_with_resume(
    client: PureHikClient,
    playback_uri: str,
    dest_path: str,
    progress_callback=None,
    limit_mbps: float = 0.0,
    verify_size: bool = True,
) -> int:
    """
    Download a recording to dest_path, resuming a partial file if one exists.
    Sends Range: bytes=<existing_size>- if a partial file is found.
    Falls back to a full download if the server returns 200 instead of 206.
    Returns total bytes in the final file.
    """
    expected_size = 0
    size_match = re.search(r'[?&]size=(\d+)', playback_uri)
    if size_match:
        expected_size = int(size_match.group(1))

    existing_size = 0
    if os.path.exists(dest_path):
        existing_size = os.path.getsize(dest_path)

    if expected_size > 0 and existing_size >= expected_size:
        return existing_size  # already complete

    escaped_uri = escape(playback_uri)
    dl_xml = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<downloadRequest version="1.0"'
        ' xmlns="http://urn:selfextension:psiaext-ver10-xsd">'
        f'<playbackURI>{escaped_uri}</playbackURI>'
        '</downloadRequest>'
    ).encode("utf-8")

    resp = None
    for endpoint in DOWNLOAD_ENDPOINTS:
        url     = client.base_url + endpoint
        headers = {
            "Content-Type": "application/xml",
            "User-Agent":   "OpenHik-Client/1.0",
        }
        if existing_size > 0:
            headers["Range"] = f"bytes={existing_size}-"

        req = urllib.request.Request(url, data=dl_xml, headers=headers, method="POST")
        try:
            resp = client.opener.open(req, timeout=max(client.timeout, 30.0))
            break
        except urllib.error.HTTPError as e:
            if e.code in (404, 405):
                continue
            raise

    if resp is None:
        raise ConnectionError("NVR did not accept any download endpoint.")

    http_status = resp.status  # 206 = partial content, 200 = full

    if existing_size > 0 and http_status == 200:
        existing_size = 0
        file_mode     = "wb"
        print("\n      (server ignored Range header — restarting from scratch)", flush=True)
    elif existing_size > 0 and http_status == 206:
        file_mode = "ab"
        print(f"\n      (resuming from {existing_size/(1024*1024):.1f} MB)", flush=True)
    else:
        file_mode = "wb"

    chunk_size       = 512 * 1024
    newly_downloaded = 0
    t0               = time.time()

    os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)

    with resp, open(dest_path, file_mode) as f_out:
        while True:
            chunk = resp.read(chunk_size)
            if not chunk:
                break
            f_out.write(chunk)
            newly_downloaded += len(chunk)

            if limit_mbps > 0:
                target_time = ((existing_size + newly_downloaded) * 8) / (limit_mbps * 1e6)
                elapsed = time.time() - t0
                if target_time > elapsed:
                    time.sleep(target_time - elapsed)

            if progress_callback:
                total_so_far = existing_size + newly_downloaded
                elapsed      = max(0.001, time.time() - t0)
                speed        = (newly_downloaded / (1024.0 * 1024.0)) / elapsed
                progress_callback(total_so_far, speed)

    total_on_disk = existing_size + newly_downloaded

    if verify_size and expected_size > 0 and total_on_disk < expected_size:
        raise DownloadIncompleteError(
            f"Incomplete: {total_on_disk:,} of {expected_size:,} bytes "
            f"({100*total_on_disk/expected_size:.1f}%)"
        )

    return total_on_disk


# ── Download a channel's segments (optionally selected) ───────────────────────

def download_segments(client, segments, ch, date_str, save_dir,
                      convert, keep_raw, limit_mbps, log):
    total_ok  = 0
    total_err = 0

    for idx, seg in enumerate(segments, 1):
        safe_start = (seg.start_time or "unknown") \
            .replace(":", "-").replace("T", "_").rstrip("Z")[:19]
        base_name = f"ch{ch}_{safe_start}"
        dest_dir  = os.path.join(save_dir, date_str, f"ch{ch}")
        dest_ps   = os.path.join(dest_dir, base_name + ".ps")
        dest_mp4  = os.path.join(dest_dir, base_name + ".mp4")

        final = dest_mp4 if (convert and not keep_raw) else dest_ps
        if os.path.exists(final) and os.path.getsize(final) > 0:
            size_match = re.search(r'[?&]size=(\d+)', seg.playback_uri)
            expected   = int(size_match.group(1)) if size_match else 0
            if expected == 0 or os.path.getsize(final) >= expected:
                log.info(f"  [{idx}/{len(segments)}] Skip (complete): {os.path.basename(final)}")
                total_ok += 1
                continue

        size_str = f"{seg.size_mb:.1f} MB" if seg.size_mb > 0 else "?"
        existing = os.path.exists(dest_ps) and os.path.getsize(dest_ps) > 0
        action   = "Resuming" if existing else "Downloading"
        log.info(f"  [{idx}/{len(segments)}] {action} {base_name}.ps  ({size_str})")

        os.makedirs(dest_dir, exist_ok=True)
        try:
            total_bytes = download_with_resume(
                client, seg.playback_uri, dest_ps,
                progress_callback=progress,
                limit_mbps=limit_mbps,
                verify_size=True,
            )
            print()
            log.info(f"    ✓ {total_bytes/(1024*1024):.1f} MB  →  {dest_ps}")

            if convert:
                log.info("    Converting to MP4 ...")
                ok = convert_to_mp4(dest_ps, dest_mp4)
                if ok:
                    log.info(f"    ✓ {dest_mp4}")
                    if not keep_raw:
                        os.remove(dest_ps)
                else:
                    log.warning("    ffmpeg conversion failed — raw .ps kept")

            total_ok += 1

        except DownloadIncompleteError as e:
            print()
            log.warning(f"    Partial saved (will resume next run): {e}")
            total_err += 1
        except Exception as e:
            print()
            log.error(f"    Failed: {e}")
            if os.path.exists(dest_ps) and os.path.getsize(dest_ps) == 0:
                try:
                    os.remove(dest_ps)
                except Exception:
                    pass
            total_err += 1

    return total_ok, total_err


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 56)
    print("   Hikvision NVR/DVR Backup Tool")
    print("=" * 56)

    # ── Connection details ────────────────────────────────────
    print("\n── Device Connection ─────────────────────────────────")
    host      = ask("NVR/DVR IP address", "192.168.2.241")
    http_port = ask_int("HTTP port", 8080)
    username  = ask("Username", "admin")
    password  = getpass.getpass("Password: ") or ask("Password (visible)", "")

    # ── Connect and show device info ──────────────────────────
    # (log goes to stdout; file handler added after we have save_dir or skip)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)s  %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    log = logging.getLogger(__name__)

    print("\n" + "=" * 56)
    log.info(f"Connecting to {host}:{http_port} ...")
    client = PureHikClient(host, http_port, username, password)

    try:
        info = client.get_device_info()
        log.info(f"Device : {info.model}  |  FW: {info.firmware}  |  SN: {info.serial}")
    except Exception as e:
        log.error(f"Cannot reach device: {e}")
        sys.exit(1)

    try:
        status = client.get_device_status()
        log.info(f"Status : CPU {status.cpu_utilization}%  |  "
                 f"Uptime {status.uptime_seconds // 3600}h  |  "
                 f"Time {status.current_time}")
    except Exception:
        pass

    try:
        disks = client.get_storage()
        for d in disks:
            log.info(f"Storage: {d.name}  {d.capacity_gb:.0f} GB  "
                     f"({d.free_space_mb/1024:.0f} GB free)  status={d.status}")
    except Exception:
        pass

    # ── Action menu ───────────────────────────────────────────
    while True:
        print("\n── What would you like to do? ────────────────────────")
        print("  1  List recordings for a channel / date")
        print("  2  Download recordings")
        print("  3  List then download")
        print("  Q  Quit")
        choice = input("Choice [1/2/3/Q]: ").strip().upper()
        if choice == "Q":
            print("Bye.")
            sys.exit(0)
        if choice in ("1", "2", "3"):
            break
        print("  Enter 1, 2, 3 or Q.")

    do_list     = choice in ("1", "3")
    do_download = choice in ("2", "3")

    # ── Date ──────────────────────────────────────────────────
    print("\n── Date ──────────────────────────────────────────────")
    print("  Leave blank for YESTERDAY (UTC).")
    date_input = ask("Date (YYYY-MM-DD)", "").strip()
    if date_input:
        try:
            target_day = datetime.strptime(date_input, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            print("Invalid date — using yesterday.")
            target_day = None
    else:
        target_day = None

    if target_day is None:
        now_utc    = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        target_day = now_utc - timedelta(days=1)

    start_utc = target_day
    end_utc   = target_day + timedelta(days=1)
    date_str  = target_day.strftime("%Y-%m-%d")
    print(f"  Date : {date_str}  "
          f"({start_utc.strftime('%Y-%m-%dT%H:%M:%SZ')} → {end_utc.strftime('%Y-%m-%dT%H:%M:%SZ')})")

    # ── Channels ──────────────────────────────────────────────
    print("\n── Channels ──────────────────────────────────────────")
    ch_input = ask("Channel numbers (comma-separated)", "2,3,4")
    channels = []
    for c in ch_input.split(","):
        c = c.strip()
        if c.isdigit():
            channels.append(int(c))
    if not channels:
        print("No valid channels. Exiting.")
        sys.exit(1)
    print(f"  Channels: {channels}")

    # ── LIST ──────────────────────────────────────────────────
    if do_list:
        list_recordings(client, channels, start_utc, end_utc)
        if not do_download:
            input("Press Enter to exit...")
            return

    # ── DOWNLOAD options ──────────────────────────────────────
    print("\n── Save Location ─────────────────────────────────────")
    save_dir = ask("Save folder", r"C:\NVR-Backup")

    print("\n── Options ───────────────────────────────────────────")
    convert  = ask_yn("Convert .ps to .mp4? (requires ffmpeg)", default=False)
    keep_raw = True
    if convert:
        keep_raw = ask_yn("Keep raw .ps after conversion?", default=False)
    limit_raw = ask("Bandwidth limit MB/s (0 = unlimited)", "0")
    try:
        limit_mbps = float(limit_raw) * 8
    except ValueError:
        limit_mbps = 0.0

    # Add file log handler now that we have save_dir
    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hikvision_backup.log")
    logging.getLogger().addHandler(logging.FileHandler(log_path, encoding="utf-8"))

    # ── Download loop ─────────────────────────────────────────
    log.info(f"\n=== Starting backup for {date_str} ===")
    grand_ok  = 0
    grand_err = 0

    for ch in channels:
        log.info(f"\nChannel {ch}:")
        try:
            segments = client.search_recordings(ch, start_utc, end_utc, max_results=500)
        except Exception as e:
            log.error(f"  Search failed: {e}")
            grand_err += 1
            continue

        if not segments:
            log.info("  No recordings found.")
            continue

        total_size_mb = sum(s.size_mb for s in segments)
        log.info(f"  Found {len(segments)} segment(s)  (~{total_size_mb:.0f} MB total)")

        # ── Optional per-channel segment selection ────────────
        print(f"\n  Channel {ch} — {len(segments)} segment(s):")
        for i, seg in enumerate(segments, 1):
            dur  = fmt_duration(seg.start_time, seg.end_time) \
                   if seg.start_time and seg.end_time else "?"
            size = f"{seg.size_mb:.1f} MB" if seg.size_mb > 0 else "?"
            print(f"    {i:>3}.  {seg.start_time}  →  {seg.end_time}  "
                  f"dur={dur}  size={size}")

        sel_raw = ask(
            f"  Which to download? (e.g. 1,3-5,7  or  all)",
            "all"
        ).strip().lower()

        if sel_raw in ("all", "a", ""):
            selected = segments
        else:
            indices = set()
            for part in sel_raw.split(","):
                part = part.strip()
                m = re.match(r'^(\d+)-(\d+)$', part)
                if m:
                    indices.update(range(int(m.group(1)), int(m.group(2)) + 1))
                elif part.isdigit():
                    indices.add(int(part))
            selected = [segments[i-1] for i in sorted(indices)
                        if 1 <= i <= len(segments)]
            if not selected:
                log.info("  No valid indices — skipping channel.")
                continue
            log.info(f"  Selected {len(selected)} of {len(segments)} segment(s).")

        ok, err = download_segments(
            client, selected, ch, date_str, save_dir,
            convert, keep_raw, limit_mbps, log
        )
        grand_ok  += ok
        grand_err += err

    log.info(f"\n=== Done: {grand_ok} saved, {grand_err} failed/incomplete ===")
    log.info(f"Log: {log_path}")
    input("\nPress Enter to exit...")


if __name__ == "__main__":
    main()
