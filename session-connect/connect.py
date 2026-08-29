#!/usr/bin/env python3
"""
Connect Instagram / LinkedIn / X to their bridges — capture your session once,
no DevTools, no "Copy as cURL".

Usage:  python3 session-connect/connect.py {twitter|linkedin|instagram}

Prereq: you're signed into the site in Chrome (the Default profile). Whatever
account is logged in there is the one captured — you never log in from the Hub
itself, and no password is ever entered or stored.

  twitter    reads x.com cookies (ct0, auth_token) and submits them to the
             bridge's provisioning API over the loopback docker network. No
             paste — it connects directly.
  linkedin   reads your linkedin.com cookies (incl. the httpOnly li_at) to
             build the Cookie header, and submits via the provisioning API.
             NOTE: LinkedIn also wants two request headers (X-LI-Track /
             X-LI-Page-Instance) that no cookie store holds; we synthesize
             them. If LinkedIn ever rejects that, the Hub's paste box is the
             fallback (Copy-as-cURL still works).
  instagram  reads your instagram.com cookies (sessionid, csrftoken, ds_user_id,
             mid) and submits them via the provisioning API of the mautrix-meta
             `ig-` build. No paste — it connects directly, same as the others.

The credential never touches a Matrix room: it goes straight into the bridge
over the loopback docker network.
"""
import base64
import json
import os
import re
import subprocess
import sys

import chrome_cookies as ck

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
USER_ID = "@jkali:localhost"
# Bridge-supplied ids get interpolated into a `sh -c` string; allow only the
# characters real login_id/step_id values use (alnum, dot, dash, underscore),
# so a hostile/garbled bridge response can never break out with a quote.
ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")

NETWORKS = {
    "twitter": dict(service="mautrix-twitter", port=29327,
                    config="twitter/config.yaml", flow="cookies",
                    domain="%x.com"),
    "linkedin": dict(service="mautrix-linkedin", port=29319,
                     config="linkedin/config.yaml", flow="cookies",
                     domain="%linkedin.com"),
    # instagram: the ig- build of mautrix-meta (Meta split Instagram out in
    # 2026) exposes a server-side cookies provisioning flow
    # (sessionid/csrftoken/ds_user_id/mid) — same shape as twitter/linkedin,
    # fully one-click, no mgmt-room paste.
    "instagram": dict(service="mautrix-meta", port=29319,
                      config="meta/config.yaml", flow="instagram",
                      domain="%instagram.com"),
}


def die(msg):
    print("ERROR:", msg)
    sys.exit(1)


def shared_secret(config):
    with open(os.path.join(REPO, config)) as f:
        for line in f:
            m = re.match(r"\s*shared_secret:\s*(\S+)", line)
            if m and m.group(1) not in ("generate", "null", "disable", '""'):
                return m.group(1)
    die("provisioning shared_secret not set in %s (set a value and restart the bridge)" % config)


def api(net, path, secret, body=None, timeout=15):
    """Call the bridge provisioning API inside its container via docker compose exec."""
    url = "http://localhost:%d/_matrix/provision/v3%s?user_id=%s" % (net["port"], path, USER_ID)
    wget = ("wget -qO- -T %d --header='Authorization: Bearer %s' "
            "--header='Content-Type: application/json' --post-file=/dev/stdin '%s'"
            % (timeout, secret, url))
    inp = (json.dumps(body) if body is not None else "{}").encode()
    p = subprocess.run(
        ["docker", "compose", "exec", "-T", net["service"], "sh", "-c", wget],
        input=inp, capture_output=True, cwd=REPO, timeout=timeout + 20)
    return p.stdout.decode("utf-8", "replace").strip()


def synth_header(header, jar):
    """Values the provisioning flow needs that live in the request, not the
    cookie jar. `Cookie` is rebuilt from the jar (incl. httpOnly cookies);
    the two LinkedIn tracking headers are synthesized — the bridge only
    pattern-checks them, and LinkedIn's read APIs accept a well-formed value."""
    h = (header or "").lower()
    if h == "cookie":
        return "; ".join("%s=%s" % (k, v) for k, v in jar.items())
    if h == "x-li-track":
        # clientVersion just has to be present + reasonably current; bump if
        # LinkedIn starts returning 426 upgrade-required.
        return json.dumps({
            "clientVersion": "1.13.36400", "mpVersion": "1.13.36400",
            "osName": "web", "timezoneOffset": 0, "timezone": "UTC",
            "deviceFormFactor": "DESKTOP", "mpName": "voyager-web",
            "displayDensity": 2, "displayWidth": 2560, "displayHeight": 1440})
    if h == "x-li-page-instance":
        rid = base64.b64encode(os.urandom(16)).decode().rstrip("=")
        return "urn:li:page:d_flagship3_feed;%s" % rid
    return None


def resolve_fields(name, net, fields):
    jar = ck.read(net["domain"])
    if not jar:
        die("no %s cookies found — sign into the site in Chrome (Default profile) and re-run." % name)
    values = {}
    for f in fields:
        fid = f.get("id")
        got = None
        for s in f.get("sources", []):
            if s.get("type") == "cookie":
                got = jar.get(s.get("name"))
            elif s.get("type") == "request_header":
                got = synth_header(s.get("name"), jar)
            if got:
                break
        if got is None and f.get("required"):
            die("could not resolve required field '%s' for %s — the session may be "
                "incomplete; sign in fully and re-run (or use the Hub's paste box)." % (fid, name))
        if got is not None:
            values[fid] = got
    return values


def provisioning_login(name, net):
    secret = shared_secret(net["config"])
    start = api(net, "/login/start/%s" % net["flow"], secret, body={})
    try:
        data = json.loads(start)
    except ValueError:
        die("could not start login (is the bridge running with provisioning enabled?): %s" % start[:300])
    lid, step = data.get("login_id"), data.get("step_id")
    fields = (data.get("cookies") or {}).get("fields", [])
    if not lid or not step or not ID_RE.match(lid) or not ID_RE.match(step):
        die("unexpected login-start response: %s" % start[:300])
    print("  bridge asked for: %s" % ", ".join(f.get("id", "?") for f in fields))
    values = resolve_fields(name, net, fields)
    resp = api(net, "/login/step/%s/%s/cookies" % (lid, step), secret, body=values)
    if '"type":"complete"' in resp or '"complete"' in resp:
        who = re.search(r'"user_login_id":"([^"]+)"', resp)
        tag = " as " + who.group(1).split("/")[0] if who else ""
        print("\nConnected%s. Your chats will sync into the %s space shortly." % (tag, name))
    else:
        die("the bridge did not accept the session (it may be stale — re-sign-in and re-run, "
            "or use the Hub's paste fallback): %s" % resp[:400])


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in NETWORKS:
        die("usage: connect.py {%s}" % "|".join(NETWORKS))
    name = sys.argv[1]
    net = NETWORKS[name]
    print("- Reading your %s session from Chrome (approve the Keychain prompt if it appears)..." % name)
    try:
        provisioning_login(name, net)
    except ck.CookieError as e:
        die(str(e))


if __name__ == "__main__":
    main()
