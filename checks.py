#!/usr/bin/env python3
"""Check implementations for the homelab monitor."""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import json
import re
import shlex
import shutil
import socket
import sqlite3
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from common import (
    BASE_DIR,
    check_json_expect,
    failed_units,
    format_age,
    format_duration,
    read_meminfo,
    read_temp_c,
    run_command,
    sub_result,
)


def check_systemd(sub: dict[str, Any], timeout: float) -> dict[str, Any]:
    started = time.monotonic()
    unit = sub["unit"]
    code, out, err = run_command(["systemctl", "is-active", unit], timeout)
    state = out.strip() or "unknown"
    if state == "active":
        status = "up"
        message = "active"
    elif state in ("activating", "reloading"):
        status = "degraded"
        message = state
    else:
        status = "down"
        message = state
    if code not in (0, 3) and err:
        message = f"{state}: {err[:160]}"
    return sub_result(
        sub.get("label", "systemd"),
        status,
        message,
        (time.monotonic() - started) * 1000,
    )


def check_docker(sub: dict[str, Any], timeout: float) -> dict[str, Any]:
    started = time.monotonic()
    container = sub["container"]
    code, out, err = run_command(
        ["docker", "inspect", "--format", "{{json .State}}", container],
        timeout,
    )
    if code != 0:
        detail = (err or out or "Container nicht gefunden")[:200]
        return sub_result(
            sub.get("label", "docker"),
            "down",
            detail,
            (time.monotonic() - started) * 1000,
        )
    try:
        state = json.loads(out)
    except Exception as exc:
        return sub_result(
            sub.get("label", "docker"),
            "down",
            f"Docker-Antwort ungültig: {exc}",
            (time.monotonic() - started) * 1000,
        )
    status = str(state.get("Status", "unknown"))
    health = (state.get("Health") or {}).get("Status")
    if state.get("Restarting"):
        check_status = "degraded"
        message = "neustartend"
    elif status == "running":
        if health == "unhealthy":
            check_status = "down"
            message = "läuft, health=unhealthy"
        elif health == "starting":
            check_status = "degraded"
            message = "läuft, health=starting"
        else:
            check_status = "up"
            message = "läuft" + (" (healthy)" if health == "healthy" else "")
    else:
        check_status = "down"
        message = f"Status {status}"
    return sub_result(
        sub.get("label", "docker"),
        check_status,
        message,
        (time.monotonic() - started) * 1000,
        metrics={"restart_count": state.get("RestartCount", 0)},
    )


def decode_body(raw: bytes, charset: str | None) -> str:
    return raw.decode(charset or "utf-8", errors="replace")


def check_http(sub: dict[str, Any], timeout: float) -> dict[str, Any]:
    started = time.monotonic()
    url = sub["url"]
    headers = {"User-Agent": "homelab-monitor/1.0"}
    headers.update(sub.get("headers", {}))
    request = urllib.request.Request(url, headers=headers)
    context = None
    if url.startswith("https://"):
        if sub.get("insecure"):
            context = ssl._create_unverified_context()
        else:
            context = ssl.create_default_context()
    kwargs: dict[str, Any] = {"timeout": timeout}
    if context is not None:
        kwargs["context"] = context
    status_code = None
    body = ""
    try:
        with urllib.request.urlopen(request, **kwargs) as response:
            status_code = response.getcode()
            body = decode_body(
                response.read(1024 * 1024),
                response.headers.get_content_charset(),
            )
    except urllib.error.HTTPError as exc:
        status_code = exc.code
        body = decode_body(
            exc.read(1024 * 1024),
            exc.headers.get_content_charset() if exc.headers else None,
        )
    except Exception as exc:
        return sub_result(
            sub.get("label", "HTTP"),
            "down",
            f"Verbindung fehlgeschlagen: {exc}",
            (time.monotonic() - started) * 1000,
        )
    expected = sub.get("expect_status", [200])
    if isinstance(expected, int):
        expected = [expected]
    if status_code not in expected:
        return sub_result(
            sub.get("label", "HTTP"),
            "down",
            f"HTTP {status_code} (erwartet {expected})",
            (time.monotonic() - started) * 1000,
        )
    if "expect_body_contains" in sub:
        needle = str(sub["expect_body_contains"])
        if needle not in body:
            return sub_result(
                sub.get("label", "HTTP"),
                "down",
                f"HTTP {status_code}, Text {needle!r} fehlt",
                (time.monotonic() - started) * 1000,
            )
    if "expect_json" in sub:
        ok, message, _ = check_json_expect(body, sub["expect_json"])
        if not ok:
            return sub_result(
                sub.get("label", "HTTP"),
                "down",
                f"HTTP {status_code}, {message}",
                (time.monotonic() - started) * 1000,
            )
        message = f"HTTP {status_code} (JSON OK)"
    else:
        message = f"HTTP {status_code}"
    return sub_result(
        sub.get("label", "HTTP"),
        "up",
        message,
        (time.monotonic() - started) * 1000,
    )


