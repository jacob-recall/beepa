"""Fresh, read-only evidence for CRM grants to existing one-to-one rooms.

Unlike number_resolver this is an authorization input: incomplete reads never
mean an empty result. No Matrix writes, account creation or native messages are
performed here. All returned room IDs come from local bridge databases, then
are checked against authenticated Matrix state. Private rooms are eligible;
conversation consent remains the caller's versioned journal responsibility.

Native iMessage exposes no Apple-account identity API. Its epoch below proves
the local configured bridge incarnation, NOT an Apple ID or Apple login epoch.
The caller also binds proofs to its persisted pairing/database generation.
"""
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time
import urllib.parse

from agents.enrich.number_resolver import _parse_imessage_handle, _norm_phone, _norm_wa_pn
from agents.uplink import consent
from install_config import compose_prefix


# Pinned Compose services/schemas, not identifiers supplied by the master.
PROVIDERS = {
    "whatsapp": ("whatsapp", 29318, "mautrix_whatsapp"),
    "gmessages": ("gmessages", 29336, "mautrix_gmessages"),
    "instagram": ("meta", 29319, "mautrix_meta"),
    "linkedin": ("linkedin", 29319, "mautrix_linkedin"),
    "twitter": ("twitter", 29327, "mautrix_twitter"),
    "discord": ("discord", 29334, "mautrix_discord"),
}
PROTOCOLS = {source: ("instagramgo" if source == "instagram" else "discordgo" if source == "discord" else source) for source in PROVIDERS}


def provider_query(source):
    """Fixed SELECTs: never select login tokens or credential metadata.

    bridgev2 schema is mautrix v0.30; Discord v0.7.7 still uses its legacy
    portal/user schema. In bridgev2 bridge_id is a DB namespace (usually the
    empty string), not a login generation or the Matrix bridge-info state key.
    """
    if source == "discord":
        return '''SELECT row_to_json(e) FROM (
            SELECT p.mxid AS room_id,p.dcid AS native_id,p.receiver,
                   p.type AS room_type,p.other_user_id,u.mxid AS owner,
                   u.dcid AS login_id,u.space_room AS login_space
            FROM portal p JOIN "user" u ON u.dcid=p.receiver
            WHERE p.mxid IS NOT NULL AND p.type=1 AND (p.dc_guild_id IS NULL OR p.dc_guild_id='')
        ) e'''
    if source not in PROVIDERS:
        _fail("unsupported", "Provider schema is unsupported")
    extra = ", m.pn AS phone" if source == "whatsapp" else ", g.metadata->>'phone' AS phone"
    join = " LEFT JOIN whatsmeow_lid_map m ON p.other_user_id='lid-' || m.lid" if source == "whatsapp" else ""
    return '''SELECT row_to_json(e) FROM (
        SELECT p.mxid AS room_id,p.id AS native_id,p.receiver,p.room_type,
               p.other_user_id,p.bridge_id,up.user_mxid AS owner,
               ul.id AS login_id,ul.space_room AS login_space,g.identifiers''' + extra + '''
        FROM portal p JOIN user_portal up
          ON p.bridge_id=up.bridge_id AND p.id=up.portal_id AND p.receiver=up.portal_receiver
        JOIN user_login ul
          ON ul.bridge_id=up.bridge_id AND ul.id=up.login_id AND ul.user_mxid=up.user_mxid
        JOIN ghost g ON g.bridge_id=p.bridge_id AND g.id=p.other_user_id
        ''' + join + " WHERE p.mxid IS NOT NULL AND p.room_type='dm') e"


class EvidenceError(Exception):
    def __init__(self, status, reason):
        self.status, self.reason = status, reason


def _fail(status, reason):
    raise EvidenceError(status, reason)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _text(value):
    return isinstance(value, str) and bool(value) and len(value) <= 2048


def _room(value):
    return isinstance(value, str) and re.fullmatch(r"![^\s:]+:[^\s]+", value) is not None


