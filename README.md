# Homelab Monitoring

Leichtgewichtige Überwachung für den Raspberry Pi: Systemd- und Docker-Dienste,
HTTP-/TCP-Endpunkte, Pi-Ressourcen, Backup-Frische und ein kleines Dashboard.
Ausfälle werden nach zwei fehlgeschlagenen Checks per Telegram gemeldet,
sonntags um 09:00 Europe/Berlin kommt ein deutscher Wochenbericht.

## Installation

Auf dem Pi:

```bash
cd /home/philipp/homelab-monitoring
./install.sh
```

Das Skript legt bei Bedarf `.env` an, installiert die systemd-Units und startet
den Dienst sowie den Wochen-Timer.

## Telegram einrichten

1. Bei `@BotFather` einen neuen Bot anlegen und den Token kopieren.
2. Token in `.env` eintragen:

   ```bash
   TELEGRAM_BOT_TOKEN=123456:ABC...
   ```

3. Bot in Telegram öffnen, `/start` senden, dann:

   ```bash
   python3 monitor.py --setup-telegram
   ```

Die Chat-ID wird automatisch ermittelt und in `.env` gespeichert. Mit
`python3 monitor.py --test-telegram` lässt sich der Versand prüfen.

Ohne Telegram läuft das Dashboard weiter; Alarme und Wochenbericht werden
übersprungen.

## Dashboard

- LAN: <http://192.168.178.62:8090>
- Tailscale: <http://100.125.255.120:8090>
- JSON: <http://192.168.178.62:8090/api/status>
- Health: <http://192.168.178.62:8090/healthz>

Das Dashboard braucht kein Node und wird direkt von `monitor.py` ausgeliefert.
Es aktualisiert sich alle 20 Sekunden.

## Befehle

```bash
python3 monitor.py --once              # alle Checks einmal ausführen
python3 monitor.py --weekly            # Wochenbericht jetzt senden
python3 monitor.py --test-telegram     # Testnachricht
python3 monitor.py --setup-telegram    # Chat-ID ermitteln
python3 monitor.py --failure-alert     # Ausfallmeldung des Monitors
```

## Betrieb

```bash
systemctl status homelab-monitoring.service
journalctl -u homelab-monitoring.service -f
systemctl list-timers homelab-monitoring-weekly.timer
```

Der Monitor prüft alle 60 Sekunden. Ein Dienst gilt nach zwei aufeinander
folgenden Fehlversuchen als ausgefallen. Der Start hat 120 Sekunden Schonzeit,
damit Neustarts nicht sofort alarmieren.

Zustand und Historie liegen in `data/monitor.db` (SQLite). Vorfälle und
Heartbeats werden nach 120 Tagen bereinigt.

## Konfiguration

Alle Checks stehen in `config.json`. Ein Eintrag kann mehrere Teilprüfungen
haben; die schlechteste Teilprüfung bestimmt den Gesamtstatus.

Unterstützte Typen:

- `systemd`: `systemctl is-active`
- `docker`: Container-Status und Health
- `http`: Statuscode, Text oder JSON-Felder
- `tcp`: offener Port
- `command`: Rückgabecode, stdout-Text, Regex oder JSON
- `disk`, `temp`, `memory`: Pi-Ressourcen
- `failed_units`: fehlgeschlagene systemd-Units
- `timer_age`: letzter Lauf eines Timers und Ergebnis des zugehörigen Service
- `heartbeat`: Statusdatei eines externen Jobs, optional mit Erfolgsmarker

Einstellungen wie Intervall, Schwellwerte, Dashboard-Port und Datenverzeichnis
stehen im Abschnitt `settings`.

Der Apple-TV-Sideloader (`atvloadly`) wird über seinen Docker-Container und den
Health-Endpunkt `http://192.168.178.62:5533/healthcheck` überwacht. Die
Weboberfläche ist bewusst nur im LAN erreichbar.

## Ente Backup auf dem Desktop

Der Ente-Backup-Job läuft auf dem Desktop-PC per Cron und exportiert die Daten
vom Pi. Nach jedem Lauf schreibt `/home/philipp/bin/backup-ente.sh` über die
bestehende SSH-Verbindung zwei Marker auf den Pi:

- `data/ente-backup.json`: Status des letzten Laufs
- `data/ente-backup-success`: wird nur bei Erfolg aktualisiert

Der Monitor zeigt den Status in der Gruppe **Backups**. Nach 72 Stunden ohne
erfolgreichen Lauf wird der Dienst im Dashboard gelb, nach 7 Tagen erfolgt ein
Telegram-Alarm. Ein explizit gemeldeter Fehlschlag wird sofort als Ausfall
behandelt.

## Bewusst nicht überwacht

Nextcloud (durch Ente ersetzt), Kosync (stillgelegt), TinyMediaManager,
Netflix-Kiosk, rpi-clone und `dphys-swapfile.service`.
Letzteres ist ein bekannter Fehler; zram-Swap ist aktiv und wird informational
angezeigt. Der Gutschein-Organizer wird nur über seine systemd-Unit und den
HTTP-Port geprüft, nicht über sein Repository.
