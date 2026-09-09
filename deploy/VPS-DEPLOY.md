# Runbook: SupBot auf dem VPS deployen

Anleitung für einen Agenten (Claude Code), der auf dem VPS als `root` läuft.
Arbeite die Schritte **der Reihe nach** ab und prüfe nach jedem Schritt die
angegebene Verifikation. Bei einem Fehlschlag: **stoppen, Ursache melden,
nicht weiterlaufen.**

- **Domain:** `supbot.fabian-social.dev`
- **Repo:** `https://github.com/Fabian5231/SupBot`
- **Ziel:** Dienst unter `/opt/supbot`, nur auf `127.0.0.1` lauschend,
  nginx als Reverse Proxy davor, Let's-Encrypt-Zertifikat via certbot.

## Regeln

1. **Erfinde keine Geheimnisse.** Telegram-Token und Web-Passwort kommen vom
   Benutzer. Fehlen sie, frage danach und warte auf die Antwort.
2. **Gib keine Secrets im Klartext aus.** Weder Token noch Passwort noch der
   Inhalt von `.env` gehören in die Ausgabe oder in ein Log.
3. **Fass fremde Dienste nicht an.** Auf dem Server laufen andere Anwendungen.
   Keine bestehende nginx-Site ändern, keinen fremden Port belegen, keinen
   fremden Dienst neu starten.
4. **Kein `git push`** von diesem Server aus.

---

## Schritt 1 — Vorprüfung

```bash
. /etc/os-release && echo "$PRETTY_NAME"      # Ubuntu 20.04+ / Debian 10+
free -m | awk '/Mem:/ {print $2" MB RAM"}'
df -h / | tail -1
systemctl is-active nginx || echo "nginx laeuft nicht"
```

**Verifikation:** OS passt, mindestens ~200 MB RAM frei, Platz auf `/`.

Prüfe, ob SupBot schon installiert ist. Existiert `/opt/supbot/.env` bereits,
ist das ein Update: Schritt 5 entfällt dann bis auf den Port, und der Installer
überschreibt die vorhandene `.env` nicht.

```bash
systemctl list-unit-files | grep -i supbot || echo "noch nicht installiert"
ls -la /opt/supbot/.env 2>/dev/null || echo "keine .env vorhanden"
```

## Schritt 2 — DNS prüfen (vor certbot zwingend)

```bash
SERVER_IP=$(curl -fsS -4 ifconfig.me)
DNS_IP=$(dig +short A supbot.fabian-social.dev | tail -1)
echo "Server: $SERVER_IP | DNS: $DNS_IP"
```

**Verifikation:** Beide IPs sind identisch.

Wenn nicht: **hier stoppen** und dem Benutzer melden, dass im DNS ein
A-Record `supbot` → `$SERVER_IP` fehlt. Certbot scheitert sonst an der
HTTP-01-Challenge. Nach dem Eintragen können einige Minuten Propagation
nötig sein.

> `.dev` ist HSTS-preloaded: Browser erzwingen HTTPS. Ohne Zertifikat ist die
> Seite in keinem Browser erreichbar — Schritt 7 ist also Pflicht, nicht Kür.

## Schritt 3 — Freien Port ermitteln

Der Dienst braucht einen lokalen Port, den noch niemand nutzt. Sammle alle
belegten Ports aus drei Quellen und nimm den ersten freien aus `8080–8099`:

```bash
# 1) aktuell lauschende Sockets
ss -ltn | awk 'NR>1 {print $4}' | sed 's/.*://' > /tmp/used_ports
# 2) Ports, auf die nginx bereits weiterleitet
grep -rhoE 'proxy_pass[[:space:]]+https?://[^;]*:[0-9]+' /etc/nginx/ 2>/dev/null \
  | grep -oE '[0-9]+$' >> /tmp/used_ports
# 3) Ports, auf denen nginx selbst lauscht (auch "listen [::]:8084 ssl")
grep -rhoE '^[[:space:]]*listen[[:space:]]+[^;]+' /etc/nginx/ 2>/dev/null \
  | grep -oE '[0-9]+' >> /tmp/used_ports

# Belegte Ports im Zielbereich anzeigen
sort -un /tmp/used_ports | awk '$1 >= 8080 && $1 <= 8099' | tr '\n' ' '; echo

# ersten freien Port merken — als Datei, damit er den naechsten Befehl ueberlebt
for P in $(seq 8080 8099); do
  grep -qx "$P" /tmp/used_ports || { echo "$P" > /tmp/supbot_port; break; }
done
echo "gewaehlter Port: $(cat /tmp/supbot_port)"
```

> Quelle 3 liefert durch IP-Adressen auch Zahlen wie `127` oder `0` mit. Das ist
> Absicht: lieber einen Port zu viel als belegt behandeln als einen zu wenig.
> Die Anzeige oben filtert deshalb auf `8080–8099`.

**Wichtig:** Jeder Befehlsblock läuft in einer eigenen Shell — Variablen aus
einem früheren Block sind weg. Beginne deshalb jeden folgenden Block, der den
Port braucht, mit:

