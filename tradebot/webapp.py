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
  --bg:#F6F7F5;--bg2:#EEF0EC;--surface:#FFFFFF;--raised:#FFFFFF;--sunken:#F1F3EF;
  --ink:#111417;--ink2:#3C444A;--muted:#727C82;--faint:#9AA3A8;
  --hair:rgba(17,20,23,.09);--hair2:rgba(17,20,23,.05);
  --pos:#12855A;--pos-bg:rgba(18,133,90,.10);--neg:#B23A44;--neg-bg:rgba(178,58,68,.10);
  --warn:#8A6210;--accent:#4B49E4;--accent2:#7B5CF0;
  --grad:linear-gradient(135deg,#4B49E4,#7B5CF0);
  --shadow:0 1px 2px rgba(17,20,23,.04),0 10px 30px -14px rgba(17,20,23,.16);
  --shadow-sm:0 1px 2px rgba(17,20,23,.05),0 4px 12px -8px rgba(17,20,23,.14);
  --ring:0 0 0 3px rgba(75,73,228,.18);
}
@media (prefers-color-scheme:dark){:root{
  --bg:#0B0D0F;--bg2:#0E1113;--surface:#14181B;--raised:#171C20;--sunken:#101417;
  --ink:#EDEFEC;--ink2:#C2C8C4;--muted:#8B948E;--faint:#69726C;
  --hair:rgba(255,255,255,.10);--hair2:rgba(255,255,255,.05);
  --pos:#4FBE93;--pos-bg:rgba(79,190,147,.13);--neg:#E28E97;--neg-bg:rgba(226,142,151,.13);
  --warn:#D8AC5A;--accent:#7E7BFF;--accent2:#9E86FF;
  --grad:linear-gradient(135deg,#6D6BFF,#9E86FF);
  --shadow:0 1px 2px rgba(0,0,0,.4),0 18px 44px -20px rgba(0,0,0,.66);
  --shadow-sm:0 1px 2px rgba(0,0,0,.35),0 8px 20px -12px rgba(0,0,0,.55);
  --ring:0 0 0 3px rgba(126,123,255,.28);
}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;color:var(--ink);background:var(--bg);
  background-image:
    radial-gradient(900px 460px at 12% -8%,rgba(75,73,228,.10),transparent 60%),
    radial-gradient(760px 420px at 100% 0%,rgba(123,92,240,.08),transparent 55%);
  background-attachment:fixed;
  font:15px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  -webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
.wrap{max-width:1060px;margin:0 auto;padding:34px 22px 72px;display:flex;flex-direction:column;gap:26px}
.mono{font-variant-numeric:tabular-nums;
  font-family:ui-monospace,"SF Mono",SFMono-Regular,Menlo,Consolas,monospace;letter-spacing:-.01em}

/* header */
.top{display:flex;align-items:center;gap:14px;flex-wrap:wrap}
.brand{display:flex;align-items:center;gap:11px;margin-right:auto}
.logo{width:30px;height:30px;border-radius:9px;background:var(--grad);position:relative;
  box-shadow:var(--shadow-sm),inset 0 1px 0 rgba(255,255,255,.25)}
.logo::after{content:"";position:absolute;inset:8px;border-radius:4px;
  background:linear-gradient(135deg,rgba(255,255,255,.9),rgba(255,255,255,.35));
  -webkit-mask:linear-gradient(135deg,transparent 42%,#000 42%,#000 58%,transparent 58%);
          mask:linear-gradient(135deg,transparent 42%,#000 42%,#000 58%,transparent 58%)}
h1{margin:0;font-size:19px;font-weight:640;letter-spacing:-.02em}
.ver{font-size:11px;color:var(--faint);border:1px solid var(--hair);border-radius:6px;padding:2px 6px}
.badge{display:inline-flex;align-items:center;gap:6px;font-size:11px;font-weight:600;
  text-transform:uppercase;letter-spacing:.1em;padding:5px 11px;border-radius:999px;
  background:var(--sunken);color:var(--muted);border:1px solid var(--hair)}
.badge::before{content:"";width:6px;height:6px;border-radius:50%;background:currentColor;opacity:.7}
.badge.pro{background:var(--grad);color:#fff;border-color:transparent;
  box-shadow:0 6px 18px -8px rgba(75,73,228,.7)}
.badge.pro::before{background:#fff;opacity:1}
.tiernote{flex-basis:100%;color:var(--muted);font-size:13px;margin-top:-4px}

/* card */
.card{background:var(--surface);border:1px solid var(--hair);border-radius:16px;
  box-shadow:var(--shadow);overflow:hidden}
.card-h{display:flex;justify-content:space-between;align-items:center;gap:12px;
  padding:16px 20px;border-bottom:1px solid var(--hair2);flex-wrap:wrap}
.card-h .title{font-weight:620;letter-spacing:-.01em}
.card-h .sub{color:var(--muted);font-size:12.5px}

/* form */
form{padding:18px 20px;display:grid;
  grid-template-columns:repeat(auto-fit,minmax(158px,1fr));gap:14px 16px;align-items:end}
.field{display:flex;flex-direction:column;gap:6px}
.field > span{font-size:10.5px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:var(--faint)}
input,select{font:inherit;width:100%;padding:9px 11px;color:var(--ink);
  background:var(--sunken);border:1px solid var(--hair);border-radius:10px;
  transition:border-color .14s ease,box-shadow .14s ease,background .14s ease;appearance:none}
select{background-image:linear-gradient(45deg,transparent 50%,var(--muted) 50%),
  linear-gradient(135deg,var(--muted) 50%,transparent 50%);
  background-position:calc(100% - 17px) 51%,calc(100% - 12px) 51%;
  background-size:5px 5px,5px 5px;background-repeat:no-repeat;padding-right:32px}
input:focus,select:focus{outline:none;border-color:var(--accent);box-shadow:var(--ring);background:var(--surface)}
input::placeholder{color:var(--faint)}

/* buttons */
.btn{font:inherit;font-weight:560;padding:9px 16px;border-radius:10px;cursor:pointer;
  border:1px solid var(--hair);background:var(--surface);color:var(--ink);
  transition:transform .12s ease,box-shadow .14s ease,background .14s ease,border-color .14s ease}
.btn:hover{border-color:var(--faint)}
.btn:active{transform:translateY(1px)}
.btn:disabled{opacity:.45;cursor:not-allowed}
.btn.primary{border-color:transparent;color:#fff;background:var(--grad);
  box-shadow:0 8px 22px -10px rgba(75,73,228,.75)}
.btn.primary:hover{box-shadow:0 12px 28px -10px rgba(75,73,228,.9)}
.btn.danger{color:var(--neg)}
.btn.danger:hover{border-color:var(--neg);background:var(--neg-bg)}
.btn.sm{padding:7px 13px;font-size:13px}
.form-actions{display:flex;align-items:end}

/* session */
#sessions{display:flex;flex-direction:column;gap:16px}
.s-h{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;
  padding:16px 20px;flex-wrap:wrap}
.s-name{font-weight:620;font-size:15.5px;letter-spacing:-.01em;display:flex;align-items:center;gap:9px}
.s-meta{color:var(--muted);font-size:12.5px;margin-top:3px}
.s-meta a{color:var(--accent);text-decoration:none}.s-meta a:hover{text-decoration:underline}
.dot{width:8px;height:8px;border-radius:50%;background:var(--faint);flex:none}
.dot.on{background:var(--pos);box-shadow:0 0 0 4px var(--pos-bg)}
.chip{font-size:10.5px;font-weight:600;letter-spacing:.07em;text-transform:uppercase;
  padding:3px 9px;border-radius:999px;background:var(--sunken);color:var(--muted);border:1px solid var(--hair)}
.chip.on{background:var(--pos-bg);color:var(--pos);border-color:transparent}

.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(116px,1fr));
  border-top:1px solid var(--hair2);border-bottom:1px solid var(--hair2)}
.stat{padding:14px 20px;border-right:1px solid var(--hair2)}
.stat:last-child{border-right:none}
.stat .k{font-size:10px;font-weight:600;letter-spacing:.09em;text-transform:uppercase;color:var(--faint)}
.stat .v{font-size:19px;margin-top:4px;letter-spacing:-.01em}
.stat.lead .v{font-size:24px;font-weight:600}
.pos{color:var(--pos)}.neg{color:var(--neg)}.flat{color:var(--muted)}
.s-actions{display:flex;gap:9px;padding:14px 20px;flex-wrap:wrap}
.s-wait{padding:18px 20px;color:var(--muted);font-size:13px;
  border-top:1px solid var(--hair2)}

.empty{padding:44px 20px;text-align:center;color:var(--muted)}
.empty .big{font-size:15px;color:var(--ink2);font-weight:560;margin-bottom:4px}
.err{margin:0 20px 16px;padding:10px 13px;border-radius:10px;font-size:13px;
  color:var(--neg);background:var(--neg-bg);border:1px solid transparent}
footer{display:flex;align-items:center;gap:8px;color:var(--faint);font-size:12px;
  border-top:1px solid var(--hair2);padding-top:16px}
footer::before{content:"\1F512";filter:grayscale(1);opacity:.7}
@keyframes rise{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
.card{animation:rise .22s ease both}
</style></head><body>
<div class="wrap">
  <div class="top">
    <div class="brand"><div class="logo"></div><h1>tradebot</h1><span class="ver">v1.0</span></div>
    <span class="badge" id="tierBadge">free</span>
    <span class="tiernote" id="tierNote"></span>
  </div>

  <div class="card">
    <div class="card-h"><span class="title">New paper session</span>
      <span class="sub">simulated money &middot; live prices</span></div>
    <form id="newForm">
      <label class="field"><span>Name</span>
        <input name="name" placeholder="my-btc-test" required></label>
      <label class="field"><span>Strategy</span>
        <select name="strategy" id="stratSel"></select></label>
      <label class="field"><span>Venue</span>
        <select name="venue" id="venueSel">
          <option value="paper">Built-in simulator</option>
          <option value="alpaca">Alpaca paper</option></select></label>
      <label class="field"><span>Symbol</span>
        <input name="symbol" id="symIn" value="BTC_USD"></label>
      <label class="field"><span>Interval</span>
        <select name="interval">
          <option>1m</option><option>5m</option><option>15m</option><option>30m</option>
          <option selected>1h</option><option>4h</option><option>1D</option></select></label>
      <label class="field"><span>Starting cash</span>
        <input name="starting_cash" type="number" value="1000" min="1"></label>
      <label class="field"><span>Currency</span>
        <input name="currency" value="USD"></label>
      <div class="form-actions"><button class="btn primary" type="submit">Create session</button></div>
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
    t.name==="pro" ? `Pro · ${t.max_running} concurrent sessions, every strategy, Alpaca venue, CSV export`
                   : `Free · ${t.max_running} session, ${t.strategies.length} strategies — set TRADEBOT_LICENSE for Pro`;
  const sel=document.getElementById("stratSel");
  const allowed=t.strategies;
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
  const running=s.running, cur=s.currency||"";
  let body;
  if(s.waiting){
    body=`<div class="s-wait">No bar has closed yet${s.interval?` — on ${s.interval} bars the first update can take a while`:""}.</div>`;
  }else{
    const pnl=s.equity-s.starting_cash, pct=s.starting_cash?(s.equity/s.starting_cash-1)*100:0;
    const um=!s.marked;
    body=`<div class="stats">
      <div class="stat lead"><div class="k">Equity</div>
        <div class="v mono">${um?"—":cur+fmt(s.equity)}</div></div>
      <div class="stat"><div class="k">Profit</div>
        <div class="v mono ${um?"flat":cls(pnl)}">${um?"—":cur+sign(pnl)}</div></div>
      <div class="stat"><div class="k">Return</div>
        <div class="v mono ${um?"flat":cls(pnl)}">${um?"—":sign(pct)+"%"}</div></div>
      <div class="stat"><div class="k">Position</div>
        <div class="v mono">${Math.abs(s.qty)>1e-12?fmt(s.qty,6):"flat"}</div></div>
      <div class="stat"><div class="k">Trades</div>
        <div class="v mono">${s.trade_count||0}</div></div>
      <div class="stat"><div class="k">Fees</div>
        <div class="v mono">${cur}${fmt(s.fees||0)}</div></div>
    </div>`;
  }
  const exp = (TIER&&TIER.can_export&&s.trade_count)
    ? ` &middot; <a href="/api/export/${s.name}">export CSV</a>` : "";
  return `<div class="card">
    <div class="s-h">
      <div>
        <div class="s-name"><span class="dot ${running?"on":""}"></span>${s.name}
          <span class="chip ${running?"on":""}">${running?"running":"stopped"}</span></div>
        <div class="s-meta">${s.strategy||"?"} &middot; ${s.symbol||"?"} &middot; ${s.interval||""}${exp}</div>
      </div>
      <div class="s-actions" style="padding:0">
        ${running
          ? `<button class="btn sm" data-act="stop" data-name="${s.name}">Stop</button>`
          : `<button class="btn primary sm" data-act="start" data-name="${s.name}">Start</button>`}
        ${s.removable ? `<button class="btn danger sm" data-act="delete" data-name="${s.name}">Delete</button>` : ""}
      </div>
    </div>
    ${body}
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
      : `<div class="card"><div class="empty"><div class="big">No sessions yet</div>
         Create one above to start paper trading.</div></div>`;
    document.getElementById("foot").textContent =
      `Paper only · bound to 127.0.0.1 · starts real OS processes, do not expose it · ${d.sessions.length} session(s)`;
  }catch(ex){
    document.getElementById("foot").textContent="cannot reach the panel server";
  }
}
tick(); setInterval(tick,5000);
</script></body></html>
"""
