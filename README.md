# SupBot 💊

Kleiner Supplement-Tracker für den eigenen VPS:

- **Weboberfläche** mit dem Tagesplan zum Abhaken (mobil bedienbar)
- **Telegram-Bot**, der zu jedem Zeit-Slot erinnert und so lange nachhakt,
  bis du ein Codewort (`genommen`, `erledigt`, …) zurückschreibst
- **Artikel-/Vorratsverwaltung**: jede Packung hat einen Inhalt, jede Einnahme
  bucht ab – daraus berechnet SupBot Reichweite und das Datum, an dem die
  Packung leer ist, und meldet sich rechtzeitig zum Nachbestellen

Ein einziger Python-Prozess, SQLite als Datenbank, keine externen Dienste außer
der Telegram-API.

---

## 1. Telegram-Bot anlegen

1. In Telegram [@BotFather](https://t.me/BotFather) anschreiben → `/newbot`
2. Namen und Username vergeben → du bekommst einen Token
   (`123456789:AAE...`) — den brauchst du gleich.
3. Deinen eigenen Bot einmal anschreiben und `/start` senden. SupBot merkt
   sich dann automatisch deine Chat-ID.

## 2. Installation auf dem VPS

```bash
ssh root@dein-vps
apt-get update && apt-get install -y git
git clone https://github.com/Fabian5231/SupBot.git /root/SupBot
cd /root/SupBot
sudo bash deploy/install.sh
```

> Das Repo ist privat – für `git clone` braucht der Server also Zugriff.
> Zwei Wege, siehe [Abschnitt 7](#7-updates-über-git): Deploy-Key (empfohlen)
> oder das Repo öffentlich schalten.

Das Skript legt an:

- den Systembenutzer `supbot`
- `/opt/supbot` mit Virtualenv und Abhängigkeiten
- `/opt/supbot/.env` (aus `.env.example`, mit erzeugtem `SUPBOT_SECRET`)
- den systemd-Dienst `supbot`

Danach die Konfiguration ausfüllen:

```bash
nano /opt/supbot/.env      # Token + Passwort eintragen
systemctl restart supbot
journalctl -u supbot -f    # Logs mitlesen
```

Wichtige Werte in `.env`:

| Variable | Bedeutung |
|---|---|
| `SUPBOT_TELEGRAM_TOKEN` | Token vom BotFather (Pflicht für den Bot) |
| `SUPBOT_TELEGRAM_CHAT_ID` | optional – wird sonst per `/start` gesetzt |
| `SUPBOT_PASSWORD` | Passwort für die Weboberfläche |
| `SUPBOT_SECRET` | Zufallsstring zum Signieren des Login-Cookies |
| `SUPBOT_TZ` | Zeitzone, Standard `Europe/Berlin` |
| `SUPBOT_HOST` / `SUPBOT_PORT` | Bind-Adresse, Standard `127.0.0.1:8080` |

## 3. Erreichbar machen

Der Dienst lauscht absichtlich nur auf `127.0.0.1`. Für den Zugriff von außen
nginx als Reverse Proxy davorsetzen und TLS holen:

```bash
cp deploy/nginx.conf.example /etc/nginx/sites-available/supbot
# Domain anpassen
ln -s /etc/nginx/sites-available/supbot /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx
certbot --nginx -d supplements.deine-domain.de
```

Ohne Domain: `SUPBOT_HOST=0.0.0.0` setzen und Port 8080 in der Firewall
freigeben – dann läuft es aber unverschlüsselt, also mindestens ein starkes
`SUPBOT_PASSWORD` setzen.

## 4. Benutzung

### Weboberfläche

- **Heute** – Tagesplan nach Zeit-Slots, einzeln oder slotweise abhaken,
  Fortschritt und Serie. Über die Pfeile kommst du zu anderen Tagen.
- **Supplemente** – Name, Marke, Einheit, Artikel-Link und der Einnahmeplan:
  pro Slot Menge und Wochentage. Pausieren statt löschen erhält die Historie.
- **Vorrat** – pro Supplement Bestand, Tagesverbrauch, Reichweite in Tagen und
  das Datum „leer am“. Packungen anlegen, Bestand korrigieren, Preise erfassen.
- **Verlauf** – Quote der letzten 14–90 Tage als Heatmap und Tabelle.
- **Einstellungen** – Zeit-Slots, Erinnerungsintervall, Ruhezeiten,
  Codewörter, Warnschwelle für den Vorrat, Testnachricht.

### Telegram

| Eingabe | Wirkung |
|---|---|
| `/start` | verbindet den Chat |
| `/heute`, `/status` | Tagesplan mit Status |
| `/vorrat` | Reichweite aller Packungen |
| `/genommen` oder ein Codewort | hakt alles gerade Fällige ab |
| `/snooze` | 30 Minuten Ruhe |
| `/hilfe` | Übersicht |

Jede Erinnerung hat außerdem Buttons: **✅ Genommen**, **😴 30 Min** und – bei
mehreren Supplementen im Slot – einen Knopf je Eintrag.

**So funktioniert das Nachhaken:** Zur Slot-Zeit kommt die erste Nachricht.
Solange noch etwas offen ist, wiederholt der Bot sie alle `X` Minuten
(Standard 30), maximal `max_reminders`-mal pro Slot und Tag. Innerhalb der
Ruhezeit (Standard 22:30–06:30) wird nicht wiederholt; die erste Nachricht
eines Slots geht immer raus. Sobald du das Codewort schickst oder in der
Weboberfläche abhakst, ist Ruhe.

## 5. Vorratsrechnung

- **Tagesverbrauch** = Summe aller Planeinträge × (Anzahl Wochentage ÷ 7).
  Beispiel: 1 Kapsel Mo/Mi/Fr → 0,43 pro Tag.
- **Bestand** = Summe der Restmengen aller nicht leeren Packungen.
- **Reichweite** = Bestand ÷ Tagesverbrauch (abgerundet), **leer am** = heute + Reichweite.
- Jedes Abhaken bucht von der angebrochenen Packung ab; ist sie leer, läuft es
  automatisch in die nächste. Ein zurückgenommener Haken bucht wieder zurück.
- Fällt die Reichweite unter die Warnschwelle (Standard 10 Tage), schickt der
  Bot einmal täglich ab 9 Uhr eine Nachbestell-Erinnerung.

## 6. Lokal testen

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt      # Windows: .venv\Scripts\pip
cp .env.example .env
.venv/bin/python -m app.main                   # http://127.0.0.1:8080
```

Ohne `SUPBOT_PASSWORD` läuft die Oberfläche ohne Login, ohne
`SUPBOT_TELEGRAM_TOKEN` startet der Bot nicht – für lokale Tests praktisch.

## 7. Updates über Git

Auf dem Entwicklungsrechner:

```bash
git add -A && git commit -m "…" && git push
```

Auf dem VPS:

```bash
sudo bash /root/SupBot/deploy/update.sh
```

Das Skript macht `git pull`, spielt die Dateien nach `/opt/supbot` ein,
aktualisiert die Abhängigkeiten und startet den Dienst neu. `.env` und
`data/` bleiben unangetastet.

### Zugriff auf das private Repo

**Deploy-Key (empfohlen)** – ein Schlüssel, der nur dieses eine Repo lesen darf:

```bash
# auf dem VPS
ssh-keygen -t ed25519 -f ~/.ssh/supbot_deploy -N "" -C "supbot-vps"
cat ~/.ssh/supbot_deploy.pub
```

Den ausgegebenen Public Key auf GitHub eintragen unter
*Repo → Settings → Deploy keys → Add deploy key* (ohne Schreibrechte).
Danach auf dem VPS:

```bash
cat >> ~/.ssh/config <<'EOF'
Host github.com
    IdentityFile ~/.ssh/supbot_deploy
    IdentitiesOnly yes
EOF

git clone git@github.com:Fabian5231/SupBot.git /root/SupBot
```

**Alternative:** Repo auf öffentlich stellen (`gh repo edit Fabian5231/SupBot
--visibility public`), dann genügt die HTTPS-URL ohne Schlüssel. Achte dann
darauf, dass niemals eine `.env` committet wird – sie steht in `.gitignore`,
und in der Datenbank (`data/`, ebenfalls ignoriert) liegen deine Daten.

## 8. Betrieb

```bash
systemctl status supbot          # Status
systemctl restart supbot         # Neustart nach .env-Änderung
journalctl -u supbot -f          # Logs
```

**Backup:** es genügt `/opt/supbot/data/supbot.db`.

```bash
sqlite3 /opt/supbot/data/supbot.db ".backup '/root/supbot-$(date +%F).db'"
```

> Der Dienst muss mit **einem** Worker laufen (so im Unit-File hinterlegt),
> sonst würde der Telegram-Bot mehrfach pollen.

## Projektstruktur

```
app/
  config.py      .env-Auswertung und Einstellungen
  db.py          SQLite-Schema, Verbindung, Settings-Tabelle
  services.py    Tagesplan, Abhaken, Vorrat, Reichweite, Verlauf
  telegram.py    Bot: Polling, Erinnerungsschleife, Befehle, Buttons
  main.py        FastAPI-Routen, Login, Task-Start
  templates/     Jinja2-Seiten
  static/        CSS und ein bisschen JS
deploy/
  install.sh           Installationsskript für den VPS
  update.sh            git pull + Neuinstallation
  supbot.service       systemd-Unit
  nginx.conf.example   Reverse-Proxy-Vorlage
```
