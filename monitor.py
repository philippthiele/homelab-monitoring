#!/usr/bin/env python3
"""Lightweight homelab monitor with a web dashboard and Telegram alerts."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import signal
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import checks
import common
import state


def current_snapshot() -> dict[str, Any]:
    snapshot = dict(common.get_snapshot())
    monitor = dict(snapshot.get("monitor") or {})
    last_cycle = common.parse_iso(monitor.get("last_cycle_at"))
    interval = int(monitor.get("check_interval_seconds") or 60)
    age = (
        (dt.datetime.now(dt.timezone.utc) - last_cycle).total_seconds()
        if last_cycle
        else 0
    )
    monitor["stale_seconds"] = int(age)
    monitor["stale"] = not last_cycle or age > interval * 3
    snapshot["monitor"] = monitor
    return snapshot


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "homelab-monitor"
    sys_version = ""

    def _headers(self, content_type: str, length: int, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'unsafe-inline'; "
            "script-src 'unsafe-inline'; connect-src 'self'",
        )
        self.end_headers()

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._headers("application/json; charset=utf-8", len(body), status)
        self.wfile.write(body)

    def _send_html(self, path: Path) -> None:
        try:
            body = path.read_bytes()
        except Exception:
            body = b"Dashboard nicht gefunden"
            self._headers("text/plain; charset=utf-8", len(body), 500)
            self.wfile.write(body)
            return
        self._headers("text/html; charset=utf-8", len(body))
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send_html(common.BASE_DIR / "static" / "index.html")
        elif path == "/api/status":
            self._send_json(current_snapshot())
        elif path == "/healthz":
            snapshot = current_snapshot()
            monitor = snapshot.get("monitor", {})
            self._send_json(
                {
                    "status": "ok",
                    "stale": monitor.get("stale", True),
                    "last_cycle_at": monitor.get("last_cycle_at"),
                }
            )
        elif path == "/favicon.ico":
            self._headers("image/x-icon", 0, 204)
        else:
            self._send_json({"error": "not found"}, 404)

    def log_message(self, format: str, *args: Any) -> None:
        common.LOG.debug("dashboard %s - %s", self.address_string(), format % args)


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def start_dashboard(config: dict[str, Any]) -> DashboardServer:
    settings = config["settings"]
    host = str(settings["dashboard_host"])
    port = int(settings["dashboard_port"])
    server = DashboardServer((host, port), DashboardHandler)
    thread = threading.Thread(
        target=server.serve_forever,
        name="dashboard",
        daemon=True,
    )
    thread.start()
    common.LOG.info("Dashboard läuft auf http://%s:%s", host, port)
    return server


def once_main(config: dict[str, Any]) -> int:
    results = checks.run_all_checks(config)
    metrics = common.collect_system_metrics(config)
    print(f"{'STATUS':10} {'DIENST':28} {'DETAIL':70} {'LATENZ':>8}")
    print("-" * 120)
    down_count = 0
    for result in results:
        label = f"[{common.STATUS_LABELS.get(result['status'], '?')}]"
        detail = result["message"].replace("\n", " ")[:70]
        print(
            f"{label:10} {result['name'][:28]:28} {detail:70} "
            f"{result['latency_ms']:>6} ms"
        )
        if result["status"] == "down":
            down_count += 1
    print()
    print(
        f"System: {metrics['hostname']} | Uptime {metrics['uptime_human']} | "
        f"Load {' / '.join(str(v) for v in metrics['load'])} | "
        f"Temp {metrics['temperature_c']} °C | "
        f"RAM {metrics['memory']['available_mb'] / 1024:.1f} GB frei | "
        f"Disk {metrics['disk']['percent']:.1f}% | "
        f"Swap {metrics['memory']['swap_percent']:.0f}%"
    )
    if metrics["failed_units"]:
        print("Fehlgeschlagene Units: " + ", ".join(metrics["failed_units"]))
    if metrics["ignored_failed_units"]:
        print("Ignorierte Units: " + ", ".join(metrics["ignored_failed_units"]))
    return 0 if down_count == 0 else 1


def weekly_main(config: dict[str, Any]) -> int:
    conn = state.get_conn(config)
    try:
        state.init_db(conn)
        metrics = common.collect_system_metrics(config)
        text = state.build_weekly_summary(conn, config, metrics)
    finally:
        conn.close()
    print(text)
    if not common.telegram_configured():
        common.LOG.warning(
            "Telegram ist nicht konfiguriert; Wochenbericht nur auf stdout."
        )
        return 2
    ok, error = common.send_telegram(text)
    if ok:
        common.LOG.info("Wochenbericht gesendet")
        return 0
    common.LOG.error("Wochenbericht konnte nicht gesendet werden: %s", error)
    return 1


def setup_telegram() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print(
            "TELEGRAM_BOT_TOKEN fehlt.\n"
            f"Trage ihn in {common.BASE_DIR / '.env'} ein und starte den Befehl erneut."
        )
        return 2
    me, error = common.telegram_api("getMe", None, timeout=15)
    if error or not me or not me.get("ok"):
        print(f"Bot-Token ungültig: {error or me}")
        return 1
    username = me.get("result", {}).get("username", "?")
    updates, error = common.telegram_api(
        "getUpdates",
        {
            "limit": 100,
            "timeout": 0,
            "allowed_updates": [
                "message",
                "edited_message",
                "channel_post",
                "my_chat_member",
            ],
        },
        timeout=15,
    )
    if error or not updates or not updates.get("ok"):
        print(f"getUpdates fehlgeschlagen: {error or updates}")
        return 1
    chat_id = None
    chat_title = None
    for update in updates.get("result", []):
        for key in ("message", "edited_message", "channel_post", "my_chat_member"):
            item = update.get(key)
            if not item:
                continue
            chat = item.get("chat") or {}
            if chat.get("id") is None:
                continue
            chat_id = chat["id"]
            chat_title = (
                chat.get("title")
                or chat.get("username")
                or chat.get("first_name")
                or str(chat_id)
            )
    if chat_id is None:
        print(
            f"Kein Chat gefunden. Bot @{username} in Telegram öffnen, "
            "/start senden und den Befehl erneut ausführen."
        )
        return 1
    common.update_env_file("TELEGRAM_CHAT_ID", str(chat_id))
    os.environ["TELEGRAM_CHAT_ID"] = str(chat_id)
    print(f"Chat gefunden: {chat_title} ({chat_id})")
    print("TELEGRAM_CHAT_ID wurde in .env gespeichert.")
    ok, error = common.send_telegram(
        "Homelab-Monitor ist mit diesem Chat verbunden."
    )
    if ok:
        print("Bestätigungsnachricht gesendet.")
        return 0
    print(f"Bestätigungsnachricht fehlgeschlagen: {error}")
    return 1


def test_telegram() -> int:
    text = (
        "Testnachricht vom Homelab-Monitor.\n"
        f"Host: {os.uname().nodename}\n"
        f"Zeit: {dt.datetime.now().astimezone():%d.%m.%Y %H:%M:%S}"
    )
    ok, error = common.send_telegram(text)
    if ok:
        print("Testnachricht gesendet.")
        return 0
    print(f"Testnachricht fehlgeschlagen: {error}")
    return 1


def failure_alert() -> int:
    text = (
        "[AUSFALL] Homelab-Monitor\n"
        f"Der Überwachungsdienst auf {os.uname().nodename} ist ausgefallen.\n"
        f"Zeit: {dt.datetime.now().astimezone():%d.%m.%Y %H:%M:%S}\n"
        "Bitte prüfen: systemctl status homelab-monitoring.service"
    )
    ok, error = common.send_telegram(text)
    if ok:
        return 0
    print(f"Ausfallmeldung fehlgeschlagen: {error}")
    return 2


def daemon_main(config: dict[str, Any]) -> int:
    settings = config["settings"]
    conn = state.get_conn(config)
    state.init_db(conn)
    started_at = common.now_iso()
    state.set_meta(conn, "monitor_started_at", started_at)
    if not state.get_meta(conn, "first_started_at"):
        state.set_meta(conn, "first_started_at", started_at)
    conn.commit()
    server = start_dashboard(config)
    daemon_started = time.monotonic()
    interval = int(settings["check_interval_seconds"])
    cycle_timeout = float(settings["cycle_timeout_seconds"])
    common.LOG.info(
        "Monitor gestartet: %d Checks, Intervall %ss",
        len(config["checks"]),
        interval,
    )
    if not common.telegram_configured():
        common.LOG.warning(
            "Telegram ist nicht konfiguriert; Dashboard läuft, Alerts sind aus."
        )
    try:
        while not common.STOP.is_set():
            cycle_started = time.monotonic()
            try:
                results = checks.run_all_checks(config)
                metrics = common.collect_system_metrics(config)
                timestamp = common.now_iso()
                state.update_states(conn, results, settings, timestamp)
                conn.execute(
                    "INSERT OR REPLACE INTO heartbeat(ts) VALUES(?)",
                    (timestamp,),
                )
                state.set_meta(conn, "last_cycle_at", timestamp)
                conn.commit()
                next_check = (
                    dt.datetime.now(dt.timezone.utc)
                    + dt.timedelta(seconds=interval)
                ).isoformat(timespec="seconds")
                snapshot = state.build_snapshot(
                    conn,
                    config,
                    results,
                    metrics,
                    started_at,
                    timestamp,
                    next_check,
                )
                common.set_snapshot(snapshot)
                state.process_notifications(
                    conn,
                    config,
                    settings,
                    timestamp,
                    daemon_started,
                    time.monotonic(),
                )
                state.cleanup(conn, int(settings["retention_days"]))
            except Exception:
                common.LOG.exception("Check-Zyklus fehlgeschlagen")
            elapsed = time.monotonic() - cycle_started
            if elapsed > cycle_timeout:
                common.LOG.error(
                    "Zyklus dauerte %.0fs (> %.0fs); beende für systemd-Neustart",
                    elapsed,
                    cycle_timeout,
                )
                return 2
            common.STOP.wait(max(1.0, interval - elapsed))
    finally:
        server.shutdown()
        server.server_close()
        conn.close()
        common.LOG.info("Monitor beendet")
    return 0


def handle_signal(signum: int, frame: Any) -> None:
    del signum, frame
    common.STOP.set()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Homelab monitor with dashboard and Telegram alerts."
    )
    parser.add_argument("--config", default=None, help="Pfad zur config.json")
    parser.add_argument("--verbose", action="store_true", help="Debug-Logging")
    parser.add_argument("--version", action="version", version="homelab-monitor 1.0")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--once",
        action="store_true",
        help="Alle Checks einmalig ausführen und Tabelle ausgeben",
    )
    group.add_argument(
        "--weekly",
        action="store_true",
        help="Wochenbericht erstellen und per Telegram senden",
    )
    group.add_argument(
        "--setup-telegram",
        action="store_true",
        help="Chat-ID ermitteln und in .env speichern",
    )
    group.add_argument(
        "--test-telegram",
        action="store_true",
        help="Testnachricht senden",
    )
    group.add_argument(
        "--failure-alert",
        action="store_true",
        help="Kurze Ausfallmeldung für systemd OnFailure senden",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    common.load_env(common.BASE_DIR / ".env")
    common.setup_logging(args.verbose)
    if args.setup_telegram:
        return setup_telegram()
    if args.test_telegram:
        return test_telegram()
    if args.failure_alert:
        return failure_alert()
    config = common.load_config(args.config)
    if args.once:
        return once_main(config)
    if args.weekly:
        return weekly_main(config)
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    return daemon_main(config)


if __name__ == "__main__":
    sys.exit(main())
