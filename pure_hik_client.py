#!/usr/bin/env python3
"""
OpenHik-DVR - Pure Python ISAPI Client (Zero Dependencies, Zero DLLs)
Works across Linux, macOS, Windows, Android (Termux/Kivy), Docker, and Raspberry Pi.

Clean-room implementation of the Hikvision ISAPI (Intelligent Security API) over HTTP/REST.
Supports both V3.x (std-cgi.com) and V4.x+ (hikvision.com) firmware generations.
"""

from __future__ import annotations
import socket
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape
from datetime import datetime, timezone, timedelta
import os
import sys
import time
import re
import subprocess

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# ── Exceptions ────────────────────────────────────────────────────────────────

class DownloadIncompleteError(Exception):
    """Raised when downloaded file size is less than the expected size in playbackURI."""
    pass

# ── XML helpers ───────────────────────────────────────────────────────────────

def _lname(tag: str) -> str:
    """Strips XML namespaces to ensure cross-firmware compatibility (e.g. {http://...}Tag -> Tag)."""
    return tag.rsplit('}', 1)[-1]

def _find_text(element: ET.Element, name: str) -> str:
    """Finds first matching child element text by local tag name."""
    for child in element.iter():
        if _lname(child.tag) == name and child.text and child.text.strip():
            return child.text.strip()
    return ""

# ── ffmpeg helper ─────────────────────────────────────────────────────────────

def convert_to_mp4(input_path: str, output_mp4_path: str, ffmpeg_path: str = "ffmpeg") -> bool:
    """
    Remuxes a Hikvision MPEG Program Stream (.ps) into a standard MP4 container.

    Uses '-f mpeg' to explicitly name the MPEG-PS demuxer (required in ffmpeg 9.x+
    where the old 'mpegps' alias was removed).

    Hikvision devices record audio as AAC, G.711 (PCMU/PCMA), G.722, G.725, or PCM.
    MP4 natively supports AAC but not the others, so three passes are attempted in order:

      Pass 1 — copy video + copy audio  (lossless; works when source audio is AAC)
      Pass 2 — copy video + transcode audio to AAC 128k
               (for G.711 / G.722 / G.725 / PCM sources)
      Pass 3 — copy video, drop audio   (last resort if audio track is unreadable)

    Returns True if an MP4 was produced successfully, False otherwise.
    """
    base_cmd = [ffmpeg_path, "-y", "-f", "mpeg", "-i", input_path]
    passes = [
        base_cmd + ["-c:v", "copy", "-c:a", "copy", output_mp4_path],
        base_cmd + ["-c:v", "copy", "-c:a", "aac", "-b:a", "128k", output_mp4_path],
        base_cmd + ["-c:v", "copy", "-an", output_mp4_path],
    ]
    for attempt, cmd in enumerate(passes, 1):
        try:
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=600,
            )
            if result.returncode == 0 and os.path.exists(output_mp4_path):
                return True
            # pass 1 failed — try pass 2 (audio-free)
        except FileNotFoundError:
            # ffmpeg not installed or not in PATH
            return False
        except subprocess.TimeoutExpired:
            return False
    return False

# ── Data classes ──────────────────────────────────────────────────────────────

class DeviceInfo:
    def __init__(self, model: str, serial: str, firmware: str, mac: str, dev_type: str, encoder: str):
        self.model = model
        self.serial = serial
        self.firmware = firmware
        self.mac = mac
        self.device_type = dev_type
        self.encoder_version = encoder

    def __repr__(self):
        return f"<DeviceInfo model={self.model} fw={self.firmware} mac={self.mac}>"


class DeviceStatus:
    def __init__(self, current_time: str, uptime_seconds: int, cpu_utilization: int):
        self.current_time = current_time
        self.uptime_seconds = uptime_seconds
        self.cpu_utilization = cpu_utilization

    def __repr__(self):
        return f"<DeviceStatus time={self.current_time} uptime={self.uptime_seconds}s cpu={self.cpu_utilization}%>"


class DeviceStorage:
    def __init__(self, hdd_id: str, name: str, hdd_type: str, status: str, capacity_mb: int, free_space_mb: int):
        self.hdd_id = hdd_id
        self.name = name
        self.hdd_type = hdd_type
        self.status = status
        self.capacity_mb = capacity_mb
        self.capacity_gb = capacity_mb / 1024.0
        self.free_space_mb = free_space_mb

    def __repr__(self):
        return f"<DeviceStorage {self.name} status={self.status} capacity={self.capacity_gb:.1f}GB>"