def check_tcp(sub: dict[str, Any], timeout: float) -> dict[str, Any]:
    started = time.monotonic()
    host = sub.get("host", "127.0.0.1")
    port = int(sub["port"])
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
    except Exception as exc:
        return sub_result(
            sub.get("label", "TCP"),
            "down",
            f"TCP {host}:{port}: {exc}",
            (time.monotonic() - started) * 1000,
        )
    return sub_result(
        sub.get("label", "TCP"),
        "up",
        f"TCP {host}:{port} offen",
        (time.monotonic() - started) * 1000,
    )


def check_command(sub: dict[str, Any], timeout: float) -> dict[str, Any]:
    started = time.monotonic()
    args = sub["cmd"]
    if isinstance(args, str):
        args = shlex.split(args)
    code, out, err = run_command(args, timeout)
    combined = (out + "\n" + err).strip()
    expected_code = int(sub.get("expect_rc", 0))
    if code != expected_code:
        detail = combined.splitlines()[-1][:220] if combined else ""
        message = f"rc={code}" + (f": {detail}" if detail else "")
        return sub_result(
            sub.get("label", "Befehl"),
            "down",
            message,
            (time.monotonic() - started) * 1000,
        )
    if "expect_stdout_contains" in sub:
        needle = str(sub["expect_stdout_contains"])
        if needle not in combined:
            return sub_result(
                sub.get("label", "Befehl"),
                "down",
                f"Text {needle!r} fehlt",
                (time.monotonic() - started) * 1000,
            )
    if "expect_stdout_regex" in sub:
        pattern = str(sub["expect_stdout_regex"])
        if not re.search(pattern, combined, re.MULTILINE):
            return sub_result(
                sub.get("label", "Befehl"),
                "down",
                "Muster nicht gefunden",
                (time.monotonic() - started) * 1000,
            )
    json_data = None
    if "expect_json" in sub:
        ok, message, json_data = check_json_expect(out, sub["expect_json"])
        if not ok:
            return sub_result(
                sub.get("label", "Befehl"),
                "down",
                message,
                (time.monotonic() - started) * 1000,
            )
    if sub.get("success_message"):
        message = str(sub["success_message"])
    elif json_data is not None:
        backend = json_data.get("BackendState") if isinstance(json_data, dict) else None
        message = f"BackendState={backend}" if backend else "JSON OK"
    elif out:
        message = out.splitlines()[0][:160]
    else:
        message = "OK"
    return sub_result(
        sub.get("label", "Befehl"),
        "up",
        message,
        (time.monotonic() - started) * 1000,
    )


def check_disk(sub: dict[str, Any], timeout: float) -> dict[str, Any]:
    del timeout
    started = time.monotonic()
    path = sub.get("path", "/")
    try:
        usage = shutil.disk_usage(path)
    except Exception as exc:
        return sub_result(
            sub.get("label", "Disk"),
            "down",
            f"Disk nicht lesbar: {exc}",
            (time.monotonic() - started) * 1000,
        )
    percent = usage.used / usage.total * 100 if usage.total else 0.0
    warn = float(sub.get("warn_percent", 80))
    maximum = float(sub.get("max_percent", 90))
    status = "down" if percent >= maximum else "degraded" if percent >= warn else "up"
    free_gb = usage.free / (1024**3)
    return sub_result(
        sub.get("label", "Disk"),
        status,
        f"{percent:.1f}% belegt, {free_gb:.1f} GB frei",
        (time.monotonic() - started) * 1000,
        metrics={"disk_percent": round(percent, 1), "disk_free_gb": round(free_gb, 1)},
    )


