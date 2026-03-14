# llm-gateway

Ein lokaler OpenAI-kompatibler Orchestrator fuer VS Code Clients, der Requests an einen externen `llama.cpp`-Server mit `Qwen2.5-Coder` weiterleitet.

## Praxis-Setup

Dieses Repository ist fuer ein konkretes, praxisnahes Homelab-/Workstation-Setup gedacht:

- Proxmox als Host
- eine dedizierte VM mit AMD MI50 per PCIe-Passthrough
- auf der MI50-VM laeuft `llama.cpp`
- ein separater LXC-Container oder kleiner Linux-Host betreibt diesen Gateway
- VS Code oder Continue sprechen nur mit dem Gateway, nicht direkt mit `llama.cpp`

Die grobe Architektur sieht so aus:

```text
VS Code / Continue
        |
        v
llm-gateway (FastAPI, Python, Uvicorn)
        |
        v
MI50-VM mit llama.cpp + Qwen2.5-Coder
```

Warum diese Aufteilung sinnvoll ist:

- Die GPU-VM bleibt auf Inferenz konzentriert.
- Der Gateway kann Auth, Logging, Modell-Mapping, Health-Checks und Admin-Funktionen uebernehmen.
- VS Code Clients bekommen eine einfache OpenAI-kompatible HTTP-Schnittstelle.
- Netzwerk, Neustarts und spaetere Routing-Logik landen nicht direkt auf der GPU-VM.

Wichtige praktische Annahme:

- Der Gateway kennt nur die HTTP-Adresse des `llama.cpp`-Backends, zum Beispiel `http://192.168.40.111:8080`.
- Wenn du auf der MI50-VM den echten Kontext von 8K auf 16K erhoehst, muss `llama.cpp` dort selbst mit passendem Kontext gestartet werden. Der Gateway-Wert allein reicht dafuer nicht.

## Ziel

Dieses Grundgeruest stellt drei Endpunkte bereit:

- `GET /health`
- `GET /internal/health`
- `GET /v1/models`
- `POST /v1/chat/completions`
- `GET /internal/metrics`
- `GET /internal/admin`

Die Minimalversion priorisiert:

- einfache lokale Startbarkeit
- ein oeffentliches Modellalias `qwen2.5-coder`
- konfigurierbare Weiterleitung an `llama.cpp`
- saubere Timeout- und Fehlerbehandlung

## Modell-Mapping

Der Gateway bietet genau einen oeffentlichen Modellnamen an:

- `PUBLIC_MODEL_NAME=qwen2.5-coder`

Intern sendet der Gateway Requests an das echte Backend-Modell:

- `BACKEND_MODEL_NAME=qwen2.5-coder-7b-instruct-q4_k_m.gguf`

Das bedeutet:

- Clients sprechen immer gegen `qwen2.5-coder`
- der Gateway mappt intern auf das reale `llama.cpp`-Modell
- Antworten werden wieder auf den oeffentlichen Modellnamen zurueckgeschrieben, damit Clients konsistent bleiben

## Authentifizierung

Der Gateway verwendet eine einfache statische Bearer-Token-Authentifizierung aus der `.env`.

- Geschuetzt:
  - `GET /v1/models`
  - `POST /v1/chat/completions`
- Offen:
  - `GET /health`

`/health` bleibt absichtlich offen, damit lokale Betriebschecks und einfache Liveness-Pruefungen ohne Token moeglich bleiben.

## Token-Haertung

Fuer lokale Entwicklung kannst du einen statischen Token setzen. Fuer einen produktionsnaeheren Betrieb solltest du ihn rotieren und nicht bei einem einfachen Platzhalter lassen.

Token erzeugen:

```bash
./scripts/generate_token.sh
```

Token in `.env` rotieren und laufenden Service neu starten:

```bash
./scripts/rotate_token.sh
```

Hinweis:

- Das Rotationsskript aktualisiert `API_BEARER_TOKEN` in `.env`.
- Wenn `llm-gateway.service` aktiv ist, wird der Dienst danach automatisch neu gestartet.

## Request-ID

Jeder eingehende Request bekommt eine Request-ID.

- Wenn der Client `X-Request-ID` mitsendet, uebernimmt der Gateway diesen Wert.
- Wenn der Header fehlt, erzeugt der Gateway eine neue Request-ID.
- Die Request-ID erscheint in den Gateway-Logs.
- Die Request-ID wird als `X-Request-ID` im Response zurueckgegeben.