```bash
PORT=$(cat /tmp/supbot_port)
```

Gegenprobe, dass wirklich nichts auf dem Port lauscht:

```bash
PORT=$(cat /tmp/supbot_port)
ss -ltnp "sport = :$PORT" | tail -n +2 | grep . && echo "BELEGT" || echo "frei"
```

**Verifikation:** Ausgabe `frei`. Bei `BELEGT` den nächsten Kandidaten nehmen.

Prüfe außerdem, dass die Domain nicht schon von einer anderen Site bedient wird:

```bash
grep -rn "server_name" /etc/nginx/sites-enabled/ /etc/nginx/conf.d/ 2>/dev/null \
  | grep -i "supbot.fabian-social.dev" && echo "ACHTUNG: bereits konfiguriert"
```

## Schritt 4 — Anwendung installieren

Repo holen (falls noch nicht vorhanden) und Installer laufen lassen:

```bash
apt-get update -qq && apt-get install -y git
[ -d /root/SupBot/.git ] || git clone https://github.com/Fabian5231/SupBot.git /root/SupBot
cd /root/SupBot && git pull --ff-only
bash deploy/install.sh
```

Das Skript legt Benutzer `supbot`, `/opt/supbot` samt Virtualenv, `/opt/supbot/.env`
(mit erzeugtem `SUPBOT_SECRET`) und den systemd-Dienst an.

> Ist das Repo privat und der Clone scheitert an fehlender Authentifizierung:
> **stoppen** und den Benutzer nach einem Deploy-Key fragen (siehe README
> Abschnitt 7). Keine Zugangsdaten selbst erzeugen oder erraten.

**Verifikation:** `ls /opt/supbot/.venv/bin/uvicorn` existiert.

## Schritt 5 — Konfiguration setzen

Setze zuerst den Port — der enthält kein Geheimnis:

```bash
cat > /tmp/setkv.py <<'PY'
import re, sys, pathlib
p = pathlib.Path("/opt/supbot/.env")
s = p.read_text(encoding="utf-8")
for arg in sys.argv[1:]:
    key, _, value = arg.partition("=")
    line = f"{key}={value}"
    s = (re.sub(rf"(?m)^{re.escape(key)}=.*$", lambda _m: line, s)
         if re.search(rf"(?m)^{re.escape(key)}=", s) else s.rstrip() + "\n" + line + "\n")
p.write_text(s, encoding="utf-8")
print("gesetzt:", " ".join(a.split("=")[0] for a in sys.argv[1:]))
PY

PORT=$(cat /tmp/supbot_port)
python3 /tmp/setkv.py "SUPBOT_PORT=$PORT" "SUPBOT_HOST=127.0.0.1" "SUPBOT_TZ=Europe/Berlin"
```

Für **Token und Passwort** frage den Benutzer. Er hat zwei Möglichkeiten — biete
beide an:

**a) Der Benutzer trägt sie selbst ein** (bevorzugt, du siehst die Werte nie):

> „Führe bitte `nano /opt/supbot/.env` aus, trage `SUPBOT_TELEGRAM_TOKEN=` und
> `SUPBOT_PASSWORD=` ein, speichern mit `Strg+O`, `Enter`, `Strg+X`. Sag mir
> Bescheid, wenn du fertig bist."

**b) Der Benutzer nennt dir die Werte**, dann setzt du sie so:

```bash
python3 /tmp/setkv.py "SUPBOT_TELEGRAM_TOKEN=<token>" "SUPBOT_PASSWORD=<passwort>"
```

Danach aufräumen und übernehmen:

```bash
rm -f /tmp/setkv.py
chown supbot:supbot /opt/supbot/.env && chmod 600 /opt/supbot/.env
systemctl restart supbot
```

Kontrolle, dass alle Schlüssel gefüllt sind — **nur Schlüsselnamen ausgeben,
niemals `cat /opt/supbot/.env`**:

```bash
awk -F= '/^SUPBOT_[A-Z_]+=/ {print $1, (length($2) ? "gesetzt" : "LEER")}' /opt/supbot/.env
```

Prüfe, dass `SUPBOT_SECRET` gefüllt ist (der Installer erzeugt ihn) — nur die
Länge ausgeben, nicht den Wert:

```bash
awk -F= '/^SUPBOT_SECRET=/ {print "SECRET Laenge:", length($2)}' /opt/supbot/.env
```

**Verifikation:** Länge 64. Ist sie 0, neu erzeugen:

```bash
SECRET=$(/opt/supbot/.venv/bin/python -c "import secrets;print(secrets.token_hex(32))")
sed -i "s|^SUPBOT_SECRET=.*|SUPBOT_SECRET=$SECRET|" /opt/supbot/.env
systemctl restart supbot
```

## Schritt 6 — Dienst prüfen

```bash
PORT=$(cat /tmp/supbot_port)
systemctl status supbot --no-pager | head -20
sleep 3
curl -fsS "http://127.0.0.1:$PORT/healthz"; echo
journalctl -u supbot -n 30 --no-pager
```