def check_temp(sub: dict[str, Any], timeout: float) -> dict[str, Any]:
    del timeout
    started = time.monotonic()
    temperature = read_temp_c()
    if temperature is None:
        return sub_result(
            sub.get("label", "CPU"),
            "down",
            "Temperatur nicht lesbar",
            (time.monotonic() - started) * 1000,
        )
    warn = float(sub.get("warn_c", 75))
    maximum = float(sub.get("max_c", 82))
    status = "down" if temperature >= maximum else "degraded" if temperature >= warn else "up"
    return sub_result(
        sub.get("label", "CPU"),
        status,
        f"{temperature:.1f} °C",
        (time.monotonic() - started) * 1000,
        metrics={"temperature_c": round(temperature, 1)},
    )


def check_memory(sub: dict[str, Any], timeout: float) -> dict[str, Any]:
    del timeout
    started = time.monotonic()
    mem = read_meminfo()
    if not mem.get("MemTotal"):
        return sub_result(
            sub.get("label", "RAM"),
            "down",
            "Meminfo nicht lesbar",
            (time.monotonic() - started) * 1000,
        )
    available_mb = mem.get("MemAvailable", 0) / 1024.0
    total_mb = mem.get("MemTotal", 0) / 1024.0
    swap_total = mem.get("SwapTotal", 0)
    swap_free = mem.get("SwapFree", 0)
    swap_percent = (
        (swap_total - swap_free) / swap_total * 100 if swap_total else 0.0
    )
    warn = float(sub.get("warn_available_mb", 300))
    minimum = float(sub.get("min_available_mb", 150))
    status = "down" if available_mb < minimum else "degraded" if available_mb < warn else "up"
    return sub_result(
        sub.get("label", "RAM"),
        status,
        f"{available_mb / 1024:.1f} GB verfügbar von {total_mb / 1024:.1f} GB, Swap {swap_percent:.0f}%",
        (time.monotonic() - started) * 1000,
        metrics={
            "mem_available_mb": round(available_mb, 1),
            "mem_total_mb": round(total_mb, 1),
            "swap_percent": round(swap_percent, 1),
        },
    )


def check_failed_units(sub: dict[str, Any], timeout: float) -> dict[str, Any]:
    del timeout
    started = time.monotonic()
    ignore = set(sub.get("ignore", []))
    units = failed_units()
    relevant = [unit for unit in units if unit not in ignore]
    ignored_present = [unit for unit in units if unit in ignore]
    if relevant:
        return sub_result(
            sub.get("label", "systemd"),
            "degraded",
            "Fehlgeschlagen: " + ", ".join(relevant[:8]),
            (time.monotonic() - started) * 1000,
            metrics={"failed_units": relevant},
        )
    message = "Keine fehlgeschlagenen Units"
    if ignored_present:
        message += " (" + ", ".join(ignored_present) + " ignoriert)"
    return sub_result(
        sub.get("label", "systemd"),
        "up",
        message,
        (time.monotonic() - started) * 1000,
        metrics={"failed_units": []},
    )


def parse_systemd_timestamp(value: str) -> float | None:
    text = value.strip()
    if not text or text == "n/a":
        return None
    for fmt in ("%a %Y-%m-%d %H:%M:%S %Z", "%a %Y-%m-%d %H:%M:%S"):
        try:
            parsed = dt.datetime.strptime(text, fmt)
            return parsed.timestamp()
        except ValueError:
            continue
    code, out, _ = run_command(["date", "-d", text, "+%s"], 5)
    if code == 0:
        try:
            return float(out.strip())
        except ValueError:
            return None
    return None