class VideoChannel:
    def __init__(self, channel_id: int, name: str, enabled: bool, channel_type: str = "analog"):
        self.channel_id = channel_id
        self.name = name
        self.enabled = enabled
        self.channel_type = channel_type

    def __repr__(self):
        return f"<VideoChannel id={self.channel_id} type={self.channel_type} name='{self.name}' enabled={self.enabled}>"


class StreamChannel:
    def __init__(self, stream_id: str, name: str, width: int, height: int, codec: str, fps: float):
        self.stream_id = stream_id
        self.name = name
        self.width = width
        self.height = height
        self.codec = codec
        self.fps = fps

    def __repr__(self):
        return f"<StreamChannel id={self.stream_id} res={self.width}x{self.height} codec={self.codec} fps={self.fps}>"


class RecordingSegment:
    _DT_FMTS = ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S")

    def __init__(self, track_id: int, start_time: str, end_time: str, playback_uri: str, size_bytes: int = 0):
        self.track_id = track_id
        self.start_time = start_time
        self.end_time = end_time
        self.playback_uri = playback_uri
        self.size_bytes = size_bytes
        self.size_mb = size_bytes / (1024.0 * 1024.0)

    @staticmethod
    def _parse_dt(s: str) -> datetime | None:
        for fmt in RecordingSegment._DT_FMTS:
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
        return None

    @property
    def duration_seconds(self) -> int:
        """Wall-clock duration of this segment in whole seconds (0 if timestamps cannot be parsed)."""
        t0 = self._parse_dt(self.start_time)
        t1 = self._parse_dt(self.end_time)
        if t0 and t1:
            return max(0, int((t1 - t0).total_seconds()))
        return 0

    def __repr__(self):
        return f"<RecordingSegment track={self.track_id} {self.start_time}->{self.end_time} size={self.size_mb:.2f}MB>"


class EventAlert:
    """Represents a real-time hardware alarm or motion detection notification from alertStream."""
    def __init__(self, event_type: str, channel_id: int, event_state: str, date_time: str, description: str):
        self.event_type = event_type
        self.channel_id = channel_id
        self.event_state = event_state
        self.date_time = date_time
        self.description = description

    def is_active(self) -> bool:
        return self.event_state.lower() == "active"

    def is_motion(self) -> bool:
        return self.event_type.upper() in ("VMD", "MOTION")

    def __repr__(self):
        return f"<EventAlert type={self.event_type} ch={self.channel_id} state={self.event_state} time={self.date_time}>"

# ── Main client ───────────────────────────────────────────────────────────────