**Verifikation:** `{"ok":true,...}` und im Log `Application startup complete`
sowie `Telegram verbunden als @…`. Erscheint `SUPBOT_TELEGRAM_TOKEN fehlt`,
ist Schritt 5 nicht durchgelaufen.

## Schritt 7 — nginx einrichten

Neue Site anlegen — **nur diese eine Datei**, bestehende Konfigurationen bleiben
unberührt:

```bash
PORT=$(cat /tmp/supbot_port)
cat > /etc/nginx/sites-available/supbot <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name supbot.fabian-social.dev;

    location / {
        proxy_pass http://127.0.0.1:$PORT;
        proxy_set_header Host              \$host;
        proxy_set_header X-Real-IP         \$remote_addr;
        proxy_set_header X-Forwarded-For   \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF

ln -sfn /etc/nginx/sites-available/supbot /etc/nginx/sites-enabled/supbot
nginx -t && systemctl reload nginx
curl -fsS -H "Host: supbot.fabian-social.dev" http://127.0.0.1/healthz; echo
```

**Verifikation:** `nginx -t` meldet `syntax is ok` / `test is successful`, und
der `curl` liefert wieder `{"ok":true,...}`.

Port 80 und 443 müssen von außen erreichbar sein:

```bash
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow 'Nginx Full'; ufw status
fi
```

Der Anwendungsport selbst darf **nicht** freigegeben werden — der Dienst lauscht
ohnehin nur auf `127.0.0.1`. Gegenprobe:

```bash
PORT=$(cat /tmp/supbot_port)
ss -ltn "sport = :$PORT"    # muss 127.0.0.1:$PORT zeigen, nicht 0.0.0.0
```

## Schritt 8 — SSL mit certbot

```bash
apt-get install -y certbot python3-certbot-nginx
certbot --nginx -d supbot.fabian-social.dev \
        --agree-tos --redirect --no-eff-email -m DEINE@MAILADRESSE
```

Die Mailadresse vorher beim Benutzer erfragen (dient nur Ablaufwarnungen).
Certbot passt die Site-Datei selbst an: Zertifikat einbinden, Port 443 öffnen,
HTTP → HTTPS umleiten.

**Verifikation:**

```bash
nginx -t && systemctl reload nginx
curl -fsS https://supbot.fabian-social.dev/healthz; echo
curl -sI http://supbot.fabian-social.dev | head -3      # 301 auf https
certbot certificates | grep -A3 supbot.fabian-social.dev
```

Automatische Verlängerung prüfen:

```bash
systemctl list-timers | grep -i certbot
certbot renew --dry-run
```

**Verifikation:** Timer aktiv, Dry-Run endet mit
`Congratulations, all simulated renewals succeeded`.

Scheitert certbot mit `Timeout during connect` oder `NXDOMAIN`, stimmt der
A-Record aus Schritt 2 nicht oder Port 80 ist von außen dicht — dann dort
weitermachen, **nicht** mit `--staging` oder manuellen Zertifikaten behelfen.

## Schritt 9 — Abschluss

```bash
systemctl is-enabled supbot && systemctl is-active supbot
systemctl restart supbot && sleep 3 && curl -fsS https://supbot.fabian-social.dev/healthz; echo
```

Melde dem Benutzer zum Schluss kurz:

- verwendeter **Port** und warum (welche waren belegt)
- **URL**: `https://supbot.fabian-social.dev`
- **Zertifikat** gültig bis (aus `certbot certificates`)
- die verbleibenden **manuellen Schritte**:
  1. Dem Telegram-Bot einmal `/start` schreiben — erst dann kennt er die Chat-ID
     und kann Erinnerungen schicken.
  2. In der Weboberfläche unter *Supplemente* den Einnahmeplan anlegen und unter
     *Vorrat* die Packungen erfassen.
  3. Unter *Einstellungen* → *Testnachricht senden* die Verbindung prüfen.

---

## Später: Update einspielen

```bash
sudo bash /root/SupBot/deploy/update.sh
curl -fsS https://supbot.fabian-social.dev/healthz; echo
```

`.env` und `data/` bleiben dabei unangetastet.

## Wenn etwas schiefgeht

| Symptom | Prüfen |
|---|---|
| `502 Bad Gateway` | Läuft der Dienst? `systemctl status supbot`, `curl 127.0.0.1:$PORT/healthz`. Port in der nginx-Site identisch mit `SUPBOT_PORT` in `.env`? |
| Dienst startet nicht | `journalctl -u supbot -n 50`. Meist Tippfehler in `.env` oder Port belegt. |
| Bot schweigt | Token gesetzt? `journalctl -u supbot \| grep -i telegram`. Chat-ID erst nach `/start` vorhanden. |
| Bot schickt doppelt | Läuft der Dienst mehrfach? Es darf nur **ein** Worker laufen (`--workers 1` im Unit-File). |
| Login schlägt fehl | `SUPBOT_SECRET` nach einem Neustart geändert? Dann sind alte Cookies ungültig — neu anmelden. |

Backup vor riskanten Aktionen:

```bash
sqlite3 /opt/supbot/data/supbot.db ".backup '/root/supbot-$(date +%F).db'"
```
