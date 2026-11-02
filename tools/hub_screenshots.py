"""Screenshots of the hub web dashboard with demo data.

    python tools/hub_screenshots.py      (needs node + python playwright with chromium)

Starts a throw-away hub, adds four sims, sends realistic reports, writes a week of heartbeat samples, then
captures the pages to docs/screenshots/hub-*.png.
"""
import http.cookiejar
import json
import os
import random
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "screenshots"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def start(data, port):
    env = dict(os.environ, PORT=str(port), HOST="127.0.0.1", SCM_HUB_DATA=str(data), SCM_HUB_TITLE="Sim Center",
               SCM_HUB_CONFIG=str(data / "config.json"))
    p = subprocess.Popen([shutil.which("node"), "src/server.js"], cwd=str(ROOT / "hub"), env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
            return p
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("hub did not start")


GPU = "NVIDIA GeForce RTX 4080\n  Driver version : {drv}\n  Driver date    : 2024-10-04\n\nnvidia-smi:\nNVIDIA GeForce RTX 4080, {smi}, 95.03.1C.00.95, 4\n"
WIN = "Microsoft Windows 11 Pro\nVersion 23H2  build 22631.4317\nEdition Professional\nArchitecture 64-bit\n"
FILES = [("Prepar3D.cfg", "P3D v5"), ("dll.xml", "P3D v5"), ("exe.xml", "P3D v5"), ("Cameras.cfg", "P3D v5"),
         ("add-ons.cfg", "P3D v5"), ("scenery.cfg", "P3D v5"), ("Standard.xml (controls)", "P3D v5"),
         ("ProSim config.xml", "ProSim"), ("ProSim MCP config.xml", "ProSim"), ("ProSim CDU config.xml", "ProSim"),
         ("Graphics cards & driver", "System"), ("Windows version", "System"), ("Displays / projectors", "System")]


def report(name, version, differ=(), violations=(), alerts=(), pending=0, drv="32.0.15.6603", smi="566.03"):
    now = time.time()
    st = {f: "match" for f, _ in FILES}
    for f, s in differ:
        st[f] = s
    rb = [{"label": "Prepar3D.cfg: GRAPHICS/TEXTURE_MAX_LOAD", "required": "4096",
           "current": "2048" if "Prepar3D.cfg: GRAPHICS/TEXTURE_MAX_LOAD" in violations else "4096",
           "ok": "Prepar3D.cfg: GRAPHICS/TEXTURE_MAX_LOAD" not in violations,
           "note": "Certified value — lower causes blurry ground textures"},
          {"label": "ProSim config.xml: .//Server", "required": "192.168.10.20", "current": "192.168.10.20", "ok": True,
           "note": "ProSim server PC (fixed IP)"},
          {"label": "Prepar3D.cfg: TERRAIN/LOD_RADIUS", "required": "", "current": "4.500000", "ok": None,
           "note": "4.5 = smooth on the projectors"}]
    texts = {"match": "Matches", "modified": "Changed", "missing": "Missing on disk", "new": "Not in version"}
    return {
        "machine": name, "app_version": "1.11.0", "heartbeat": now, "files": len(FILES),
        "version": {"number": version[0], "title": version[1], "created_at": now - 86400 * 2},
        "mismatches": [{"file": f, "group": dict(FILES)[f], "status": s} for f, s in differ],
        "alerts_24h": len(alerts), "recent_alerts": [{"ts": now - 600 * (i + 1), "message": a} for i, a in enumerate(alerts)],
        "last_change": now - 3600, "pending_proposals": pending, "rulebook_violations": list(violations),
        "system": {"gpu": GPU.format(drv=drv, smi=smi), "windows": WIN},
        "files_status": [{"file": f, "group": f"{name} – {g}", "status": st[f], "status_text": texts[st[f]]} for f, g in FILES],
        "rulebook": rb,
        "versions": [{"number": n, "title": t, "created_at": now - 86400 * (8 - n), "created_by": "Sim Tech"}
                     for n, t in [(version[0], version[1]), (2, "Lower LOD radius for smoother frame rates"),
                                  (1, "Initial certified setup")] if n <= version[0]],
        "host": {"hostname": name.replace(" ", "").upper() + "-PC", "os": "Windows-11-10.0.22631-SP0", "admin": True},
    }


def main():
    from playwright.sync_api import sync_playwright
    tmp = Path(tempfile.mkdtemp())
    data = tmp / "data"
    data.mkdir()
    (data / "config.json").write_text(json.dumps({"dataDir": ".", "title": "Sim Center", "users": [
        {"name": "admin", "password": "demo", "role": "admin"}], "kioskToken": "wall"}))
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    proc = start(data, port)
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), urllib.request.ProxyHandler({}))

    def call(path, body=None, method="GET", key=None):
        h = {"Content-Type": "application/json", "X-Requested-With": "scm"}
        if key:
            h["Authorization"] = f"Bearer {key}"
        req = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body else None,
                                     headers=h)
        with (urllib.request.build_opener(urllib.request.ProxyHandler({})) if key else op).open(req, timeout=10) as r:
            return json.loads(r.read())

    call("/api/login", {"name": "admin", "password": "demo"}, "POST")
    keys = {n: call("/api/sims", {"name": n}, "POST")["key"] for n in ("Sim 1", "Sim 2", "Sim 3", "Sim 4")}
    v3 = (3, "ProSim switched to imperial units")
    v4 = (4, "New texture limit, audio bridge disabled")
    call("/api/v1/report", report("Sim 4", v3), "POST", keys["Sim 4"])
    call("/api/v1/report", report("Sim 3", v3), "POST", keys["Sim 3"])
    call("/api/v1/report", report("Sim 2", v3), "POST", keys["Sim 2"])
    call("/api/v1/report", report("Sim 1", v3), "POST", keys["Sim 1"])
    call("/api/v1/report", report("Sim 1", v4), "POST", keys["Sim 1"])
    call("/api/v1/report", report("Sim 2", v3, differ=[("Prepar3D.cfg", "modified"), ("dll.xml", "modified")],
                                  violations=["Prepar3D.cfg: GRAPHICS/TEXTURE_MAX_LOAD"],
                                  alerts=["[Rulebook] Prepar3D.cfg: GRAPHICS/TEXTURE_MAX_LOAD = '2048', rulebook requires '4096'",
                                          "[FSUIPC must stay loaded] dll.xml: line removed: <Name>FSUIPC 6</Name>"],
                                  pending=1, drv="32.0.15.6094", smi="560.94"), "POST", keys["Sim 2"])
    sims = {s["name"]: s["id"] for s in call("/api/sims")["sims"]}
    call(f"/api/sims/{sims['Sim 3']}/maintenance", {"on": True, "note": "Projector 2 lamp replacement"}, "POST")
    for sim, text, cat in (("Sim 2", "Instructor reports blurry ground textures on approach to EHAM 18R.", "incident"),
                           ("Sim 2", "Yoke recalibrated after session, pitch was slightly off-centre.", "calibration"),
                           ("Sim 3", "Projector 2 lamp at 2 950 h — replacement ordered.", "hardware"),
                           ("Sim 1", "Updated to v4: texture limit and audio bridge per vendor advice.", "software")):
        call(f"/api/sims/{sims[sim]}/notes", {"text": text, "category": cat}, "POST")
    proc.terminate()
    proc.wait()

    # backdate Sim 4 (offline since this morning) and write a week of heartbeats
    sj = json.loads((data / "sims.json").read_text())
    now = time.time() * 1000
    random.seed(4)
    for s in sj.values():
        samples, t = [], now - 7 * 86_400_000
        while t < now:
            hour = time.localtime(t / 1000).tm_hour
            on = 7 <= hour <= 22 and random.random() > 0.03
            if s["name"] == "Sim 4" and t > now - 4 * 3_600_000:
                on = False
            if on:
                samples.append({"ts": int(t), "state": "ok"})
            t += 5 * 60_000
        (data / "samples" / f"{s['id']}.jsonl").write_text("\n".join(json.dumps(x) for x in samples) + "\n")
        if s["name"] == "Sim 4":
            s["lastSeen"] = int(now - 4 * 3_600_000)
            s["online"] = False
    (data / "sims.json").write_text(json.dumps(sj))
    ev = data / "events.jsonl"
    ev.write_text(ev.read_text() + json.dumps({"ts": int(now - 4 * 3_600_000 + 900_000), "sim": sims["Sim 4"],
                                               "simName": "Sim 4", "type": "offline", "level": "alert",
                                               "message": "Sim 4 stopped reporting"}) + "\n")

    proc = start(data, port)
    try:
        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = b.new_page(viewport={"width": 1400, "height": 860}, device_scale_factor=1, color_scheme="dark")
            pg.on("pageerror", lambda e: print("pageerror:", e))
            pg.on("console", lambda m: print("console:", m.text) if m.type == "error" else None)
            pg.goto(base + "/")
            pg.fill("input[autocomplete=username]", "admin")
            pg.fill("input[type=password]", "demo")
            pg.click("button[type=submit]")
            pg.wait_for_selector(".card")
            pg.wait_for_timeout(800)
            pg.screenshot(path=str(OUT / "hub-fleet.png"))
            pg.goto(base + f"/#/sim/{sims['Sim 2']}")
            pg.wait_for_selector(".uptime")
            pg.wait_for_timeout(500)
            pg.screenshot(path=str(OUT / "hub-sim.png"))
            pg.goto(base + "/#/board")
            pg.wait_for_selector(".tile")
            pg.wait_for_timeout(500)
            pg.screenshot(path=str(OUT / "hub-board.png"))
            pg.goto(base + "/#/events")
            pg.wait_for_selector("td")
            pg.wait_for_timeout(500)
            pg.screenshot(path=str(OUT / "hub-events.png"))
            m = b.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=2, color_scheme="dark")
            m.context.add_cookies(pg.context.cookies())
            m.goto(base + "/#/fleet")
            m.wait_for_selector(".card")
            m.wait_for_timeout(500)
            m.screenshot(path=str(OUT / "hub-phone.png"))
            b.close()
    finally:
        proc.terminate()
    print("hub screenshots saved")


if __name__ == "__main__":
    main()