## Fehlerformat

Fehlerantworten werden, soweit sinnvoll, auf dieses Format normalisiert:

```json
{
  "error": {
    "message": "Missing Authorization header.",
    "type": "authentication_error",
    "code": "invalid_api_key",
    "request_id": "5798982f63b8407fbdfd8ace37dac9f0"
  }
}
```

Das gilt fuer die wichtigsten Faelle:

- `401 Unauthorized`
- `404 Not Found`
- `422 Validation Error`
- `500 Internal Server Error`
- Upstream-Fehler vom `llama.cpp`-Backend
- Upstream-Timeouts

## Betriebsmetriken

Der Gateway stellt einfache In-Memory-Betriebsmetriken unter `GET /internal/metrics` bereit.

- Der Endpoint ist bewusst geschuetzt wie `GET /v1/models`.
- Er liefert JSON, keine Prometheus-Ausgabe.
- Die Daten leben nur im Speicher des laufenden Prozesses.
- Nach Neustart, Reload oder Prozessabsturz beginnen die Zaehler wieder bei null.

Erfasst werden aktuell:

- Gesamtzahl aller Requests
- Requests pro Pfad
- Responses pro Statuscode
- Anzahl Backend-Aufrufe
- Anzahl Backend-Fehler
- Anzahl Backend-Timeouts
- durchschnittliche Request-Dauer in Millisekunden
- Uptime seit Prozessstart

## Health und Readiness

Es gibt bewusst zwei verschiedene Checks:

- `GET /health`
  Ein leichter, offener Liveness-Check. Er prueft nur, ob der Gateway-Prozess laeuft.

- `GET /internal/health`
  Ein geschuetzter Readiness-Check. Er prueft:
  - Gateway laeuft
  - `llama.cpp`-Backend ist erreichbar
  - das konfigurierte `BACKEND_MODEL_NAME` ist im Backend verfuegbar
  - grobe Backend-Latenz in Millisekunden

Fuer den Backend-Check wird absichtlich nur `GET /v1/models` verwendet, kein Chat-Request.

Entscheidung fuer den Statuscode:

- Wenn das Backend nicht erreichbar ist oder das konfigurierte Modell fehlt, antwortet `/internal/health` mit `503 Service Unavailable`.
- Das ist bewusst strenger als ein `200 degraded`, damit Betrieb und Deployment einen echten Readiness-Fehler klar erkennen koennen.

Fuer `systemd` wird dieser Readiness-Check auch beim Start genutzt:

- `ExecStartPost=/opt/llm-gateway/scripts/check_ready.sh`
- Wenn der Gateway zwar startet, aber nicht innerhalb kurzer Zeit bereit ist, gilt der Service-Start als fehlgeschlagen und `systemd` startet ihn gemaess Restart-Policy neu.

## Projektstruktur

```text
.
├── AGENTS.md
├── README.md
├── deploy
│   └── llm-gateway.service
├── requirements.txt
├── .env.example
└── app
    ├── main.py
    ├── config.py
    ├── routes
    │   ├── health.py
    │   ├── internal_health.py
    │   ├── metrics.py
    │   ├── models.py
    │   └── chat.py
    ├── schemas
    │   ├── chat.py
    │   └── models.py
    └── services
        └── llamacpp_client.py
```

## Voraussetzungen

- Python 3.11 oder neuer
- Ein laufender externer `llama.cpp`-Server mit OpenAI-kompatiblem `chat/completions`-Endpunkt

## Einrichtung

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Danach `.env` pruefen oder anpassen. Eine einfache Startkonfiguration ist:

```env
HOST=0.0.0.0
PORT=8000
LOG_LEVEL=info

LLAMACPP_BASE_URL=http://192.168.40.111:8080
LLAMACPP_TIMEOUT_SECONDS=60.0
LLAMACPP_API_KEY=
API_BEARER_TOKEN=change-me
BACKEND_CONTEXT_WINDOW=8192
CONTEXT_RESPONSE_RESERVE=1024
CONTEXT_CHARS_PER_TOKEN=4.0
DEFAULT_MAX_TOKENS=512

PUBLIC_MODEL_NAME=qwen2.5-coder
BACKEND_MODEL_NAME=qwen2.5-coder-7b-instruct-q4_k_m.gguf
```

