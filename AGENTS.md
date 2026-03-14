# AGENTS.md

## Projektbeschreibung

Dieses Repository dient dem Aufbau eines lokalen OpenAI-kompatiblen Orchestrators fuer VS Code Clients.
Der Dienst soll Requests entgegennehmen, validieren und an ein externes `llama.cpp`-Backend mit `Qwen2.5-Coder` weiterleiten.

## Ziel

Ziel ist ein moeglichst einfacher, robuster OpenAI-kompatibler Proxy-Orchestrator fuer `llama.cpp` und `Qwen2.5-Coder`.
Die Minimalversion priorisiert lokale Nutzbarkeit, nachvollziehbare Architektur und geringe Komplexitaet vor Erweiterungen.

## Tech-Stack

- Python
- FastAPI
- Uvicorn

## Regeln fuer Aenderungen

- Erst analysieren, dann aendern.
- Aenderungen in kleinen, ueberpruefbaren Schritten umsetzen.
- Keine unnoetige Komplexitaet einfuehren.
- Keine geheimen Daten, Tokens oder Zugangsdaten im Code, in Tests oder in Beispielkonfigurationen hinterlegen.
- `README.md` und `example.env` bzw. `.env.example` bei relevanten Aenderungen aktuell halten.
- Neue Dateien nur anlegen, wenn sie einen klaren Zweck haben; dieser Zweck ist in der Aenderung kurz zu begruenden.

## Entwicklungsregeln

- Klare, konsistente Dateinamen verwenden.
- Funktionen und Module einfach und verstaendlich halten.
- Logging und Fehlerbehandlung von Anfang an mitdenken und nicht spaeter als Nacharbeit behandeln.
- Keine stillen Annahmen treffen; wichtige Annahmen im Code, in der Konfiguration oder in der Dokumentation sichtbar machen.

## Testregeln

- Der Health-Endpunkt muss pruefbar sein.
- Der Models-Endpunkt muss pruefbar sein.
- `chat/completions` muss mit `curl` lokal testbar sein.

## Done-Definition

- Das Projekt startet lokal.
- Die vorgesehenen Endpunkte funktionieren.
- `README.md` enthaelt Setup- und Testanweisungen.
