# llm-gateway

Ein lokaler OpenAI-kompatibler Orchestrator fuer VS Code Clients, der Requests an einen externen `llama.cpp`-Server weiterleitet. Der aktuelle Standardbetrieb ist `devstral-q3`, optional mit umschaltbaren MI50-Profilen wie `Qwen`.

<p align="center">
  <img src="docs/admin-hub-preview.svg" alt="llm-gateway admin hub preview" width="1100">
</p>

<p align="center">
  Linux-styliger Admin-Hub mit Dashboard, Profilumschaltung, Skills/MCP, Chat, Memory, Database, Storage, Home Assistant und Ops.
</p>

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
MI50-VM mit llama.cpp + Devstral oder Qwen
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
- `GET /admin/login`
- `GET /internal/admin`
- `GET /internal/chat`
- `POST /api/device/ask`
- `GET /api/admin/home-assistant/status`
- `GET /api/admin/home-assistant/entities`
- `POST /api/admin/home-assistant/action`
- `POST /api/device/home-assistant/action`

Die Minimalversion priorisiert:

- einfache lokale Startbarkeit
- standardmaessig `devstral-q3` als oeffentliches Modell
- konfigurierbare Weiterleitung an `llama.cpp`
- saubere Timeout- und Fehlerbehandlung

## Modell-Mapping

Der Gateway bietet standardmaessig genau einen oeffentlichen Modellnamen an:

- `PUBLIC_MODEL_NAME=devstral-q3`

Intern sendet der Gateway Requests an das echte Backend-Modell:

- `BACKEND_MODEL_NAME=Devstral-Small-2-24B-Instruct-2512-Q3_K_M.gguf`

Das bedeutet:

- Clients sprechen standardmaessig gegen `devstral-q3`
- der Gateway mappt intern auf das reale `llama.cpp`-Modell
- Antworten werden wieder auf den oeffentlichen Modellnamen zurueckgeschrieben, damit Clients konsistent bleiben

## Plattform-V1

Der Gateway ist nicht mehr nur ein einzelner Qwen-Proxy, sondern die erste kleine AI-Plattform-Stufe:

- Fast Model, aktuell ebenfalls `devstral-q3`
- Deep Model, z. B. `devstral`
- Auto-Routing fuer einfache vs. komplexere Aufgaben
- Admin-Chatseite mit Session-Verlauf und Streaming
- vorbereitete PostgreSQL-Zielstruktur fuer spaeteren persistenten Memory-Betrieb
- Login-Grundlage fuer Browser-Adminseiten
- vorbereiteter Device-Endpunkt fuer spaetere Raspberry-Pi-/TTS-Anbindung
- vorbereitete Home-Assistant-Integration mit sicherer Service-Whitelist

Wichtige ehrliche Einschraenkung der aktuellen V1:

- Ohne gesetzte `DATABASE_URL` nutzt die neue Admin-Chat-/Session-Schicht weiterhin den In-Memory-Store im Gateway-Prozess.
- Das bedeutet dann: Sessions und Zusammenfassungen ueberleben keinen Neustart.
- Mit gesetzter `DATABASE_URL` nutzt der Gateway PostgreSQL fuer Sessions, Messages und Rolling Summaries.
- Das Schema wird dabei automatisch aus `deploy/postgres_schema.sql` initialisiert, sobald der erste PostgreSQL-Zugriff erfolgt.
- Wenn die Datenbank nicht erreichbar ist, ist das jetzt ein echter Betriebsfehler und kein stilles "wird schon irgendwie im RAM weitergehen".

## Ein-Modell-Betrieb jetzt, Deep-Modell spaeter

Wenn du aktuell nur ein Modell gleichzeitig auf der MI50 fahren kannst, ist das kein Problem.

Empfohlener Ist-Zustand:

- `FAST_MODEL_*` auf dein laufendes Devstral setzen
- `DEEP_MODEL_*` leer lassen oder noch nicht aktiv verwenden
- `Auto-Routing` faellt dann automatisch auf das Fast Model zurueck

Das ist bewusst im Code abgefangen:

- `deep` ohne konfiguriertes Deep Model -> Fallback auf Fast
- `auto` ohne konfiguriertes Deep Model -> Fast

Sobald spaeter eine zweite GPU oder ein zweiter separater Modellprozess dazukommt, kannst du `DEEP_MODEL_*` einfach nachziehen.

## Routing

Es gibt drei Modi:

- `fast`
  Erzwingt das schnelle Modell.
- `deep`
  Erzwingt das tiefere Modell.
- `auto`
  Der Gateway entscheidet regelbasiert.

Die Auto-Regeln sind absichtlich einfach und nachvollziehbar:

- Deep, wenn Deep-Keywords vorkommen
- Deep, wenn die Eingabe laenger als `ROUTING_LENGTH_THRESHOLD` ist
- Deep, wenn der Chat-Verlauf laenger als `ROUTING_HISTORY_THRESHOLD` ist
- sonst Fast