## Entwicklungsstart

```bash
source .venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Oder direkt:

```bash
./scripts/run_dev.sh
```

## Produktionsstart

```bash
source .venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Verhalten

- `PUBLIC_MODEL_NAME` ist der Modellname, den Clients gegen diesen Proxy verwenden.
- `BACKEND_MODEL_NAME` ist der Modellname, der an den externen `llama.cpp`-Server gesendet wird.
- Wenn ein Client `qwen2.5-coder` anfragt, kann der Proxy daraus intern jeden beliebigen Backend-Modellnamen machen.
- Upstream-Timeouts werden als `504 Gateway Timeout` zurueckgegeben.
- Netzwerk- oder Backend-Fehler werden als JSON-Fehlerantwort an den Client weitergereicht oder gemappt.
- Eingehende Requests, Backend-Requests und Fehler werden geloggt.
- Geschuetzte Endpunkte erwarten `Authorization: Bearer <token>`.
- `X-Request-ID` wird uebernommen, wenn der Client sie sendet, sonst vom Gateway erzeugt.
- Der Gateway schaetzt das Kontextbudget vor dem Backend-Call und kann alte Messages abschneiden, wenn der Request sonst zu gross waere.
- `GET /health` ist nur ein Liveness-Check ohne Upstream-Pruefung.
- `GET /internal/health` ist der geschuetzte Readiness-Check gegen das Backend.
- `GET /internal/metrics` zeigt einfache Laufzeitdaten seit Prozessstart.
- `GET /internal/admin` liefert eine kleine interne Admin-Seite fuer ausgewaehlte Runtime-Einstellungen.
- Wenn das Backend echtes SSE-Streaming liefert, leitet der Gateway dieses durch.
- Wenn das Backend bei `stream=true` nur JSON liefert, erzeugt der Gateway einen einfachen SSE-Fallback.
- Dieser Fallback ist absichtlich minimal: brauchbar fuer OpenAI-aehnliche Clients, aber nicht gleichwertig zu nativer Token-fuer-Token-Ausgabe.

## Kontextbudget

Der Gateway hat eine einfache Kontextbudget-Logik, damit grosse IDE-Requests nicht sofort hart am `llama.cpp`-Limit scheitern.

Konfigurierbar:

- `BACKEND_CONTEXT_WINDOW`
  Das grobe Kontextfenster des Backends. In deinem aktuellen Setup: `8192`.

- `CONTEXT_RESPONSE_RESERVE`
  Reserviert Platz fuer die Modellantwort. Standard: `1024`.

- `CONTEXT_CHARS_PER_TOKEN`
  Ein grober Schaetzwert fuer die Tokenheuristik. Standard: `4.0`.

- `DEFAULT_MAX_TOKENS`
  Wird verwendet, wenn der Client selbst kein `max_tokens` mitsendet. Standard: `512`.

Verhalten:

- Der Gateway schaetzt die Promptgroesse vor dem Upstream-Request.
- Wenn der Request zu gross ist, werden zuerst aeltere Nicht-System-Messages entfernt.
- Wenn das nicht reicht, wird als letzte Notloesung der juengste String-Content gekuerzt.
- Wenn selbst das nicht reicht, kommt ein klarer API-Fehler zurueck statt eines haesslichen Streaming-Abbruchs.

Wichtige Einschraenkung:

- Das ist bewusst keine "intelligente Aufteilung" ueber mehrere Modell-Requests.
- Fuer Coding-Workflows waere so ein automatisches Zerstueckeln oft semantisch kaputt.
- Diese V1 ist nur ein Schutz gegen offensichtliche Kontext-Ueberlaeufe.

## Interne Admin-Seite

Unter `GET /internal/admin` gibt es eine kleine Browser-Oberflaeche fuer:

- `LLAMACPP_BASE_URL`
- `PUBLIC_MODEL_NAME`
- `BACKEND_MODEL_NAME`
- `BACKEND_CONTEXT_WINDOW`
- `CONTEXT_RESPONSE_RESERVE`
- `CONTEXT_CHARS_PER_TOKEN`
- `DEFAULT_MAX_TOKENS`
- `MI50_SSH_HOST`
- `MI50_SSH_USER`
- `MI50_SSH_PORT`
- `MI50_RESTART_COMMAND`
- `MI50_STATUS_COMMAND`
- Continue-YAML-Vorschau

