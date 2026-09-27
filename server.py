#!/usr/bin/env python3
"""
printer-dash: dashboard backend for the Flashforge Creator 5 Pro (LAN-only mode).

Built 2026-09-25 to serve as OrcaSlicer's "Device UI" page. The printer's own
HTTP API (port 8898) has no CORS and its camera (mjpg-streamer, port 8080)
effectively serves one client, so this service sits in between:

  * polls /detail every POLL_SECONDS, pushes state to browsers over SSE
  * records sensor history (SQLite), job history, events and a command audit log
  * relays the camera: one upstream connection fanned out to any number of viewers
  * proxies controls, applying the Creator 5 protocol rules (see PROTOCOL NOTES)
  * file list / thumbnails / upload / start print
  * notifications (ntfy or generic JSON webhook) and a Prometheus /metrics endpoint

PROTOCOL NOTES (Creator 5; from GhostTypes/ff-5mp-api-py, verified there on hardware):
  * temperatureCtl_cmd: -200 = leave unchanged, -100 = off for scalar heaters,
    but inside the 4-entry `nozzles` array only a literal 0 turns a tool off.
  * printerCtl_cmd sets zAxisCompensation/speed/chamberFan/coolingFan together
    and only works while printing -> always resend the current values.
  * No TCP/8899 channel on this model: no homing, jogging or raw G-code.

Config: /etc/printer-dash/config.json   Data: /var/lib/printer-dash/dash.db
"""
import asyncio
import base64
import json
import logging
import os
import re
import sqlite3
import tempfile
import time

import aiohttp
from aiohttp import web

CONFIG_PATH = os.environ.get("PRINTER_DASH_CONFIG", "/etc/printer-dash/config.json")
DB_PATH = os.environ.get("PRINTER_DASH_DB", "/var/lib/printer-dash/dash.db")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

POLL_SECONDS = 2
SAMPLE_SECONDS = 5
SAMPLE_RETENTION_DAYS = 30
EVENT_RETENTION_DAYS = 180
OFFLINE_AFTER_FAILURES = 3
CAMERA_IDLE_STOP_SECONDS = 20

TEMP_NO_CHANGE = -200
TEMP_OFF_SCALAR = -100
NOZZLE_OFF = 0
LIMITS = {"tool": (0, 300), "bed": (0, 110), "chamber": (0, 65)}

ACTIVE_STATES = {"printing", "pause", "pausing", "paused", "heating", "busy", "calibrate_doing"}
C5_PALETTE = {
    "White": "#FFFFFF", "Yellow": "#FFF245", "Light Green": "#DEF578", "Green": "#21CC3D",
    "Dark Green": "#167A4B", "Teal": "#156682", "Cyan": "#24E4A0", "Light Blue": "#7BD9F0",
    "Blue": "#4CAAF8", "Dark Blue": "#2E54DD", "Purple": "#48358C", "Violet": "#A341F7",
    "Magenta": "#F435F6", "Pink": "#D5B4DE", "Coral": "#FA6173", "Red": "#F82D29",
    "Brown": "#805003", "Orange": "#F9903B", "Cream": "#FCEBD7", "Tan": "#D5C5A1",
    "Dark Brown": "#B17C38", "Gray": "#8C8C89", "Light Gray": "#BEBEBE", "Black": "#1B1B1B",
}
DEFAULT_PRESETS = [
    {"name": "PLA", "tool": 210, "bed": 55, "chamber": 0},
    {"name": "PETG", "tool": 240, "bed": 70, "chamber": 0},
    {"name": "ABS", "tool": 250, "bed": 100, "chamber": 45},
    {"name": "TPU", "tool": 225, "bed": 45, "chamber": 0},
]
NOTIFY_EVENTS = ["print_started", "print_completed", "print_cancelled", "print_paused",
                 "print_resumed", "print_error", "printer_offline", "printer_online"]

log = logging.getLogger("printer-dash")


# ---------------------------------------------------------------- database