Konfigurierbar per `.env`:

- `FAST_MODEL_PUBLIC_NAME`
- `FAST_MODEL_BACKEND_NAME`
- `FAST_MODEL_BASE_URL`
- `DEEP_MODEL_PUBLIC_NAME`
- `DEEP_MODEL_BACKEND_NAME`
- `DEEP_MODEL_BASE_URL`
- `ADMIN_DEFAULT_MODE`
- `ROUTING_DEEP_KEYWORDS`
- `ROUTING_LENGTH_THRESHOLD`
- `ROUTING_HISTORY_THRESHOLD`
- `DATABASE_URL`
- `ADMIN_USERNAME`
- `ADMIN_PASSWORD`
- `ADMIN_SESSION_SECRET`
- `ADMIN_SESSION_TTL_HOURS`
- `ADMIN_COOKIE_SECURE`
- `DEVICE_SHARED_TOKEN`
- `HOME_ASSISTANT_BASE_URL`
- `HOME_ASSISTANT_TOKEN`
- `HOME_ASSISTANT_TIMEOUT_SECONDS`
- `HOME_ASSISTANT_ALLOWED_SERVICES`
- `HOME_ASSISTANT_ALLOWED_ENTITY_PREFIXES`

## Session- und Context-Management

Fuer die neue Admin-Chatseite arbeitet der Gateway mit drei einfachen Bausteinen:

- Session-Verlauf
  Letzte Nachrichten einer Session werden gespeichert.
- Sliding Window
  Fuer den Modellaufruf werden nur die letzten relevanten Nachrichten verwendet.
- Rolling Summary
  Wenn eine Session laenger wird, erzeugt der Gateway eine knappe Text-Zusammenfassung aelterer Inhalte statt sie einfach stumpf zu verwerfen.

Das ist bewusst keine magische Langzeit-KI-Memory-Schicht. Es ist eine kleine, robuste V1 gegen offensichtliche Kontextueberlaeufe.

## PostgreSQL-Zielstruktur

Fuer den persistenten Memory-Betrieb liegt das Schema hier:

- `deploy/postgres_schema.sql`

Enthalten sind:

- `chat_sessions`
- `chat_messages`
- `memory_summaries`
- `routing_events`

Konfigurationsschalter:

- `DATABASE_URL`

Wenn `DATABASE_URL` gesetzt ist:

- wird automatisch auf den PostgreSQL-Store umgeschaltet
- das Schema wird bei Bedarf automatisch angelegt
- Sessions und Rolling Summaries bleiben nach Gateway-Neustarts erhalten

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

PUBLIC_MODEL_NAME=devstral-q3
BACKEND_MODEL_NAME=Devstral-Small-2-24B-Instruct-2512-Q3_K_M.gguf
FAST_MODEL_PUBLIC_NAME=devstral-q3
FAST_MODEL_BACKEND_NAME=Devstral-Small-2-24B-Instruct-2512-Q3_K_M.gguf
FAST_MODEL_BASE_URL=http://192.168.40.111:8080
DEEP_MODEL_PUBLIC_NAME=
DEEP_MODEL_BACKEND_NAME=
DEEP_MODEL_BASE_URL=
ADMIN_DEFAULT_MODE=fast
ROUTING_DEEP_KEYWORDS=architektur,analyse,refactor,refactoring,debug,design,plan,root cause,komplex
ROUTING_LENGTH_THRESHOLD=1200
ROUTING_HISTORY_THRESHOLD=8
# Beispiel: DATABASE_URL=postgresql://llmgateway:change-me@192.168.40.120:5432/llm_gateway
DATABASE_URL=
ADMIN_USERNAME=admin
ADMIN_PASSWORD=
ADMIN_SESSION_SECRET=
ADMIN_SESSION_TTL_HOURS=24
ADMIN_COOKIE_SECURE=false
DEVICE_SHARED_TOKEN=
HOME_ASSISTANT_BASE_URL=
HOME_ASSISTANT_TOKEN=
HOME_ASSISTANT_TIMEOUT_SECONDS=10.0
HOME_ASSISTANT_ALLOWED_SERVICES=light.turn_on,light.turn_off,switch.turn_on,switch.turn_off,climate.set_temperature,script.turn_on
HOME_ASSISTANT_ALLOWED_ENTITY_PREFIXES=light.,switch.,climate.,script.
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
- `GET /admin/login` liefert den Browser-Login fuer die geschuetzten Admin-Seiten.
- `GET /internal/admin` liefert jetzt einen echten Admin-Hub mit Menueleiste.
- `GET /internal/chat` bleibt als eigenstaendige Chat-Seite bestehen und wird im Hub eingebettet.
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

## Admin-Hub

Unter `GET /internal/admin` gibt es jetzt einen Browser-Hub mit Login und Menueleiste fuer:

