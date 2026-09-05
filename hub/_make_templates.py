#!/usr/bin/env python3
# Worker for hub/make-templates.sh. Value-based scrub: extract each real secret
# value from the current (gitignored) config, replace every occurrence with a
# named ${PLACEHOLDER}, and write the result as a tracked template. Because it
# replaces by *value*, it correctly catches a secret wherever it appears (e.g.
# the appservice as_token also embedded under double_puppet.secrets) without
# per-file fragility. Prints only counts + a leak check — never a secret value.
import os, re, sys, stat

ROOT = sys.argv[1]
TPL = os.path.join(ROOT, "hub", "templates")

# bridge key -> its registration filename under synapse/
REGS = {
    "WHATSAPP":  "synapse/registration.yaml",
    "GMESSAGES": "synapse/gmessages-registration.yaml",
    "IMESSAGE":  "synapse/imessage-registration.yaml",
    "LINKEDIN":  "synapse/linkedin-registration.yaml",
    "META":      "synapse/meta-registration.yaml",
    "TWITTER":   "synapse/twitter-registration.yaml",
}
# bridge key -> its config.yaml (imessage has no mautrix config; it uses daemon.json)
CONFIGS = {
    "WHATSAPP":  "whatsapp/config.yaml",
    "GMESSAGES": "gmessages/config.yaml",
    "LINKEDIN":  "linkedin/config.yaml",
    "META":      "meta/config.yaml",
    "TWITTER":   "twitter/config.yaml",
}
# Each containerized bridge ALSO reads its OWN registration.yaml from its data
# dir (same tokens as the synapse/ copy Synapse loads). If it's absent on a
# fresh clone the bridge generates a NEW one with a fresh token that Synapse
# rejects (401). Templatize these too so their tokens match — value-based, so
# they pick up the same ${AS_TOKEN_*}/${HS_TOKEN_*} already captured above.
BRIDGE_REGS = {
    "WHATSAPP":  "whatsapp/registration.yaml",
    "GMESSAGES": "gmessages/registration.yaml",
    "LINKEDIN":  "linkedin/registration.yaml",
    "META":      "meta/registration.yaml",
    "TWITTER":   "twitter/registration.yaml",
}

def read(rel):
    with open(os.path.join(ROOT, rel), "r") as f:
        return f.read()

def first(pattern, text, rel):
    m = re.search(pattern, text, re.MULTILINE)
    if not m:
        sys.exit(f"FATAL: pattern {pattern!r} not found in {rel}")
    return m.group(1)

# --- 1. collect secret value -> placeholder -------------------------------
secrets = {}   # placeholder -> real value
# mautrix sentinels that are NOT secrets: 'disable' turns a feature OFF,
# 'generate' means auto-generate at runtime. Templating them would flip
# behavior on a fresh install (e.g. minting a value where 'disable' stood would
# ENABLE a provisioning API). Leave any sentinel or too-short value literal.
SENTINELS = {"generate", "disable", "null", "", '""'}
def add(ph, val):
    if val and val not in SENTINELS and len(val) >= 16:
        secrets[ph] = val

hs = read("synapse/homeserver.yaml")
add("DB_PASSWORD",     first(r'^\s*password:\s*"([^"]+)"',           hs, "homeserver.yaml"))
add("MACAROON_SECRET", first(r'^macaroon_secret_key:\s*"([^"]+)"',   hs, "homeserver.yaml"))
add("FORM_SECRET",     first(r'^form_secret:\s*"([^"]+)"',           hs, "homeserver.yaml"))

for key, rel in REGS.items():
    t = read(rel)
    add(f"AS_TOKEN_{key}", first(r'^as_token:\s*(\S+)', t, rel))
    add(f"HS_TOKEN_{key}", first(r'^hs_token:\s*(\S+)', t, rel))

for key, rel in CONFIGS.items():
    t = read(rel)
    # anchored to the real (uncommented, 4-space-indented) keys
    add(f"PROV_{key}",   first(r'^    shared_secret:\s*(\S+)', t, rel))
    add(f"PICKLE_{key}", first(r'^    pickle_key:\s*(\S+)',    t, rel))

# --- 2. templatize each source file (longest values first) ----------------
order = sorted(secrets.items(), key=lambda kv: len(kv[1]), reverse=True)

# The local-account mxid the six bridges grant permissions to is parameterized
# (${LOCAL_MXID}) so a teammate's install isn't hardcoded to the author's
# account. It is NOT a secret — scrub it by exact string, and ONLY in the files
# that grant it (bridge configs + the gmessages appservice user-regex), so an
# unrelated mention could never be caught. Use the same read-only identity
# resolver as rendering; missing/conflicting configuration fails explicitly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from install_config import configured_identity
LIVE_MXID = configured_identity(ROOT)
MXID_FILES = {
    "meta/config.yaml", "twitter/config.yaml", "gmessages/config.yaml",
    "linkedin/config.yaml", "whatsapp/config.yaml",
    "gmessages/registration.yaml", "synapse/gmessages-registration.yaml",
}

def templatize(rel):
    text = read(rel)
    for ph, val in order:
        text = text.replace(val, "${%s}" % ph)
    if rel in MXID_FILES:
        text = text.replace(LIVE_MXID, "${LOCAL_MXID}")
    dest = os.path.join(TPL, rel + ".tmpl")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "w") as f:
        f.write(text)
    # leak check: no real secret value may survive in the template
    for ph, val in order:
        if val in text:
            sys.exit(f"LEAK: {ph} value still present in {dest}")
    return dest

made = []
made.append(templatize("synapse/homeserver.yaml"))
for rel in REGS.values():
    made.append(templatize(rel))
for rel in CONFIGS.values():
    made.append(templatize(rel))
for rel in BRIDGE_REGS.values():
    made.append(templatize(rel))

# --- 3. capture current secrets so first render reproduces exactly --------
# DB_PASSWORD lives in .env (POSTGRES_PASSWORD) — not stored here.
sec_path = os.path.join(ROOT, "synapse", ".hub-secrets.local")
os.makedirs(os.path.dirname(sec_path), exist_ok=True)
fd = os.open(sec_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    f.write("# Rendered-hub secrets — mode 600, gitignored, DO NOT COMMIT.\n")
    f.write("# Captured from the current config so the first render reproduces it;\n")
    f.write("# hub/render-hub.sh reuses these and mints any that are missing.\n")
    for ph, val in secrets.items():
        if ph == "DB_PASSWORD":
            continue
        esc = val.replace("'", "'\\''")
        f.write("%s='%s'\n" % (ph, esc))
os.chmod(sec_path, 0o600)

print(f"[make-templates] wrote {len(made)} templates under hub/templates/")
print(f"[make-templates] captured {len(secrets)-1} secrets into synapse/.hub-secrets.local (600)")
print(f"[make-templates] leak check: PASS (no secret value present in any template)")
