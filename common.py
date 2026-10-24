#!/usr/bin/env python3
"""Shared helpers for the homelab monitor."""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
LOG = logging.getLogger("homelab-monitor")
STOP = threading.Event()
SNAPSHOT_LOCK = threading.Lock()
SNAPSHOT: dict[str, Any] = {"state": "starting"}
TELEGRAM_WARNED = False
DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
STATUS_LABELS = {
    "up": "OK",
    "degraded": "WARNUNG",
    "down": "AUSFALL",
    "unknown": "UNBEKANNT",
}


def load_env(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def load_config(path: str | None = None) -> dict[str, Any]:
    config_path = Path(path) if path else BASE_DIR / "config.json"
    with config_path.open(encoding="utf-8") as handle:
        config = json.load(handle)
    defaults = {
        "check_interval_seconds": 60,
        "failure_threshold": 2,
        "startup_grace_seconds": 120,
        "cycle_timeout_seconds": 180,
        "retention_days": 120,
        "dashboard_host": "0.0.0.0",
        "dashboard_port": 8090,
        "dashboard_refresh_seconds": 20,
        "data_dir": "data",
        "db_file": "monitor.db",
        "weekly_label": "Sonntag 09:00 (Europe/Berlin)",
        "telegram_timeout_seconds": 15,
        "max_message_chars": 4096,
        "check_timeout_seconds": 8,
    }
    settings = dict(defaults)
    settings.update(config.get("settings", {}))
    config["settings"] = settings
    config.setdefault("groups", [])
    config.setdefault("not_monitored", [])
    seen: set[str] = set()
    for check in config.get("checks", []):
        check_id = check.get("id")
        if not check_id or check_id in seen:
            raise ValueError(f"Ungültige oder doppelte Check-ID: {check_id!r}")
        seen.add(check_id)
        check.setdefault("notify", True)
        check.setdefault("notify_degraded", False)
    return config


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def fmt_dt(value: str | None) -> str:
    parsed = parse_iso(value)
    if not parsed:
        return "-"
    return parsed.astimezone().strftime("%d.%m.%Y %H:%M")


def format_duration(seconds: float) -> str:
    total = int(max(0, seconds))
    if total < 60:
        return f"{total} Sek."
    minutes = total // 60
    if minutes < 60:
        return f"{minutes} Min."
    hours = minutes // 60
    minutes = minutes % 60
    if hours < 24:
        return f"{hours} Std." + (f" {minutes} Min." if minutes else "")
    days = hours // 24
    hours = hours % 24
    return f"{days} Tg." + (f" {hours} Std." if hours else "")


def format_age(seconds: float) -> str:
    total = int(max(0, seconds))
    if total < 90:
        return f"vor {total} Sekunden"
    minutes = total // 60
    if minutes < 60:
        return f"vor {minutes} Minuten"
    hours = minutes // 60
    if hours < 48:
        return f"vor {hours} Stunden"
    return f"vor {hours // 24} Tagen"


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


def run_command(args: list[Any], timeout: float) -> tuple[int, str, str]:
    env = os.environ.copy()
    env["PATH"] = DEFAULT_PATH + ":" + env.get("PATH", "")
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    try:
        process = subprocess.run(
            [str(item) for item in args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        return process.returncode, process.stdout.strip(), process.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", f"Timeout nach {timeout}s"
    except FileNotFoundError as exc:
        return 127, "", f"Befehl nicht gefunden: {exc}"
    except Exception as exc:
        return 1, "", str(exc)


def sub_result(
    label: str,
    status: str,
    message: str,
    latency_ms: float = 0,
    details: dict[str, Any] | None = None,
    metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "label": label,
        "status": status,
        "message": message,
        "latency_ms": int(latency_ms),
        "details": details or {},
        "metrics": metrics or {},
    }


def json_path(data: Any, path: str) -> tuple[bool, Any]:
    current = data
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif (
            isinstance(current, list)
            and part.isdigit()
            and int(part) < len(current)
        ):
            current = current[int(part)]
        else:
            return False, None
    return True, current


def check_json_expect(text: str, expected: dict[str, Any]) -> tuple[bool, str, Any]:
    try:
        data = json.loads(text)
    except Exception as exc:
        return False, f"JSON ungültig: {exc}", None
    for key, wanted in expected.items():
        found, actual = json_path(data, key)
        if not found:
            return False, f"JSON-Feld {key} fehlt", data
        if actual != wanted:
            return False, f"JSON {key}={actual!r} (erwartet {wanted!r})", data
    return True, "", data


def read_meminfo() -> dict[str, int]:
    values: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if ":" not in line:
                continue
            key, raw = line.split(":", 1)
            parts = raw.strip().split()
            if parts:
                values[key] = int(parts[0])
    except Exception:
        pass
    return values


def read_temp_c() -> float | None:
    try:
        raw = Path("/sys/class/thermal/thermal_zone0/temp").read_text(
            encoding="utf-8"
        ).strip()
        return int(raw) / 1000.0
    except Exception:
        code, out, _ = run_command(["vcgencmd", "measure_temp"], 5)
        if code == 0:
            match = re.search(r"temp=([\d.]+)", out)
            if match:
                return float(match.group(1))
    return None


def failed_units() -> list[str]:
    code, out, _ = run_command(
        ["systemctl", "--failed", "--no-legend", "--plain", "--no-pager"],
        6,
    )
    if code not in (0, 1):
        return []
    units: list[str] = []
    for line in out.splitlines():
        parts = line.strip().split()
        if not parts or parts[0].startswith("0"):
            continue
        unit = parts[0].lstrip("●* ").strip()
        if unit:
            units.append(unit)
    return units


def ignored_failed_units(config: dict[str, Any]) -> set[str]:
    ignored: set[str] = set()
    for check in config.get("checks", []):
        for sub in check.get("checks", []):
            if sub.get("type") == "failed_units":
                ignored.update(sub.get("ignore", []))
    return ignored


def collect_system_metrics(config: dict[str, Any]) -> dict[str, Any]:
    now = dt.datetime.now().astimezone()
    try:
        uptime_seconds = float(
            Path("/proc/uptime").read_text(encoding="utf-8").split()[0]
        )
    except Exception:
        uptime_seconds = 0.0
    try:
        load = os.getloadavg()
    except Exception:
        load = (0.0, 0.0, 0.0)
    mem = read_meminfo()
    total_mb = mem.get("MemTotal", 0) / 1024.0
    available_mb = mem.get("MemAvailable", 0) / 1024.0
    swap_total = mem.get("SwapTotal", 0)
    swap_free = mem.get("SwapFree", 0)
    swap_percent = (
        (swap_total - swap_free) / swap_total * 100 if swap_total else 0.0
    )
    try:
        disk = shutil.disk_usage("/")
        disk_percent = disk.used / disk.total * 100 if disk.total else 0.0
        disk_free_gb = disk.free / (1024**3)
        disk_total_gb = disk.total / (1024**3)
    except Exception:
        disk_percent = 0.0
        disk_free_gb = 0.0
        disk_total_gb = 0.0
    temperature = read_temp_c()
    all_failed = failed_units()
    ignored = ignored_failed_units(config)
    relevant_failed = [unit for unit in all_failed if unit not in ignored]
    ignored_present = [unit for unit in all_failed if unit in ignored]
    return {
        "hostname": socket.gethostname(),
        "kernel": platform.release(),
        "time": now.isoformat(timespec="seconds"),
        "timezone": now.tzname() or "",
        "uptime_seconds": int(uptime_seconds),
        "uptime_human": format_duration(uptime_seconds),
        "load": [round(value, 2) for value in load],
        "cpu_count": os.cpu_count() or 1,
        "memory": {
            "total_mb": round(total_mb, 1),
            "available_mb": round(available_mb, 1),
            "used_percent": (
                round((total_mb - available_mb) / total_mb * 100, 1)
                if total_mb
                else 0.0
            ),
            "swap_percent": round(swap_percent, 1),
        },
        "disk": {
            "path": "/",
            "percent": round(disk_percent, 1),
            "free_gb": round(disk_free_gb, 1),
            "total_gb": round(disk_total_gb, 1),
        },
        "temperature_c": round(temperature, 1) if temperature is not None else None,
        "failed_units": relevant_failed,
        "ignored_failed_units": ignored_present,
    }


def telegram_configured() -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    return bool(token and chat_id)


def telegram_api(
    method: str,
    payload: dict[str, Any] | None = None,
    timeout: float = 15,
) -> tuple[dict[str, Any] | None, str | None]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return None, "TELEGRAM_BOT_TOKEN fehlt"
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = None
    headers: dict[str, str] = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8")), None
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body)
            description = parsed.get("description", body)
        except Exception:
            description = body
        return None, f"HTTP {exc.code}: {str(description)[:300]}"
    except Exception as exc:
        return None, str(exc)


def send_telegram(text: str) -> tuple[bool, str]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return False, "Telegram nicht konfiguriert"
    message = text.strip()
    if len(message) > 4096:
        message = message[:4090] + "\n..."
    payload = {
        "chat_id": chat_id,
        "text": message,
        "disable_web_page_preview": True,
    }
    response, error = telegram_api("sendMessage", payload, timeout=15)
    if error:
        return False, error
    if not response or not response.get("ok"):
        return False, str(response)[:300]
    return True, ""


def update_env_file(key: str, value: str) -> None:
    path = BASE_DIR / ".env"
    if not path.exists():
        example = BASE_DIR / ".env.example"
        path.write_text(
            example.read_text(encoding="utf-8") if example.exists() else "",
            encoding="utf-8",
        )
    lines = path.read_text(encoding="utf-8").splitlines()
    output: list[str] = []
    found = False
    for line in lines:
        if line.strip().startswith(key + "="):
            output.append(f"{key}={value}")
            found = True
        else:
            output.append(line)
    if not found:
        output.append(f"{key}={value}")
    temporary = path.with_suffix(".tmp")
    temporary.write_text("\n".join(output) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def set_snapshot(snapshot: dict[str, Any]) -> None:
    with SNAPSHOT_LOCK:
        global SNAPSHOT
        SNAPSHOT = snapshot


def get_snapshot() -> dict[str, Any]:
    with SNAPSHOT_LOCK:
        return SNAPSHOT