- `LLAMACPP_BASE_URL`
- `PUBLIC_MODEL_NAME`
- `BACKEND_MODEL_NAME`
- `FAST_MODEL_*`
- `DEEP_MODEL_*`
- `BACKEND_CONTEXT_WINDOW`
- `CONTEXT_RESPONSE_RESERVE`
- `CONTEXT_CHARS_PER_TOKEN`
- `DEFAULT_MAX_TOKENS`
- `ADMIN_DEFAULT_MODE`
- Routing-Regeln
- `MI50_SSH_HOST`
- `MI50_SSH_USER`
- `MI50_SSH_PORT`
- `MI50_RESTART_COMMAND`
- `MI50_STATUS_COMMAND`
- `MI50_LOGS_COMMAND`
- `DATABASE_URL`
- Datenbankstatus und Schema-Initialisierung
- Home-Assistant-Verbindung und erlaubte Services
- Speicher-/Persistenz-Ueberblick
- Continue-YAML-Vorschau

Wichtig:

- Browser-Zugriff laeuft jetzt ueber `GET /admin/login` und ein Session-Cookie.
- API-Endpunkte im Admin-Bereich akzeptieren Session-Cookie oder Bearer-Token.
- Aenderungen werden in `.env` geschrieben und fuer neue Requests sofort uebernommen.
- Das ist bewusst noch kein vollwertiges Internet-Admin-System, aber eine saubere V1 fuer spaeteren Reverse-Proxy-Betrieb.
- Ueber die Admin-Seite kann auch ein geschuetzter MI50-Neustart per SSH angestossen werden.
- Ein groesserer Wert bei `BACKEND_CONTEXT_WINDOW` aendert nur die Gateway-Heuristik.
- Wenn das entfernte `llama.cpp` wirklich mit 16K statt 8K laufen soll, muss der MI50-Startbefehl selbst entsprechend angepasst werden, zum Beispiel mit `-c 16384` oder ueber eine passende entfernte systemd-Konfiguration.

## Admin-Chatseite

Unter `GET /internal/chat` gibt es jetzt eine getrennte Chatoberflaeche fuer den direkten Betrieb:

- Cookie-Login ueber den Admin-Hub oder Bearer-Token fuer direkte API-Tests
- Sessions anlegen, laden, resetten und loeschen
- `Auto`, `Fast` oder `Deep` waehlen
- Antworten direkt streamen
- Text- und PDF-Dateien direkt im Chat hochladen und sofort als Kontext auswaehlen
- den aufgeloesten Modellnamen und die Routing-Regel pro Session sehen
- pro Assistant-Antwort Tokens und `t/s` sehen, wenn `llama.cpp` Nutzungs-/Timingdaten liefert

Im Hub wird diese Seite ueber den Chat-Tab eingebettet, damit du nicht mehr staendig zwischen komplett getrennten Browser-Seiten springen musst.

Wichtige Grenze:

- Ohne `DATABASE_URL` bleiben die Session-Daten weiter prozesslokal im Speicher.
- Fuer persistenten Betrieb musst du also wirklich eine PostgreSQL-URL setzen.

## Home-Assistant-Integration

Der Gateway hat jetzt eine erste sichere Home-Assistant-V1:

- `GET /api/admin/home-assistant/status`
- `GET /api/admin/home-assistant/entities`
- `POST /api/admin/home-assistant/action`
- `POST /api/device/home-assistant/action`
- `GET /api/admin/home-assistant/notes`
- `POST /api/admin/home-assistant/notes`

Konfigurationsschalter:

- `HOME_ASSISTANT_BASE_URL`
- `HOME_ASSISTANT_TOKEN`
- `HOME_ASSISTANT_TIMEOUT_SECONDS`
- `HOME_ASSISTANT_ALLOWED_SERVICES`
- `HOME_ASSISTANT_ALLOWED_ENTITY_PREFIXES`

Wichtige Grenze:

- Der Gateway laesst absichtlich nur freigegebene Services und erlaubte Entity-Praefixe zu.
- Das ist bewusst keine freie "KI darf alles in Home Assistant ausfuehren"-Schicht.
- Die aktuelle V1 ist bewusst regelbasiert: lesen, Notizen merken und einfache freigegebene Aktionen ja, freie Hausautomations-Magie nein.

Im Admin-Chat gibt es jetzt eine erste Home-Assistant-Chat-V1:

- Der Chat kann Home-Assistant-Entities als Kontext einbeziehen.
- Das geht automatisch bei klaren HA-/Entity-Anfragen oder explizit ueber `Home Assistant lesen`.
- Dauerhafte Bedeutungen fuer Entities kannst du im Chat so speichern:
  - `Merke HA light.schreibtisch: Das ist die LED-Leiste am Gaming-Tisch.`
