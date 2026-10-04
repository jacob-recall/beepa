#!/usr/bin/env python3
"""F0 propagation probe: send ONE canary iMessage to YOUR OWN handle and time
every hop it takes through Beepa. On demand only; never part of tests/run.py.

    python3 tests/integration/probe_propagation.py --i-am-sending-real-imessages [--leg matrix|native|both]

Target: always the self-chat (`any;-;<self_handle>` from imessage/daemon.json),
i.e. a message from you to you. No other chat, contact or teammate is ever
addressed, and nothing is changed: no consent, no settings, no deletions.

Legs:
  matrix  app -> local Synapse -> iMessage daemon -> Messages.app   (the "send" path)
          + local Synapse -> uplink -> master mirror                 (the "mirror" path)
  native  Messages.app (via the CLI) -> daemon poll -> local Synapse (the "phone-sent" path)
          + local Synapse -> uplink -> master mirror

Each hop has a budget; a miss exits 1. Output is a table plus a JSON report
(--report). Message BODIES are the probe's own canary string only; nothing
else from any room is printed or stored.
"""
import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_HS = os.environ.get("PROBE_LOCAL_HS", "http://127.0.0.1:8008")
MASTER_HS = os.environ.get("PROBE_MASTER_HS", "http://127.0.0.1:8018")
BUDGET = {"daemon_s": 30, "local_echo_s": 60, "master_s": 90}


def die(msg):
    print("probe: " + msg, file=sys.stderr)
    sys.exit(2)


def http(base, token, method, path, body=None, timeout=30):
    req = urllib.request.Request(base + path, method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Authorization": "Bearer " + token,
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode() or "{}")


def load_setup():
    s = {}
    try:
        sess = json.load(open(os.path.join(ROOT, "apps/user/session.local.json")))
        s["local_token"], s["local_user"] = sess["access_token"], sess["user_id"]
    except Exception as e:
        die("apps/user/session.local.json unreadable (%s)" % type(e).__name__)
    try:
        cfg = json.load(open(os.path.join(ROOT, "imessage/daemon.json")))
    except Exception as e:
        die("imessage/daemon.json unreadable (%s)" % type(e).__name__)
    s["self_handle"] = cfg.get("self_handle") or die("daemon.json has no self_handle")
    s["bot_id"] = cfg.get("bot_id")
    s["cli"] = cfg.get("cli_path")
    s["chat_id"] = "any;-;" + s["self_handle"]
    s["imsg_db"] = os.path.join(ROOT, "imessage/state.db")
    row = sqlite3.connect("file:%s?mode=ro" % s["imsg_db"], uri=True).execute(
        "SELECT room_id FROM map WHERE chat_id=?", (s["chat_id"],)).fetchone()
    s["room"] = row[0] if row else die("no portal room mapped for your self-chat %s" % s["chat_id"])
    s["uplink_db"] = os.path.join(ROOT, "agents/uplink/state.db")
    row = sqlite3.connect("file:%s?mode=ro" % s["uplink_db"], uri=True).execute(
        "SELECT master_room_id FROM mirror_rooms WHERE local_room_id=?", (s["room"],)).fetchone()
    s["mirror"] = row[0] if row else None
    try:
        link = http(LOCAL_HS, s["local_token"], "GET",
                    "/_matrix/client/v3/user/%s/account_data/com.jkali.master_link" % urllib.parse.quote(s["local_user"], safe=""))
        s["master_token"], s["master_user"] = link.get("master_token"), link.get("master_user")
    except Exception:
        s["master_token"] = None
    return s


def poll(fn, budget_s, every=0.5):
    t0 = time.time()
    while time.time() - t0 < budget_s:
        v = fn()
        if v:
            return v, time.time() - t0
        time.sleep(every)
    return None, time.time() - t0


def find_in_room(base, token, room, canary, want_sender=None, since_ts=0):
    def go():
        try:
            data = http(base, token, "GET", "/_matrix/client/v3/rooms/%s/messages?dir=b&limit=30" % urllib.parse.quote(room, safe=""))
        except Exception:
            return None
        for e in data.get("chunk") or []:
            c = e.get("content") or {}
            if e.get("type") == "m.room.message" and c.get("body") == canary and (e.get("origin_server_ts") or 0) >= since_ts:
                if want_sender and e.get("sender") != want_sender:
                    continue
                return e
        return None
    return go


def daemon_outcome(db_path, event_id):
    def go():
        row = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True).execute(
            "SELECT state, reason, updated FROM outbound_event WHERE event_id=?", (event_id,)).fetchone()
        return row if row and row[0] in ("confirmed", "refused", "ambiguous", "retryable") else None
    return go