def _readonly(path):
    # as_uri quotes '?' and '#', so a caller-controlled path cannot add SQLite
    # URI options. Missing files must not create an empty database.
    connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _state(events):
    if not isinstance(events, list):
        _fail("unavailable", "Room state is unavailable")
    result = {}
    for event in events:
        if not isinstance(event, dict) or not isinstance(event.get("type"), str) or not isinstance(event.get("state_key"), str) or not isinstance(event.get("content"), dict):
            _fail("unavailable", "Room state is malformed")
        key = (event["type"], event["state_key"])
        if key in result:
            _fail("ambiguous", "Room state contains duplicate identities")
        result[key] = event
    return result


def _event(state, kind, key=""):
    event = state.get((kind, key))
    if not event:
        _fail("unavailable", "Required room evidence is missing")
    return event


def _joined(state, user):
    if _event(state, "m.room.member", user)["content"].get("membership") != "join":
        _fail("stale", "Required account is no longer joined")


def _ordinary(state):
    if _event(state, "m.room.create")["content"].get("type") == "m.space" or any(t in ("com.jkali.bridge.mgmt", "m.room.tombstone") for t, _ in state):
        _fail("stale", "The room is not an active conversation")


class ContactRoomReader:
    """Adapter for Uplink; callbacks must read authenticated LOCAL account data.

    read_context() returns the current trusted owner_mxid and generation.
    read_room_state(room_id) returns complete Matrix state events, including
    sender/event_id. Policy and override callbacks use Uplink's normalized
    forms. read_profiles() returns the existing contact_profiles list.
    """

    def __init__(self, *, contacts_db, local_user_mxid, read_context,
                 read_room_state, read_profiles, read_contact_policy,
                 read_contact_overrides, imessage_db=None,
                 imessage_config=None, install_root=None, clock=time.time,
                 read_provisioning=None, query_provider=None, runner=subprocess.run):
        root = Path(install_root or os.environ.get("BEEPA_INSTALL_ROOT") or Path(__file__).resolve().parents[2])
        self.contacts_db = contacts_db
        self.local_user = local_user_mxid
        self.read_context = read_context
        self.read_room_state = read_room_state
        self.read_profiles = read_profiles
        self.read_contact_policy = read_contact_policy
        self.read_contact_overrides = read_contact_overrides
        self.imessage_db = imessage_db or Path(os.environ.get("IMESSAGE_STATE_DIR", str(root / "imessage"))) / "state.db"
        self.imessage_config = imessage_config or os.environ.get("IMESSAGE_CONFIG") or root / "imessage/daemon.json"
        self.clock = clock
        self.root = root
        self.runner = runner
        self.read_provisioning = read_provisioning or self._provisioning
        self.query_provider = query_provider or self._query_provider

    def _config_scalar(self, source, key):
        directory = PROVIDERS[source][0]
        text = (self.root / directory / "config.yaml").read_text(encoding="utf-8")
        values = re.findall(r"(?m)^\s*" + re.escape(key) + r":\s*([^\r\n#]+)", text)
        if len(values) != 1:
            _fail("unavailable", "Bridge configuration is ambiguous")
        return values[0].strip().strip("\"'")

    def _provisioning(self, source):
        service, port, _ = PROVIDERS[source]
        secret = self._config_scalar(source, "shared_secret")
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,256}", secret):
            _fail("unavailable", "Bridge provisioning status is not configured")
        route = "/_matrix/provision/v1/ping" if source == "discord" else "/_matrix/provision/v3/whoami"
        # BusyBox nc is already used by the installed Discord connector. The
        # secret travels only over stdin, never argv, environment, logs or a
        # shell command. HTTP/1.0 requests a bounded closing response.
        request = ("GET " + route + "?user_id=" + urllib.parse.quote(self.local_user, safe="") +
                   " HTTP/1.0\r\nHost: 127.0.0.1\r\nConnection: close\r\nAuthorization: Bearer " + secret + "\r\n\r\n").encode()
        args = compose_prefix(str(self.root), str(Path(__file__).resolve().parents[2])) + ["exec", "-T", "mautrix-" + service, "nc", "-w", "15", "127.0.0.1", str(port)]
        result = self.runner(args, input=request, capture_output=True, timeout=20)
        if result.returncode or len(result.stdout) > 1024 * 1024:
            _fail("unavailable", "Bridge status request failed")
        class ResponseSocket:
            def makefile(self, mode):
                return io.BytesIO(result.stdout)
        response = http.client.HTTPResponse(ResponseSocket())
        response.begin()
        if response.status != 200:
            _fail("unavailable", "Bridge status request was refused")
        value = json.loads(response.read(1024 * 1024))
        if not isinstance(value, dict):
            _fail("unavailable", "Bridge status is malformed")
        return value

    def _query_provider(self, source):
        database = PROVIDERS[source][2]
        args = compose_prefix(str(self.root), str(Path(__file__).resolve().parents[2])) + ["exec", "-T", "postgres", "psql", "-X", "-v", "ON_ERROR_STOP=1", "-U", "matrix", "-d", database, "-tAc", provider_query(source)]
        result = self.runner(args, capture_output=True, timeout=20)
        if result.returncode or len(result.stdout) > 16 * 1024 * 1024:
            _fail("unavailable", "Bridge ownership query failed")
        values = [json.loads(line) for line in result.stdout.decode().splitlines() if line.strip()]
        if any(not isinstance(value, dict) for value in values):
            _fail("unavailable", "Bridge ownership query is malformed")
        return values

    def _contact(self, contact, expected_version=None):
        if not isinstance(contact, dict) or not _text(contact.get("source")) or not _text(contact.get("network_id")):
            _fail("stale", "Contact identity is invalid")
        db = _readonly(self.contacts_db)
        try:
            rows = db.execute("SELECT source,network_id,version,deleted FROM contacts WHERE source=? AND network_id=?", (contact["source"], contact["network_id"])).fetchall()
        finally:
            db.close()
        if len(rows) != 1 or rows[0]["deleted"] != 0:
            _fail("unshared", "Contact is no longer available")
        row = dict(rows[0])
        if type(row["version"]) is not int or row["version"] < 1:
            _fail("unavailable", "Contact version is unavailable")
        version = str(row["version"])
        if expected_version is not None and (not isinstance(expected_version, str) or version != expected_version):
            _fail("stale", "Contact version changed")
        policy, overrides = self.read_contact_policy(), self.read_contact_overrides()
        if not isinstance(policy, dict) or not isinstance(overrides, dict):
            _fail("unavailable", "Contact sharing could not be verified")
        key = consent.contact_override_key(contact["source"], contact["network_id"])
        if key in overrides and overrides[key] not in ("share", "private"):
            _fail("unavailable", "Contact sharing is malformed")
        if not consent.resolve_contact_share(contact["source"], policy, overrides.get(key))["shared"]:
            _fail("unshared", "Contact is no longer shared")
        return version

    def _native_map(self):
        db = _readonly(self.imessage_db)
        try:
            mappings = [dict(row) for row in db.execute("SELECT chat_id,room_id FROM map ORDER BY chat_id")]
            meta = dict(db.execute("SELECT k,v FROM meta WHERE k IN ('mgmt_room','space_id')").fetchall())
        finally:
            db.close()
        return mappings, meta

    def read_connections(self, source):
        """Read current bridge health, never infer connected from DB existence."""
        if source != "imessage":
            if source not in PROVIDERS:
                _fail("unsupported", "Provider is unsupported")
            status = self.read_provisioning(source)
            if not isinstance(status, dict):
                _fail("unavailable", "Current provider status is malformed")
            if source == "discord":
                remote = status.get("Discord")
                if not isinstance(remote, dict) or remote.get("connected") is not True or remote.get("logged_in") is not True or not re.fullmatch(r"[0-9]{1,32}", str(remote.get("id", ""))):
                    _fail("unavailable", "Discord account is not connected")
                domain = self._config_scalar(source, "domain")
                bot = "@" + self._config_scalar(source, "username") + ":" + domain
                return {"bot_mxid": bot, "protocol": "discordgo", "logins": {str(remote["id"]): {}}, "domain": domain}
            network, logins = status.get("network"), status.get("logins")
            if not isinstance(network, dict) or network.get("network_id") != source or network.get("beeper_bridge_type") != PROTOCOLS[source] or not isinstance(logins, list) or not _text(status.get("bridge_bot")):
                _fail("unavailable", "Current provider identity is malformed")
            active, seen = {}, set()
            for login in logins:
                if not isinstance(login, dict) or not _text(login.get("id")) or not isinstance(login.get("state"), dict):
                    _fail("unavailable", "Current login state is malformed")
                if login["id"] in seen:
                    _fail("ambiguous", "Provider has duplicate login identities")
                seen.add(login["id"])
                if login["state"].get("state_event") != "CONNECTED":
                    continue
                if not _room(login.get("space_room")):
                    _fail("unavailable", "Current login incarnation is unavailable")
                active[login["id"]] = {"login_space": login["space_room"]}
            if not active:
                _fail("unavailable", "Provider has no connected login")
            return {"bot_mxid": status["bridge_bot"], "protocol": network["beeper_bridge_type"], "logins": active}
        with open(self.imessage_config, encoding="utf-8") as stream:
            config = json.load(stream)
        if not isinstance(config, dict) or config.get("user_id") != self.local_user or not _text(config.get("bot_id")) or not _text(config.get("domain")):
            _fail("stale", "Native bridge account binding changed")
        _, meta = self._native_map()
        management = meta.get("mgmt_room")
        if not _room(management):
            _fail("unavailable", "Native bridge management evidence is missing")
        state = _state(self.read_room_state(management))
        bot = config["bot_id"]
        created = _event(state, "m.room.create")
        marker = _event(state, "com.jkali.bridge.mgmt", "imessage")
        health = _event(state, "com.jkali.imessage_health")
        if created["content"].get("type") == "m.space" or ("m.room.tombstone", "") in state:
            _fail("stale", "Native bridge management room was replaced")
        if created.get("sender") != bot or not _text(created.get("event_id")) or marker.get("sender") != bot or marker["content"] != {"bridge": "imessage", "role": "management"} or health.get("sender") != bot:
            _fail("stale", "Native bridge management identity changed")
        _joined(state, self.local_user)
        _joined(state, bot)
        updated = health["content"].get("updated_at")
        if health["content"].get("poll_ok") is not True or type(updated) not in (int, float) or not -30 <= self.clock() - updated <= 600:
            _fail("unavailable", "Native bridge is offline or its health is stale")
        binding = {"local_owner_mxid": self.local_user, "bot_mxid": bot,
                   "bridge_id": config["domain"] + "/imessage",
                   "management_room": management, "created_event": created["event_id"]}
        return dict(binding, bridge_epoch=_digest(binding), bridge_epoch_kind="local_bridge_incarnation")

    def _imessage(self, contact, room_id=None):
        connection = self.read_connections("imessage")
        mappings, _ = self._native_map()
        target = _parse_imessage_handle("any;-;" + contact["network_id"]) if contact else None
        if target is None and room_id is None:
            _fail("unsupported", "This contact has no supported native identity")
        rooms = []
        for mapping in mappings:
            parsed = _parse_imessage_handle(mapping["chat_id"])
            if parsed is None or (mapping["room_id"] != room_id if room_id is not None else parsed != target):
                continue
            rid = mapping["room_id"]
            if not _room(rid):
                _fail("unavailable", "Native mapping is malformed")
            state = _state(self.read_room_state(rid))
            _ordinary(state)
            _joined(state, self.local_user)
            _joined(state, connection["bot_mxid"])
            if _event(state, "m.room.create").get("sender") != connection["bot_mxid"]:
                _fail("stale", "Native portal creator changed")
            marker = _event(state, "uk.half-shot.bridge", connection["bridge_id"])
            content = marker["content"]
            if marker.get("sender") != connection["bot_mxid"] or content.get("bridgebot") != connection["bot_mxid"] or content.get("protocol", {}).get("id") != "imessage" or content.get("channel", {}).get("id") != mapping["chat_id"]:
                _fail("stale", "Native portal identity changed")
            rooms.append(dict(connection, room_id=rid, source="imessage", native_id=mapping["chat_id"]))
        if len(rooms) > 1:
            _fail("ambiguous", "Multiple native rooms match the same identity")
        return rooms

    def _counterpart(self, source, contact, row):
        value = contact["network_id"]
        native = row.get("other_user_id")
        if source == "whatsapp":
            expected = _norm_phone(value)
            actual = _norm_wa_pn(row.get("phone")) if str(native).startswith("lid-") else _norm_wa_pn(native)
            return expected is not None and expected == actual
        if source == "gmessages":
            expected = _norm_phone(value)
            actual = _norm_phone(row.get("phone"))
            return expected is not None and expected == actual
        # Only stable IDs accepted here. Vanity URLs/usernames are mutable and
        # are not the LinkedIn member URN / Instagram or X numeric identity.
        if source == "linkedin":
            return value == native or value == "linkedin:" + str(native)
        if source in ("instagram", "twitter", "discord"):
            return bool(re.fullmatch(r"[0-9]{1,32}", value)) and value == native
        return False

    def _provider(self, source, contact=None, room_id=None):
        if source == "imessage":
            return self._imessage(contact, room_id)
        connection = self.read_connections(source)
        rows = self.query_provider(source)
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            _fail("unavailable", "Provider ownership evidence is malformed")
        proofs = {}
        for row in rows:
            if row.get("owner") != self.local_user:
                continue
            if room_id is not None:
                if row.get("room_id") != room_id:
                    continue
            elif not self._counterpart(source, contact, row):
                continue
            rid, login = row.get("room_id"), row.get("login_id")
            if not _room(rid) or not _text(row.get("native_id")) or not _text(row.get("other_user_id")):
                _fail("unavailable", "Provider portal identity is malformed")
            if row.get("room_type") != (1 if source == "discord" else "dm"):
                _fail("stale", "Provider portal is not a one-to-one conversation")
            if login not in connection["logins"]:
                _fail("stale", "Portal belongs to an inactive login")
            if source != "discord" and row.get("bridge_id") != "":
                _fail("stale", "Portal belongs to another bridge namespace")
            if row.get("receiver") not in ("", login) or (source == "discord" and row.get("receiver") != login):
                _fail("stale", "Portal receiver belongs to another login")
            login_space = row.get("login_space")
            if source != "discord" and login_space != connection["logins"][login]["login_space"]:
                _fail("stale", "Login incarnation changed")
            if not _room(login_space):
                _fail("unavailable", "Login incarnation is unavailable")
            space = _state(self.read_room_state(login_space))
            space_create = _event(space, "m.room.create")
            if ("m.room.tombstone", "") in space or space_create.get("sender") != connection["bot_mxid"] or space_create["content"].get("type") != "m.space" or not _text(space_create.get("event_id")):
                _fail("stale", "Login space identity changed")
            _joined(space, self.local_user)
            _joined(space, connection["bot_mxid"])
            state = _state(self.read_room_state(rid))
            _ordinary(state)
            _joined(state, self.local_user)
            creator = connection["bot_mxid"]
            if source == "discord":
                template = self._config_scalar(source, "username_template")
                if not re.fullmatch(r"[a-z0-9_]+\{\{\.\}\}", template):
                    _fail("unsupported", "Discord ghost identity template is unsupported")
                creator = "@" + template.replace("{{.}}", row["other_user_id"]) + ":" + connection["domain"]
            _joined(state, creator)
            if _event(state, "m.room.create").get("sender") != creator:
                _fail("stale", "Provider portal creator changed")
            markers = [event for (kind, _), event in state.items() if kind == "uk.half-shot.bridge"]
            if len(markers) != 1:
                _fail("ambiguous", "Portal bridge identity is ambiguous")
            marker = markers[0]
            content = marker["content"]
            channel = content.get("channel", {})
            if marker.get("sender") != creator or content.get("bridgebot") != connection["bot_mxid"] or content.get("creator") != creator or content.get("protocol", {}).get("id") != connection["protocol"] or channel.get("id") != row["native_id"] or content.get("com.beeper.room_type.v2") != "dm":
                _fail("stale", "Provider portal proof changed")
            if source != "discord" and channel.get("fi.mau.receiver", "") != row["receiver"]:
                _fail("stale", "Provider portal receiver changed")
            incarnation = {"local_owner_mxid": self.local_user, "bot_mxid": connection["bot_mxid"], "login_id": login, "login_space": login_space, "created_event": space_create["event_id"]}
            proof = dict(incarnation, room_id=rid, source=source, native_id=row["native_id"], counterpart_id=row["other_user_id"], receiver=row["receiver"], bridge_epoch=_digest(incarnation), bridge_epoch_kind="provider_login_binding")
            if rid in proofs and proofs[rid] != proof:
                _fail("ambiguous", "Portal has multiple current account bindings")
            proofs[rid] = proof
        if room_id is None and len(proofs) > 1:
            _fail("ambiguous", "Multiple provider rooms match the contact")
        return [proofs[rid] for rid in sorted(proofs)]

    def _profile_room(self, rid):
        state = _state(self.read_room_state(rid))
        markers = [e for (kind, _), e in state.items() if kind == "uk.half-shot.bridge"]
        if len(markers) != 1:
            _fail("ambiguous", "Linked room bridge identity is ambiguous")
        protocol = markers[0]["content"].get("protocol", {}).get("id")
        # This only selects an adapter. _provider independently verifies its
        # SQL ownership + authenticated current connection + complete state.
        candidates = {"imessage": "imessage", "discordgo": "discord",
                      "whatsapp": "whatsapp", "gmessages": "gmessages",
                      "instagramgo": "instagram", "linkedin": "linkedin",
                      "twitter": "twitter"}
        source = candidates.get(protocol)
        if source is None:
            _fail("unsupported", "Linked room provider identity is unsupported")
        result = self._provider(source, room_id=rid)
        if len(result) != 1:
            _fail("stale", "Linked room has no current owned counterpart mapping")
        return result[0]

    def _snapshot(self, contact, version, owner, generation):
        if self.read_context() != {"owner_mxid": owner, "generation": generation} or not _text(owner) or not _text(generation):
            _fail("stale", "Account pairing changed")
        version = self._contact(contact, version)
        profiles = self.read_profiles()
        if not isinstance(profiles, list):
            _fail("unavailable", "Contact profiles are unavailable")
        # Do not silently omit a profile's other conversations. Each linked
        # source must have an authoritative adapter before the group is ready.
        claimed = []
        for profile in profiles:
            if not isinstance(profile, dict):
                _fail("unavailable", "Contact profiles are malformed")
            handles = profile.get("handleIds", [])
            if not isinstance(handles, list):
                _fail("unavailable", "Contact profile handles are malformed")
            if any(isinstance(h, dict) and h.get("source") == contact["source"] and h.get("network_id") == contact["network_id"] for h in handles):
                claimed.append(profile)
        if len(claimed) > 1:
            _fail("ambiguous", "Contact belongs to multiple profiles")
        handles = claimed[0].get("handleIds", []) if claimed else [contact]
        rooms, versions = {}, []
        for handle in handles:
            handle_version = self._contact(handle)
            versions.append([handle["source"], handle["network_id"], handle_version])
            for proof in self._provider(handle["source"], handle):
                if proof["room_id"] in rooms and rooms[proof["room_id"]] != proof:
                    _fail("ambiguous", "Room belongs to conflicting identities")
                rooms[proof["room_id"]] = proof
        profile_rooms = claimed[0].get("roomIds", []) if claimed else []
        if not isinstance(profile_rooms, list) or any(not _room(rid) for rid in profile_rooms):
            _fail("unavailable", "Contact profile rooms are malformed")
        for rid in sorted(set(profile_rooms) - set(rooms)):
            rooms[rid] = self._profile_room(rid)
        scope = _digest({"profile": claimed, "versions": sorted(versions), "owner": owner, "generation": generation})
        return {"status": "ready" if rooms else "no_existing_conversation", "contact_version": version,
                "rooms": [dict(rooms[rid], contact_version=version, profile_scope=scope) for rid in sorted(rooms)]}

    def resolve_contact(self, contact, contact_version, owner_mxid, generation):
        try:
            first = self._snapshot(contact, contact_version, owner_mxid, generation)
            second = self._snapshot(contact, contact_version, owner_mxid, generation)
            if first != second:
                _fail("stale", "Contact or bridge scope changed during verification")
            return second
        except EvidenceError as error:
            return {"status": error.status, "rooms": [], "reason": error.reason}
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, AttributeError, RuntimeError, subprocess.SubprocessError, http.client.HTTPException):
            # Never return database rows, handles, credentials or exception text.
            return {"status": "unavailable", "rooms": [], "reason": "Contact or bridge evidence could not be read"}
