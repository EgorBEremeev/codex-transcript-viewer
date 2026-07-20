"""Build the self-contained session-breakdown trace viewer."""

from __future__ import annotations

import json
from importlib import resources
from typing import Any


def _asset(name: str) -> str:
    return resources.files(__package__).joinpath(name).read_text(encoding="utf-8")


def _embedded_json(value: Any) -> str:
    """Serialize safely inside a non-executable script element."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def build_trace_html(breakdown: dict[str, Any], spans: dict[str, Any]) -> str:
    """Return a portable HTML document for immutable breakdown plus derived spans."""
    root_id = str(breakdown.get("root_session_id") or "unknown")
    return _TEMPLATE.format(
        title=root_id[:12],
        css=_asset("trace_style.css"),
        js=_asset("trace_viewer.js"),
        breakdown=_embedded_json(breakdown),
        spans=_embedded_json(spans),
    )


_TEMPLATE = """<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Codex breakdown trace — {title}</title>
  <style>{css}</style>
</head>
<body>
  <main id="trace-app">
    <header class="trace-header">
      <div><h1>Codex session breakdown</h1><p id="trace-summary"></p></div>
      <div class="summary-cards" id="summary-cards"></div>
    </header>

    <section class="controls" aria-label="Фильтры trace">
      <label>Role <select id="role-filter"><option value="">Все роли</option></select></label>
      <label>Session <select id="session-filter"><option value="">Все сессии</option></select></label>
      <label>Span kind <select id="span-filter"><option value="">Все</option><option value="turn">turn</option><option value="tool">tool</option></select></label>
      <label>Event kind <select id="event-filter"><option value="">Все markers</option></select></label>
      <label class="search-label">Поиск <input id="search-filter" type="search" placeholder="tool, команда, event"></label>
      <button id="reset-zoom" type="button">Весь интервал</button>
      <label>Показывать до <input id="until-filter" type="text" inputmode="numeric" placeholder="YYYY-MM-DD:HH:MM:SS"></label>
      <button id="apply-until" type="button">Применить</button>
      <button id="clear-until" type="button">Весь trace</button>
    </section>

    <section class="panel trace-panel" aria-label="Trace">
      <div class="panel-title"><h2>Trace</h2><span id="time-range"></span></div>
      <div class="trace-layout">
        <div id="trace-labels" class="trace-labels" aria-label="Сессии"></div>
        <div class="trace-canvas-wrap"><canvas id="trace-canvas" aria-label="Временная шкала spans"></canvas></div>
      </div>
    </section>

    <section class="panel metrics-panel" aria-label="Кумулятивные метрики выбранной сессии">
      <div class="panel-title"><h2 id="metrics-title">Кумулятивные метрики</h2><span>Выберите сессию в Trace</span></div>
      <div class="metric-grid">
        <figure><figcaption>Context material · cumulative payload bytes</figcaption><canvas id="payload-chart"></canvas></figure>
        <figure><figcaption>Cumulative token usage</figcaption><canvas id="total-chart"></canvas></figure>
        <figure><figcaption>Last model request / context window</figcaption><canvas id="last-chart"></canvas></figure>
      </div>
      <div id="series-toggles" class="series-toggles"></div>
    </section>

    <section class="panel event-panel" aria-label="Числовая таблица событий">
      <div class="panel-title"><h2 id="table-title">Events</h2><span id="table-count"></span></div>
      <div class="event-table" id="event-table"></div>
    </section>
  </main>
  <script id="breakdown-data" type="application/json">{breakdown}</script>
  <script id="spans-data" type="application/json">{spans}</script>
  <script>{js}</script>
</body>
</html>
"""