def leg_matrix(s, canary, report):
    print("\n== leg: matrix (app -> daemon -> Messages; and -> uplink -> master)")
    txn = "probe_" + uuid.uuid4().hex
    t0 = time.time()
    res = http(LOCAL_HS, s["local_token"], "PUT",
               "/_matrix/client/v3/rooms/%s/send/m.room.message/%s" % (urllib.parse.quote(s["room"], safe=""), txn),
               {"msgtype": "m.text", "body": canary})
    eid = res.get("event_id")
    t_local = time.time() - t0
    print("  local hs accepted      %6.2fs  %s" % (t_local, eid))
    out, dt = poll(daemon_outcome(s["imsg_db"], eid), BUDGET["daemon_s"])
    ok_daemon = bool(out) and out[0] == "confirmed"
    print("  daemon outcome         %6.2fs  %s" % (dt, (out[0] + "/" + str(out[1])) if out else "NOT RECORDED within budget"))
    master = None
    if s["mirror"] and s["master_token"]:
        master, dtm = poll(find_in_room(MASTER_HS, s["master_token"], s["mirror"], canary, since_ts=int(t0 * 1000) - 60000), BUDGET["master_s"])
        if master:
            hops = (master.get("content") or {}).get("com.jkali.hops") or {}
            lt, ut, mt = hops.get("local_ts"), hops.get("uplink_ts"), master.get("origin_server_ts")
            extra = ""
            if isinstance(lt, int) and isinstance(ut, int) and isinstance(mt, int):
                extra = "  (local->uplink %.1fs, uplink->master %.1fs)" % ((ut - lt) / 1000, (mt - ut) / 1000)
            print("  master mirror          %6.2fs  from_me=%s%s" % (dtm, (master.get("content") or {}).get("com.jkali.from_me"), extra))
        else:
            print("  master mirror          %6.2fs  NOT MIRRORED within budget" % dtm)
    else:
        print("  master mirror           skipped (%s)" % ("self-chat not mirrored" if not s["mirror"] else "no master link"))
    report["matrix"] = {"event_id": eid, "local_s": t_local, "daemon": out[:2] if out else None, "daemon_s": dt,
                        "master_s": (dtm if s["mirror"] and s["master_token"] else None), "master_found": bool(master)}
    return ok_daemon and (master is not None or not (s["mirror"] and s["master_token"]))


def leg_native(s, canary, report):
    print("\n== leg: native (Messages.app -> daemon poll -> local hs; and -> uplink -> master)")
    if not s["cli"] or not os.path.exists(s["cli"]):
        print("  skipped: imessage-cli not found at %s" % s["cli"])
        return True
    t0 = time.time()
    p = subprocess.run([s["cli"], "--no-events", "--json", "send", s["chat_id"], canary],
                       capture_output=True, timeout=60)
    print("  cli send exit=%d          %6.2fs" % (p.returncode, time.time() - t0))
    if p.returncode != 0:
        report["native"] = {"cli_exit": p.returncode}
        return False
    echo, dt = poll(find_in_room(LOCAL_HS, s["local_token"], s["room"], canary, since_ts=int(t0 * 1000) - 60000), BUDGET["local_echo_s"])
    if echo:
        c = echo.get("content") or {}
        renders_sent = (echo.get("sender") == s["bot_id"] and c.get("com.jkali.from_me") is True) or echo.get("sender") == s["local_user"]
        print("  local hs has it        %6.2fs  sender=%s from_me=%s -> renders as %s" % (
            dt, echo.get("sender"), c.get("com.jkali.from_me"), "SENT" if renders_sent else "RECEIVED (bug)"))
    else:
        renders_sent = False
        print("  local hs has it        %6.2fs  NOT SEEN within budget" % dt)
    master = None
    if echo and s["mirror"] and s["master_token"]:
        master, dtm = poll(find_in_room(MASTER_HS, s["master_token"], s["mirror"], canary, since_ts=int(t0 * 1000) - 60000), BUDGET["master_s"])
        print("  master mirror          %6.2fs  %s" % (dtm, ("from_me=%s" % (master.get("content") or {}).get("com.jkali.from_me")) if master else "NOT MIRRORED within budget"))
    report["native"] = {"cli_exit": p.returncode, "local_s": dt, "local_found": bool(echo), "renders_sent": renders_sent,
                        "master_found": bool(master)}
    return bool(echo) and renders_sent and (master is not None or not (s["mirror"] and s["master_token"]))