Wichtig:

- Die HTML-Seite selbst ist leicht aufrufbar.
- Die eigentlichen Daten-Endpunkte dahinter bleiben per Bearer-Token geschuetzt.
- Aenderungen werden in `.env` geschrieben und fuer neue Requests sofort uebernommen.
- Das ist bewusst nur eine kleine interne Betriebsseite, kein komplettes Admin-System.
- Ueber die Admin-Seite kann auch ein geschuetzter MI50-Neustart per SSH angestossen werden.
- Ein groesserer Wert bei `BACKEND_CONTEXT_WINDOW` aendert nur die Gateway-Heuristik.
- Wenn das entfernte `llama.cpp` wirklich mit 16K statt 8K laufen soll, muss der MI50-Startbefehl selbst entsprechend angepasst werden, zum Beispiel mit `-c 16384` oder ueber eine passende entfernte systemd-Konfiguration.

## Tests mit curl

Schneller Kompletttest:

```bash
API_BEARER_TOKEN=change-me ./scripts/smoke_test.sh
```

Optional mit anderer Basis-URL:

```bash
API_BEARER_TOKEN=change-me ./scripts/smoke_test.sh http://127.0.0.1:8000
```

Health pruefen:

```bash
curl -s http://127.0.0.1:8000/health
```

Internen Health-/Readiness-Check pruefen:

```bash
curl -s http://127.0.0.1:8000/internal/health \
  -H "Authorization: Bearer change-me"
```

Health mit eigener Request-ID pruefen:

```bash
curl -i http://127.0.0.1:8000/health \
  -H "X-Request-ID: client-rid-123"
```

Models pruefen:

```bash
curl -s http://127.0.0.1:8000/v1/models \
  -H "Authorization: Bearer change-me"
```

Metrics pruefen:

```bash
curl -s http://127.0.0.1:8000/internal/metrics \
  -H "Authorization: Bearer change-me"
```

Admin-Seite oeffnen:

```bash
xdg-open http://127.0.0.1:8000/internal/admin
```

Chat Completion pruefen:

```bash
curl -s http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer change-me" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen2.5-coder",
    "messages": [
      {"role": "system", "content": "You are a helpful coding assistant."},
      {"role": "user", "content": "Write a Python function that adds two numbers."}
    ],
    "temperature": 0.2,
    "max_tokens": 256,
    "stream": false
  }'
```

Streaming pruefen:

```bash
curl -N http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer change-me" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen2.5-coder",
    "messages": [
      {"role": "user", "content": "Explain quicksort in 5 lines."}
    ],
    "stream": true
  }'
```

Erwartung beim Streaming:

- Bei deinem aktuellen `llama.cpp`-Setup kommen echte `data: ...` SSE-Chunks und am Ende `data: [DONE]`.
- Wenn spaeter ein anderes Backend kein SSE liefert, macht der Gateway daraus wenige synthetische SSE-Chunks als Fallback.

Fehlerfall ohne Bearer-Token:

```bash
curl -i http://127.0.0.1:8000/v1/models
```

Fehlerfall fuer internen Health-Check ohne Token:

```bash
curl -i http://127.0.0.1:8000/internal/health
```

Fehlerfall fuer ungueltigen Pfad:

```bash
curl -i http://127.0.0.1:8000/not-found
```

Fehlerfall fuer unvollstaendige Chat-Anfrage:

```bash
curl -i http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer change-me" \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen2.5-coder"}'
```

Metrics ohne Token:

```bash
curl -i http://127.0.0.1:8000/internal/metrics
```

## VS Code Client

Als konkretes Beispiel ist eine Continue-Konfiguration unter `deploy/continue.config.yaml.example` hinterlegt.

Fuer eine direkt nutzbare lokale Datei mit dem aktuellen Token:

```bash
./scripts/render_continue_config.sh
```

Das erzeugt:

- `deploy/continue.config.local.yaml`

Diese Datei ist bewusst in `.gitignore`, damit der Bearer-Token nicht versehentlich ins Repository kommt.

Vorgehen:

1. Continue in VS Code installieren.
2. Die Beispielkonfiguration als Ausgangspunkt verwenden.
3. Entweder die Beispielkonfiguration manuell anpassen oder `./scripts/render_continue_config.sh` verwenden.
4. `apiBase` auf deinen Gateway setzen.
5. `apiKey` durch den aktuellen Bearer-Token aus `.env` ersetzen, falls du nicht die gerenderte Datei verwendest.

Beispiel:

```yaml
name: llm-gateway
version: 0.0.1
schema: v1

models:
  - name: qwen2.5-coder-local
    provider: openai
    model: qwen2.5-coder
    apiBase: http://127.0.0.1:8000/v1
    apiKey: CHANGE_ME
```

Wichtige Annahme:

- Dieses Beispiel geht davon aus, dass VS Code auf derselben Maschine laeuft wie der Gateway.
- Wenn VS Code per SSH, Dev Container oder WSL laeuft, ist `127.0.0.1` unter Umstaenden nicht der richtige Host.
- Dann musst du statt `127.0.0.1` die aus Sicht des VS-Code-Clients erreichbare Adresse verwenden.

## Admin-Seite

Die interne Admin-Seite liegt unter:

- `GET /internal/admin`

Wichtig:

- Die HTML-Seite selbst ist absichtlich leicht erreichbar.
- Ohne Bearer-Token kann die Seite nur den offenen Liveness-Check `GET /health` abfragen.
- Dann wird der Gateway-Status gruen gezeigt, wenn der Prozess lebt.
- Der MI50- bzw. Backend-Status bleibt ohne Token bewusst gelb mit Hinweis, weil `/internal/health`, `/internal/metrics` und die Konfigurationsdaten geschuetzt sind.
- Fuer den vollstaendigen Status also erst den Bearer-Token eintragen und dann `Alles aktualisieren` klicken.

## Bekannte Grenzen dieses MVP

- Es gibt nur ein minimales Modell-Registry-Verhalten.
- Es werden noch keine Embeddings, Tools oder Responses-API-Endpunkte angeboten.
- Die Kompatibilitaet ist auf das OpenAI-uebliche `chat/completions`-Muster begrenzt.
- `scripts/smoke_test.sh` ist nur ein einfacher Betriebscheck, kein Testframework.

## Entwicklungsworkflow

1. Virtuelle Umgebung anlegen und aktivieren.
2. Abhaengigkeiten installieren.
3. `.env` mit der `LLAMACPP_BASE_URL` befuellen.
4. FastAPI lokal mit Uvicorn starten.
5. Mit `curl` zuerst `health`, dann `models`, dann `chat/completions` pruefen.

Wenn `health` und `models` lokal funktionieren, aber `chat/completions` fehlschlaegt, liegt der Fehler fast sicher an der Erreichbarkeit oder API-Kompatibilitaet des externen `llama.cpp`-Servers.

## Direkter Backend-Test

Bevor du den Proxy testest, kannst du das externe Backend direkt pruefen:

```bash
curl -s http://192.168.40.111:8080/v1/models
```

Wenn dort kein JSON kommt, ist nicht der Proxy das Problem, sondern die Verbindung oder der laufende `llama.cpp`-Dienst.

## systemd

Eine Beispiel-Service-Datei liegt hier:

- `deploy/llm-gateway.service`

Service-User anlegen:

```bash
groupadd --system llmgateway
useradd --system --gid llmgateway --home-dir /opt/llm-gateway --shell /usr/sbin/nologin llmgateway
chown -R llmgateway:llmgateway /opt/llm-gateway
chmod 750 /opt/llm-gateway
chmod 640 /opt/llm-gateway/.env
```

Installieren:

```bash
cp deploy/llm-gateway.service /etc/systemd/system/llm-gateway.service
systemctl daemon-reload
systemctl enable llm-gateway
```

Starten und pruefen:

```bash
systemctl start llm-gateway
systemctl status llm-gateway --no-pager
journalctl -u llm-gateway -f
```

## MI50-Backend per SSH neu starten

Fuer den externen MI50-Host gibt es ein einfaches Hilfsskript:

- `scripts/restart_mi50_backend.sh`

Es liest diese Variablen aus `.env` oder aus der aktuellen Shell:

- `MI50_SSH_HOST`
- `MI50_SSH_USER`
- `MI50_SSH_PORT`
- `MI50_RESTART_COMMAND`
- `MI50_STATUS_COMMAND`