class PureHikClient:
    """
    Pure Python Hikvision ISAPI Client.
    Communicates directly with the DVR/NVR/IPCam over HTTP/HTTPS with Digest Authentication.
    """
    def __init__(self, host: str, port: int = 80, user: str = "admin", password: str = "",
                 use_https: bool = False, timeout: float = 10.0):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.protocol = "https" if use_https else "http"
        self.base_url = f"{self.protocol}://{self.host}:{self.port}"
        self.timeout = timeout

        # Configure HTTP Digest Authentication Handler
        self.passman = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        self.passman.add_password(None, self.base_url, self.user, self.password)
        self.auth_handler = urllib.request.HTTPDigestAuthHandler(self.passman)
        self.opener = urllib.request.build_opener(self.auth_handler)

    def _request(self, method: str, path: str, data: bytes = None, headers: dict = None) -> bytes:
        url = self.base_url + path
        hdrs = {'User-Agent': 'OpenHik-Client/1.0'}
        if headers:
            hdrs.update(headers)
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        with self.opener.open(req, timeout=self.timeout) as resp:
            return resp.read()

    # ── Connection ────────────────────────────────────────────────────────────

    def ping(self) -> bool:
        """
        Quick connectivity and authentication check.
        Returns True if the device responds to /ISAPI/System/deviceInfo, False otherwise.
        Useful for verifying credentials before starting a long operation.
        """
        try:
            self._request("GET", "/ISAPI/System/deviceInfo")
            return True
        except Exception:
            return False

    # ── Device info ───────────────────────────────────────────────────────────

    def get_device_info(self) -> DeviceInfo:
        """Queries system information: Model, Serial, Firmware Version, MAC, and Encoder."""
        raw = self._request("GET", "/ISAPI/System/deviceInfo")
        root = ET.fromstring(raw)
        return DeviceInfo(
            model=_find_text(root, "model"),
            serial=_find_text(root, "serialNumber"),
            firmware=_find_text(root, "firmwareVersion"),
            mac=_find_text(root, "macAddress"),
            dev_type=_find_text(root, "deviceType"),
            encoder=_find_text(root, "encoderVersion")
        )

    def get_device_status(self) -> DeviceStatus:
        """Queries real-time hardware status: CPU load, Uptime, and Current Device Time."""
        raw = self._request("GET", "/ISAPI/System/status")
        root = ET.fromstring(raw)
        uptime_str = _find_text(root, "deviceUpTime")
        cpu_str = _find_text(root, "cpuUtilization")
        return DeviceStatus(
            current_time=_find_text(root, "currentDeviceTime"),
            uptime_seconds=int(uptime_str) if uptime_str.isdigit() else 0,
            cpu_utilization=int(cpu_str) if cpu_str.isdigit() else 0
        )

    def get_time(self) -> dict:
        """Queries hardware RTC time and NTP configuration."""
        raw = self._request("GET", "/ISAPI/System/time")
        root = ET.fromstring(raw)
        return {
            "timeMode": _find_text(root, "timeMode"),
            "localTime": _find_text(root, "localTime"),
            "timeZone": _find_text(root, "timeZone")
        }

    def set_time(self, local_dt: datetime, tz_offset: str | None = None) -> bool:
        """
        Synchronizes hardware RTC time.
        Format: YYYY-MM-DDTHH:MM:SS+HH:MM (e.g. 2026-10-14T12:00:00+03:00)
        If tz_offset is None, it is derived from local_dt.tzinfo or the local system timezone.
        """
        if tz_offset is None:
            if local_dt.tzinfo is not None:
                raw_z = local_dt.strftime("%z")
                tz_offset = f"{raw_z[:3]}:{raw_z[3:]}" if raw_z else "+00:00"
            else:
                offset_sec = -time.timezone if (time.localtime().tm_isdst == 0) else -time.altzone
                sign = "-" if offset_sec < 0 else "+"
                total_sec = abs(offset_sec)
                hours = total_sec // 3600
                minutes = (total_sec % 3600) // 60
                tz_offset = f"{sign}{hours:02d}:{minutes:02d}"

        time_str = local_dt.strftime("%Y-%m-%dT%H:%M:%S") + tz_offset
        xml_payload = f"""<?xml version="1.0" encoding="UTF-8"?>
<Time version="1.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">
<timeMode>manual</timeMode>
<localTime>{time_str}</localTime>
</Time>"""
        try:
            self._request("PUT", "/ISAPI/System/time",
                          data=xml_payload.encode('utf-8'),
                          headers={'Content-Type': 'application/xml'})
            return True
        except Exception:
            return False

    # ── Storage & channels ────────────────────────────────────────────────────

    def get_storage(self) -> list[DeviceStorage]:
        """Queries hard drive capacity, partition status, and remaining free space."""
        raw = self._request("GET", "/ISAPI/ContentMgmt/Storage")
        root = ET.fromstring(raw)
        storages = []
        for hdd in root.iter():
            if _lname(hdd.tag) == "hdd":
                cap = _find_text(hdd, "capacity")
                free = _find_text(hdd, "freeSpace")
                storages.append(DeviceStorage(
                    hdd_id=_find_text(hdd, "id"),
                    name=_find_text(hdd, "hddName"),
                    hdd_type=_find_text(hdd, "hddType"),
                    status=_find_text(hdd, "status"),
                    capacity_mb=int(cap) if cap.isdigit() else 0,
                    free_space_mb=int(free) if free.isdigit() else 0
                ))
        return storages

    def get_video_channels(self) -> list[VideoChannel]:
        """
        Lists all camera input channels (analog inputs and NVR IP proxy channels).
        Properly handles hybrid DVRs where IP proxy channels coexist with analog channels.
        Raises on authentication failures (401/403) and network errors.
        """
        channels: list[VideoChannel] = []
        analog_err = None

        # 1. Analog / Direct Video Input Channels
        try:
            raw = self._request("GET", "/ISAPI/System/Video/inputs/channels")
            root = ET.fromstring(raw)
            for ch in root.iter():
                if _lname(ch.tag) == "VideoInputChannel":
                    cid = _find_text(ch, "id")
                    enabled_str = _find_text(ch, "videoInputEnabled")
                    # Default to True when the field is absent (older firmware omits it)
                    enabled = (enabled_str.lower() == "true") if enabled_str else True
                    channels.append(VideoChannel(
                        channel_id=int(cid) if cid.isdigit() else 0,
                        name=_find_text(ch, "name"),
                        enabled=enabled,
                        channel_type="analog"
                    ))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise
            analog_err = e
        except (ConnectionError, socket.error, urllib.error.URLError):
            raise
        except Exception as e:
            analog_err = e

        # 2. Digital / IP Proxy Channels (on NVRs and hybrid DVRs)
        try:
            raw = self._request("GET", "/ISAPI/ContentMgmt/InputProxy/channels")
            root = ET.fromstring(raw)
            for ch in root.iter():
                if _lname(ch.tag) == "InputProxyChannel":
                    cid = _find_text(ch, "id")
                    int_cid = int(cid) if cid.isdigit() else 0
                    channels.append(VideoChannel(
                        channel_id=int_cid,
                        name=_find_text(ch, "name"),
                        enabled=True,
                        channel_type="ip"
                    ))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise
            if analog_err and not channels:
                raise analog_err
        except (ConnectionError, socket.error, urllib.error.URLError):
            raise
        except Exception:
            if analog_err and not channels:
                raise analog_err

        return channels

    def set_channel_name(self, channel_id: int, new_name: str) -> bool:
        """Updates OSD camera channel title."""
        escaped_name = escape(new_name)
        xml_payload = f"""<?xml version="1.0" encoding="UTF-8"?>
<VideoInputChannel version="1.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">
<id>{channel_id}</id>
<inputPort>{channel_id}</inputPort>
<name>{escaped_name}</name>
<videoInputEnabled>true</videoInputEnabled>
</VideoInputChannel>"""
        try:
            self._request("PUT", f"/ISAPI/System/Video/inputs/channels/{channel_id}",
                          data=xml_payload.encode('utf-8'),
                          headers={'Content-Type': 'application/xml'})
            return True
        except Exception:
            return False

    def get_streaming_channels(self) -> list[StreamChannel]:
        """Queries streaming profile channels (resolutions, codec, FPS, bitrates)."""
        raw = self._request("GET", "/ISAPI/Streaming/channels")
        root = ET.fromstring(raw)
        streams = []
        for ch in root.iter():
            if _lname(ch.tag) == "StreamingChannel":
                cid = _find_text(ch, "id")
                w = _find_text(ch, "videoResolutionWidth")
                h = _find_text(ch, "videoResolutionHeight")
                fps = _find_text(ch, "maxFrameRate")
                streams.append(StreamChannel(
                    stream_id=cid,
                    name=_find_text(ch, "channelName"),
                    width=int(w) if w.isdigit() else 0,
                    height=int(h) if h.isdigit() else 0,
                    codec=_find_text(ch, "videoCodecType"),
                    fps=float(fps) / 100.0 if fps.isdigit() else 0.0
                ))
        return streams

    def get_snapshot(self, channel_id: int, stream_index: int = 1) -> bytes | None:
        """
        Captures a live full-resolution JPEG snapshot from the specified camera.
        Stream index 1 = Main stream (high-res), index 2 = Sub stream.
        Channel 1 Main = 101, Sub = 102; Channel 11 Main = 1101.
        """
        track_id = channel_id * 100 + stream_index
        endpoints = [
            f"/ISAPI/Streaming/channels/{track_id}/picture",
            f"/ISAPI/Streaming/channels/{channel_id}/picture"
        ]
        for ep in endpoints:
            try:
                data = self._request("GET", ep)
                if data and data[:2] == b'\xff\xd8':  # JPEG Magic SOI
                    return data
            except Exception:
                continue
        return None

    # ── Recordings ────────────────────────────────────────────────────────────

    def search_recordings(self, channel_id: int, start_time: datetime, end_time: datetime,
                          stream_index: int = 1, max_results: int = 10000) -> list[RecordingSegment]:
        """
        Searches recordings stored on the DVR hard disk for a specific channel and time range.
        Automatically handles pagination when results exceed device single-page limit.

        NOTE ON TIMEZONES:
        Hikvision embedded firmware indexes recordings using the DVR internal local clock,
        formatting index entries with a trailing 'Z'. Therefore, pass device local time
        (e.g., datetime.now() or datetime(YYYY, MM, DD, HH, MM, SS)).
        """
        track_id = channel_id * 100 + stream_index
        start_str = start_time.strftime("%Y-%m-%dT%H:%M:%SZ")
        end_str = end_time.strftime("%Y-%m-%dT%H:%M:%SZ")

        segments: list[RecordingSegment] = []
        pos = 0
        page_size = min(max_results, 100)

        while pos < max_results:
            current_batch = min(page_size, max_results - pos)
            search_xml = f"""<?xml version="1.0" encoding="utf-8"?>
<CMSearchDescription>
<searchID>C1C1C1C1-0000-0000-0000-000000000001</searchID>
<trackList><trackID>{track_id}</trackID></trackList>
<timeSpanList><timeSpan><startTime>{start_str}</startTime><endTime>{end_str}</endTime></timeSpan></timeSpanList>
<maxResults>{current_batch}</maxResults>
<searchResultPostion>{pos}</searchResultPostion>
<metadataList><metadataDescriptor>//recordType.meta.std-cgi.com</metadataDescriptor></metadataList>
</CMSearchDescription>"""

            raw = self._request("POST", "/ISAPI/ContentMgmt/search",
                                data=search_xml.encode('utf-8'),
                                headers={'Content-Type': 'application/xml'})
            root = ET.fromstring(raw)
            status_str = _find_text(root, "responseStatusStrg")

            batch_items = []
            for it in root.iter():
                if _lname(it.tag) == "searchMatchItem":
                    s = _find_text(it, "startTime")
                    e = _find_text(it, "endTime")
                    uri = _find_text(it, "playbackURI")
                    size_match = re.search(r'[?&]size=(\d+)', uri)
                    sz = int(size_match.group(1)) if size_match else 0
                    if uri:
                        batch_items.append(RecordingSegment(track_id, s, e, uri, sz))

            if not batch_items:
                break

            segments.extend(batch_items)
            pos += len(batch_items)

            if status_str.upper() != "MORE":
                break

        return segments

    def get_available_dates(self, channel_id: int, days: int = 30,
                            stream_index: int = 1) -> list[datetime]:
        """
        Scans the last *days* calendar days and returns those that have at least one recording
        for the given channel.

        Useful for building a date-picker that only shows dates with footage.
        Each returned datetime is at midnight (00:00:00) of the day (no timezone).
        """
        available: list[datetime] = []
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        for offset in range(days):
            day_start = today - timedelta(days=offset)
            day_end = day_start + timedelta(days=1) - timedelta(seconds=1)
            try:
                segs = self.search_recordings(channel_id, day_start, day_end,
                                              stream_index=stream_index, max_results=1)
                if segs:
                    available.append(day_start)
            except Exception:
                continue
        return sorted(available)

    # ── Download ──────────────────────────────────────────────────────────────

    def _open_download(self, playback_uri: str, resume_offset: int = 0):
        """
        Opens a streaming HTTP response for a recording download.
        Returns (response, expected_size).
        Tries /ISAPI/ContentMgmt/download then the legacy PSIA endpoint.
        """
        escaped_uri = escape(playback_uri)
        dl_xml = f"""<?xml version="1.0" encoding="utf-8"?>
<downloadRequest version="1.0" xmlns="http://urn:selfextension:psiaext-ver10-xsd">
<playbackURI>{escaped_uri}</playbackURI>
</downloadRequest>"""

        expected_size = 0
        m = re.search(r'[?&]size=(\d+)', playback_uri)
        if m:
            expected_size = int(m.group(1))

        paths = ["/ISAPI/ContentMgmt/download", "/PSIA/Custom/SelfExt/ContentMgmt/download"]
        for p in paths:
            try:
                hdrs = {
                    'Content-Type': 'application/xml',
                    'User-Agent': 'OpenHik-Client/1.0',
                }
                if resume_offset > 0:
                    hdrs['Range'] = f'bytes={resume_offset}-'
                req = urllib.request.Request(
                    self.base_url + p,
                    data=dl_xml.encode('utf-8'),
                    headers=hdrs,
                )
                resp = self.opener.open(req, timeout=max(self.timeout, 30.0))
                return resp, expected_size
            except urllib.error.HTTPError as e:
                if e.code in (404, 405):
                    continue
                raise

        raise ConnectionError("DVR did not accept download endpoint")

    def download_recording(self, playback_uri: str, destination_path: str,
                           progress_callback=None, limit_mbps: float = 0.0,
                           verify_size: bool = True) -> int:
        """
        Downloads a recording directly from the DVR hard drive over HTTP.
        Supports progress callbacks, bandwidth limiting, and size integrity verification.
        Returns total bytes downloaded (not including any pre-existing partial file).

        For resumable downloads across retries, use download_with_resume() instead.
        """
        resp, expected_size = self._open_download(playback_uri)

        total_downloaded = 0
        t0 = time.time()
        chunk_size = 512 * 1024  # 512 KB

        with resp, open(destination_path, "wb") as f_out:
            while True:
                chunk = resp.read(chunk_size)
                if not chunk:
                    break
                f_out.write(chunk)
                total_downloaded += len(chunk)

                if limit_mbps > 0:
                    target_time = (total_downloaded * 8) / (limit_mbps * 1e6)
                    elapsed = time.time() - t0
                    if target_time > elapsed:
                        time.sleep(target_time - elapsed)

                if progress_callback:
                    elapsed = max(0.001, time.time() - t0)
                    speed_mbs = (total_downloaded / (1024.0 * 1024.0)) / elapsed
                    progress_callback(total_downloaded, speed_mbs)

        if verify_size and expected_size > 0 and total_downloaded < expected_size:
            raise DownloadIncompleteError(
                f"Download interrupted: received {total_downloaded} bytes, "
                f"expected {expected_size} bytes"
            )
        return total_downloaded

    def download_with_resume(self, playback_uri: str, destination_path: str,
                             progress_callback=None, limit_mbps: float = 0.0,
                             verify_size: bool = True) -> int:
        """
        Downloads a recording with HTTP Range resume support.

        If *destination_path* already exists it is treated as a partial download:
        the existing byte count is sent as a Range header so the DVR picks up from
        that offset.  The file is opened in append mode and the new bytes are added
        to the end.

        progress_callback(bytes_so_far, speed_mbs) is called after each chunk if provided.
        Returns the total file size in bytes (existing + newly downloaded).

        Raises DownloadIncompleteError if the final file is smaller than the size
        embedded in the playbackURI.
        """
        # How much do we already have?
        existing_bytes = 0
        if os.path.exists(destination_path):
            existing_bytes = os.path.getsize(destination_path)

        resp, expected_size = self._open_download(playback_uri, resume_offset=existing_bytes)

        # Some DVRs ignore the Range header and return 200 (full file) instead of 206.
        # Detect this and truncate so we don't double-write the start of the file.
        status = getattr(resp, 'status', None) or getattr(resp, 'code', None)
        if status == 200 and existing_bytes > 0:
            existing_bytes = 0  # server sent from the beginning — start fresh

        new_bytes = 0
        t0 = time.time()
        chunk_size = 512 * 1024  # 512 KB

        mode = "ab" if existing_bytes > 0 else "wb"
        with resp, open(destination_path, mode) as f_out:
            while True:
                chunk = resp.read(chunk_size)
                if not chunk:
                    break
                f_out.write(chunk)
                new_bytes += len(chunk)

                if limit_mbps > 0:
                    target_time = (new_bytes * 8) / (limit_mbps * 1e6)
                    elapsed = time.time() - t0
                    if target_time > elapsed:
                        time.sleep(target_time - elapsed)

                if progress_callback:
                    elapsed = max(0.001, time.time() - t0)
                    speed_mbs = (new_bytes / (1024.0 * 1024.0)) / elapsed
                    progress_callback(existing_bytes + new_bytes, speed_mbs)

        total = existing_bytes + new_bytes
        if verify_size and expected_size > 0 and total < expected_size:
            raise DownloadIncompleteError(
                f"Download incomplete: {total} / {expected_size} bytes "
                f"(missing {expected_size - total} bytes)"
            )
        return total

    # ── Streaming ─────────────────────────────────────────────────────────────

    def get_rtsp_url(self, channel_id: int, stream_index: int = 1, rtsp_port: int = 554) -> str:
        """
        Generates a standard RTSP streaming URL for use with VLC, FFmpeg, OpenCV, or NVRs.
        Channel 1 Main Stream = 101, Sub Stream = 102.
        """
        import urllib.parse
        track_id = channel_id * 100 + stream_index
        enc_u = urllib.parse.quote(self.user, safe='')
        enc_p = urllib.parse.quote(self.password, safe='')
        return f"rtsp://{enc_u}:{enc_p}@{self.host}:{rtsp_port}/Streaming/channels/{track_id}"

    # ── Events ────────────────────────────────────────────────────────────────

    def listen_events(self, callback, stop_event=None, retry_delay: float = 3.0):
        """
        Listens to real-time event alerts from /ISAPI/Event/notification/alertStream over HTTP.
        Dispatches EventAlert objects to callback whenever motion, tampering, or alarms trigger.
        Automatically reconnects on network drop; halts immediately on authentication failure (401/403)
        to prevent hardware account lockout.
        """
        _MAX_BUF = 64 * 1024  # cap per-event buffer at 64 KB to avoid unbounded growth

        while stop_event is None or not stop_event.is_set():
            try:
                url = self.base_url + "/ISAPI/Event/notification/alertStream"
                req = urllib.request.Request(url, headers={'User-Agent': 'OpenHik-Client/1.0'})
                with self.opener.open(req, timeout=30.0) as resp:
                    buf = b""
                    while stop_event is None or not stop_event.is_set():
                        line = resp.readline()
                        if not line:
                            break
                        if line.startswith(b"--"):
                            if b"<EventNotificationAlert" in buf:
                                try:
                                    s_idx = buf.find(b"<EventNotificationAlert")
                                    e_idx = buf.find(b"</EventNotificationAlert>") + len(b"</EventNotificationAlert>")
                                    if s_idx != -1 and e_idx > s_idx:
                                        xml_bytes = buf[s_idx:e_idx]
                                        root = ET.fromstring(xml_bytes)
                                        cid_str = _find_text(root, "channelID")
                                        ev = EventAlert(
                                            event_type=_find_text(root, "eventType"),
                                            channel_id=int(cid_str) if cid_str.isdigit() else 0,
                                            event_state=_find_text(root, "eventState"),
                                            date_time=_find_text(root, "dateTime"),
                                            description=_find_text(root, "eventDescription")
                                        )
                                        callback(ev)
                                except Exception:
                                    pass
                            buf = b""
                        else:
                            buf += line
                            if len(buf) > _MAX_BUF:
                                buf = b""  # discard malformed/oversized event, keep listening
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    raise PermissionError(
                        f"Authentication failed on alertStream (HTTP {e.code}). "
                        f"Aborting to prevent device lockout."
                    )
                if stop_event is not None and stop_event.is_set():
                    break
                time.sleep(retry_delay)
            except Exception:
                if stop_event is not None and stop_event.is_set():
                    break
                time.sleep(retry_delay)

    # ── System ────────────────────────────────────────────────────────────────

    def reboot(self) -> bool:
        """Remotely reboots the DVR."""
        try:
            self._request("PUT", "/ISAPI/System/reboot")
            return True
        except Exception:
            return False