- Diese Notizen landen in PostgreSQL und werden bei spaeteren HA-Chats wieder als Kontext verwendet.
- Nach erfolgreichen HA-Aktionen lernt der Gateway jetzt auch einfache Alias-Begriffe automatisch nach, z. B. kann aus `das Licht im Gamingzimmer` spaeter direkt `gamingzimmer licht` werden.
- Einfache Schaltbefehle versteht der Chat jetzt direkt, z. B.:
  - `Schalte gaming licht aus`
  - `Kannst du bitte alle gaming licht aus machen`
  - `gaming licht an machen alle bitte`
  - `Schalte das Wohnzimmer Licht an`
  - `Aktiviere light.schreibtisch`
  - `Stelle Schlafzimmer auf 21 Grad`
  - `Starte script.gute_nacht`
- Mehrere Zielnamen mit `und` oder Komma gehen jetzt ebenfalls, z. B.:
  - `mach bjorn und lena licht an`
  - `schalte björn und lena licht aus`
- Wenn `alle` im Befehl steckt, schaltet der Gateway jetzt mehrere passende Entities desselben Typs direkt gemeinsam.
- Einfache Follow-up-Befehle auf die letzte HA-Aktion gehen jetzt ebenfalls, z. B.:
  - `ok und jetzt wieder aus`
  - `und jetzt wieder an`
- Fenster-/Schalter-Formulierungen koennen jetzt natuerlicher sein, z. B.:
  - `mach wohnzimmer fenster auf`
  - `schliesse das fenster im wohnzimmer`
- Der Chat kann HA-Entities jetzt auch direkt ueber die HA-Engine auflisten, z. B.:
  - `suche mal nach fenstern`
  - `welche fenster gibt es`
- Du kannst dem Gateway Ausdruecke dauerhaft beibringen, z. B.:
  - `Merke HA Alias wohnzimmer fenster: switch.fenster_wohnzimmer`
  - `Wenn ich wohnzimmer fenster sage, meine ich switch.fenster_wohnzimmer`
- Bei Mehrdeutigkeit schaltet der Gateway absichtlich nicht blind, sondern nennt die Kandidaten.
- Das aktuelle "Dazulernen" passiert bewusst ueber PostgreSQL-Notizen und HA-Aliase, nicht ueber automatische LoRA-/Modell-Neutrainings.
- Vor einer HA-Aktion laeuft jetzt zusaetzlich eine kleine Intent-Stufe ueber das Modell:
  - `chat`
  - `ha_query`
  - `ha_action`
  Erst danach fuehrt der Gateway die HA-Logik validiert aus. Das macht Folge-Saetze wie `mach es bitte doch wieder auf` robuster, ohne dem Modell freie API-Kontrolle zu geben.

Beispiel:

```bash
TOKEN=$(sed -n 's/^API_BEARER_TOKEN=//p' /opt/llm-gateway/.env)

curl -s http://127.0.0.1:8000/api/admin/home-assistant/status \
  -H "Authorization: Bearer $TOKEN"
```

```bash
curl -s http://127.0.0.1:8000/api/admin/home-assistant/action \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "domain":"light",
    "service":"turn_on",
    "entity_id":"light.wohnzimmer",
    "service_data":{"brightness_pct":50}
  }'
```

Fuer den spaeteren Pi-Client:

```bash
curl -s http://127.0.0.1:8000/api/device/home-assistant/action \
  -H "Authorization: Bearer DEVICE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "domain":"switch",
    "service":"turn_off",
    "entity_id":"switch.kaffeemaschine"
  }'
```

## Database- und Storage-Tab

Im Admin-Hub gibt es jetzt zusaetzlich:

- `Dashboard`
  Zeigt den laufenden Betriebszustand der Plattform:
  - Gateway-/Backend-Status
  - Requests und mittlere Request-Dauer
  - GPU-Temperatur, GPU-Power und VRAM
  - kleine Live-Telemetrie im sticky Header fuer CPU-Last, CPU-Temp, GPU-Auslastung, GPU-Temp, GPU-Power und VRAM
  - aktives Modell, Limits, Datenbank- und Storage-Zustand
- `Settings`
  Hier liegen die schreibbaren Plattform-Settings:
  - ganz oben KI-Profile fuer mehrere MI50-Services wie `kai-devstral` und `kai-qwen`
  - jedes KI-Profil kann eigene Basiswerte fuer `BACKEND_CONTEXT_WINDOW`, `CONTEXT_RESPONSE_RESERVE` und `DEFAULT_MAX_TOKENS` mitbringen
  - jedes KI-Profil kann optional auch einen eigenen `NGL`-Wert tragen; der Gateway reicht ihn beim Aktivieren als `KAI_NGL` an den Remote-Befehl weiter oder ersetzt `{ngl}` in einem hinterlegten Aktivierungs-Kommando
  - darunter nur die alltagsrelevanten Basisfelder wie Backend-URL, sichtbarer Modellname, Kontextfenster, Token-Limits und MI50-SSH
  - Fast/Deep-Kompatibilitaet, Routing-Regeln und rohe MI50-Kommandos nur noch als erweiterte Settings
  - Continue-YAML weiterhin direkt im selben Tab