def db_connect():
    conn = sqlite3.connect(DB_PATH, isolation_level=None, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS samples (
            ts INTEGER PRIMARY KEY, status TEXT,
            t0 REAL, t1 REAL, t2 REAL, t3 REAL, tt0 REAL, tt1 REAL, tt2 REAL, tt3 REAL,
            bed REAL, bed_t REAL, chamber REAL, chamber_t REAL,
            chamber_fan REAL, cooling_fan REAL, speed REAL, progress REAL, layer INTEGER, tvoc REAL);
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, file TEXT, started INTEGER, ended INTEGER,
            result TEXT, progress REAL, layers INTEGER, est_seconds INTEGER, filament_g REAL,
            thumb BLOB);
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, kind TEXT, message TEXT, source TEXT);
        CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
        CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
    """)
    return conn


class Store:
    def __init__(self):
        self.db = db_connect()

    def kv_get(self, key, default):
        row = self.db.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def kv_set(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO kv(k, v) VALUES (?, ?)", (key, json.dumps(value)))

    def event(self, kind, message, source="system"):
        self.db.execute("INSERT INTO events(ts, kind, message, source) VALUES (?,?,?,?)",
                        (int(time.time()), kind, message, source))

    def prune(self):
        now = int(time.time())
        self.db.execute("DELETE FROM samples WHERE ts < ?", (now - SAMPLE_RETENTION_DAYS * 86400,))
        self.db.execute("DELETE FROM events WHERE ts < ?", (now - EVENT_RETENTION_DAYS * 86400,))


# ---------------------------------------------------------------- printer client

class PrinterError(Exception):
    pass


class Printer:
    def __init__(self, cfg):
        self.host = cfg["host"]
        self.serial = cfg["serialNumber"]
        self.code = cfg["checkCode"]
        self.base = f"http://{self.host}:8898"
        self.camera_url = f"http://{self.host}:8080/?action=stream"
        self.session = None
        self.lock = asyncio.Lock()

    async def start(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))

    def _auth(self, **extra):
        return {"serialNumber": self.serial, "checkCode": self.code, **extra}

    async def post(self, path, body=None, timeout=10):
        async with self.session.post(self.base + path, json=body or self._auth(),
                                     timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            text = await r.text()
        try:
            data = json.loads(text)
        except ValueError:
            raise PrinterError(f"{path}: HTTP {r.status}, non-JSON reply")
        return data

    async def detail(self):
        data = await self.post("/detail", timeout=5)
        if data.get("code") != 0:
            raise PrinterError(f"/detail: {data.get('message')}")
        return data["detail"]

    async def product(self):
        return (await self.post("/product")).get("product", {})

    async def control(self, cmd, args):
        async with self.lock:
            data = await self.post("/control", self._auth(payload={"cmd": cmd, "args": args}))
        if data.get("code") != 0:
            raise PrinterError(f"{cmd}: {data.get('message')} (code {data.get('code')})")
        return data

    async def gcode_list(self):
        return await self.post("/gcodeList")

    async def gcode_thumb(self, name):
        data = await self.post("/gcodeThumb", self._auth(fileName=name))
        img = data.get("imageData")
        return base64.b64decode(img) if img else None

    async def current_thumb(self):
        async with self.session.get(self.base + "/getThum") as r:
            if r.status != 200:
                return None, None
            return await r.read(), r.headers.get("Content-Type", "image/bmp")

    async def print_file(self, body):
        async with self.lock:
            data = await self.post("/printGcode", self._auth(**body), timeout=30)
        if data.get("code") != 0:
            raise PrinterError(f"printGcode: {data.get('message')} (code {data.get('code')})")
        return data

    async def upload(self, path, filename, opts):
        size = os.path.getsize(path)
        headers = {
            "serialNumber": self.serial, "checkCode": self.code, "fileSize": str(size),
            "printNow": _bool(opts.get("printNow")),
            "levelingBeforePrint": _bool(opts.get("levelingBeforePrint")),
            "flowCalibration": _bool(opts.get("flowCalibration")),
            "timeLapseVideo": _bool(opts.get("timeLapseVideo")),
            "useMatlStation": _bool(opts.get("useMatlStation")),
            "gcodeToolCnt": str(max(1, min(4, int(opts.get("gcodeToolCnt") or 1)))),
            "Expect": "100-continue",
        }
        with open(path, "rb") as f:
            form = aiohttp.FormData()
            form.add_field("gcodeFile", f, filename=filename, content_type="application/octet-stream")
            async with self.lock:
                async with self.session.post(self.base + "/uploadGcode", data=form, headers=headers,
                                             timeout=aiohttp.ClientTimeout(total=1800)) as r:
                    text = await r.text()
        try:
            data = json.loads(text)
        except ValueError:
            raise PrinterError(f"upload: HTTP {r.status}, non-JSON reply")
        if data.get("code") != 0:
            raise PrinterError(f"upload: {data.get('message')} (code {data.get('code')})")
        return data


def _bool(v):
    return "true" if v in (True, "true", "on", "1", 1) else "false"


# ---------------------------------------------------------------- camera relay

class CameraRelay:
    """One upstream MJPEG connection, fanned out to any number of viewers."""

    def __init__(self, url):
        self.url = url
        self.frame = None
        self.frame_ts = 0.0
        self.cond = asyncio.Condition()
        self.viewers = 0
        self.task = None
        self.last_viewer_left = 0.0
        self.error = None

    def ensure_running(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run())

    async def _run(self):
        backoff = 1
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_read=15)) as s:
            while True:
                if self.viewers == 0 and time.time() - self.last_viewer_left > CAMERA_IDLE_STOP_SECONDS:
                    return
                try:
                    async with s.get(self.url) as r:
                        buf = b""
                        async for chunk in r.content.iter_chunked(65536):
                            buf += chunk
                            while True:
                                start = buf.find(b"\xff\xd8")
                                end = buf.find(b"\xff\xd9", start + 2) if start >= 0 else -1
                                if start < 0 or end < 0:
                                    if start > 0:
                                        buf = buf[start:]
                                    break
                                frame, buf = buf[start:end + 2], buf[end + 2:]
                                async with self.cond:
                                    self.frame, self.frame_ts, self.error = frame, time.time(), None
                                    self.cond.notify_all()
                            if len(buf) > 4 * 1024 * 1024:
                                buf = b""
                            if self.viewers == 0 and time.time() - self.last_viewer_left > CAMERA_IDLE_STOP_SECONDS:
                                return
                        backoff = 1
                except Exception as e:
                    self.error = str(e) or e.__class__.__name__
                    log.warning("camera upstream: %s", self.error)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 15)

    async def next_frame(self, after_ts, timeout=10):
        async with self.cond:
            await asyncio.wait_for(self.cond.wait_for(lambda: self.frame_ts > after_ts), timeout)
            return self.frame, self.frame_ts


# ---------------------------------------------------------------- app state

class Dash:
    def __init__(self, cfg):
        self.cfg = cfg
        self.store = Store()
        self.printer = Printer(cfg)
        self.camera = CameraRelay(self.printer.camera_url)
        self.state = {}
        self.product = {}
        self.online = False
        self.failures = 0
        self.last_ok = 0
        self.last_sample = 0
        self.last_status = None
        self.subscribers = set()
        self.files_cache = (0, None)
        self.thumb_cache = {}
        self.job = self.store.db.execute(
            "SELECT id, file FROM jobs WHERE ended IS NULL ORDER BY id DESC LIMIT 1").fetchone()

    # ---- notifications
    async def notify(self, kind, title, message):
        self.store.event(kind, message)
        settings = self.store.kv_get("notify", {"targets": []})
        for t in settings.get("targets", []):
            if kind not in t.get("events", NOTIFY_EVENTS) or not t.get("url"):
                continue
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
                    if t.get("type") == "ntfy":
                        await s.post(t["url"], data=message.encode(),
                                     headers={"Title": title, "Tags": "printer",
                                              "Priority": "high" if kind in ("print_error", "printer_offline") else "default"})
                    else:
                        await s.post(t["url"], json={"event": kind, "title": title, "message": message,
                                                     "printer": self.state.get("name"), "ts": int(time.time())})
            except Exception as e:
                self.store.event("notify_failed", f"{t.get('url')}: {e}")

    # ---- job tracking
    async def track(self, d):
        status = d.get("status")
        prev = self.last_status
        name = d.get("printFileName") or ""
        if prev is not None and status != prev:
            self.store.event("status", f"{prev} → {status}")
            if status in ("pause", "paused") and prev not in ("pause", "paused", "pausing"):
                await self.notify("print_paused", "Print paused", f"{name} paused at {d.get('printProgress', 0) * 100:.0f}%")
            if prev in ("pause", "paused") and status == "printing":
                await self.notify("print_resumed", "Print resumed", name)
            if status == "error" or d.get("errorCode"):
                await self.notify("print_error", "Printer error", f"status={status} errorCode={d.get('errorCode') or '-'} ({name})")
        self.last_status = status

        if status == "printing" and name and (self.job is None or self.job[1] != name):
            if self.job:
                self._close_job("superseded", d)
            est = int(d.get("estimatedTime") or 0) + int(d.get("printDuration") or 0)
            cur = self.store.db.execute(
                "INSERT INTO jobs(file, started, est_seconds) VALUES (?,?,?)",
                (name, int(time.time()) - int(d.get("printDuration") or 0), est))
            self.job = (cur.lastrowid, name)
            thumb, _ = await self._safe(self.printer.current_thumb(), (None, None))
            if thumb:
                self.store.db.execute("UPDATE jobs SET thumb=? WHERE id=?", (thumb, self.job[0]))
            files = await self._safe(self.printer.gcode_list(), {})
            for f in files.get("gcodeListDetail") or []:
                if f.get("gcodeFileName") == name:
                    self.store.db.execute("UPDATE jobs SET filament_g=? WHERE id=?",
                                          (f.get("totalFilamentWeight"), self.job[0]))
            await self.notify("print_started", "Print started", name)
        elif self.job and status not in ACTIVE_STATES:
            progress = d.get("printProgress") or 0
            result = {"completed": "completed", "cancel": "cancelled", "cancelled": "cancelled",
                      "error": "error", "failed": "error"}.get(status) or (
                "completed" if progress >= 0.99 else "cancelled")
            self._close_job(result, d)
            kind = {"completed": "print_completed", "cancelled": "print_cancelled"}.get(result, "print_error")
            await self.notify(kind, f"Print {result}", f"{self.job_name_closed}")
        elif self.job:
            self.store.db.execute("UPDATE jobs SET progress=?, layers=? WHERE id=?",
                                  (d.get("printProgress"), d.get("printLayer"), self.job[0]))

    def _close_job(self, result, d):
        self.job_name_closed = self.job[1]
        self.store.db.execute("UPDATE jobs SET ended=?, result=? WHERE id=?",
                              (int(time.time()), result, self.job[0]))
        self.job = None

    async def _safe(self, coro, default):
        try:
            return await coro
        except Exception:
            return default

    # ---- sampling
    def sample(self, d):
        now = int(time.time())
        if now - self.last_sample < SAMPLE_SECONDS:
            return
        self.last_sample = now
        n = (d.get("nozzleTemps") or [None] * 4) + [None] * 4
        nt = (d.get("nozzleTargetTemps") or [None] * 4) + [None] * 4
        chamber = d.get("chamberTemp")
        chamber = None if chamber is not None and chamber <= -100 else chamber
        self.store.db.execute(
            "INSERT OR REPLACE INTO samples VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now, d.get("status"), n[0], n[1], n[2], n[3], nt[0], nt[1], nt[2], nt[3],
             d.get("platTemp"), d.get("platTargetTemp"), chamber, d.get("chamberTargetTemp"),
             d.get("chamberFanSpeed"), d.get("coolingFanSpeed"), d.get("printSpeedAdjust"),
             d.get("printProgress"), d.get("printLayer"), d.get("tvoc")))

    # ---- poll loop
    async def poll_forever(self):
        prune_at = 0
        while True:
            try:
                d = await self.printer.detail()
                self.state = d
                self.failures = 0
                self.last_ok = time.time()
                if not self.online:
                    self.online = True
                    if not self.product:
                        self.product = await self._safe(self.printer.product(), {})
                    await self.notify("printer_online", "Printer online", f"{d.get('name')} is reachable")
                self.sample(d)
                await self.track(d)
            except Exception as e:
                self.failures += 1
                if self.online and self.failures >= OFFLINE_AFTER_FAILURES:
                    self.online = False
                    await self.notify("printer_offline", "Printer offline", f"No response from printer: {e}")
            self.broadcast()
            if time.time() > prune_at:
                self.store.prune()
                prune_at = time.time() + 3600
            await asyncio.sleep(POLL_SECONDS)

    def snapshot(self):
        return {"online": self.online, "lastOk": self.last_ok, "detail": self.state,
                "product": self.product, "job": self.job[0] if self.job else None,
                "cameraViewers": self.camera.viewers, "serverTime": time.time()}

    def broadcast(self):
        payload = json.dumps(self.snapshot())
        for q in list(self.subscribers):
            if q.qsize() < 5:
                q.put_nowait(payload)


# ---------------------------------------------------------------- HTTP handlers

routes = web.RouteTableDef()


def dash(request) -> Dash:
    return request.app["dash"]


def ok(data=None):
    return web.json_response({"ok": True, **(data or {})})


def fail(msg, status=400):
    return web.json_response({"ok": False, "error": msg}, status=status)


async def command(request, label, coro):
    d = dash(request)
    src = request.remote or "?"
    try:
        await coro
    except PrinterError as e:
        d.store.event("command_failed", f"{label}: {e}", src)
        return fail(str(e), 502)
    except Exception as e:
        d.store.event("command_failed", f"{label}: {e}", src)
        return fail(f"printer unreachable: {e}", 504)
    d.store.event("command", label, src)
    return ok()


def _asset_version():
    """Short hash of the static files' mtimes/sizes; changes whenever any asset is redeployed."""
    h = 0
    for root, _, files in os.walk(STATIC_DIR):
        for f in sorted(files):
            st = os.stat(os.path.join(root, f))
            h = (h * 31 + int(st.st_mtime_ns) + st.st_size) % (1 << 48)
    return format(h, "x")


