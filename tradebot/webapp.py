"""A local control panel for tradebot, in the browser.

The `dashboard` module is a viewer - it reads the files a session writes and never
touches anything. This is the other half: it *starts and stops* paper sessions from a
web page, so someone who will not open a terminal can still run the thing.

It is the same shape as the dashboard on purpose - standard library only, one HTML
file, no build step - and it keeps the dashboard's hard rule: **127.0.0.1 only**.
This server can spawn and kill processes, so exposing it on a network would hand that
control to the network. There is no account system and there should not be; the right
way to reach it from elsewhere is an SSH tunnel.

Free and Pro
------------
The panel has two tiers. Free is the default and needs nothing. Pro is unlocked by a
licence key in the ``TRADEBOT_LICENSE`` environment variable and lifts the limits
below. ``verify_license`` checks the key's shape and signature offline - like every
offline licence scheme the signing secret ships in the binary, so this deters casual
sharing rather than preventing it. It is the single place to swap in a real
server-side check if this is ever sold properly; doing that, and everything the
comment at the top of ``cli.cmd_web`` lists, is required before it is.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import config as config_mod
from . import dashboard as dashboard_mod
from .strategies import available

HOST = "127.0.0.1"
DEFAULT_PORT = 8770

CONFIG_DIR = Path("configs")
REGISTRY_FILE = Path("state") / "webapp_sessions.json"

# Configs this panel created. Kept separate from hand-written ones so "delete" can
# never remove a file the user wrote by hand.
WEB_PREFIX = "web_"
_SLUG = re.compile(r"[^a-z0-9_-]+")


# --------------------------------------------------------------------------- tiers


@dataclass(frozen=True)
class Tier:
    name: str
    max_running: int
    #: Strategy names allowed, or None for "all of them".
    strategies: frozenset[str] | None
    #: "paper" is always present; "alpaca" gates the Alpaca venue.
    venues: frozenset[str]
    can_arm_live: bool
    can_export: bool

    def allows_strategy(self, name: str) -> bool:
        return self.strategies is None or name in self.strategies


FREE = Tier(
    name="free",
    max_running=1,
    strategies=frozenset({"buy_and_hold", "slow_trend", "mean_reversion"}),
    venues=frozenset({"paper"}),
    can_arm_live=False,
    can_export=False,
)
PRO = Tier(
    name="pro",
    max_running=32,
    strategies=None,
    venues=frozenset({"paper", "alpaca"}),
    can_arm_live=True,
    can_export=True,
)

# Not a real secret - see the module docstring. Changing it invalidates every key
# already issued.
_LICENSE_SECRET = b"tradebot-pro-2026-offline"
_LICENSE_RE = re.compile(r"^TB-PRO-([0-9A-F]{16})-([0-9A-F]{12})$")


def _license_tag(seed_hex: str) -> str:
    mac = hmac.new(_LICENSE_SECRET, seed_hex.encode("ascii"), hashlib.sha256)
    return mac.hexdigest()[:12].upper()


def issue_license(seed: int | None = None) -> str:
    """Mint a Pro key. Used to hand keys to buyers; not exposed on the CLI."""
    seed_hex = f"{(seed if seed is not None else int.from_bytes(os.urandom(8), 'big')):016X}"
    return f"TB-PRO-{seed_hex}-{_license_tag(seed_hex)}"


def verify_license(key: str) -> bool:
    match = _LICENSE_RE.match(key.strip().upper())
    if not match:
        return False
    seed_hex, tag = match.groups()
    return hmac.compare_digest(tag, _license_tag(seed_hex))


def resolve_tier() -> Tier:
    key = os.environ.get("TRADEBOT_LICENSE", "")
    return PRO if key and verify_license(key) else FREE


# ------------------------------------------------------------------- process store


@dataclass
class Manager:
    """Tracks the paper sessions this panel has started."""

    tier: Tier
    python: str = field(default_factory=lambda: sys.executable or "python")
    _procs: dict[str, subprocess.Popen] = field(default_factory=dict)

    # -- registry persistence (survives a panel restart) --------------------

    def _load_registry(self) -> dict:
        try:
            return json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_registry(self, data: dict) -> None:
        REGISTRY_FILE.parent.mkdir(parents=True, exist_ok=True)
        REGISTRY_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")

    # -- liveness ----------------------------------------------------------

    def _alive(self, name: str, pid: int | None) -> bool:
        proc = self._procs.get(name)
        if proc is not None:
            return proc.poll() is None
        if not pid:
            return False
        return _pid_alive(pid)

    def running_names(self) -> set[str]:
        reg = self._load_registry()
        return {n for n, row in reg.items() if self._alive(n, row.get("pid"))}

    # -- config listing --------------------------------------------------

    def configs(self) -> list[Path]:
        return sorted(CONFIG_DIR.glob("*.toml"))

    def _config_path(self, name: str) -> Path:
        return CONFIG_DIR / f"{name}.toml"

    # -- create ----------------------------------------------------------

    def create(self, spec: dict) -> str:
        """Validate a form payload against the tier and write a config. Returns the name."""
        raw_name = str(spec.get("name", "")).strip().lower()
        slug = _SLUG.sub("-", raw_name).strip("-")
        if not slug:
            raise ValueError("give the session a name")
        name = f"{WEB_PREFIX}{slug}"
        path = self._config_path(name)
        if path.exists():
            raise ValueError(f"a session called {slug!r} already exists")

        strategy = str(spec.get("strategy", "")).strip()
        if strategy not in available():
            raise ValueError(f"unknown strategy {strategy!r}")
        if not self.tier.allows_strategy(strategy):
            raise ValueError(f"the {strategy} strategy needs Pro")

        venue = str(spec.get("venue", "paper")).strip()
        if venue not in ("paper", "alpaca"):
            raise ValueError(f"unknown venue {venue!r}")
        if venue == "alpaca" and "alpaca" not in self.tier.venues:
            raise ValueError("trading through Alpaca needs Pro")

        interval = str(spec.get("interval", "1h")).strip()
        if interval not in dashboard_mod._INTERVAL_MINUTES:
            raise ValueError(f"unsupported interval {interval!r}")

        try:
            cash = float(spec.get("starting_cash", 1000))
        except (TypeError, ValueError):
            raise ValueError("starting cash must be a number") from None
        if cash <= 0:
            raise ValueError("starting cash must be positive")

        symbol = str(spec.get("symbol", "")).strip() or ("BTC/USD" if venue == "alpaca" else "BTC_USD")
        currency = str(spec.get("currency", "USD")).strip().upper() or "USD"

        path.write_text(
            _render_config(
                name=name, symbol=symbol, interval=interval, cash=cash,
                currency=currency, strategy=strategy, venue=venue,
            ),
            encoding="utf-8",
        )
        # Fail fast if the file we just wrote does not load.
        config_mod.load(str(path))
        return name

    # -- start / stop / delete ----------------------------------------

    def start(self, name: str) -> None:
        path = self._config_path(name)
        if not path.exists():
            raise ValueError(f"no config for {name!r}")
        if self._alive(name, self._load_registry().get(name, {}).get("pid")):
            return  # already up

        running = self.running_names()
        if name not in running and len(running) >= self.tier.max_running:
            raise ValueError(
                f"the {self.tier.name} tier runs {self.tier.max_running} session"
                f"{'s' if self.tier.max_running != 1 else ''} at once"
                + ("" if self.tier is PRO else " - upgrade to Pro for more")
            )

        cfg = config_mod.load(str(path))
        log_path = Path(cfg.live.log_file or f"state/{name}_session.log")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handle = log_path.open("a", encoding="utf-8")
        proc = subprocess.Popen(
            [self.python, "-m", "tradebot", "--config", str(path), "paper"],
            stdout=handle, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            cwd=str(Path.cwd()), **_detach_kwargs(),
        )
        self._procs[name] = proc
        reg = self._load_registry()
        reg[name] = {"pid": proc.pid, "started_ms": int(time.time() * 1000), "config": str(path)}
        self._save_registry(reg)

    def stop(self, name: str) -> None:
        proc = self._procs.get(name)
        reg = self._load_registry()
        pid = reg.get(name, {}).get("pid")
        if proc is not None and proc.poll() is None:
            _terminate(proc)
        elif pid and _pid_alive(pid):
            _terminate_pid(pid)
        self._procs.pop(name, None)
        if name in reg:
            reg[name]["pid"] = None
            self._save_registry(reg)

    def delete(self, name: str) -> None:
        if not name.startswith(WEB_PREFIX):
            raise ValueError("only sessions created here can be deleted here")
        self.stop(name)
        path = self._config_path(name)
        try:
            path.unlink()
        except OSError:
            pass
        reg = self._load_registry()
        reg.pop(name, None)
        self._save_registry(reg)

    # -- overview ------------------------------------------------------

    def overview(self) -> dict:
        running = self.running_names()
        sessions = []
        for path in self.configs():
            try:
                cfg = config_mod.load(str(path))
            except Exception:  # noqa: BLE001 - a broken config should not blank the panel
                continue
            sessions.append(dashboard_mod.Session(
                name=path.stem,
                state_file=Path(cfg.live.state_file),
                trades_file=Path(cfg.live.trades_file),
                starting_cash=cfg.account.starting_cash,
                currency=cfg.account.symbol,
                symbol=cfg.market.symbol,
                interval=cfg.market.interval,
            ))
        snap = dashboard_mod.snapshot(sessions)
        for row in snap["sessions"]:
            row["running"] = row["name"] in running
            row["removable"] = row["name"].startswith(WEB_PREFIX)
        snap["tier"] = _tier_json(self.tier)
        return snap


# ------------------------------------------------------------------- os specifics


def _detach_kwargs() -> dict:
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
            subprocess, "DETACHED_PROCESS", 0
        )
        return {"creationflags": flags}
    return {"start_new_session": True}


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, check=False,
        )
        return str(pid) in out.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _terminate(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def _terminate_pid(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, check=False)
        return
    import signal
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    for _ in range(20):
        if not _pid_alive(pid):
            return
        time.sleep(0.5)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


# ------------------------------------------------------------------- config text


def _render_config(*, name, symbol, interval, cash, currency, strategy, venue) -> str:
    alpaca = "true" if venue == "alpaca" else "false"
    asset_class = "crypto" if "/" in symbol or venue != "alpaca" else "us_equity"
    return f"""# Written by the tradebot web panel. Safe to delete from the panel.