- `Database`
  Hier setzt du `DATABASE_URL`, testest die Verbindung und kannst das PostgreSQL-Schema manuell initialisieren. Der Gateway zeigt auch an, ob gerade `memory` oder `postgres` aktiv ist.
  Der Tab speichert und testet bewusst serverseitig, damit das auch in hakeligen Browser-WebViews oder bei kaputtem JS noch funktioniert.
  Neue Werte aus `.env` werden vom laufenden Gateway sofort wieder bevorzugt, ohne dass dafuer erst ein kompletter Service-Neustart noetig ist.
  Gespeicherte Datenbank-Profile werden nur noch redaktiert angezeigt, also ohne Passwort im sichtbaren UI. Mehrere Profile koennen lokal hinterlegt und bei Bedarf wieder aktiviert werden.
- `Memory`
  Hier siehst du, ob der Gateway gerade wirklich persistenten Memory nutzt, wie viele Sessions/Nachrichten/Summaries schon gespeichert sind und welche Rolling Summaries aktuell aus den aelteren Chats entstanden sind.
- `Storage`
  Hier legst du jetzt echte Storage-Ziele an:
  - `local` fuer einen lokalen Pfad auf dem Gateway-Host oder im LXC
  - `smb_mount` fuer einen bereits auf dem Host gemounteten SMB-Pfad
  Dateien wie `.txt`, `.md`, `.pdf`, `.csv`, `.json`, `.log`, `.yaml`, `.yml` koennen dort abgelegt werden. Die Datei selbst bleibt im Storage, waehrend Metadaten und extrahierter Text in PostgreSQL landen.
  Damit ist die Grundlage gelegt, diese Dokumente als KI-Kontext oder Wissensbasis zu verwenden. Im Admin-Chat koennen gespeicherte Dokumente direkt als Kontext ausgewaehlt werden.
  Wichtige ehrliche Einschraenkung: Der Gateway macht bewusst keinen nativen SMB-Login. SMB wird als gemountetes Dateisystem behandelt.
- `Skills / MCP`
  Zeigt alle aktiven MCP-Tools (builtin + custom) und erlaubt das Anlegen eigener Custom-MCP-Tools.
  Ein Custom-Tool mappt genau auf einen freigegebenen Ops-Befehl (`target + command`), damit Kai kontrolliert Tools nutzen kann, ohne ein freies Root-Terminal zu bekommen.

Fuer dein geplantes Homelab-Setup ist das die pragmatische Empfehlung:

- eigener PostgreSQL-LXC mit viel Speicher
- 256 GB SSD fuer die eigentlichen DB-Dateien
- 1 TB HDD fuer Backups, Dumps, spaetere Dateispeicher oder Nextcloud
- SMB-/NAS-Shares zuerst sauber auf dem Host mounten und dann als `smb_mount`-Profil im Hub hinterlegen

## Admin-Login und Hub

Die Browser-Adminseiten laufen jetzt ueber einen einfachen Login mit Session-Cookie.

Wichtige Endpunkte:

- `GET /admin/login`
- `POST /admin/login`
- `POST /admin/logout`
- `GET /internal/admin`

Pragmatische Standardregel:

- Wenn `ADMIN_PASSWORD` leer bleibt, wird vorerst dein bestehender `API_BEARER_TOKEN` als Login-Passwort verwendet.
- Wenn `ADMIN_SESSION_SECRET` leer bleibt, wird derselbe Wert als Signatur-Secret fuer das Session-Cookie genutzt.

Wichtige Schalter:

- `ADMIN_USERNAME`
- `ADMIN_PASSWORD`
- `ADMIN_SESSION_SECRET`
- `ADMIN_SESSION_TTL_HOURS`
- `ADMIN_COOKIE_SECURE`

Empfehlung fuer spaeteren Reverse-Proxy-/Internet-Betrieb:

- `ADMIN_PASSWORD` explizit setzen
- `ADMIN_SESSION_SECRET` explizit setzen
- `ADMIN_COOKIE_SECURE=true`
- HTTPS am Reverse Proxy erzwingen

## Ops-Panel

Im Admin-Hub gibt es jetzt eine erste sichere Ops-Konsole statt eines freien Web-Terminals.

Verfuegbar:

- `gateway status`
- `gateway logs`
- `gateway restart`
- `gateway health`
- `gateway uptime`
- `gateway tools`
- `gateway skills`
- `gateway apt_update`
- `gateway install_git`
- `gateway install_curl`
- `gateway install_gh`
- `gateway install_ripgrep`
- `gateway install_htop`
- `gateway install_tmux`
- `kai status`
- `kai logs`
- `kai restart`
- `kai health`
- `kai models`
- `kai telemetry`

