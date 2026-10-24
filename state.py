#!/usr/bin/env python3
"""State, history, notifications, and weekly reporting."""

from __future__ import annotations

import datetime as dt
import sqlite3
from typing import Any

import common
from common import (
    BASE_DIR,
    STATUS_LABELS,
    fmt_dt,
    format_age,
    format_duration,
    now_iso,
    parse_iso,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS check_state (
    id TEXT PRIMARY KEY,
    name TEXT,
    status TEXT NOT NULL DEFAULT 'unknown',
    message TEXT NOT NULL DEFAULT '',
    latency_ms INTEGER NOT NULL DEFAULT 0,
    since TEXT,
    last_checked TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    first_failure_at TEXT,
    notified_down INTEGER NOT NULL DEFAULT 0,
    notified_degraded INTEGER NOT NULL DEFAULT 0,
    incident_id INTEGER,
    last_alert_at TEXT
);
CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    check_id TEXT NOT NULL,
    name TEXT,
    severity TEXT NOT NULL DEFAULT 'down',
    started_at TEXT NOT NULL,
    ended_at TEXT,
    notified INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_incidents_check ON incidents(check_id);
CREATE INDEX IF NOT EXISTS idx_incidents_started ON incidents(started_at);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS heartbeat (
    ts TEXT PRIMARY KEY
);
CREATE INDEX IF NOT EXISTS idx_heartbeat_ts ON heartbeat(ts);
"""


def data_dir(config: dict[str, Any]):
    path = BASE_DIR / str(config["settings"]["data_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_conn(config: dict[str, Any]) -> sqlite3.Connection:
    db_path = data_dir(config) / str(config["settings"]["db_file"])
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def update_states(
    conn: sqlite3.Connection,
    results: list[dict[str, Any]],
    settings: dict[str, Any],
    timestamp: str,
) -> None:
    threshold = int(settings["failure_threshold"])
    existing = {
        row["id"]: dict(row)
        for row in conn.execute("SELECT * FROM check_state").fetchall()
    }
    for result in results:
        check_id = result["id"]
        row = existing.get(check_id, {})
        previous_status = row.get("status", "unknown")
        consecutive = int(row.get("consecutive_failures", 0) or 0)
        first_failure = row.get("first_failure_at")
        notified_down = int(row.get("notified_down", 0) or 0)
        notified_degraded = int(row.get("notified_degraded", 0) or 0)
        incident_id = row.get("incident_id")
        since = row.get("since") or timestamp
        status = result["status"]
        if status == "down":
            consecutive += 1
            if consecutive == 1 or not first_failure:
                first_failure = timestamp
        else:
            consecutive = 0
            first_failure = None
        if status != previous_status:
            since = timestamp
        if status == "up":
            notified_degraded = 0
        if status == "down" and consecutive >= threshold and not incident_id:
            cursor = conn.execute(
                "INSERT INTO incidents(check_id,name,severity,started_at) "
                "VALUES(?,?,?,?)",
                (
                    check_id,
                    result["name"],
                    "down",
                    first_failure or timestamp,
                ),
            )
            incident_id = cursor.lastrowid
            notified_down = 0
        if status != "down" and incident_id:
            conn.execute(
                "UPDATE incidents SET ended_at=? WHERE id=? AND ended_at IS NULL",
                (timestamp, incident_id),
            )
            incident_id = None
        conn.execute(
            """
            INSERT INTO check_state (
                id,name,status,message,latency_ms,since,last_checked,
                consecutive_failures,first_failure_at,notified_down,
                notified_degraded,incident_id,last_alert_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,
                status=excluded.status,
                message=excluded.message,
                latency_ms=excluded.latency_ms,
                since=excluded.since,
                last_checked=excluded.last_checked,
                consecutive_failures=excluded.consecutive_failures,
                first_failure_at=excluded.first_failure_at,
                notified_down=excluded.notified_down,
                notified_degraded=excluded.notified_degraded,
                incident_id=excluded.incident_id
            """,
            (
                check_id,
                result["name"],
                status,
                result["message"],
                int(result["latency_ms"]),
                since,
                timestamp,
                consecutive,
                first_failure,
                notified_down,
                notified_degraded,
                incident_id,
                row.get("last_alert_at"),
            ),
        )
    conn.commit()


def uptime_for(
    conn: sqlite3.Connection,
    check_id: str,
    period_start: dt.datetime,
    period_end: dt.datetime,
) -> float:
    rows = conn.execute(
        "SELECT started_at,ended_at FROM incidents "
        "WHERE check_id=? AND severity='down' "
        "AND (ended_at IS NULL OR ended_at>?) AND started_at<?",
        (
            check_id,
            period_start.isoformat(timespec="seconds"),
            period_end.isoformat(timespec="seconds"),
        ),
    ).fetchall()
    downtime = 0.0
    for row in rows:
        start = parse_iso(row["started_at"])
        end = parse_iso(row["ended_at"]) if row["ended_at"] else period_end
        if not start or not end:
            continue
        overlap_start = max(start, period_start)
        overlap_end = min(end, period_end)
        if overlap_end > overlap_start:
            downtime += (overlap_end - overlap_start).total_seconds()
    total = (period_end - period_start).total_seconds()
    if total <= 0:
        return 100.0
    return max(0.0, min(100.0, 100.0 * (1.0 - downtime / total)))


def build_snapshot(
    conn: sqlite3.Connection,
    config: dict[str, Any],
    results: list[dict[str, Any]],
    metrics: dict[str, Any],
    started_at: str,
    last_cycle_at: str,
    next_check_at: str,
) -> dict[str, Any]:
    settings = config["settings"]
    period_end = dt.datetime.now(dt.timezone.utc)
    period_start = period_end - dt.timedelta(days=7)
    state_rows = {
        row["id"]: dict(row)
        for row in conn.execute("SELECT * FROM check_state").fetchall()
    }
    checks: list[dict[str, Any]] = []
    for result in results:
        row = state_rows.get(result["id"], {})
        checks.append(
            {
                "id": result["id"],
                "name": result["name"],
                "group": result["group"],
                "link": result.get("link"),
                "status": row.get("status", result["status"]),
                "message": row.get("message", result["message"]),
                "latency_ms": int(row.get("latency_ms", result["latency_ms"]) or 0),
                "since": row.get("since"),
                "last_checked": row.get("last_checked"),
                "uptime_7d": round(
                    uptime_for(conn, result["id"], period_start, period_end),
                    1,
                ),
                "notify": result.get("notify", True),
                "details": result.get("details", {}),
            }
        )
    order = {group["id"]: index for index, group in enumerate(config["groups"])}
    checks.sort(key=lambda item: order.get(item["group"], 99))
    groups: list[dict[str, Any]] = []
    for group in config["groups"]:
        group_checks = [item for item in checks if item["group"] == group["id"]]
        if group_checks:
            groups.append(
                {
                    "id": group["id"],
                    "name": group["name"],
                    "checks": group_checks,
                }
            )
    counts = {
        "up": sum(1 for item in checks if item["status"] == "up"),
        "degraded": sum(1 for item in checks if item["status"] == "degraded"),
        "down": sum(1 for item in checks if item["status"] == "down"),
        "total": len(checks),
    }
    if counts["down"]:
        overall = "down"
    elif counts["degraded"]:
        overall = "degraded"
    else:
        overall = "up"
    incidents: list[dict[str, Any]] = []
    for row in conn.execute(
        "SELECT * FROM incidents "
        "WHERE (ended_at IS NULL OR ended_at>?) AND started_at<? "
        "ORDER BY started_at DESC LIMIT 20",
        (
            period_start.isoformat(timespec="seconds"),
            period_end.isoformat(timespec="seconds"),
        ),
    ).fetchall():
        start = parse_iso(row["started_at"])
        end = parse_iso(row["ended_at"]) if row["ended_at"] else period_end
        incidents.append(
            {
                "id": row["id"],
                "check_id": row["check_id"],
                "name": row["name"],
                "severity": row["severity"],
                "started_at": row["started_at"],
                "ended_at": row["ended_at"],
                "duration_seconds": int((end - start).total_seconds())
                if start and end
                else 0,
                "ongoing": row["ended_at"] is None,
            }
        )
    last_cycle = parse_iso(last_cycle_at)
    stale_seconds = (
        (dt.datetime.now(dt.timezone.utc) - last_cycle).total_seconds()
        if last_cycle
        else 0
    )
    interval = int(settings["check_interval_seconds"])
    return {
        "generated_at": now_iso(),
        "overall": overall,
        "summary": counts,
        "groups": groups,
        "system": metrics,
        "incidents": incidents,
        "not_monitored": config.get("not_monitored", []),
        "monitor": {
            "started_at": started_at,
            "last_cycle_at": last_cycle_at,
            "next_check_at": next_check_at,
            "stale": stale_seconds > interval * 3,
            "stale_seconds": int(stale_seconds),
            "check_interval_seconds": interval,
            "dashboard_refresh_seconds": int(
                settings.get("dashboard_refresh_seconds", 20)
            ),
            "weekly_label": settings.get("weekly_label"),
            "telegram_configured": common.telegram_configured(),
        },
    }


def cleanup(conn: sqlite3.Connection, retention_days: int) -> None:
    cutoff = (
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=retention_days)
    ).isoformat(timespec="seconds")
    conn.execute("DELETE FROM heartbeat WHERE ts<?", (cutoff,))
    conn.execute(
        "DELETE FROM incidents WHERE ended_at IS NOT NULL AND ended_at<?",
        (cutoff,),
    )
    conn.commit()


def monitor_uptime(
    conn: sqlite3.Connection,
    period_start: dt.datetime,
    period_end: dt.datetime,
    interval_seconds: int,
) -> float:
    first_started = get_meta(conn, "first_started_at")
    effective_start = period_start
    if first_started:
        parsed = parse_iso(first_started)
        if parsed and parsed > effective_start:
            effective_start = parsed
    total = (period_end - effective_start).total_seconds()
    if total <= 0 or interval_seconds <= 0:
        return 100.0
    count = conn.execute(
        "SELECT COUNT(*) FROM heartbeat WHERE ts>=? AND ts<?",
        (
            effective_start.isoformat(timespec="seconds"),
            period_end.isoformat(timespec="seconds"),
        ),
    ).fetchone()[0]
    expected = max(1.0, total / interval_seconds)
    return max(0.0, min(100.0, count / expected * 100.0))


def build_weekly_summary(
    conn: sqlite3.Connection,
    config: dict[str, Any],
    metrics: dict[str, Any],
) -> str:
    settings = config["settings"]
    period_end = dt.datetime.now(dt.timezone.utc)
    period_start = period_end - dt.timedelta(days=7)
    local_end = period_end.astimezone()
    local_start = period_start.astimezone()
    interval = int(settings["check_interval_seconds"])
    state_rows = {
        row["id"]: dict(row)
        for row in conn.execute("SELECT * FROM check_state").fetchall()
    }
    items: list[dict[str, Any]] = []
    for check in config["checks"]:
        row = state_rows.get(check["id"], {})
        items.append(
            {
                "id": check["id"],
                "name": check["name"],
                "group": check.get("group", "apps"),
                "status": row.get("status", "unknown"),
                "message": row.get("message", "Noch keine Daten"),
                "uptime": uptime_for(
                    conn,
                    check["id"],
                    period_start,
                    period_end,
                ),
            }
        )
    counts = {
        "up": sum(1 for item in items if item["status"] == "up"),
        "degraded": sum(1 for item in items if item["status"] == "degraded"),
        "down": sum(1 for item in items if item["status"] == "down"),
        "unknown": sum(1 for item in items if item["status"] == "unknown"),
        "total": len(items),
    }
    avg_uptime = (
        sum(item["uptime"] for item in items) / len(items) if items else 0.0
    )
    last_cycle_at = get_meta(conn, "last_cycle_at")
    last_cycle = parse_iso(last_cycle_at)
    stale_seconds = (
        (period_end - last_cycle).total_seconds() if last_cycle else 0
    )
    stale = not last_cycle or stale_seconds > interval * 3
    lines = [
        "Homelab-Wochenbericht",
        f"Zeitraum: {local_start:%d.%m.%Y %H:%M} – {local_end:%d.%m.%Y %H:%M}",
        "",
    ]
    if stale:
        lines.append(
            "[WARNUNG] Überwachungsdaten sind veraltet. "
            f"Letzter Check: {format_age(stale_seconds)}."
        )
        lines.append("")
    lines.append(
        f"Gesamt: {counts['up']}/{counts['total']} OK, "
        f"{counts['degraded']} Warnung(en), {counts['down']} Ausfall/Ausfälle, "
        f"{counts['unknown']} unbekannt"
    )
    lines.append(f"Durchschnittliche Verfügbarkeit: {avg_uptime:.1f}%")
    order = {group["id"]: index for index, group in enumerate(config["groups"])}
    items.sort(key=lambda item: (order.get(item["group"], 99), item["name"]))
    for group in config["groups"]:
        group_items = [item for item in items if item["group"] == group["id"]]
        if not group_items:
            continue
        lines.append("")
        lines.append(group["name"] + ":")
        for item in group_items:
            label = STATUS_LABELS.get(item["status"], "UNBEKANNT")
            lines.append(f"- [{label}] {item['name']}: {item['uptime']:.1f}%")
            if item["status"] == "down":
                lines.append(f"  {item['message']}")
    incidents = conn.execute(
        "SELECT * FROM incidents "
        "WHERE (ended_at IS NULL OR ended_at>?) AND started_at<? "
        "ORDER BY started_at DESC",
        (
            period_start.isoformat(timespec="seconds"),
            period_end.isoformat(timespec="seconds"),
        ),
    ).fetchall()
    total_downtime = 0.0
    incident_lines: list[str] = []
    for row in incidents:
        start = parse_iso(row["started_at"])
        end = parse_iso(row["ended_at"]) if row["ended_at"] else period_end
        if not start or not end:
            continue
        overlap_start = max(start, period_start)
        overlap_end = min(end, period_end)
        duration = max(0.0, (overlap_end - overlap_start).total_seconds())
        total_downtime += duration
        incident_lines.append(
            f"- {row['name']}: {fmt_dt(row['started_at'])} – "
            f"{fmt_dt(row['ended_at']) if row['ended_at'] else 'andauernd'} "
            f"({format_duration(duration)})"
        )
    lines.append("")
    lines.append(
        f"Vorfälle: {len(incidents)} "
        f"(Gesamtausfall {format_duration(total_downtime)})"
    )
    if incident_lines:
        lines.extend(incident_lines[:10])
        if len(incident_lines) > 10:
            lines.append(f"- ... und {len(incident_lines) - 10} weitere")
    else:
        lines.append("- Keine Ausfälle")
    lines.append("")
    lines.append("Ressourcen:")
    lines.append(
        f"- Disk /: {metrics['disk']['percent']:.1f}% belegt, "
        f"{metrics['disk']['free_gb']:.1f} GB frei"
    )
    if metrics.get("temperature_c") is not None:
        lines.append(f"- Temperatur: {metrics['temperature_c']:.1f} °C")
    lines.append(
        f"- RAM: {metrics['memory']['available_mb'] / 1024:.1f} GB verfügbar "
        f"von {metrics['memory']['total_mb'] / 1024:.1f} GB"
    )
    lines.append(f"- Swap: {metrics['memory']['swap_percent']:.0f}% belegt")
    lines.append(
        "- Load: " + " / ".join(f"{value:.2f}" for value in metrics["load"])
    )
    failed = metrics.get("failed_units", [])
    if failed:
        lines.append(f"- Fehlgeschlagene Units: {', '.join(failed)}")
    else:
        lines.append("- Fehlgeschlagene Units: keine")
    if metrics.get("ignored_failed_units"):
        lines.append("- Ignoriert: " + ", ".join(metrics["ignored_failed_units"]))
    lines.append("")
    lines.append("Überwachung:")
    lines.append(
        f"- Letzter Check: {format_age(stale_seconds)}"
        if last_cycle
        else "- Letzter Check: unbekannt"
    )
    uptime = monitor_uptime(conn, period_start, period_end, interval)
    lines.append(f"- Monitor-Verfügbarkeit (7 Tage): {uptime:.1f}%")
    lines.append("- Dashboard: http://192.168.178.62:8090")
    if config.get("not_monitored"):
        lines.append("")
        lines.append(
            "Nicht überwacht: "
            + ", ".join(item["name"] for item in config["not_monitored"])
        )
    return "\n".join(lines)[: int(settings.get("max_message_chars", 4096))]


def format_down_message(rows: list[dict[str, Any]], max_chars: int) -> str:
    lines = [f"[AUSFALL] {len(rows)} Dienst(e) nicht erreichbar", ""]
    for row in rows:
        lines.append(f"- {row['name']}: {row['message']}")
        lines.append(
            f"  seit {fmt_dt(row.get('first_failure_at') or row.get('since'))}"
        )
    return "\n".join(lines)[:max_chars]


def format_recovery_message(rows: list[dict[str, Any]], max_chars: int) -> str:
    now = dt.datetime.now(dt.timezone.utc)
    lines = [f"[WIEDER ONLINE] {len(rows)} Dienst(e)", ""]
    for row in rows:
        started = parse_iso(row.get("first_failure_at") or row.get("since"))
        duration = (now - started).total_seconds() if started else 0
        suffix = "OK" if row["status"] == "up" else "WARNUNG"
        lines.append(
            f"- {row['name']}: {suffix} (Ausfallzeit {format_duration(duration)})"
        )
    return "\n".join(lines)[:max_chars]


def format_warning_message(rows: list[dict[str, Any]], max_chars: int) -> str:
    lines = [f"[WARNUNG] {len(rows)} Dienst(e)", ""]
    for row in rows:
        lines.append(f"- {row['name']}: {row['message']}")
    return "\n".join(lines)[:max_chars]


def process_notifications(
    conn: sqlite3.Connection,
    config: dict[str, Any],
    settings: dict[str, Any],
    timestamp: str,
    daemon_started_monotonic: float,
    started_monotonic: float,
) -> None:
    if (
        started_monotonic - daemon_started_monotonic
        < float(settings["startup_grace_seconds"])
    ):
        return
    if not common.telegram_configured():
        if not common.TELEGRAM_WARNED:
            common.LOG.warning(
                "Telegram ist nicht konfiguriert; Alerts werden übersprungen. "
                "Token in .env eintragen und --setup-telegram ausführen."
            )
            common.TELEGRAM_WARNED = True
        return
    threshold = int(settings["failure_threshold"])
    max_chars = int(settings.get("max_message_chars", 4096))
    checks_by_id = {check["id"]: check for check in config["checks"]}
    rows = [
        dict(row)
        for row in conn.execute("SELECT * FROM check_state").fetchall()
    ]
    down_rows: list[dict[str, Any]] = []
    recovery_rows: list[dict[str, Any]] = []
    warning_rows: list[dict[str, Any]] = []
    for row in rows:
        check = checks_by_id.get(row["id"])
        if not check or not check.get("notify", True):
            continue
        if (
            row["status"] == "down"
            and int(row["consecutive_failures"]) >= threshold
            and not int(row["notified_down"])
        ):
            down_rows.append(row)
        elif row["status"] != "down" and int(row["notified_down"]):
            recovery_rows.append(row)
        if (
            row["status"] == "degraded"
            and check.get("notify_degraded", False)
            and not int(row["notified_degraded"])
        ):
            warning_rows.append(row)
    if down_rows:
        ok, error = common.send_telegram(
            format_down_message(down_rows, max_chars)
        )
        if ok:
            for row in down_rows:
                conn.execute(
                    "UPDATE check_state SET notified_down=1,last_alert_at=? WHERE id=?",
                    (timestamp, row["id"]),
                )
                if row.get("incident_id"):
                    conn.execute(
                        "UPDATE incidents SET notified=1 WHERE id=?",
                        (row["incident_id"],),
                    )
            conn.commit()
            common.LOG.info(
                "Ausfallmeldung gesendet: %s",
                ", ".join(row["name"] for row in down_rows),
            )
        else:
            common.LOG.error("Ausfallmeldung fehlgeschlagen: %s", error)
    if recovery_rows:
        ok, error = common.send_telegram(
            format_recovery_message(recovery_rows, max_chars)
        )
        if ok:
            for row in recovery_rows:
                conn.execute(
                    "UPDATE check_state SET notified_down=0,notified_degraded=0,"
                    "last_alert_at=? WHERE id=?",
                    (timestamp, row["id"]),
                )
            conn.commit()
            common.LOG.info(
                "Wieder-online-Meldung gesendet: %s",
                ", ".join(row["name"] for row in recovery_rows),
            )
        else:
            common.LOG.error("Wieder-online-Meldung fehlgeschlagen: %s", error)
    if warning_rows:
        ok, error = common.send_telegram(
            format_warning_message(warning_rows, max_chars)
        )
        if ok:
            for row in warning_rows:
                conn.execute(
                    "UPDATE check_state SET notified_degraded=1,last_alert_at=? WHERE id=?",
                    (timestamp, row["id"]),
                )
            conn.commit()
            common.LOG.info(
                "Warnmeldung gesendet: %s",
                ", ".join(row["name"] for row in warning_rows),
            )
        else:
            common.LOG.error("Warnmeldung fehlgeschlagen: %s", error)
