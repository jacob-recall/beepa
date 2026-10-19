"""Provision the manager's private CRM operation log, not a send service.

The caller authenticates the configured manager before entering this module.
The fixed alias makes retries after a lost create response recover the same room.
All transport is injected; neither account credentials nor a second database live
here. Room version 11 is explicit because v12 changes creator power semantics.
"""
import json
import re
from urllib.parse import quote


ROOM_TYPE = "com.jkali.crm"
SCHEMA_TYPE = "com.jkali.crm.schema"
OPERATION_TYPE = "com.jkali.crm.operation"
ALIAS_LOCALPART = "beepa-crm-v1"
ROOM_RE = re.compile(r"![A-Za-z0-9._~=-]+:[A-Za-z0-9.:-]+")
USER_RE = re.compile(r"@[a-z0-9._=/-]+:[A-Za-z0-9.-]+(?::[0-9]+)?")


class CRMRoomError(Exception):
    """A safe, non-sensitive provisioning error suitable for an API response."""


def power_levels(manager):
    # The manager owns this metadata room and may administer its state. These
    # defaults deny accidental chat messages; mirror-room native-send boundaries
    # are separate and unchanged. Never claim this locks out the room's owner.
    return {
        "users": {manager: 100}, "users_default": 0,
        "events_default": 101, "state_default": 100,
        "invite": 101, "kick": 101, "ban": 101, "redact": 100,
        "events": {OPERATION_TYPE: 100, "m.room.message": 101,
                   "m.room.encrypted": 101},
    }


def room_payload(manager):
    return {
        "room_version": "11", "visibility": "private", "preset": "private_chat",
        "room_alias_name": ALIAS_LOCALPART, "name": "Beepa contact lists",
        "creation_content": {"type": ROOM_TYPE, "m.federate": False},
        "power_level_content_override": power_levels(manager),
        "initial_state": [
            {"type": SCHEMA_TYPE, "state_key": "",
             "content": {"version": 1, "manager_mxid": manager}},
            {"type": "m.room.history_visibility", "state_key": "",
             "content": {"history_visibility": "joined"}},
            {"type": "m.room.guest_access", "state_key": "",
             "content": {"guest_access": "forbidden"}},
        ],
    }


def _json(raw):
    try:
        return json.loads(raw)
    except (ValueError, TypeError, UnicodeError):
        raise CRMRoomError("Malformed CRM room response") from None


def verify_room_state(state, manager):
    """Refuse alias hijacks and modified room security rather than repairing them."""
    if not isinstance(state, list):
        raise CRMRoomError("Invalid CRM room state")
    indexed = {}
    members = {}
    for event in state:
        if not isinstance(event, dict):
            raise CRMRoomError("Invalid CRM state event")
        typ, key = event.get("type"), event.get("state_key")
        if not isinstance(typ, str) or not isinstance(key, str):
            raise CRMRoomError("Invalid CRM state identity")
        pair = (typ, key)
        if pair in indexed:
            raise CRMRoomError("Duplicate CRM room state")
        indexed[pair] = event
        if typ == "m.room.member":
            value = event.get("content")
            if not isinstance(value, dict):
                raise CRMRoomError("Invalid CRM membership")
            members[key] = value.get("membership")

    def content(typ):
        event = indexed.get((typ, ""), {})
        value = event.get("content")
        if event.get("sender") != manager or not isinstance(value, dict):
            raise CRMRoomError("Unverified CRM room identity")
        return value

    create = content("m.room.create")
    if (create.get("type") != ROOM_TYPE or create.get("room_version") != "11"
            or create.get("m.federate") is not False):
        raise CRMRoomError("Incompatible CRM room")
    schema = content(SCHEMA_TYPE)
    if schema != {"version": 1, "manager_mxid": manager} or type(schema.get("version")) is not int:
        raise CRMRoomError("Incompatible CRM schema")
    levels = content("m.room.power_levels")
    expected = power_levels(manager)
    # Synapse may add defaults, e.g. notifications. Required fields and the
    # complete users/events maps must match; an extra allowed event is unsafe.
    if any(json.dumps(levels.get(key), sort_keys=True) != json.dumps(value, sort_keys=True)
           for key, value in expected.items()):
        raise CRMRoomError("CRM room permissions changed")
    if (content("m.room.join_rules").get("join_rule") != "invite"
            or content("m.room.history_visibility").get("history_visibility") != "joined"
            or content("m.room.guest_access").get("guest_access") != "forbidden"):
        raise CRMRoomError("CRM room privacy changed")
    if members != {manager: "join"}:
        raise CRMRoomError("CRM room has unexpected members")
    if any(typ in ("m.room.encryption", "m.room.tombstone") for typ, _ in indexed):
        raise CRMRoomError("CRM room is encrypted or replaced")


def ensure_room(*, request, cs_base, token, manager_mxid):
    """Return verified metadata; the endpoint MUST authenticate before calling."""
    if not isinstance(manager_mxid, str) or not USER_RE.fullmatch(manager_mxid):
        raise CRMRoomError("Invalid configured manager")
    headers = {"Authorization": "Bearer " + token}
    alias = "#" + ALIAS_LOCALPART + ":" + manager_mxid.split(":", 1)[1]
    alias_url = cs_base + "/_matrix/client/v3/directory/room/" + quote(alias, safe="")

    def resolve():
        status, raw = request("GET", alias_url, headers=headers)
        if status == 404:
            data = _json(raw)
            if isinstance(data, dict) and data.get("errcode") == "M_NOT_FOUND":
                return None
        if status != 200:
            raise CRMRoomError("Cannot resolve CRM room")
        data = _json(raw)
        rid = data.get("room_id") if isinstance(data, dict) else None
        if not isinstance(rid, str) or not ROOM_RE.fullmatch(rid):
            raise CRMRoomError("Invalid CRM room identifier")
        return rid

    room_id = resolve()
    if room_id is None:
        try:
            status, _ = request("POST", cs_base + "/_matrix/client/v3/createRoom",
                                headers=headers, data=json.dumps(room_payload(manager_mxid)).encode())
        except (OSError, TimeoutError):
            # The create may already have succeeded. Resolve, never repeat the
            # POST with a different alias or create an untracked replacement.
            status = 0
        room_id = resolve()
        if room_id is None:
            raise CRMRoomError("CRM room creation unconfirmed (HTTP %d)" % status)
    status, raw = request("GET", cs_base + "/_matrix/client/v3/rooms/"
                          + quote(room_id, safe="") + "/state", headers=headers)
    if status != 200:
        raise CRMRoomError("Cannot verify CRM room")
    verify_room_state(_json(raw), manager_mxid)
    return {"room_id": room_id, "manager_mxid": manager_mxid, "schema_version": 1}
