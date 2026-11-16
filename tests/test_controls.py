"""Tests for the control endpoints: the code between a browser click and a heater.

The printer protocol has traps (see PROTOCOL NOTES in server.py) and a wrong value here means a
nozzle that does not switch off or a bed set past its limit, so these rules are pinned by tests.
No printer and no database are needed: the Dash object is replaced by a recorder.
"""
import asyncio
import sys
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server  # noqa: E402

UNCHANGED = server.TEMP_NO_CHANGE


class FakePrinter:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    async def control(self, cmd, args):
        self.calls.append((cmd, args))
        if self.error:
            raise self.error


class FakeStore:
    def __init__(self):
        self.events = []

    def event(self, kind, message, source="system"):
        self.events.append((kind, message))


class FakeDash:
    def __init__(self, state=None, error=None):
        self.state = state or {}
        self.printer = FakePrinter(error)
        self.store = FakeStore()


def post(path, body, dash):
    """POST body to the real route table with a fake Dash; return (status, json)."""
    async def go():
        app = web.Application()
        app["dash"] = dash
        app.add_routes(server.routes)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(path, json=body)
            return resp.status, await resp.json()
    return asyncio.run(go())


# ---- temperatures

def test_tool_target_changes_only_that_nozzle():
    dash = FakeDash()
    status, body = post("/api/temp", {"heater": "tool", "index": 2, "value": 215}, dash)
    assert (status, body["ok"]) == (200, True)
    cmd, args = dash.printer.calls[0]
    assert cmd == "temperatureCtl_cmd"
    assert args["nozzles"] == [UNCHANGED, UNCHANGED, 215, UNCHANGED]
    assert args["platform"] == args["chamber"] == UNCHANGED


def test_tool_off_is_zero_not_the_scalar_off_value():
    # Inside the nozzles array only a literal 0 switches a tool off; -100 is ignored by the printer.
    dash = FakeDash()
    post("/api/temp", {"heater": "tool", "index": 0, "value": 0}, dash)
    assert dash.printer.calls[0][1]["nozzles"][0] == 0


def test_bed_and_chamber_off_use_the_scalar_off_value():
    for heater, key in (("bed", "platform"), ("chamber", "chamber")):
        dash = FakeDash()
        post("/api/temp", {"heater": heater, "value": 0}, dash)
        args = dash.printer.calls[0][1]
        assert args[key] == server.TEMP_OFF_SCALAR
        assert args["nozzles"] == [UNCHANGED] * 4


def test_targets_over_the_limit_never_reach_the_printer():
    for heater, (_, high) in server.LIMITS.items():
        dash = FakeDash()
        status, body = post("/api/temp", {"heater": heater, "index": 0, "value": high + 1}, dash)
        assert status == 400 and body["ok"] is False
        assert dash.printer.calls == []


def test_limit_itself_is_accepted():
    dash = FakeDash()
    status, _ = post("/api/temp", {"heater": "bed", "value": server.LIMITS["bed"][1]}, dash)
    assert status == 200
    assert dash.printer.calls[0][1]["platform"] == server.LIMITS["bed"][1]


def test_bad_temperature_requests_are_refused():
    bad = [
        {"heater": "laser", "value": 50},
        {"heater": "tool", "index": 4, "value": 200},
        {"heater": "tool", "value": 200},          # no index
        {"heater": "bed", "value": "hot"},
        {"heater": "bed"},
        {"heater": "bed", "value": -5},
    ]
    for body in bad:
        dash = FakeDash()
        status, _ = post("/api/temp", body, dash)
        assert status == 400, body
        assert dash.printer.calls == [], body


# ---- speed, Z offset, fans

PRINTING = {"status": "printing", "zAxisCompensation": 0.05, "printSpeedAdjust": 100,
            "chamberFanSpeed": 40, "coolingFanSpeed": 80}


def test_print_controls_refused_when_not_printing():
    dash = FakeDash(state={"status": "ready"})
    status, _ = post("/api/printer-ctl", {"speed": 120}, dash)
    assert status == 409
    assert dash.printer.calls == []


def test_changing_one_value_resends_the_others_unchanged():
    # printerCtl_cmd sets every field at once: leaving one out would reset it on the printer.
    dash = FakeDash(state=dict(PRINTING))
    status, _ = post("/api/printer-ctl", {"speed": 120}, dash)
    assert status == 200
    cmd, args = dash.printer.calls[0]
    assert cmd == "printerCtl_cmd"
    assert args == {"zAxisCompensation": 0.05, "speed": 120, "chamberFan": 40,
                    "coolingFan": 80, "coolingLeftFan": 0}


def test_print_controls_are_clamped():
    dash = FakeDash(state=dict(PRINTING))
    post("/api/printer-ctl", {"speed": 999, "zOffset": -9, "chamberFan": 150, "coolingFan": -3}, dash)
    args = dash.printer.calls[0][1]
    assert (args["speed"], args["zAxisCompensation"], args["chamberFan"], args["coolingFan"]) == (300, -2.0, 100, 0)


def test_empty_change_is_refused():
    dash = FakeDash(state=dict(PRINTING))
    status, _ = post("/api/printer-ctl", {}, dash)
    assert status == 400
    assert dash.printer.calls == []


# ---- what the user is told when the printer says no

def test_printer_refusal_is_reported_and_logged():
    dash = FakeDash(error=server.PrinterError("temperatureCtl_cmd: busy (code 1)"))
    status, body = post("/api/temp", {"heater": "bed", "value": 60}, dash)
    assert status == 502 and "busy" in body["error"]
    assert dash.store.events[0][0] == "command_failed"


def test_unreachable_printer_is_reported_and_logged():
    dash = FakeDash(error=OSError("connection refused"))
    status, body = post("/api/temp", {"heater": "bed", "value": 60}, dash)
    assert status == 504 and "unreachable" in body["error"]
    assert dash.store.events[0][0] == "command_failed"


def test_successful_command_is_written_to_the_audit_log():
    dash = FakeDash()
    post("/api/temp", {"heater": "bed", "value": 60}, dash)
    assert dash.store.events == [("command", "bed target 60°C")]


# ---- small helpers

def test_bool_header_values():
    assert [server._bool(v) for v in (True, "true", "on", "1", 1)] == ["true"] * 5
    assert [server._bool(v) for v in (False, None, "", "no", 0)] == ["false"] * 5


def test_image_type_sniffing():
    assert server._img_type(b"\x89PNG\r\n\x1a\n....") == "image/png"
    assert server._img_type(b"\xff\xd8\xff\xe0") == "image/jpeg"
    assert server._img_type(b"BM\x00\x00") == "image/bmp"
    assert server._img_type(b"GIF89a") == "application/octet-stream"