@routes.get("/")
async def index(request):
    # Asset URLs carry ?v=<version> so an embedded browser (Orca's WebView2) can never pair a
    # new index.html with a stale cached app.js/style.css.
    with open(os.path.join(STATIC_DIR, "index.html")) as f:
        html = f.read().replace("{{v}}", _asset_version())
    return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-cache"})


@web.middleware
async def static_no_cache(request, handler):
    resp = await handler(request)
    if request.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


@routes.get("/api/state")
async def api_state(request):
    return web.json_response(dash(request).snapshot())


@routes.get("/api/events/stream")
async def api_stream(request):
    d = dash(request)
    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache",
                                       "X-Accel-Buffering": "no"})
    await resp.prepare(request)
    q = asyncio.Queue()
    d.subscribers.add(q)
    try:
        await resp.write(f"data: {json.dumps(d.snapshot())}\n\n".encode())
        while True:
            try:
                payload = await asyncio.wait_for(q.get(), 20)
                await resp.write(f"data: {payload}\n\n".encode())
            except asyncio.TimeoutError:
                await resp.write(b": keepalive\n\n")
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        d.subscribers.discard(q)
    return resp


@routes.get("/api/history")
async def api_history(request):
    since = max(60, min(int(request.query.get("seconds", 3600)), SAMPLE_RETENTION_DAYS * 86400))
    bucket = max(SAMPLE_SECONDS, since // 1200)
    cols = ["t0", "t1", "t2", "t3", "tt0", "tt1", "tt2", "tt3", "bed", "bed_t", "chamber", "chamber_t",
            "chamber_fan", "cooling_fan", "speed", "tvoc"]
    rows = dash(request).store.db.execute(
        f"SELECT (ts/{bucket})*{bucket} AS b, " + ", ".join(f"AVG({c})" for c in cols) +
        " FROM samples WHERE ts >= ? GROUP BY b ORDER BY b", (int(time.time()) - since,)).fetchall()
    out = {"ts": [r[0] for r in rows], "bucket": bucket}
    for i, c in enumerate(cols):
        out[c] = [None if r[i + 1] is None else round(r[i + 1], 1) for r in rows]
    return web.json_response(out)


@routes.get("/api/utilization")
async def api_utilization(request):
    days = max(1, min(int(request.query.get("days", 30)), SAMPLE_RETENTION_DAYS))
    rows = dash(request).store.db.execute(
        "SELECT date(ts, 'unixepoch', 'localtime') d, "
        f"SUM(CASE WHEN status='printing' THEN {SAMPLE_SECONDS} ELSE 0 END), "
        f"SUM(CASE WHEN status IN ('pause','paused','pausing') THEN {SAMPLE_SECONDS} ELSE 0 END), "
        f"COUNT(*)*{SAMPLE_SECONDS} FROM samples WHERE ts >= ? GROUP BY d ORDER BY d",
        (int(time.time()) - days * 86400,)).fetchall()
    return web.json_response({"days": [{"date": r[0], "printing": r[1], "paused": r[2], "observed": r[3]} for r in rows]})


@routes.get("/api/jobs")
async def api_jobs(request):
    rows = dash(request).store.db.execute(
        "SELECT id, file, started, ended, result, progress, layers, est_seconds, filament_g, thumb IS NOT NULL "
        "FROM jobs ORDER BY id DESC LIMIT 200").fetchall()
    keys = ["id", "file", "started", "ended", "result", "progress", "layers", "estSeconds", "filamentG", "hasThumb"]
    return web.json_response({"jobs": [dict(zip(keys, r)) for r in rows]})


@routes.get("/api/jobs/{id}/thumb")
async def api_job_thumb(request):
    row = dash(request).store.db.execute("SELECT thumb FROM jobs WHERE id=?", (int(request.match_info["id"]),)).fetchone()
    if not row or not row[0]:
        raise web.HTTPNotFound()
    return web.Response(body=row[0], content_type=_img_type(row[0]), headers={"Cache-Control": "max-age=86400"})


@routes.delete("/api/jobs/{id}")
async def api_job_delete(request):
    dash(request).store.db.execute("DELETE FROM jobs WHERE id=?", (int(request.match_info["id"]),))
    return ok()


@routes.get("/api/events")
async def api_events(request):
    limit = min(int(request.query.get("limit", 200)), 1000)
    rows = dash(request).store.db.execute(
        "SELECT ts, kind, message, source FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return web.json_response({"events": [dict(zip(["ts", "kind", "message", "source"], r)) for r in rows]})


@routes.get("/api/files")
async def api_files(request):
    d = dash(request)
    ts, data = d.files_cache
    if data is None or time.time() - ts > 30 or request.query.get("refresh"):
        try:
            data = await d.printer.gcode_list()
        except Exception as e:
            return fail(f"printer unreachable: {e}", 504)
        d.files_cache = (time.time(), data)
    details = {f.get("gcodeFileName"): f for f in data.get("gcodeListDetail") or []}
    files = []
    for name in data.get("gcodeList") or []:
        f = details.get(name, {})
        files.append({"name": name, "printingTime": f.get("printingTime"),
                      "filamentG": f.get("totalFilamentWeight"), "useMatlStation": f.get("useMatlStation"),
                      "toolCnt": f.get("gcodeToolCnt"), "tools": f.get("gcodeToolDatas") or []})
    return web.json_response({"files": files})


def _img_type(b):
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if b[:2] == b"\xff\xd8":
        return "image/jpeg"
    if b[:2] == b"BM":
        return "image/bmp"
    return "application/octet-stream"


@routes.get("/api/files/thumb")
async def api_file_thumb(request):
    d = dash(request)
    name = request.query.get("name", "")
    img = d.thumb_cache.get(name)
    if img is None:
        try:
            img = await d.printer.gcode_thumb(name)
        except Exception:
            img = None
        if img:
            d.thumb_cache[name] = img
    if not img:
        raise web.HTTPNotFound()
    return web.Response(body=img, content_type=_img_type(img), headers={"Cache-Control": "max-age=3600"})


@routes.get("/api/thumb/current")
async def api_current_thumb(request):
    try:
        img, _ = await dash(request).printer.current_thumb()
    except Exception:
        img = None
    if not img:
        raise web.HTTPNotFound()
    return web.Response(body=img, content_type=_img_type(img), headers={"Cache-Control": "no-cache"})


@routes.post("/api/job/{action}")
async def api_job(request):
    action = {"pause": "pause", "resume": "continue", "cancel": "cancel"}.get(request.match_info["action"])
    if not action:
        return fail("unknown action")
    return await command(request, f"job {request.match_info['action']}",
                         dash(request).printer.control("jobCtl_cmd", {"jobID": "", "action": action}))


@routes.post("/api/clear-platform")
async def api_clear_platform(request):
    return await command(request, "clear platform",
                         dash(request).printer.control("stateCtrl_cmd", {"action": "setClearPlatform"}))


@routes.post("/api/light")
async def api_light(request):
    on = (await request.json()).get("on")
    return await command(request, f"light {'on' if on else 'off'}",
                         dash(request).printer.control("lightControl_cmd", {"status": "open" if on else "close"}))


@routes.post("/api/camera")
async def api_camera(request):
    on = (await request.json()).get("on")
    return await command(request, f"camera {'on' if on else 'off'}",
                         dash(request).printer.control("streamCtrl_cmd", {"action": "open" if on else "close"}))


@routes.post("/api/filtration")
async def api_filtration(request):
    body = await request.json()
    args = {"internal": "open" if body.get("internal") else "close",
            "external": "open" if body.get("external") else "close"}
    return await command(request, f"filtration internal={args['internal']} external={args['external']}",
                         dash(request).printer.control("circulateCtl_cmd", args))


@routes.post("/api/temp")
async def api_temp(request):
    """{"heater": "tool"|"bed"|"chamber", "index": 0-3 (tool only), "value": °C, 0 = off}"""
    body = await request.json()
    heater, value = body.get("heater"), body.get("value")
    if heater not in LIMITS:
        return fail("heater must be tool, bed or chamber")
    try:
        value = int(round(float(value)))
    except (TypeError, ValueError):
        return fail("value must be a number")
    lo, hi = LIMITS[heater]
    if not (value == 0 or lo <= value <= hi):
        return fail(f"{heater} target must be 0 (off) or {lo}-{hi}°C")
    args = {"rightNozzle": TEMP_NO_CHANGE, "leftNozzle": TEMP_NO_CHANGE, "platform": TEMP_NO_CHANGE,
            "chamber": TEMP_NO_CHANGE, "nozzles": [TEMP_NO_CHANGE] * 4}
    if heater == "tool":
        idx = int(body.get("index", -1))
        if not 0 <= idx <= 3:
            return fail("index must be 0-3")
        args["nozzles"][idx] = value if value > 0 else NOZZLE_OFF
        label = f"T{idx} target {value}°C"
    else:
        key = "platform" if heater == "bed" else "chamber"
        args[key] = value if value > 0 else TEMP_OFF_SCALAR
        label = f"{heater} target {value}°C"
    return await command(request, label, dash(request).printer.control("temperatureCtl_cmd", args))


@routes.post("/api/printer-ctl")
async def api_printer_ctl(request):
    """Merge the requested change(s) into the current values: printerCtl_cmd sets all fields at once."""
    d = dash(request)
    body = await request.json()
    s = d.state
    if s.get("status") != "printing":
        return fail("speed / Z offset / fan changes are only accepted by the printer while a print is running", 409)
    args = {"zAxisCompensation": float(s.get("zAxisCompensation") or 0),
            "speed": int(s.get("printSpeedAdjust") or 100),
            "chamberFan": int(s.get("chamberFanSpeed") or 0),
            "coolingFan": int(s.get("coolingFanSpeed") or 0),
            "coolingLeftFan": 0}
    changes = []
    if "speed" in body:
        args["speed"] = max(10, min(300, int(body["speed"])))
        changes.append(f"speed {args['speed']}%")
    if "zOffset" in body:
        args["zAxisCompensation"] = round(max(-2.0, min(2.0, float(body["zOffset"]))), 3)
        changes.append(f"Z offset {args['zAxisCompensation']:+.3f}mm")
    if "chamberFan" in body:
        args["chamberFan"] = max(0, min(100, int(body["chamberFan"])))
        changes.append(f"chamber fan {args['chamberFan']}%")
    if "coolingFan" in body:
        args["coolingFan"] = max(0, min(100, int(body["coolingFan"])))
        changes.append(f"cooling fan {args['coolingFan']}%")
    if not changes:
        return fail("nothing to change")
    return await command(request, ", ".join(changes), d.printer.control("printerCtl_cmd", args))


@routes.post("/api/slot")
async def api_slot(request):
    body = await request.json()
    slot = int(body.get("slot", 0))
    rgb = body.get("rgb", "")
    material = str(body.get("material", "")).strip()[:16]
    if not 1 <= slot <= 4 or rgb not in C5_PALETTE.values() or not material:
        return fail("slot 1-4, a palette colour and a material name are required")
    return await command(request, f"slot {slot} = {material} {rgb}",
                         dash(request).printer.control("msConfig_cmd", {"slot": slot, "mt": material, "rgb": rgb}))


@routes.post("/api/print")
async def api_print(request):
    body = await request.json()
    name = body.get("fileName")
    if not name:
        return fail("fileName required")
    payload = {"fileName": name, "levelingBeforePrint": bool(body.get("levelingBeforePrint")),
               "flowCalibration": bool(body.get("flowCalibration")),
               "timeLapseVideo": bool(body.get("timeLapseVideo"))}
    mappings = body.get("materialMappings") or []
    if mappings:
        payload["materialMappings"] = [
            {"toolId": int(m["toolId"]), "slotId": int(m["slotId"]), "materialName": m["materialName"],
             "toolMaterialColor": m["toolMaterialColor"], "slotMaterialColor": m["slotMaterialColor"]}
            for m in mappings]
    return await command(request, f"start print {name} ({len(mappings)} tool mapping(s))",
                         dash(request).printer.print_file(payload))


@routes.post("/api/upload")
async def api_upload(request):
    d = dash(request)
    reader = await request.multipart()
    opts, tmp, filename = {}, None, None
    try:
        while True:
            part = await reader.next()
            if part is None:
                break
            if part.name == "file":
                filename = os.path.basename(part.filename or "upload.gcode")
                if not re.search(r"\.(gcode|gx|3mf)$", filename, re.I):
                    return fail("only .gcode, .gx or .3mf files")
                fd, tmp = tempfile.mkstemp(dir="/var/lib/printer-dash")
                with os.fdopen(fd, "wb") as f:
                    while chunk := await part.read_chunk(1 << 20):
                        f.write(chunk)
            else:
                opts[part.name] = (await part.text()).strip()
        if not tmp:
            return fail("no file")
        d.files_cache = (0, None)
        return await command(request, f"upload {filename} (printNow={_bool(opts.get('printNow'))})",
                             d.printer.upload(tmp, filename, opts))
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)


@routes.get("/api/settings")
async def api_settings(request):
    s = dash(request).store
    return web.json_response({"presets": s.kv_get("presets", DEFAULT_PRESETS),
                              "notify": s.kv_get("notify", {"targets": []}),
                              "notifyEvents": NOTIFY_EVENTS, "palette": C5_PALETTE, "limits": LIMITS})


@routes.put("/api/settings/{key}")
async def api_settings_put(request):
    key = request.match_info["key"]
    if key not in ("presets", "notify"):
        return fail("unknown setting")
    dash(request).store.kv_set(key, await request.json())
    dash(request).store.event("settings", f"updated {key}", request.remote or "?")
    return ok()


@routes.post("/api/notify/test")
async def api_notify_test(request):
    await dash(request).notify("test", "Test notification", "printer-dash notification test")
    return ok()


@routes.get("/camera/stream")
async def camera_stream(request):
    d = dash(request)
    cam = d.camera
    cam.viewers += 1
    cam.ensure_running()
    resp = web.StreamResponse(headers={"Content-Type": "multipart/x-mixed-replace; boundary=frame",
                                       "Cache-Control": "no-cache, private", "Pragma": "no-cache"})
    await resp.prepare(request)
    last = 0.0
    try:
        while True:
            try:
                frame, last = await cam.next_frame(last, timeout=15)
            except asyncio.TimeoutError:
                continue
            await resp.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                             + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n")
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        cam.viewers -= 1
        cam.last_viewer_left = time.time()
    return resp


@routes.get("/camera/snapshot")
async def camera_snapshot(request):
    cam = dash(request).camera
    if time.time() - cam.frame_ts > 5:
        cam.last_viewer_left = time.time()
        cam.ensure_running()
        try:
            await cam.next_frame(cam.frame_ts, timeout=10)
        except asyncio.TimeoutError:
            return fail(f"no camera frame ({cam.error or 'timeout'})", 504)
    return web.Response(body=cam.frame, content_type="image/jpeg", headers={"Cache-Control": "no-cache"})


@routes.get("/metrics")
async def metrics(request):
    d = dash(request)
    s = d.state
    lines = []

    def g(name, value, labels="", help_=None):
        if value is None:
            return
        if help_:
            lines.append(f"# HELP printer_{name} {help_}")
            lines.append(f"# TYPE printer_{name} gauge")
        lines.append(f"printer_{name}{{{labels}}} {float(value)}")

    g("up", 1 if d.online else 0, help_="1 if the printer API answered recently")
    if s:
        lines.append("# HELP printer_nozzle_temp_celsius Nozzle temperature per tool")
        lines.append("# TYPE printer_nozzle_temp_celsius gauge")
        for i, (t, tt) in enumerate(zip(s.get("nozzleTemps") or [], s.get("nozzleTargetTemps") or [])):
            lines.append(f'printer_nozzle_temp_celsius{{tool="T{i}"}} {float(t)}')
            lines.append(f'printer_nozzle_target_celsius{{tool="T{i}"}} {float(tt)}')
        g("bed_temp_celsius", s.get("platTemp"), help_="Bed temperature")
        g("bed_target_celsius", s.get("platTargetTemp"))
        ch = s.get("chamberTemp")
        g("chamber_temp_celsius", None if ch is not None and ch <= -100 else ch, help_="Chamber temperature")
        g("chamber_target_celsius", s.get("chamberTargetTemp"))
        g("chamber_fan_percent", s.get("chamberFanSpeed"), help_="Chamber fan speed")
        g("cooling_fan_percent", s.get("coolingFanSpeed"), help_="Part cooling fan speed")
        g("speed_factor_percent", s.get("printSpeedAdjust"), help_="Print speed override")
        g("progress_ratio", s.get("printProgress"), help_="Current job progress 0-1")
        g("layer", s.get("printLayer"))
        g("layers_total", s.get("targetPrintLayer"))
        g("eta_seconds", s.get("estimatedTime"))
        g("print_duration_seconds", s.get("printDuration"))
        g("tvoc", s.get("tvoc"), help_="Air quality (TVOC) reading")
        g("door_open", 1 if s.get("doorStatus") == "open" else 0)
        g("light_on", 1 if s.get("lightStatus") == "open" else 0)
        g("disk_free_gb", s.get("remainingDiskSpace"))
        g("cumulative_filament_meters", s.get("cumulativeFilament"))
        g("cumulative_print_seconds", s.get("cumulativePrintTime"))
        lines.append("# HELP printer_status Current printer status (1 for the active one)")
        lines.append("# TYPE printer_status gauge")
        lines.append(f'printer_status{{status="{s.get("status", "unknown")}"}} 1')
        lines.append(f'printer_info{{model="{s.get("model", "")}",firmware="{s.get("firmwareVersion", "")}"}} 1')
    return web.Response(text="\n".join(lines) + "\n", content_type="text/plain")


# ---------------------------------------------------------------- main

async def on_startup(app):
    d = app["dash"]
    await d.printer.start()
    d.store.event("service", "printer-dash started")
    app["poller"] = asyncio.create_task(d.poll_forever())


async def on_cleanup(app):
    app["poller"].cancel()
    await app["dash"].printer.session.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    app = web.Application(client_max_size=1024 ** 3, middlewares=[static_no_cache])
    app["dash"] = Dash(cfg)
    app.add_routes(routes)
    app.router.add_static("/static/", STATIC_DIR, append_version=True)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    web.run_app(app, host="0.0.0.0", port=int(cfg.get("port", 80)), access_log=None)


if __name__ == "__main__":
    main()