[market]
symbol = "{symbol}"
interval = "{interval}"
history_bars = 2000

[account]
starting_cash = {cash}
currency = "{currency}"

[alpaca]
enabled = {alpaca}
asset_class = "{asset_class}"
paper = true
data_feed = "iex"

[strategy]
name = "{strategy}"

[live]
enabled = false
dry_run = true
poll_seconds = 15.0
state_file = "state/{name}_state.json"
log_file = "state/{name}_session.log"
trades_file = "state/{name}_trades.csv"
"""


def _tier_json(tier: Tier) -> dict:
    return {
        "name": tier.name,
        "max_running": tier.max_running,
        "strategies": sorted(tier.strategies) if tier.strategies is not None else None,
        "venues": sorted(tier.venues),
        "can_arm_live": tier.can_arm_live,
        "can_export": tier.can_export,
    }


# ------------------------------------------------------------------- http


class _Handler(BaseHTTPRequestHandler):
    manager: Manager

    # A localhost tool still should not be drivable by a form on another page, so
    # state-changing calls must carry this header. fetch() from our own page adds it;
    # a cross-site <form> post cannot.
    GUARD = ("X-Tradebot", "panel")

    def _json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/", "/index.html"):
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/overview":
            self._json(self.manager.overview())
        elif self.path == "/api/strategies":
            self._json({"strategies": sorted(available())})
        elif self.path.startswith("/api/export/") and self.manager.tier.can_export:
            self._export(self.path.rsplit("/", 1)[-1])
        elif self.path.startswith("/api/export/"):
            self._json({"error": "exporting the trade log needs Pro"}, 402)
        else:
            self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        if self.headers.get(self.GUARD[0]) != self.GUARD[1]:
            self._json({"error": "missing panel header"}, 403)
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._json({"error": "bad JSON"}, 400)
            return

        try:
            if self.path == "/api/sessions":
                name = self.manager.create(payload)
                self._json({"ok": True, "name": name})
            elif self.path.endswith("/start"):
                self.manager.start(self._name_from_path("start"))
                self._json({"ok": True})
            elif self.path.endswith("/stop"):
                self.manager.stop(self._name_from_path("stop"))
                self._json({"ok": True})
            elif self.path.endswith("/delete"):
                self.manager.delete(self._name_from_path("delete"))
                self._json({"ok": True})
            else:
                self.send_error(404)
        except ValueError as exc:
            self._json({"error": str(exc)}, 400)

    def _name_from_path(self, verb: str) -> str:
        # /api/sessions/<name>/<verb>
        parts = [p for p in self.path.split("/") if p]
        if len(parts) != 4 or parts[:2] != ["api", "sessions"] or parts[3] != verb:
            raise ValueError("bad session path")
        return parts[2]

    def _export(self, name: str) -> None:
        path = Path("state") / f"{name}_trades.csv"
        try:
            body = path.read_bytes()
        except OSError:
            self._json({"error": "no trade log yet"}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/csv")
        self.send_header("Content-Disposition", f'attachment; filename="{name}_trades.csv"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        """Quiet: a polling page would flood the terminal."""


def serve(port: int = DEFAULT_PORT, tier: Tier | None = None) -> ThreadingHTTPServer:
    manager = Manager(tier=tier or resolve_tier())
    handler = type("Handler", (_Handler,), {"manager": manager})
    return ThreadingHTTPServer((HOST, port), handler)


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>tradebot panel</title>
<style>
:root{--ground:#F2F4F2;--surface:#fff;--sunken:#EAEDEA;--ink:#14181A;--ink2:#475156;
 --muted:#69747A;--hair:#DDE2DE;--pos:#2E7D5B;--neg:#A4404A;--warn:#8A6210;--accent:#1B5CC4}
@media (prefers-color-scheme:dark){:root{--ground:#131614;--surface:#1B1F1D;--sunken:#171A18;
 --ink:#E8EBE7;--ink2:#B3BDB6;--muted:#8D978F;--hair:#2B302D;--pos:#5FB894;--neg:#D9808A;
 --warn:#D2A64A;--accent:#5A8AE0}}
*{box-sizing:border-box}
body{margin:0;background:var(--ground);color:var(--ink);
 font:15px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1000px;margin:0 auto;padding:28px 20px 64px;display:flex;flex-direction:column;gap:20px}
header{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;border-bottom:1px solid var(--hair);padding-bottom:14px}
h1{margin:0;font-size:20px;letter-spacing:-.01em}
.badge{font-size:11px;text-transform:uppercase;letter-spacing:.08em;padding:3px 8px;border-radius:3px;
 background:var(--sunken);color:var(--muted)}
.badge.pro{background:var(--accent);color:#fff}
.mono{font-variant-numeric:tabular-nums;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.card{background:var(--surface);border:1px solid var(--hair);border-radius:4px;overflow:hidden}
.head{display:flex;justify-content:space-between;align-items:baseline;gap:12px;padding:13px 16px;
 border-bottom:1px solid var(--hair);flex-wrap:wrap}
.title{font-weight:600}
.sub{color:var(--muted);font-size:13px}
form{padding:14px 16px;display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;align-items:end}
label{display:flex;flex-direction:column;gap:4px;font-size:12px;color:var(--muted)}
input,select{font:inherit;padding:7px 8px;border:1px solid var(--hair);border-radius:3px;
 background:var(--ground);color:var(--ink)}
button{font:inherit;padding:7px 14px;border:1px solid var(--hair);border-radius:3px;background:var(--sunken);
 color:var(--ink);cursor:pointer}
button.primary{background:var(--accent);color:#fff;border-color:transparent}
button:disabled{opacity:.5;cursor:not-allowed}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:1px;background:var(--hair)}
.cell{background:var(--surface);padding:11px 14px}
.k{font-size:10px;letter-spacing:.09em;text-transform:uppercase;color:var(--muted)}
.v{font-size:18px;margin-top:2px}
.pos{color:var(--pos)}.neg{color:var(--neg)}.flat{color:var(--muted)}
.pill{font-size:11px;padding:2px 7px;border-radius:99px;background:var(--sunken);color:var(--muted)}
.pill.on{background:var(--pos);color:#fff}
.err{color:var(--neg);font-size:13px;padding:0 16px 12px}
.note{color:var(--muted);font-size:12.5px}
footer{color:var(--muted);font-size:12px;border-top:1px solid var(--hair);padding-top:14px}
a{color:var(--accent)}
</style></head><body>
<div class="wrap">
  <header>
    <h1>tradebot</h1>
    <span class="badge" id="tierBadge">free</span>
    <span class="sub" id="tierNote"></span>
  </header>

  <div class="card">
    <div class="head"><span class="title">New paper session</span>
      <span class="sub">simulated money, live prices</span></div>
    <form id="newForm">
      <label>Name<input name="name" placeholder="my-btc-test" required></label>
      <label>Strategy<select name="strategy" id="stratSel"></select></label>
      <label>Venue<select name="venue" id="venueSel">
        <option value="paper">Built-in simulator</option>
        <option value="alpaca">Alpaca paper</option></select></label>
      <label>Symbol<input name="symbol" id="symIn" value="BTC_USD"></label>
      <label>Interval<select name="interval">
        <option>1m</option><option>5m</option><option>15m</option><option>30m</option>
        <option selected>1h</option><option>4h</option><option>1D</option></select></label>
      <label>Starting cash<input name="starting_cash" type="number" value="1000" min="1"></label>
      <label>Currency<input name="currency" value="USD" size="4"></label>
      <button class="primary" type="submit">Create</button>
    </form>
    <div class="err" id="formErr" hidden></div>
  </div>

  <div id="sessions"></div>
  <footer id="foot"></footer>
</div>
<script>
const H = {"Content-Type":"application/json","X-Tradebot":"panel"};
const fmt=(n,d=2)=>(n==null||!isFinite(n))?"—":n.toLocaleString(undefined,{minimumFractionDigits:d,maximumFractionDigits:d});
const cls=n=>n>0?"pos":n<0?"neg":"flat";
const sign=n=>(n>0?"+":"")+fmt(n);
let TIER=null;

async function post(url,body){
  const r=await fetch(url,{method:"POST",headers:H,body:JSON.stringify(body||{})});
  const d=await r.json().catch(()=>({}));
  if(!r.ok) throw new Error(d.error||("HTTP "+r.status));
  return d;
}

function applyTier(t){
  TIER=t;
  const b=document.getElementById("tierBadge");
  b.textContent=t.name; b.className="badge"+(t.name==="pro"?" pro":"");
  document.getElementById("tierNote").textContent=
    t.name==="pro" ? `${t.max_running} sessions, every strategy, Alpaca`
                   : `${t.max_running} session, ${t.strategies.length} strategies — set TRADEBOT_LICENSE for Pro`;
  const sel=document.getElementById("stratSel");
  const allowed=t.strategies; // null on pro
  fetch("/api/strategies").catch(()=>null);
  sel.innerHTML="";
  (window.__STRATS||[]).forEach(name=>{
    if(allowed && !allowed.includes(name)) return;
    const o=document.createElement("option"); o.value=name; o.textContent=name; sel.appendChild(o);
  });
  const v=document.getElementById("venueSel");
  v.querySelector('option[value="alpaca"]').disabled = !t.venues.includes("alpaca");
}

document.getElementById("venueSel").addEventListener("change",e=>{
  document.getElementById("symIn").value = e.target.value==="alpaca" ? "BTC/USD" : "BTC_USD";
});

document.getElementById("newForm").addEventListener("submit",async e=>{
  e.preventDefault();
  const f=new FormData(e.target), spec={};
  for(const [k,val] of f.entries()) spec[k]=val;
  const err=document.getElementById("formErr");
  err.hidden=true;
  try{ await post("/api/sessions",spec); e.target.reset();
       document.getElementById("symIn").value="BTC_USD"; tick(); }
  catch(ex){ err.textContent=ex.message; err.hidden=false; }
});

function sessionCard(s){
  const running=s.running;
  const cur=s.currency||"";
  let stats="";
  if(s.waiting){
    stats=`<div class="cell" style="grid-column:1/-1"><div class="note">
      No bar has closed yet${s.interval?` — on ${s.interval} bars that can take a while`:""}.</div></div>`;
  }else{
    const pnl=s.equity-s.starting_cash, pct=s.starting_cash?(s.equity/s.starting_cash-1)*100:0;
    const um=!s.marked;
    stats=`
      <div class="cell"><div class="k">Equity</div><div class="v mono">${um?"—":cur+fmt(s.equity)}</div></div>
      <div class="cell"><div class="k">Profit</div><div class="v mono ${um?"flat":cls(pnl)}">${um?"—":cur+sign(pnl)}</div></div>
      <div class="cell"><div class="k">Return</div><div class="v mono ${um?"flat":cls(pnl)}">${um?"—":sign(pct)+"%"}</div></div>
      <div class="cell"><div class="k">Position</div><div class="v mono">${Math.abs(s.qty)>1e-12?fmt(s.qty,6):"flat"}</div></div>
      <div class="cell"><div class="k">Trades</div><div class="v mono">${s.trade_count||0}</div></div>
      <div class="cell"><div class="k">Fees</div><div class="v mono">${cur}${fmt(s.fees||0)}</div></div>`;
  }
  const exportLink = (TIER&&TIER.can_export&&s.trade_count)
    ? ` &middot; <a href="/api/export/${s.name}">export CSV</a>` : "";
  return `<div class="card">
    <div class="head">
      <span class="title">${s.name} <span class="pill ${running?"on":""}">${running?"running":"stopped"}</span></span>
      <span class="sub">${s.strategy||"?"} on ${s.symbol||"?"} ${s.interval||""}${exportLink}</span>
    </div>
    <div class="grid">${stats}</div>
    <div class="row" style="padding:12px 16px">
      ${running
        ? `<button data-act="stop" data-name="${s.name}">Stop</button>`
        : `<button class="primary" data-act="start" data-name="${s.name}">Start</button>`}
      ${s.removable ? `<button data-act="delete" data-name="${s.name}">Delete</button>` : ""}
    </div>
  </div>`;
}

document.getElementById("sessions").addEventListener("click",async e=>{
  const btn=e.target.closest("button[data-act]"); if(!btn) return;
  const {act,name}=btn.dataset;
  if(act==="delete" && !confirm(`Delete ${name}? Its config is removed; the trade history stays on disk.`)) return;
  btn.disabled=true;
  try{ await post(`/api/sessions/${name}/${act}`); }
  catch(ex){ alert(ex.message); }
  tick();
});

async function tick(){
  try{
    const d=await (await fetch("/api/overview",{cache:"no-store"})).json();
    if(d.tier && (!TIER || TIER.name!==d.tier.name || !window.__STRATS)) {
      if(!window.__STRATS){ window.__STRATS=(await (await fetch("/api/strategies")).json()).strategies; }
      applyTier(d.tier);
    }
    const host=document.getElementById("sessions");
    host.innerHTML = d.sessions.length
      ? d.sessions.map(sessionCard).join("")
      : `<div class="card"><div class="note" style="padding:16px">No sessions yet. Create one above.</div></div>`;
    document.getElementById("foot").textContent =
      `Paper only. This panel is bound to 127.0.0.1 and starts real OS processes — do not expose it. ${d.sessions.length} session(s).`;
  }catch(ex){
    document.getElementById("foot").textContent="cannot reach the panel server";
  }
}
tick(); setInterval(tick,5000);
</script></body></html>
"""
