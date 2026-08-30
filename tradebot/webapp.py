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
<title>tradebot</title>
<style>
:root{
  --bg:#F7F8F7;--panel:#FFFFFF;--panel2:#FBFBFA;--inset:#F0F2EF;
  --text:#0E1114;--text2:#5A626A;--text3:#8A929A;
  --line:rgba(14,17,20,.10);--line2:rgba(14,17,20,.06);
  --accent:#2F6BFF;--accent-ink:#fff;--accent-soft:rgba(47,107,255,.10);
  --pos:#0E9E62;--pos-soft:rgba(14,158,98,.12);
  --neg:#D83A48;--neg-soft:rgba(216,58,72,.12);
  --glow:radial-gradient(720px 340px at 50% -140px,rgba(47,107,255,.10),transparent 70%);
  --shadow:0 1px 1px rgba(14,17,20,.04),0 6px 16px -8px rgba(14,17,20,.12);
  --shadow-lg:0 2px 4px rgba(14,17,20,.05),0 24px 48px -20px rgba(14,17,20,.22);
  --radius:16px;--radius-sm:10px;
}
@media (prefers-color-scheme:dark){:root{
  --bg:#08090B;--panel:#101317;--panel2:#0C0F12;--inset:#161A1F;
  --text:#F3F5F7;--text2:#9AA2AB;--text3:#626A73;
  --line:rgba(255,255,255,.09);--line2:rgba(255,255,255,.055);
  --accent:#5B8CFF;--accent-ink:#08090B;--accent-soft:rgba(91,140,255,.14);
  --pos:#34D399;--pos-soft:rgba(52,211,153,.13);
  --neg:#FF6470;--neg-soft:rgba(255,100,112,.13);
  --glow:radial-gradient(760px 360px at 50% -160px,rgba(91,140,255,.16),transparent 70%);
  --shadow:0 1px 1px rgba(0,0,0,.4),0 10px 26px -12px rgba(0,0,0,.6);
  --shadow-lg:0 2px 6px rgba(0,0,0,.45),0 36px 70px -28px rgba(0,0,0,.75);
}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;color:var(--text);background:var(--bg);background-image:var(--glow);
  background-repeat:no-repeat;background-attachment:fixed;
  font:15px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  -webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
::selection{background:var(--accent-soft)}
.mono{font-variant-numeric:tabular-nums;
  font-family:ui-monospace,"SF Mono",SFMono-Regular,Menlo,Consolas,monospace;letter-spacing:-.02em}
.wrap{max-width:1080px;margin:0 auto;padding:40px 24px 88px;display:flex;flex-direction:column;gap:28px}

/* ---- header ---- */
.top{display:flex;align-items:center;gap:16px;flex-wrap:wrap}
.brand{display:flex;align-items:center;gap:12px;margin-right:auto}
.mark{width:34px;height:34px;border-radius:10px;display:grid;place-items:center;flex:none;
  background:var(--accent);
  background-image:linear-gradient(160deg,rgba(255,255,255,.22),rgba(0,0,0,.18));
  box-shadow:0 8px 20px -8px var(--accent-soft),inset 0 1px 0 rgba(255,255,255,.28)}
.mark svg{width:17px;height:17px;display:block}
.brand h1{margin:0;font-size:20px;font-weight:680;letter-spacing:-.022em}
.brand .tag{display:block;font-size:11.5px;color:var(--text3);font-weight:500;letter-spacing:.01em;margin-top:1px}
.status{display:inline-flex;align-items:center;gap:7px;font-size:12.5px;color:var(--text2)}
.pulse{width:7px;height:7px;border-radius:50%;background:var(--pos);position:relative}
.pulse::after{content:"";position:absolute;inset:-4px;border-radius:50%;border:1px solid var(--pos);
  animation:ping 1.8s ease-out infinite}
.pulse.stale{background:var(--text3)}.pulse.stale::after{animation:none;border-color:var(--text3)}
@keyframes ping{0%{transform:scale(.7);opacity:.9}100%{transform:scale(1.9);opacity:0}}
.badge{display:inline-flex;align-items:center;gap:7px;font-size:11px;font-weight:650;
  text-transform:uppercase;letter-spacing:.12em;padding:6px 12px;border-radius:999px;
  background:var(--inset);color:var(--text2);border:1px solid var(--line)}
.badge::before{content:"";width:6px;height:6px;border-radius:50%;background:currentColor}
.badge.pro{background:var(--accent);
  background-image:linear-gradient(135deg,rgba(255,255,255,.16),rgba(123,92,240,.5));
  color:#fff;border-color:transparent;box-shadow:0 8px 22px -10px var(--accent)}
.badge.pro::before{background:#fff}
.tiernote{flex-basis:100%;color:var(--text3);font-size:12.5px}

/* ---- summary strip ---- */
.summary{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
  background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
  box-shadow:var(--shadow);overflow:hidden}
.summary .m{padding:16px 20px;border-right:1px solid var(--line2)}
.summary .m:last-child{border-right:none}
.summary .k{font-size:10px;font-weight:650;letter-spacing:.1em;text-transform:uppercase;color:var(--text3)}
.summary .val{font-size:22px;font-weight:600;margin-top:5px;letter-spacing:-.02em}

/* ---- cards ---- */
.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
  box-shadow:var(--shadow);overflow:hidden;animation:rise .24s cubic-bezier(.2,.7,.3,1) both}
@keyframes rise{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
.card-h{display:flex;justify-content:space-between;align-items:baseline;gap:12px;
  padding:18px 22px 0;flex-wrap:wrap}
.card-h .title{font-size:15px;font-weight:640;letter-spacing:-.01em}
.card-h .hint{color:var(--text3);font-size:12.5px}

/* ---- form ---- */
form{padding:16px 22px 22px;display:grid;
  grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:14px 18px;align-items:end}
.field{display:flex;flex-direction:column;gap:7px}
.field > span{font-size:10.5px;font-weight:650;letter-spacing:.09em;text-transform:uppercase;color:var(--text3)}
input,select{font:inherit;width:100%;height:40px;padding:0 12px;color:var(--text);
  background:var(--inset);border:1px solid var(--line);border-radius:var(--radius-sm);appearance:none;
  transition:border-color .14s,box-shadow .14s,background .14s}
select{padding-right:34px;
  background-image:linear-gradient(45deg,transparent 50%,var(--text2) 0),linear-gradient(135deg,var(--text2) 50%,transparent 0);
  background-position:calc(100% - 18px) 55%,calc(100% - 13px) 55%;background-size:5px 5px;background-repeat:no-repeat}
input:hover,select:hover{border-color:var(--text3)}
input:focus,select:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 4px var(--accent-soft);background:var(--panel)}
input::placeholder{color:var(--text3)}
.field.go{align-self:end}

/* ---- buttons ---- */
.btn{font:inherit;font-weight:600;height:40px;padding:0 18px;border-radius:var(--radius-sm);cursor:pointer;
  border:1px solid var(--line);background:var(--panel);color:var(--text);white-space:nowrap;
  transition:transform .1s,box-shadow .16s,background .16s,border-color .16s,color .16s}
.btn:hover{border-color:var(--text3)}
.btn:active{transform:translateY(1px)}
.btn:disabled{opacity:.45;cursor:not-allowed}
.btn.primary{border-color:transparent;color:var(--accent-ink);background:var(--accent);
  box-shadow:0 10px 24px -12px var(--accent)}
.btn.primary:hover{filter:brightness(1.06);box-shadow:0 14px 30px -12px var(--accent)}
.btn.ghost{background:transparent}
.btn.danger{color:var(--neg);background:transparent}
.btn.danger:hover{border-color:var(--neg);background:var(--neg-soft)}
.btn.sm{height:34px;padding:0 14px;font-size:13px}

/* ---- session ---- */
#sessions{display:flex;flex-direction:column;gap:18px}
.s-top{display:flex;justify-content:space-between;align-items:flex-start;gap:14px;padding:18px 22px;flex-wrap:wrap}
.s-id{display:flex;align-items:center;gap:10px;font-size:16px;font-weight:660;letter-spacing:-.015em}
.dot{width:9px;height:9px;border-radius:50%;background:var(--text3);flex:none}
.dot.on{background:var(--pos);box-shadow:0 0 0 4px var(--pos-soft)}
.chip{font-size:10px;font-weight:650;letter-spacing:.09em;text-transform:uppercase;padding:3px 9px;
  border-radius:999px;background:var(--inset);color:var(--text3);border:1px solid var(--line)}
.chip.on{background:var(--pos-soft);color:var(--pos);border-color:transparent}
.s-meta{color:var(--text3);font-size:12.5px;margin-top:5px}
.s-meta a{color:var(--accent);text-decoration:none}.s-meta a:hover{text-decoration:underline}
.s-acts{display:flex;gap:9px;flex-wrap:wrap}

.s-figure{display:flex;align-items:flex-end;gap:16px;padding:6px 22px 18px;flex-wrap:wrap}
.s-figure .big{font-size:34px;font-weight:640;letter-spacing:-.03em;line-height:1}
.delta{display:inline-flex;align-items:center;gap:8px;font-size:13px;font-weight:600;padding:5px 11px;
  border-radius:999px;background:var(--inset);color:var(--text2)}
.delta.pos{background:var(--pos-soft);color:var(--pos)}
.delta.neg{background:var(--neg-soft);color:var(--neg)}

.s-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));
  border-top:1px solid var(--line2)}