Beispiel:

```env
MI50_SSH_HOST=192.168.40.111
MI50_SSH_USER=llmadmin
MI50_SSH_PORT=22
MI50_RESTART_COMMAND=sudo systemctl restart llama.cpp
MI50_STATUS_COMMAND=sudo systemctl status llama.cpp --no-pager
```

Aufruf:

```bash
./scripts/restart_mi50_backend.sh
```

Wenn du das ueber die Admin-Seite machen willst:

1. Bearer-Token eintragen
2. `MI50_SSH_HOST`, `MI50_SSH_USER` und optional die Commands pflegen
3. `Speichern`
4. `MI50 neu starten`

Fuer einen 16K-Start brauchst du einen passenden entfernten Startbefehl. Beispielhaft waere das nicht mehr nur ein einfaches `systemctl restart`, sondern ein Remote-Command oder eine entfernte Service-Definition, die `llama.cpp` mit einem groesseren Kontext startet.

Wichtige Annahmen:

- Der Gateway-Host muss den MI50-Host per SSH erreichen koennen.
- Fuer produktionsnahen Betrieb solltest du SSH-Schluessel statt Passwort-Prompts verwenden.
- Der entfernte User muss den Restart-Befehl ausfuehren duerfen, idealerweise gezielt fuer den `llama.cpp`-Service statt mit zu breiten `sudo`-Rechten.

Starten:

```bash
systemctl start llm-gateway
```

Status pruefen:

```bash
systemctl status llm-gateway
```

Logs ansehen:

```bash
journalctl -u llm-gateway -f
```

Hinweise zur Haertung:

- `ProtectSystem=full` passt fuer das aktuelle Setup, solange der Gateway nicht in Projektdateien schreiben muss.
- `ProtectHome=true` ist unkritisch, solange das Deployment unter `/opt/llm-gateway` liegt.
- Wenn du spaeter lokale Dateischreibzugriffe oder Socket-Dateien brauchst, musst du die Haertung gezielt anpassen.
- Der Start nutzt zusaetzlich einen lokalen Readiness-Check ueber `scripts/check_ready.sh`.

## Deployment-Schritte

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Dann in `.env` mindestens setzen:

- `LLAMACPP_BASE_URL`
- `API_BEARER_TOKEN`
- optional `BACKEND_MODEL_NAME`

Danach Service-User anlegen, Service-Datei installieren und den Dienst starten.

## Typische Fehlerquellen

- Falscher Port fuer `LLAMACPP_BASE_URL`
  In deinem aktuellen Setup ist `http://192.168.40.111:8080` korrekt, nicht `:8000`.

- Falscher interner Modellname
  Wenn `BACKEND_MODEL_NAME` nicht zum echten `llama.cpp`-Modell passt, scheitert `chat/completions`.

- Lokaler Gateway laeuft, aber Chat scheitert
  Dann zuerst das Backend direkt mit `curl http://192.168.40.111:8080/v1/models` pruefen.

- `401 Unauthorized`
  Dann fehlt der `Authorization: Bearer ...` Header oder `API_BEARER_TOKEN` stimmt nicht.

- `/health` ist gruen, aber `/internal/health` liefert `503`
  Dann lebt der Gateway-Prozess zwar, aber das Backend ist nicht erreichbar oder das konfigurierte `BACKEND_MODEL_NAME` fehlt.

- Nach Token-Rotation funktionieren alte Clients nicht mehr
  Dann verwendet der Client noch den alten Bearer-Token. Den neuen Wert aus `.env` oder `rotate_token.sh` uebernehmen.

- VS Code erreicht den Gateway nicht
  Dann stimmt oft `apiBase` nicht oder `127.0.0.1` ist aus Sicht des VS-Code-Clients nicht der richtige Host.

- Metrics-Zaehler wirken nach Neustart leer
  Das ist in dieser Version normal, weil die Metriken nur im Speicher gehalten werden.

- `stream=true` liefert nichts Brauchbares
  Dann liefert das Backend wahrscheinlich kein echtes SSE oder wird von einem Reverse Proxy verfremdet.

- `504 Gateway Timeout`
  Dann war das Backend zu langsam oder nicht erreichbar innerhalb von `LLAMACPP_TIMEOUT_SECONDS`.
