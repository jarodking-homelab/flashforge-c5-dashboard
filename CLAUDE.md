# printer-dash: notes for whoever works on this code (human or model)

Dashboard for the Flashforge Creator 5 Pro in LAN mode. One Python file (`server.py`, aiohttp)
plus a static frontend. `README.md` covers what it does and how to install it. Homelab-wide
rules (risk tiers, who merges, tracker) are in the `infra` repo, `agent/CLAUDE.md`.

| What | Where |
|---|---|
| Main copy | Forgejo `homelab/flashforge-c5-dashboard`; GitHub is a mirror, never push there by hand |
| Running copy | CT105 `printer-dash`, 192.168.4.73, `printer-dash.service`, config `/etc/printer-dash/config.json` |
| Printer | 192.168.4.44 (API :8898, camera :8080) |
| Checks | `sh ci/test.sh` (ruff, then pytest); the pipeline runs the same script |

## Rules
- Branch, commit as `hl-xxx: what changed`, pull request to `main`, green checks.
- **The control endpoints can heat things and move a running print.** Any change to
  `api_temp`, `api_printer_ctl`, `api_print`, `api_upload` or the `Printer` class comes with a
  test in `tests/test_controls.py`, and is tried on the real printer while it is idle before
  it is deployed. Deploying while a print is running needs Jarod's OK.
- The protocol traps are in `PROTOCOL NOTES` at the top of `server.py`: nozzle off is `0` not
  `-100`; `printerCtl_cmd` sets all its fields at once; there is no homing or raw G-code.
- The printer's serial number and check code live only in the config file on CT105. Jarod
  regards them as non-sensitive, but they still do not go into this repo, which is meant to
  become public.
- Comments say why, not what.