Das ist bewusst kontrollierter als ein generisches Browser-Terminal. Fuer spaeter kann daraus ein groesseres Panel werden, aber die V1 bleibt absichtlich enger.

Neu dazu:

- der Gateway kann jetzt auch lokale, kuratierte Tool-Tasks ausfuehren
- Root-Rechte laufen dabei bewusst nicht als freie Shell, sondern ueber `GATEWAY_LOCAL_ROOT_PREFIX`
- Standard ist `sudo -n`, also nur nichtinteraktive `sudo`-Kommandos
- dieselben freigegebenen Gateway-Tasks koennen auch direkt aus dem Admin-Chat angestossen werden, z. B.:
  - `installiere git`
  - `installiere ripgrep`
  - `zeige gateway tools`
  - `zeige skills`
  - `aktualisiere die paketlisten`

Wichtiger Sicherheitspunkt:

- Auch im rein lokalen Betrieb ist das absichtlich keine offene Root-Konsole fuer das Modell.
- Der Chat darf nur die explizit erlaubten Gateway-Tasks ausloesen.
- Wenn `sudo -n` fuer den Service-User nicht freigeschaltet ist, schlagen Root-Tasks mit einer klaren Fehlermeldung fehl.

## KI-Profile fuer mehrere MI50-Services

Wenn du auf der MI50 mehrere `llama.cpp`-Services vorbereitest, aber immer nur einen davon aktiv fahren willst, ist der neue Weg:

- auf der MI50 z. B. zwei Services anlegen:
  - `kai-devstral.service`
  - `kai-qwen.service`
- im Admin-Hub unter `Settings` zwei KI-Profile speichern
- beim Aktivieren eines Profils stellt der Gateway um:
  - `PUBLIC_MODEL_NAME`
  - `BACKEND_MODEL_NAME`
  - `FAST_MODEL_*`
  - `LLAMACPP_BASE_URL`
  - optional die MI50-Status-/Restart-/Log-Kommandos

Pragmatische Empfehlung:

- wenn dein SSH-User auf der MI50 `sudo systemctl ...` ohne Passwort darf, reicht der reine Service-Name
- wenn das nicht geht, hinterlegst du im Profil stattdessen ein eigenes Aktivierungs-Kommando
  - Beispiel: `~/switch-kai.sh qwen`
  - oder `sudo -n systemctl restart kai-qwen`

Wichtig:

- Das ist bewusst fuer deinen Ein-GPU-Alltag gedacht.
- Mehrere Profile ja, aber real laeuft auf der MI50 typischerweise immer nur ein Modellprozess aktiv.

Auf dem Dashboard koennen zusaetzlich einfache MI50-Telemetriedaten aus `rocm-smi` erscheinen:

- VRAM belegt / gesamt
- Temperatur
- Watt

Wenn `rocm-smi` keine sauberen Used/Total-Werte liefert, faellt das Dashboard fuer VRAM auf den Prozentwert (`VRAM%`) zurueck.

Voraussetzung dafuer:

- SSH-Zugriff vom Gateway zur MI50-VM
- `rocm-smi` auf der MI50-VM verfuegbar
- ein passender Command in `MI50_ROCM_SMI_COMMAND`

Genutzte MI50-/SSH-Schalter:

- `MI50_SSH_HOST`
- `MI50_SSH_USER`
- `MI50_SSH_PORT`
- `MI50_RESTART_COMMAND`
- `MI50_STATUS_COMMAND`
- `MI50_LOGS_COMMAND`
- `MI50_ROCM_SMI_COMMAND`

## Raspberry Pi / Device API

Fuer spaetere Pi-Anbindung ist ein einfacher Device-Endpunkt vorbereitet:

- `POST /api/device/ask`

Gedacht fuer:

- Raspberry Pi mit lokalem TTS/STT
- Gateway uebernimmt Routing und Kontext
- der Pi liest die Antwort lokal vor

Auth:

- `DEVICE_SHARED_TOKEN`
- wenn leer, faellt der Device-Endpunkt auf `API_BEARER_TOKEN` zurueck
- damit kann ein Raspberry Pi spaeter mit eigenem Token arbeiten, ohne den Browser-/Admin-Login zu benutzen

Beispiel:

```bash
curl -s http://127.0.0.1:8000/api/device/ask \
  -H "Authorization: Bearer DEVICE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "message":"Wie ist der Status von Kai?",
    "mode":"auto"
  }'
```

Neu im Admin-Hub unter `Pi / Devices`:

