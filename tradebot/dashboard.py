"""A local web dashboard for watching live sessions.

Reading a running bot out of log files works, and nobody does it for long. This serves
the same information as a page that refreshes itself, so leaving it open in a tab is
enough to see what the thing is doing.

It is deliberately small. No framework, no build step, no packages - the standard
library serves it and the page is one file - because a dashboard that needs its own
install is a dashboard nobody runs. It reads the state and trade files the live runner
already writes and never touches them, so it cannot corrupt a session, and it can be
started and stopped without the bot noticing.

Bound to localhost, always
--------------------------
The server binds to 127.0.0.1 and refuses to be talked out of it. This page shows
positions, balances and a trading history; on 0.0.0.0 it would be readable by anything
that can reach the machine, which on a home network is every device on it and on a VPS
is the entire internet. There is no authentication here and there should not be - the
right answer for remote viewing is an SSH tunnel, which costs nothing and leaves this
listening only to itself.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

#: Never configurable. See the module docstring.
HOST = "127.0.0.1"
DEFAULT_PORT = 8765


@dataclass
class Session:
    """One live session, as the dashboard needs it."""

    name: str
    state_file: Path
    trades_file: Path
    starting_cash: float
    currency: str


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _read_trades(path: Path) -> list[dict]:
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))
    except (OSError, csv.Error):
        return []


def snapshot(sessions: list[Session]) -> dict:
    """Everything the page shows, read fresh off disk.

    A session that has not started yet, or whose file is being rewritten as we read it,
    comes back marked ``waiting`` rather than raising. The dashboard is a viewer: it is
    never the reason a run stops.
    """
    out = []
    for session in sessions:
        state = _read_json(session.state_file)
        if not state:
            out.append({"name": session.name, "waiting": True,
                        "currency": session.currency,
                        "starting_cash": session.starting_cash})
            continue

        engine = state.get("engine", {})
        book = engine.get("portfolio", {})
        risk = engine.get("risk", {})
        trades = _read_trades(session.trades_file)

        qty = float(book.get("qty", 0.0))
        avg = float(book.get("avg_price", 0.0))
        cash = float(book.get("cash", 0.0))
        fees = float(book.get("fees_paid", 0.0)) + float(book.get("slippage_paid", 0.0))

        # Value an open position at the last price the session itself saw. Nothing else
        # on disk is trustworthy for this: the last closed trade's exit can be hours old
        # or, if a trade log has been copied between runs, belong to a different session
        # entirely - which is how this was found, showing a £20 profit that never
        # happened. A session written before this field existed has no honest mark, so
        # it is reported as unmarked rather than valued with a guess.
        raw_mark = float(state.get("last_price", 0.0) or 0.0)
        marked = raw_mark > 0
        mark = raw_mark if marked else avg
        equity = cash + qty * mark

        wins = sum(1 for t in trades if float(t.get("net", 0) or 0) > 0)
        out.append({
            "name": session.name,
            "waiting": False,
            "symbol": state.get("symbol", "?"),
            "interval": state.get("interval", ""),
            "strategy": state.get("strategy", "?"),
            "updated_ms": state.get("saved_at", 0),
            "started_ms": state.get("started_at", 0),
            "bars": int(state.get("live_bars", engine.get("bars_seen", 0))),
            "qty": qty,
            "avg_price": avg,
            "cash": cash,
            "equity": equity,
            "mark": mark,
            "marked": marked or abs(qty) < 1e-12,
            "fees": fees,
            "starting_cash": session.starting_cash,
            "currency": session.currency,
            "halted": risk.get("halted_reason"),
            "stop": engine.get("active_stop"),
            "target": engine.get("active_target"),
            "trades": trades[-40:],
            "trade_count": len(trades),
            "wins": wins,
        })
    return {"sessions": out}


class _Handler(BaseHTTPRequestHandler):
    sessions: list[Session] = []

    def do_GET(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        if self.path.startswith("/api/state"):
            body = json.dumps(snapshot(self.sessions)).encode("utf-8")
            self._send(body, "application/json")
        elif self.path in ("/", "/index.html"):
            self._send(PAGE.encode("utf-8"), "text/html; charset=utf-8")
        else:
            self.send_error(404)

    def _send(self, body: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        """Silence per-request logging; a polling page would drown the terminal."""


def serve(sessions: list[Session], port: int = DEFAULT_PORT) -> HTTPServer:
    handler = type("Handler", (_Handler,), {"sessions": sessions})
    return HTTPServer((HOST, port), handler)


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>tradebot</title>
<style>
:root{
  --ground:#F2F4F2;--surface:#fff;--sunken:#EAEDEA;--ink:#14181A;--ink2:#475156;
  --muted:#69747A;--hair:#DDE2DE;--pos:#2E7D5B;--neg:#A4404A;--warn:#8A6210;--accent:#1B5CC4;
}
@media (prefers-color-scheme:dark){:root{
  --ground:#131614;--surface:#1B1F1D;--sunken:#171A18;--ink:#E8EBE7;--ink2:#B3BDB6;
  --muted:#8D978F;--hair:#2B302D;--pos:#5FB894;--neg:#D9808A;--warn:#D2A64A;--accent:#5A8AE0;}}
*{box-sizing:border-box}
body{margin:0;background:var(--ground);color:var(--ink);
  font:15px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1000px;margin:0 auto;padding:28px 20px 64px;display:flex;flex-direction:column;gap:22px}
header{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;
  border-bottom:1px solid var(--hair);padding-bottom:14px}
h1{margin:0;font-size:20px;letter-spacing:-.01em}
.live{display:inline-flex;align-items:center;gap:7px;color:var(--muted);font-size:13px}
.dot{width:8px;height:8px;border-radius:50%;background:var(--pos)}
.stale .dot{background:var(--warn)}
.mono{font-variant-numeric:tabular-nums;
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.card{background:var(--surface);border:1px solid var(--hair);border-radius:4px;overflow:hidden}
.head{display:flex;justify-content:space-between;align-items:baseline;gap:12px;
  padding:14px 16px;border-bottom:1px solid var(--hair);flex-wrap:wrap}
.title{font-weight:600}
.sub{color:var(--muted);font-size:13px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(132px,1fr));gap:1px;background:var(--hair)}
.cell{background:var(--surface);padding:12px 16px}
.k{font-size:10.5px;letter-spacing:.09em;text-transform:uppercase;color:var(--muted)}
.v{font-size:19px;margin-top:3px}
.pos{color:var(--pos)}.neg{color:var(--neg)}
.flat{color:var(--muted)}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:7px 10px;text-align:right;white-space:nowrap;border-bottom:1px solid var(--hair)}
th:first-child,td:first-child,td.why,th.why{text-align:left}
thead th{font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);
  font-weight:500;background:var(--sunken)}
td.why{color:var(--muted)}
tbody tr:last-child td{border-bottom:none}
.scroll{max-height:280px;overflow:auto}
.empty{padding:16px;color:var(--muted);font-size:13.5px}
.banner{padding:10px 16px;background:var(--sunken);color:var(--warn);font-size:13px;
  border-bottom:1px solid var(--hair)}
footer{color:var(--muted);font-size:12.5px;border-top:1px solid var(--hair);padding-top:14px}
</style></head><body>
<div class="wrap">
  <header>
    <h1>tradebot</h1>
    <span class="live" id="live"><span class="dot"></span><span id="ago">connecting</span></span>
  </header>
  <div id="sessions"></div>
  <footer>
    Paper unless you deliberately armed live trading. Open positions are valued at the
    last price the session itself saw, so equity lags the market by up to one bar.
    Reading only &mdash; this page never writes to a session, and stopping it does not
    stop the bot.
  </footer>
</div>
<script>
const fmt = (n, d = 2) => (n === null || n === undefined || !isFinite(n))
  ? "\\u2014" : n.toLocaleString(undefined, {minimumFractionDigits: d, maximumFractionDigits: d});
const cls = n => n > 0 ? "pos" : n < 0 ? "neg" : "flat";
const sign = n => (n > 0 ? "+" : "") + fmt(n);

function sessionCard(s) {
  if (s.waiting) {
    return `<div class="card"><div class="head"><span class="title">${s.name}</span>
      <span class="sub">waiting for its first bar</span></div>
      <div class="empty">No state file yet. It writes one after the first completed bar.</div></div>`;
  }
  const pnl = s.equity - s.starting_cash;
  const pct = s.starting_cash ? (s.equity / s.starting_cash - 1) * 100 : 0;
  // An unmarked open position cannot be valued honestly, so it is not valued at all.
  const unmarked = !s.marked;
  const cur = s.currency || "";
  const held = Math.abs(s.qty) > 1e-12;
  const mins = s.updated_ms ? Math.round((Date.now() - s.updated_ms) / 60000) : null;

  const rows = (s.trades || []).slice().reverse().map(t => `<tr>
      <td>${t.n}</td><td>${t.closed}</td>
      <td>${fmt(parseFloat(t.entry))}</td><td>${fmt(parseFloat(t.exit))}</td>
      <td class="${cls(parseFloat(t.net))}">${sign(parseFloat(t.net))}</td>
      <td>${fmt(parseFloat(t.balance))}</td>
      <td class="why">${t.reason || ""}</td></tr>`).join("");

  return `<div class="card">
    <div class="head">
      <span class="title">${s.name}</span>
      <span class="sub">${s.strategy} on ${s.symbol} ${s.interval}
        &middot; ${s.bars.toLocaleString()} bars
        &middot; updated ${mins === null ? "?" : mins + "m"} ago</span>
    </div>
    ${s.halted ? `<div class="banner">Trading halted: ${s.halted}</div>` : ""}
    ${unmarked ? `<div class="banner">This session holds a position but has not recorded a
      price to value it at, so equity is not shown. It appears after the next bar.</div>` : ""}
    <div class="grid">
      <div class="cell"><div class="k">Equity</div>
        <div class="v mono">${unmarked ? "\u2014" : cur + fmt(s.equity)}</div></div>
      <div class="cell"><div class="k">Profit</div>
        <div class="v mono ${unmarked ? "flat" : cls(pnl)}">${unmarked ? "\u2014" : cur + sign(pnl)}</div></div>
      <div class="cell"><div class="k">Return</div>
        <div class="v mono ${unmarked ? "flat" : cls(pnl)}">${unmarked ? "\u2014" : sign(pct) + "%"}</div></div>
      <div class="cell"><div class="k">Position</div>
        <div class="v mono">${held ? fmt(s.qty, 8) : "flat"}</div></div>
      <div class="cell"><div class="k">Avg price</div>
        <div class="v mono">${held ? fmt(s.avg_price) : "\\u2014"}</div></div>
      <div class="cell"><div class="k">Fees paid</div>
        <div class="v mono">${cur}${fmt(s.fees)}</div></div>
      <div class="cell"><div class="k">Closed trades</div>
        <div class="v mono">${s.trade_count}${s.trade_count ? ` &middot; ${s.wins} won` : ""}</div></div>
      <div class="cell"><div class="k">Stop</div>
        <div class="v mono">${s.stop ? fmt(s.stop) : "none"}</div></div>
    </div>
    ${rows ? `<div class="scroll"><table>
        <thead><tr><th>#</th><th>Closed</th><th>Entry</th><th>Exit</th><th>Net</th>
        <th>Balance</th><th class="why">Why</th></tr></thead>
        <tbody>${rows}</tbody></table></div>`
      : `<div class="empty">No completed round trips yet.
         ${held ? "A position is open - it appears here when it closes." : ""}</div>`}
  </div>`;
}

async function tick() {
  const live = document.getElementById("live");
  try {
    const r = await fetch("/api/state", {cache: "no-store"});
    const d = await r.json();
    document.getElementById("sessions").innerHTML =
      d.sessions.length ? d.sessions.map(sessionCard).join("")
                        : `<div class="card"><div class="empty">No sessions configured.</div></div>`;
    const newest = Math.max(0, ...d.sessions.map(s => s.updated_ms || 0));
    const mins = newest ? Math.round((Date.now() - newest) / 60000) : null;
    live.className = "live" + (mins !== null && mins > 5 ? " stale" : "");
    document.getElementById("ago").textContent =
      mins === null ? "no data yet" : mins > 5 ? `last update ${mins}m ago` : "live";
  } catch (e) {
    live.className = "live stale";
    document.getElementById("ago").textContent = "cannot reach the server";
  }
}
tick();
setInterval(tick, 5000);
</script></body></html>
"""