.s-row .cell{padding:13px 22px;border-right:1px solid var(--line2)}
.s-row .cell:last-child{border-right:none}
.s-row .k{font-size:9.5px;font-weight:650;letter-spacing:.1em;text-transform:uppercase;color:var(--text3)}
.s-row .v{font-size:15px;margin-top:4px}
.pos{color:var(--pos)}.neg{color:var(--neg)}.flat{color:var(--text3)}

.s-wait{padding:20px 22px 24px}
.skl{height:34px;width:190px;border-radius:8px;
  background:linear-gradient(90deg,var(--inset) 25%,var(--line2) 37%,var(--inset) 63%);
  background-size:400% 100%;animation:sh 1.4s ease infinite}
.s-wait p{margin:12px 0 0;color:var(--text3);font-size:12.5px}
@keyframes sh{0%{background-position:100% 0}100%{background-position:0 0}}

.empty{padding:56px 22px;text-align:center}
.empty .ic{width:44px;height:44px;border-radius:12px;margin:0 auto 14px;display:grid;place-items:center;
  background:var(--inset);color:var(--text3)}
.empty .big{font-size:15px;font-weight:600}
.empty p{margin:4px 0 0;color:var(--text3);font-size:13px}

.err{margin:0 22px 18px;padding:11px 14px;border-radius:var(--radius-sm);font-size:13px;
  color:var(--neg);background:var(--neg-soft)}