- gespeicherte Pi-/Device-Profile
- eigener Device-Token pro Profil
- SSH-Ziel fuer den Pi
- serverseitig generiertes Bootstrap-Skript
- SSH-Bootstrap-V1 direkt aus dem Admin-Hub
- expliziter Button `PI installieren` pro Profil (fuer rohe Pi-Installationen)
- direkter Connect-Flow `Verbinden / .env sync` fuer bestehende Kai-Pis
- neue zentrale `Kai Face`-Steuerung (Style + State + Layer) direkt aus dem Gateway
- in der Face-Steuerung jetzt auch `Render Mode` (`vector` oder `sprite_pack`) plus Pack-Name
- `F1..F17`-Varianten direkt im Gateway waehlbar (nahe am Referenz-Set)
- fuer fertige Kai-Pis reicht auch ein einfaches Profil mit Host, User, Port und optional Passwort
- wenn `DEVICE_TOKEN` leer bleibt, wird beim Speichern automatisch ein neuer Token erzeugt

Wichtige praktische Entscheidung:

- V1 bevorzugt SSH-Key-Auth, kann fuer einfache Pi-Setups aber auch SSH-Passwoerter nutzen
- beim Aktivieren eines Device-Profils wird dessen Token als `DEVICE_SHARED_TOKEN` in den Gateway uebernommen
- `PI installieren` startet denselben SSH-Install-Flow direkt pro Profil und ist fuer frische Pi-Basisimages gedacht
- der Connect-Flow schreibt `GATEWAY_BASE_URL` und `DEVICE_TOKEN` direkt in die Pi-`.env` und startet `kai.service` neu
- Face-Styles koennen jetzt via Gateway ueber SSH direkt auf dem Pi angewendet werden (kein Pi-Menue notwendig)
- eigene Sprite-Packs koennen auf dem Pi unter `/home/pi/kai/styles/packs/<name>` abgelegt und danach im Gateway aktiviert werden
- bei der Pi-Probe erkennt der Gateway jetzt sowohl `kai.service` als User-Service als auch als System-Service
- der Bootstrap legt auf dem Pi eine kleine Python-Basis an:
  - `.env`
  - `requirements.txt`
  - `pi_gateway_client.py`
- damit ist der Pi noch nicht der volle Sprach-/Avatar-Stack, aber sauber fuer die naechste Ausbaustufe vorbereitet

Fuer spaetere Kamera-/Snapshot-Events gibt es jetzt zusaetzlich:

- `POST /api/device/vision/event`

Gedacht fuer:

- Pi-Webcam oder externe Kamera-Node
- Snapshot bei Trigger wie `person_detected`
- Bild landet im Storage
- wenn `VISION_MODEL_NAME` gesetzt ist, wird direkt eine Bildanalyse erzeugt
- optional kann zusammen mit dem Bild auch gleich eine Sprach-/Chat-Anfrage an Kai geschickt werden

Beispiel:

```bash
curl -s http://127.0.0.1:8000/api/device/vision/event \
  -H "Authorization: Bearer DEVICE_TOKEN" \
  -F "CAMERA_NAME=pi-cam" \
  -F "TRIGGER_TYPE=person_detected" \
  -F "MESSAGE=Wer oder was ist auf diesem Bild und wie soll ich darauf reagieren?" \
  -F "IMAGE_FILE=@/pfad/zum/snapshot.jpg"
```

## MCP (Tool-Broker)

Der Gateway bietet eine kleine MCP-V1, damit Clients wie spaeter VS Code kontrollierte Tools nutzen koennen.
Die Tools sind bewusst whitelisted und laufen ueber den Gateway als Broker.

Endpoints:

- `GET /api/mcp/tools` (Liste der verfuegbaren Tools)
- `POST /api/mcp/call` (Tool ausfuehren)

Admin-Management (Skills-Tab):

- `GET /api/admin/mcp/tools`
- `GET /api/admin/mcp/custom-tools`
- `POST /api/admin/mcp/custom-tools`
- `POST /api/admin/mcp/custom-tools/{tool_name}/delete`
- `GET /api/admin/ops/catalog` (erlaubte Ops-Targets/Befehle fuer Custom-Tools)

Auth:

- Standard ist `Authorization: Bearer API_BEARER_TOKEN`
- Optional fuer Devices: `Authorization: Bearer DEVICE_SHARED_TOKEN` oder `X-Device-Token`
- Sensitive Gateway-Tools (`gateway.ops`, `gateway.custom_tool.*` und daraus abgeleitete Custom-Ops-Tools) sind bewusst nur fuer Admin/API-Token erlaubt, nicht fuer Device-Token.

Beispiel:

```bash
curl -s http://127.0.0.1:8000/api/mcp/tools \
  -H "Authorization: Bearer API_BEARER_TOKEN"
```

```bash
curl -s http://127.0.0.1:8000/api/mcp/call \
  -H "Authorization: Bearer API_BEARER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "tool":"ha.entities",
    "arguments":{"domain":"light","limit":20}
  }'
```

Hinweis:

- Die MCP-V1 ist absichtlich klein gehalten.
- Schreiben/Schalten laeuft nur ueber freigegebene HA-Services.
- Custom-Tools sind kein freies Shell-Feature: sie duerfen nur auf den vorhandenen Ops-Allowlist-Befehlen aufsetzen.
- Fuer automatisches Tool-Management stehen zusaetzlich die MCP-Tools `gateway.custom_tool.list`, `gateway.custom_tool.save` und `gateway.custom_tool.delete` bereit.

