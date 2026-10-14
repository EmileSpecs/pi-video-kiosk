"""Host telemetry. Missing sysfs files degrade to nulls."""

from __future__ import annotations

import socket
import time
from pathlib import Path
from typing import Any

from pikiosk import __version__


def read_cpu_temp(path: Path | None = None) -> float | None:
    file = path or Path("/sys/class/thermal/thermal_zone0/temp")
    try:
        return int(file.read_text(encoding="utf-8").strip()) / 1000
    except (OSError, ValueError):
        return None


def read_boot(stat_path: Path | None = None, uptime_path: Path | None = None) -> tuple[str | None, float | None]:
    boot = None
    uptime = None
    stat = stat_path or Path("/proc/stat")
    up = uptime_path or Path("/proc/uptime")
    try:
        for line in stat.read_text(encoding="utf-8").splitlines():
            if line.startswith("btime "):
                stamp = int(line.split()[1])
                boot = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(stamp))
    except (OSError, ValueError, IndexError):
        boot = None
    try:
        uptime = float(up.read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        uptime = None
    return boot, uptime


def local_ip(remote_host: str, remote_port: int = 80) -> str | None:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect((remote_host, remote_port))
            address = sock.getsockname()[0]
        finally:
            sock.close()
    except OSError:
        return None
    if address.startswith("127."):
        return None
    return str(address)


def cache_usage(directory: str) -> tuple[float, int]:
    root = Path(directory).expanduser()
    if not root.is_dir():
        return 0.0, 0
    total = 0
    items = 0
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix in {".part", ".json", ".tmp"}:
            continue
        try:
            total += path.stat().st_size
        except OSError:
            continue
        if path.suffix in {".mkv", ".mp4", ".webm", ".m4a"}:
            items += 1
    return round(total / (1024**3), 2), items


def collect(
    *,
    mqtt_host: str,
    ytdlp_version: str = "",
    mpv_version: str = "",
    cache_used_gb: float = 0,
    cache_items: int = 0,
    temp_path: Path | None = None,
    commit: str = "",
    audio_sink: str | None = None,
) -> dict[str, Any]:
    boot, uptime = read_boot()
    return {
        "cpu_temp": read_cpu_temp(temp_path),
        "boot_time": boot,
        "uptime_s": uptime,
        "ip": local_ip(mqtt_host),
        "hostname": socket.gethostname(),
        "version": __version__,
        "commit": commit,
        "audio_sink": audio_sink,
        "ytdlp_version": ytdlp_version,
        "mpv_version": mpv_version,
        "cache_used_gb": round(cache_used_gb, 2),
        "cache_items": cache_items,
        "throttled": "0x0",
        "load_1m": None,
    }