def check_timer_age(sub: dict[str, Any], timeout: float) -> dict[str, Any]:
    started = time.monotonic()
    unit = sub["unit"]
    code, out, err = run_command(
        ["systemctl", "show", unit, "-p", "LastTriggerUSec", "--value"],
        timeout,
    )
    if code != 0:
        return sub_result(
            sub.get("label", "Timer"),
            "down",
            f"Timer-Status nicht lesbar: {(err or out)[:160]}",
            (time.monotonic() - started) * 1000,
        )
    last_trigger = parse_systemd_timestamp(out)
    if last_trigger is None:
        return sub_result(
            sub.get("label", "Timer"),
            "down",
            "Timer hat noch nie ausgelöst",
            (time.monotonic() - started) * 1000,
        )
    result_value = ""
    if sub.get("service"):
        result_code, result_out, _ = run_command(
            ["systemctl", "show", sub["service"], "-p", "Result", "--value"],
            timeout,
        )
        if result_code == 0:
            result_value = result_out.strip()
    maximum_age = float(sub.get("max_age_seconds", 8 * 86400))
    age = max(0.0, time.time() - last_trigger)
    if result_value and result_value != "success":
        return sub_result(
            sub.get("label", "Timer"),
            "down",
            f"Letzter Lauf: {result_value}",
            (time.monotonic() - started) * 1000,
            metrics={"age_seconds": int(age)},
        )
    if age > maximum_age:
        return sub_result(
            sub.get("label", "Timer"),
            "down",
            f"Letzte Ausführung {format_age(age)}",
            (time.monotonic() - started) * 1000,
            metrics={"age_seconds": int(age)},
        )
    message = f"Letzte Ausführung {format_age(age)}"
    if result_value:
        message += f" ({result_value})"
    return sub_result(
        sub.get("label", "Timer"),
        "up",
        message,
        (time.monotonic() - started) * 1000,
        metrics={"age_seconds": int(age)},
    )