footer{display:flex;align-items:center;gap:9px;color:var(--text3);font-size:12px;
  border-top:1px solid var(--line2);padding-top:18px}
.lock{width:13px;height:13px;flex:none;opacity:.75}
a{color:var(--accent)}
@media (max-width:560px){.wrap{padding:28px 16px 64px}.s-figure .big{font-size:28px}}
</style></head><body>
<div class="wrap">
  <div class="top">
    <div class="brand">
      <span class="mark"><svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2.4"
        stroke-linecap="round" stroke-linejoin="round"><path d="M3 17l5-5 4 3 8-9"/><path d="M15 6h5v5"/></svg></span>
      <div><h1>tradebot</h1><span class="tag">local trading panel</span></div>
    </div>
    <span class="status"><span class="pulse" id="pulse"></span><span id="agoTxt">connecting…</span></span>
    <span class="badge" id="tierBadge">free</span>
    <span class="tiernote" id="tierNote"></span>
  </div>

  <div class="summary" id="summary" hidden></div>

  <div class="card">
    <div class="card-h"><span class="title">New paper session</span>
      <span class="hint">simulated money, live prices — nothing real is traded</span></div>
    <form id="newForm">
      <label class="field"><span>Name</span><input name="name" placeholder="my-btc-test" required></label>
      <label class="field"><span>Strategy</span><select name="strategy" id="stratSel"></select></label>
      <label class="field"><span>Venue</span>
        <select name="venue" id="venueSel">
          <option value="paper">Built-in simulator</option>
          <option value="alpaca">Alpaca paper</option></select></label>
      <label class="field"><span>Symbol</span><input name="symbol" id="symIn" value="BTC_USD"></label>
      <label class="field"><span>Interval</span>
        <select name="interval">
          <option>1m</option><option>5m</option><option>15m</option><option>30m</option>
          <option selected>1h</option><option>4h</option><option>1D</option></select></label>
      <label class="field"><span>Starting cash</span>
        <input name="starting_cash" type="number" value="1000" min="1"></label>
      <label class="field"><span>Currency</span><input name="currency" value="USD"></label>
      <div class="field go"><button class="btn primary" type="submit">Create session</button></div>
    </form>
    <div class="err" id="formErr" hidden></div>
  </div>

  <div id="sessions"></div>
  <footer id="foot"><svg class="lock" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
    stroke-linecap="round"><rect x="4" y="10" width="16" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/></svg>
    <span id="footTxt"></span></footer>
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
    t.name==="pro" ? `Pro · ${t.max_running} concurrent sessions · every strategy · Alpaca venue · CSV export`
                   : `Free · ${t.max_running} session · ${t.strategies.length} strategies · set TRADEBOT_LICENSE for Pro`;
  const sel=document.getElementById("stratSel"), allowed=t.strategies;
  sel.innerHTML="";
  (window.__STRATS||[]).forEach(name=>{
    if(allowed && !allowed.includes(name)) return;
    const o=document.createElement("option"); o.value=name; o.textContent=name; sel.appendChild(o);
  });
  document.getElementById("venueSel").querySelector('option[value="alpaca"]').disabled = !t.venues.includes("alpaca");
}

