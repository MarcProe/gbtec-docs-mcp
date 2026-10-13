# GBTEC Docs MCP

MCP-Server (stdio) für die GBTEC-Platform-Dokumentation unter
<https://docs.gbtec.com> (Sphinx auf Read the Docs).

Gibt AI-Agenten (Claude Desktop, Cursor, mcphub, ...) strukturierten
Zugriff auf die Doku: **Volltextsuche** (SQLite FTS5), **Seitenabruf als
Markdown** und **Kapitelübersicht** – für die Produkte BIC Process Design
(`Design/`), EAM, GRC Management (`GRCM/`) und Work Orchestrator (`WO/`),
in den Sprachen **de** und **en**.

## Tools

| Tool | Beschreibung |
|---|---|
| `search_docs(query, lang, limit)` | Volltextsuche, liefert Titel, Snippet, URL, `path` |
| `get_page(lang, path)` | Komplette Seite als Markdown |
| `list_sections(lang)` | Alle Seiten gruppiert nach Produkt |
| `index_status` | Index-Alter, Seitenzahlen, Größe |

## Wie der Index funktioniert

* **Quelle:** Der Sphinx-Suchindex (`searchindex.js` pro Sprache) liefert
  die autoritative Seitenliste (aktuell ~61 Seiten/Sprache); die HTML-Seiten
  werden zu Markdown konvertiert und in SQLite (FTS5) indexiert.
* **Frische:** Bei jedem Tool-Aufruf wird das Index-Alter geprüft. Ist der
  Index **älter als 24 Stunden** (konfigurierbar via
  `GBTEC_DOCS_MCP_MAX_AGE` in Sekunden), startet der Server einen
  **Background-Rebuild**, der **atomar** (Write-to-temp + `os.replace`)
  einspielt. Anfragen laufen immer weiter gegen den aktuellen Index –
  der Server wartet nie auf den Refresh. Der Check selbst ist auf max.
  1×/Minute gedrosselt.
* **Cache:** `~/.cache/gbtec-docs-mcp/index.db`
  (override via `GBTEC_DOCS_MCP_CACHE`).

## Nutzung

### Direkt (uv)

```bash
uvx --from "gbtec-docs-mcp @ git+https://github.com/MarcProe/gbtec-docs-mcp" gbtec-docs-mcp
# oder lokal:
uv sync && uv run gbtec-docs-mcp
```

### mcphub

```bash
uvx --from "gbtec-docs-mcp @ git+https://github.com/MarcProe/gbtec-docs-mcp" gbtec-docs-mcp
```

### Claude Desktop / generische MCP-Client-Konfig

```json
{
  "mcpServers": {
    "gbtec-docs": {
      "command": "uvx",
      "args": [
        "--from",
        "gbtec-docs-mcp @ git+https://github.com/MarcProe/gbtec-docs-mcp",
        "gbtec-docs-mcp"
      ]
    }
  }
}
```

## Entwicklung

```bash
uv sync
uv run gbtec-docs-mcp            # stdio-Server
```

Die Ingestion lässt sich auch standalone testen:

```python
from gbtec_docs_mcp import build_index
print(build_index())
```

## Lizenz

MIT