def check_heartbeat(sub: dict[str, Any], timeout: float) -> dict[str, Any]:
    del timeout
    started = time.monotonic()
    label = sub.get("label", "Heartbeat")
    status_path = Path(sub["path"])
    if not status_path.is_absolute():
        status_path = BASE_DIR / status_path
    if not status_path.is_file():
        return sub_result(
            label,
            "unknown",
            "Noch kein Heartbeat",
            (time.monotonic() - started) * 1000,
            details={"path": str(status_path)},
        )
    try:
        data = json.loads(status_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return sub_result(
            label,
            "down",
            f"Statusdatei ungültig: {exc}",
            (time.monotonic() - started) * 1000,
            details={"path": str(status_path)},
        )
    if not isinstance(data, dict):
        return sub_result(
            label,
            "down",
            "Statusdatei enthält kein JSON-Objekt",
            (time.monotonic() - started) * 1000,
            details={"path": str(status_path)},
        )
    now = time.time()
    status = str(data.get("status", "unknown")).strip().lower()
    detail = str(data.get("message", "")).strip()
    duration = data.get("duration_seconds")
    try:
        duration_seconds = int(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration_seconds = None
    status_age = max(0.0, now - status_path.stat().st_mtime)
    success_age = None
    success_path_raw = sub.get("success_path")
    if success_path_raw:
        success_path = Path(success_path_raw)
        if not success_path.is_absolute():
            success_path = BASE_DIR / success_path
        if success_path.is_file():
            success_age = max(0.0, now - success_path.stat().st_mtime)
    if success_age is None and status == "success":
        success_age = status_age
    warn_age = float(sub.get("warn_age_seconds", 3 * 86400))
    max_age = float(sub.get("max_age_seconds", 7 * 86400))
    last_success = (
        f"Letzter erfolgreicher Lauf {format_age(success_age)}"
        if success_age is not None
        else "Noch kein erfolgreicher Lauf"
    )
    duration_text = (
        f", Dauer {format_duration(duration_seconds)}"
        if duration_seconds is not None
        else ""
    )
    details = {
        "path": str(status_path),
        "status": status,
        "run_at": data.get("run_at"),
        "message": detail,
        "status_age_seconds": int(status_age),
        "success_age_seconds": int(success_age)
        if success_age is not None
        else None,
        "duration_seconds": duration_seconds,
    }
    metrics = {
        "heartbeat_age_seconds": int(status_age),
        "success_age_seconds": int(success_age)
        if success_age is not None
        else None,
    }
    latency = (time.monotonic() - started) * 1000
    if status == "failure":
        message = "Backup fehlgeschlagen"
        if detail:
            message += f": {detail}"
        return sub_result(
            label,
            "down",
            f"{message}. {last_success}",
            latency,
            details=details,
            metrics=metrics,
        )
    if success_age is not None and success_age > max_age:
        return sub_result(
            label,
            "down",
            f"Kein erfolgreicher Backup seit {format_duration(success_age)}",
            latency,
            details=details,
            metrics=metrics,
        )
    if status == "skipped":
        message = "Backup übersprungen"
        if detail:
            message += f": {detail}"
        return sub_result(
            label,
            "degraded",
            f"{message}. {last_success}{duration_text}",
            latency,
            details=details,
            metrics=metrics,
        )
    if success_age is not None and success_age > warn_age:
        return sub_result(
            label,
            "degraded",
            f"{last_success} (Warnung nach {format_duration(warn_age)})"
            f"{duration_text}",
            latency,
            details=details,
            metrics=metrics,
        )
    if status == "success":
        return sub_result(
            label,
            "up",
            f"{last_success}{duration_text}",
            latency,
            details=details,
            metrics=metrics,
        )
    return sub_result(
        label,
        "degraded",
        f"Unbekannter Heartbeat-Status: {status}. {last_success}",
        latency,
        details=details,
        metrics=metrics,
    )


CHECKERS = {
    "systemd": check_systemd,
    "docker": check_docker,
    "http": check_http,
    "tcp": check_tcp,
    "command": check_command,
    "disk": check_disk,
    "temp": check_temp,
    "memory": check_memory,
    "failed_units": check_failed_units,
    "timer_age": check_timer_age,
    "heartbeat": check_heartbeat,
}


def run_subcheck(sub: dict[str, Any], timeout: float) -> dict[str, Any]:
    started = time.monotonic()
    check_type = sub.get("type")
    checker = CHECKERS.get(check_type)
    if checker is None:
        return sub_result(
            sub.get("label", "?"),
            "down",
            f"Unbekannter Check-Typ: {check_type!r}",
        )
    try:
        result = checker(sub, timeout)
    except Exception as exc:
        result = sub_result(
            sub.get("label", str(check_type)),
            "down",
            f"Fehler: {exc}",
        )
    if not result.get("latency_ms"):
        result["latency_ms"] = int((time.monotonic() - started) * 1000)
    return result


def run_all_checks(config: dict[str, Any]) -> list[dict[str, Any]]:
    checks = config.get("checks", [])
    default_timeout = float(config["settings"].get("check_timeout_seconds", 8))
    tasks: list[tuple[int, int, dict[str, Any]]] = []
    for check_index, check in enumerate(checks):
        subs = check.get("checks")
        if not subs:
            subs = [
                {
                    key: value
                    for key, value in check.items()
                    if key
                    not in {
                        "id",
                        "name",
                        "group",
                        "link",
                        "notify",
                        "notify_degraded",
                    }
                }
            ]
        for sub_index, sub in enumerate(subs):
            tasks.append((check_index, sub_index, sub))
    gathered: dict[int, list[tuple[int, dict[str, Any]]]] = {}
    if tasks:
        workers = min(16, max(4, len(tasks)))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    run_subcheck,
                    sub,
                    float(sub.get("timeout", default_timeout)),
                ): (check_index, sub_index)
                for check_index, sub_index, sub in tasks
            }
            for future in concurrent.futures.as_completed(futures):
                check_index, sub_index = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = sub_result("?", "down", f"Fehler: {exc}")
                gathered.setdefault(check_index, []).append((sub_index, result))
    output: list[dict[str, Any]] = []
    for check_index, check in enumerate(checks):
        sub_results = [
            result
            for _, result in sorted(
                gathered.get(check_index, []),
                key=lambda item: item[0],
            )
        ]
        if len(sub_results) == 1:
            base = sub_results[0]
            status = base["status"]
            message = base["message"]
            latency = base["latency_ms"]
            details = base.get("details", {})
            metrics = base.get("metrics", {})
        else:
            statuses = [item["status"] for item in sub_results]
            if "down" in statuses:
                status = "down"
            elif "degraded" in statuses:
                status = "degraded"
            else:
                status = "up"
            message = "; ".join(
                f'{item.get("label", "?")}: {item["message"]}'
                for item in sub_results
            )[:900]
            latency = max(
                (item.get("latency_ms", 0) for item in sub_results),
                default=0,
            )
            details = {"subchecks": sub_results}
            metrics = {}
        output.append(
            {
                "id": check["id"],
                "name": check["name"],
                "group": check.get("group", "apps"),
                "link": check.get("link"),
                "notify": check.get("notify", True),
                "notify_degraded": check.get("notify_degraded", False),
                "status": status,
                "message": message,
                "latency_ms": int(latency),
                "details": details,
                "metrics": metrics,
            }
        )
    return output