# ── Quick demo ────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    # Configuration - Change to your Hikvision DVR details:
    DVR_HOST = "192.168.1.100"
    DVR_PORT = 80
    DVR_USER = "admin"
    DVR_PASS = "password123"

    print("Connecting to Hikvision DVR...")
    client = PureHikClient(DVR_HOST, DVR_PORT, DVR_USER, DVR_PASS)

    if not client.ping():
        print("Could not reach device — check IP, port and credentials.")
        sys.exit(1)

    try:
        # 1. Device Info
        info = client.get_device_info()
        print(f"Device: {info.model} | FW: {info.firmware} | MAC: {info.mac}")

        # 2. Hardware Status
        status = client.get_device_status()
        print(f"Status: CPU: {status.cpu_utilization}% | Uptime: {status.uptime_seconds // 3600}h | Time: {status.current_time}")

        # 3. Storage Info
        disks = client.get_storage()
        for d in disks:
            print(f"Storage: {d.name} ({d.capacity_gb:.1f} GB) - Status: {d.status}")

        # 4. Camera Channels
        channels = client.get_video_channels()
        print(f"Total Video Channels: {len(channels)}")
        for ch in channels[:4]:
            print(f"  Channel {ch.channel_id:02d}: {ch.name} (Active: {ch.enabled})")

    except Exception as e:
        print("Error:", e)