document.getElementById("venueSel").addEventListener("change",e=>{
  document.getElementById("symIn").value = e.target.value==="alpaca" ? "BTC/USD" : "BTC_USD";
});

document.getElementById("newForm").addEventListener("submit",async e=>{
  e.preventDefault();
  const f=new FormData(e.target), spec={};
  for(const [k,val] of f.entries()) spec[k]=val;
  const err=document.getElementById("formErr"); err.hidden=true;
  try{ await post("/api/sessions",spec); e.target.reset();
       document.getElementById("symIn").value="BTC_USD"; tick(); }
  catch(ex){ err.textContent=ex.message; err.hidden=false; }
});

function summaryStrip(sessions){
  const el=document.getElementById("summary");
  if(!sessions.length){ el.hidden=true; return; }
  const cur=sessions[0].currency||"";
  const running=sessions.filter(s=>s.running).length;
  const ready=sessions.filter(s=>!s.waiting && s.marked);
  const staked=sessions.reduce((a,s)=>a+(s.starting_cash||0),0);
  const equity=ready.reduce((a,s)=>a+s.equity,0);
  const complete=ready.length===sessions.length && sessions.length>0;
  const pnl=complete?equity-staked:null;
  el.hidden=false;
  el.innerHTML=`
    <div class="m"><div class="k">Sessions</div><div class="val mono">${sessions.length}</div></div>
    <div class="m"><div class="k">Running</div><div class="val mono">${running}</div></div>
    <div class="m"><div class="k">Staked</div><div class="val mono">${cur}${fmt(staked)}</div></div>
    <div class="m"><div class="k">Equity</div><div class="val mono ${complete?cls(pnl):""}">${
      complete?cur+fmt(equity):"—"}</div></div>
    <div class="m"><div class="k">Net P&amp;L</div><div class="val mono ${complete?cls(pnl):"flat"}">${
      complete?sign(pnl):"—"}</div></div>`;
}