def passive(s, report, stale_s=900, lag_budget_ms=60000):
    """No message is sent. Read the newest mirrored events of the self-chat on
    the master and report their hop lags, plus how fresh the daemons' own
    health reports are. Exit 1 when the uplink or daemon report is stale or
    the median lag is over budget. Safe to run on a schedule."""
    print("== passive: recent hop stamps + daemon freshness (nothing is sent)")
    ok = True
    lags = []
    if s["mirror"] and s["master_token"]:
        try:
            data = http(MASTER_HS, s["master_token"], "GET", "/_matrix/client/v3/rooms/%s/messages?dir=b&limit=50" % urllib.parse.quote(s["mirror"], safe=""))
        except Exception as e:
            data = {}
            print("  master read failed: %s" % type(e).__name__); ok = False
        for e in data.get("chunk") or []:
            hops = (e.get("content") or {}).get("com.jkali.hops") or {}
            lt, mt = hops.get("local_ts"), e.get("origin_server_ts")
            if isinstance(lt, int) and isinstance(mt, int) and 0 <= mt - lt < 86400000:
                lags.append(mt - lt)
        if lags:
            lags.sort(); med = lags[len(lags) // 2]
            print("  mirror lag over %d stamped events: median %.2fs, max %.2fs" % (len(lags), med / 1000, lags[-1] / 1000))
            if med > lag_budget_ms:
                ok = False
        else:
            print("  no stamped events yet (hop stamps start with the 2026-10-03 uplink)")
    try:
        health = json.loads(sqlite3.connect("file:%s?mode=ro" % s["uplink_db"], uri=True).execute(
            "SELECT v FROM meta WHERE k='sync_health'").fetchone()[0])
        age = time.time() - (health.get("updated_at") or 0)
        print("  uplink health age %.0fs  queued=%s refused=%s errors=%s" % (age, health.get("pending_events"), health.get("delivery_refused"), sorted((health.get("errors") or {}).keys())))
        if age > stale_s or (health.get("errors") or {}):
            ok = False
    except Exception as e:
        print("  uplink health unreadable (%s)" % type(e).__name__); ok = False
    try:
        d = json.loads(urllib.request.urlopen("http://127.0.0.1:29350/health", timeout=5).read().decode())
        out = d.get("outbound") or {}
        print("  imessage daemon: confirmed=%s refused=%s retryable=%s ambiguous=%s last_inbound_age=%s" % (
            out.get("confirmed", 0), out.get("refused", 0), out.get("retryable", 0), out.get("ambiguous", 0),
            ("%.0fs" % (time.time() - d["last_inbound_ts"])) if d.get("last_inbound_ts") else "n/a"))
        if out.get("retryable") or out.get("ambiguous"):
            ok = False
    except Exception as e:
        print("  imessage daemon /health unreachable (%s)" % type(e).__name__); ok = False
    report["passive"] = {"lags_ms": lags, "ok": ok}
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--i-am-sending-real-imessages", action="store_true", help="required for the live legs: this sends real iMessages to your own handle")
    ap.add_argument("--leg", choices=["matrix", "native", "both"], default="both")
    ap.add_argument("--passive", action="store_true", help="send nothing; read recent hop stamps and daemon health (safe for a schedule)")
    ap.add_argument("--report", default=None, help="write a JSON report here")
    a = ap.parse_args()
    s = load_setup()
    if a.passive:
        report = {"started": time.time(), "room": s["room"], "mirror": s["mirror"]}
        ok = passive(s, report)
        if a.report:
            with open(a.report, "w") as f:
                json.dump(report, f, indent=2)
        print("\nprobe: %s" % ("HEALTHY" if ok else "ATTENTION NEEDED"))
        sys.exit(0 if ok else 1)
    if not a.i_am_sending_real_imessages:
        die("refusing: pass --i-am-sending-real-imessages (the canary is a real iMessage to yourself), or --passive")
    print("probe: self-chat %s -> room %s -> mirror %s" % (s["chat_id"], s["room"], s["mirror"] or "(none)"))
    report = {"started": time.time(), "room": s["room"], "mirror": s["mirror"], "budgets": BUDGET}
    ok = True
    if a.leg in ("matrix", "both"):
        ok = leg_matrix(s, "beepa-probe %s %s" % (uuid.uuid4().hex[:8], time.strftime("%H:%M:%S")), report) and ok
    if a.leg in ("native", "both"):
        ok = leg_native(s, "beepa-probe-native %s %s" % (uuid.uuid4().hex[:8], time.strftime("%H:%M:%S")), report) and ok
    report["ok"] = ok
    if a.report:
        with open(a.report, "w") as f:
            json.dump(report, f, indent=2)
    print("\nprobe: %s" % ("ALL HOPS WITHIN BUDGET" if ok else "A HOP MISSED ITS BUDGET"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
