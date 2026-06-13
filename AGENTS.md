# AGENTS.md

## Projektbeschreibung

Dieses Repository ist ein lokaler OpenAI-kompatibler KI-Orchestrator fuer Chat, Coding, Agenten, Tools und Voice.
Der Gateway nimmt Anfragen von OpenWebUI, VS Code/Codex, der eigenen Weboberflaeche, einem Raspberry-Pi-Voice-Interface, Home Assistant/n8n und weiteren externen OpenAI-kompatiblen Clients entgegen.

Die Inferenz laeuft nicht im Gateway selbst, sondern auf externen `llama.cpp`-Backends. Der Gateway uebernimmt Routing, Validierung, frei definierbare Modellprofile, Worker/Reviewer-Pipelines, Monitoring, Memory-Anbindung und die Admin UI.

Es werden zwei Betriebsarten unterschieden:

1. Arbeitsmodus/Systemmodus: technische oder systemnahe Aufgaben laufen ueber eine Worker + Reviewer Pipeline. Der Worker erstellt einen kurzen Loesungsvorschlag, der Reviewer prueft Korrektheit, Risiken und fehlende Punkte.
2. Chatmodus/Kai-Modus: persoenlicher Chat ueber Web, OpenWebUI oder Voice mit Memory und einem weiterentwickelbaren Charakter fuer Kai.

Ein Toolmodus ist vorgesehen, aber nur mit Safety Layer, klarer Tool-Registry und expliziter Bestaetigung fuer riskante Aktionen. Gefaehrliche Aktionen duerfen nicht direkt und nicht ohne Pruefung/Freigabe ausgefuehrt werden.

## Ziel

Ziel ist ein robuster, lokal betreibbarer KI-Gateway fuer mehrere Clients und Modellprofile, der bestehende OpenAI-kompatible Nutzung erhaelt und schrittweise um Agenten, Memory, Voice und kontrollierte Tools erweitert wird.
Die Architektur soll bewusst evolutionaer bleiben: kein neues Framework, keine unnoetigen Abstraktionen und keine direkte Ausfuehrung riskanter Aktionen ohne Policy-Check und Freigabe.

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