function sessionCard(s){
  const running=s.running, cur=s.currency||"";
  const acts=`<div class="s-acts">
    ${running
      ? `<button class="btn sm ghost" data-act="stop" data-name="${s.name}">Stop</button>`
      : `<button class="btn sm primary" data-act="start" data-name="${s.name}">Start</button>`}
    ${s.removable ? `<button class="btn sm danger" data-act="delete" data-name="${s.name}">Delete</button>` : ""}
  </div>`;
  const exp = (TIER&&TIER.can_export&&s.trade_count)
    ? ` · <a href="/api/export/${s.name}">export CSV</a>` : "";
  const head=`<div class="s-top">
    <div>
      <div class="s-id"><span class="dot ${running?"on":""}"></span>${s.name}
        <span class="chip ${running?"on":""}">${running?"running":"stopped"}</span></div>
      <div class="s-meta">${s.strategy||"?"} &nbsp;·&nbsp; ${s.symbol||"?"} &nbsp;·&nbsp; ${s.interval||""}${exp}</div>
    </div>${acts}</div>`;

  if(s.waiting){
    return `<div class="card">${head}<div class="s-wait"><div class="skl"></div>
      <p>No bar has closed yet${s.interval?` — on ${s.interval} bars the first update can take a while`:""}.</p>
    </div></div>`;
  }
  const pnl=s.equity-s.starting_cash, pct=s.starting_cash?(s.equity/s.starting_cash-1)*100:0;
  const um=!s.marked;
  const figure=`<div class="s-figure">
    <span class="big mono">${um?"—":cur+fmt(s.equity)}</span>
    ${um?"":`<span class="delta ${cls(pnl)}"><span class="mono">${cur+sign(pnl)}</span>
      <span class="mono">${sign(pct)}%</span></span>`}
  </div>`;
  const row=`<div class="s-row">
    <div class="cell"><div class="k">Position</div><div class="v mono">${Math.abs(s.qty)>1e-12?fmt(s.qty,6):"flat"}</div></div>
    <div class="cell"><div class="k">Avg price</div><div class="v mono">${Math.abs(s.qty)>1e-12?fmt(s.avg_price):"—"}</div></div>
    <div class="cell"><div class="k">Trades</div><div class="v mono">${s.trade_count||0}</div></div>
    <div class="cell"><div class="k">Fees</div><div class="v mono">${cur}${fmt(s.fees||0)}</div></div>
    <div class="cell"><div class="k">Stop</div><div class="v mono">${s.stop?fmt(s.stop):"none"}</div></div>
  </div>`;
  return `<div class="card">${head}${figure}${row}${s.halted?`<div class="err">Trading halted: ${s.halted}</div>`:""}</div>`;
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
  const pulse=document.getElementById("pulse"), ago=document.getElementById("agoTxt");
  try{
    const d=await (await fetch("/api/overview",{cache:"no-store"})).json();
    if(d.tier && (!TIER || TIER.name!==d.tier.name || !window.__STRATS)){
      if(!window.__STRATS){ window.__STRATS=(await (await fetch("/api/strategies")).json()).strategies; }
      applyTier(d.tier);
    }
    summaryStrip(d.sessions);
    const host=document.getElementById("sessions");
    host.innerHTML = d.sessions.length
      ? d.sessions.map(sessionCard).join("")
      : `<div class="card"><div class="empty">
           <div class="ic"><svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor"
             stroke-width="2" stroke-linecap="round"><path d="M3 17l5-5 4 3 8-9"/><path d="M15 6h5v5"/></svg></div>
           <div class="big">No sessions yet</div><p>Create one above to start paper trading.</p></div></div>`;
    const newest=Math.max(0,...d.sessions.map(s=>s.updated_ms||0));
    const mins=newest?Math.round((Date.now()-newest)/60000):null;
    const stale=mins!==null && mins>5;
    pulse.className="pulse"+(stale||mins===null?" stale":"");
    ago.textContent = mins===null ? "no data yet" : stale ? `updated ${mins}m ago` : "live";
    document.getElementById("footTxt").textContent =
      `Paper only · bound to 127.0.0.1 · this panel starts real OS processes, so don't expose it · ${d.sessions.length} session(s)`;
  }catch(ex){
    pulse.className="pulse stale"; ago.textContent="offline";
    document.getElementById("footTxt").textContent="cannot reach the panel server";
  }
}
tick(); setInterval(tick,5000);
</script></body></html>
"""