## Vision / Bildanalyse

Fuer Bild-Uploads im Chat, Snapshot-Events vom Pi und spaetere Kamera-Nodes kann der Gateway optional ein eigenes Vision-Modell benutzen.

Relevante Settings:

- `VISION_BASE_URL`
- `VISION_MODEL_NAME`
- `VISION_PROMPT`
- `VISION_MAX_TOKENS`

Wichtige praktische Entscheidung:

- Devstral bleibt dein Text-/Steuerungsmodell.
- Bildverstehen sollte ueber ein separates Vision-Modell laufen.
- Wenn kein Vision-Modell gesetzt ist, werden Bilddateien trotzdem gespeichert, aber nur ohne echte Bildbeschreibung.

## llama.cpp Startbefehl

Fuer dein aktuelles Ein-Modell-Setup ist der pragmatische Weg:

```bash
./llama-server \
  -m /pfad/zu/qwen2.5-coder.gguf \
  --host 0.0.0.0 \
  --port 8080 \
  --api-key DEIN_BACKEND_TOKEN \
  -c 16384 \
  -ngl 999 \
  -t 8
```

Wichtige Punkte:

- `--api-key` schuetzt den `llama-server`
- der Gateway sendet diesen Token ueber `LLAMACPP_API_KEY`
- `-c 16384` setzt das echte Kontextfenster im Backend
- wenn du aktuell eher konservativ starten willst, kannst du auch zuerst mit `8192` testen

Passend dazu im Gateway:

```env
LLAMACPP_BASE_URL=http://DEINE_MI50_VM:8080
LLAMACPP_API_KEY=DEIN_BACKEND_TOKEN
PUBLIC_MODEL_NAME=devstral-q3
BACKEND_MODEL_NAME=Devstral-Small-2-24B-Instruct-2512-Q3_K_M.gguf
FAST_MODEL_PUBLIC_NAME=devstral-q3
FAST_MODEL_BACKEND_NAME=Devstral-Small-2-24B-Instruct-2512-Q3_K_M.gguf
FAST_MODEL_BASE_URL=http://DEINE_MI50_VM:8080
DEEP_MODEL_PUBLIC_NAME=
DEEP_MODEL_BACKEND_NAME=
DEEP_MODEL_BASE_URL=
BACKEND_CONTEXT_WINDOW=16384
CONTEXT_RESPONSE_RESERVE=2048
DEFAULT_MAX_TOKENS=1024
ADMIN_DEFAULT_MODE=fast
```

Wenn du spaeter ein Deep-Modell als zweiten Prozess oder auf einer zweiten GPU startest, bekommt es einfach einen zweiten Port oder Host und wird ueber `DEEP_MODEL_BASE_URL` eingebunden.

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
    "model": "devstral-q3",
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
    "model": "devstral-q3",
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
  -d '{"model":"devstral-q3"}'
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
  - name: devstral-q3-local
    provider: openai
    model: devstral-q3
    apiBase: http://127.0.0.1:8000/v1
    apiKey: CHANGE_ME
```

Wichtige Annahme:

- Dieses Beispiel geht davon aus, dass VS Code auf derselben Maschine laeuft wie der Gateway.
- Wenn VS Code per SSH, Dev Container oder WSL laeuft, ist `127.0.0.1` unter Umstaenden nicht der richtige Host.
- Dann musst du statt `127.0.0.1` die aus Sicht des VS-Code-Clients erreichbare Adresse verwenden.

## Browser-Zugriff

Die Admin-Oberflaeche liegt jetzt hier:

- `GET /admin/login`
- `GET /internal/admin`

Der Hub enthaelt:

- Dashboard / Konfiguration
- Chat-Tab mit eingebetteter Admin-Chat-Oberflaeche
- Ops-Tab fuer Gateway und Kai
- Device-/Pi-Vorbereitung

Pragmatischer Default:

- Login-Benutzername: `ADMIN_USERNAME` oder standardmaessig `admin`
- Login-Passwort: `ADMIN_PASSWORD`
- wenn `ADMIN_PASSWORD` leer ist, gilt vorerst dein `API_BEARER_TOKEN` als Browser-Passwort

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
- `MI50_LOGS_COMMAND`
- `MI50_ROCM_SMI_COMMAND`

Beispiel:

```env
MI50_SSH_HOST=192.168.40.111
MI50_SSH_USER=mi50
MI50_SSH_PORT=22
MI50_RESTART_COMMAND=sudo systemctl restart kai
MI50_STATUS_COMMAND=sudo systemctl status kai --no-pager
MI50_LOGS_COMMAND=journalctl -u kai -n 80 --no-pager
MI50_ROCM_SMI_COMMAND=rocm-smi --showtemp --showpower --showmemuse --json
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
