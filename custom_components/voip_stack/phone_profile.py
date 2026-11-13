"""Resolve common phone keys and render a verified, data-only vendor bundle."""

from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import PurePosixPath
import re
from xml.etree import ElementTree

from jinja2 import DictLoader, StrictUndefined
from jinja2.sandbox import SandboxedEnvironment, safe_range


def safe_profile_path(value):
    path = PurePosixPath(str(value))
    if (not str(value) or path.is_absolute() or ".." in path.parts or "\\" in str(value)
            or any(ord(char) < 32 for char in str(value))):
        raise ValueError("invalid_profile_path")
    return str(path)


def resolve_phone_language(profile, requested):
    languages = profile.get("capabilities", {}).get("languages", {})
    value = str(requested or "").replace("_", "-").lower()
    choices = {key.replace("_", "-").lower(): (key, item) for key, item in languages.items()}
    if value in choices:
        return choices[value]
    base = value.split("-", 1)[0]
    if base in choices:
        return choices[base]
    match = next((item for key, item in choices.items() if key.split("-", 1)[0] == base), None)
    if match is None:
        raise ValueError("phone_language_unsupported")
    return match


def _key_target(runtime, key, endpoint_id):
    function, target = key["function"], key.get("target", "")
    if str(target).casefold().startswith("contact:"):
        from .phone_directory import contact_dial_target
        directory = getattr(runtime, "directory", None)
        entry = directory.key_contact(target) if directory is not None else None
        if function != "speed_dial" or entry is None:
            raise ValueError("invalid_phone_key_target")
        return [contact_dial_target(entry)]
    if function in {"queue_login", "queue_pause"}:
        queue = runtime.queues.resolve(target) if runtime.queues else None
        if queue is None or endpoint_id not in queue.members:
            raise ValueError("phone_queue_membership_required")
        prefix = "queue-login+" if function == "queue_login" else "queue-pause+"
        return [(prefix + part) for part in (queue.extension, queue.name, queue.queue_id) if part]
    if function in {"park", "park_call"}:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", target):
            raise ValueError("invalid_parking_target")
        return [target]
    if function in {"line", "directory"}:
        return [""]
    endpoint = runtime.endpoints.resolve(target)
    if endpoint is not None:
        return [str(value) for value in (endpoint.extension, endpoint.sip_uri_user, endpoint.name) if value]
    if function == "speed_dial" and re.fullmatch(r"[+*#0-9]{1,64}", target):
        return [target]
    raise ValueError("invalid_phone_key_target")


def resolve_phone_keys(runtime, phone, profile):
    caps = profile["capabilities"]
    slots = {str(item["slot"]): item for item in caps.get("key_slots", [])}
    lines = {line["slot"]: line["endpoint_id"] for line in phone["lines"]}
    result = []
    for key in phone["keys"]:
        cap = slots.get(key["slot"])
        if cap is None or key["function"] not in cap.get("functions", caps.get("functions", [])):
            raise ValueError("phone_key_unsupported")
        candidates = _key_target(runtime, key, lines[key["line"]])
        limit = int(cap.get("max_value_bytes", 31))
        target = next((item for item in candidates if len(item.encode()) <= limit), None)
        if target is None:
            raise ValueError("phone_key_target_too_long")
        queue = runtime.queues.resolve(key["target"]) if key["function"] in {"queue_login", "queue_pause"} else None
        endpoint = runtime.endpoints.resolve(key.get("target", ""))
        directory = getattr(runtime, "directory", None)
        contact = (directory.key_contact(key.get("target", ""))
                   if directory is not None and str(key.get("target", "")).casefold().startswith("contact:") else None)
        label = (key.get("label") or getattr(queue, "name", "") or getattr(endpoint, "name", "")
                 or (contact.display_name if contact is not None else "") or key["function"])
        if len(label.encode()) > int(cap.get("max_label_bytes", 31)):
            raise ValueError("phone_key_label_too_long")
        result.append({**key, "line": int(key["line"]), "label": label, "dial_target": target,
                       "subscription_target": "" if key["function"] == "park_call" else target, "event_package": "dialog" if key["function"]
                       in {"blf", "queue_login", "queue_pause", "park"} else "",
                       "pickup_target": ""})
    return result


# Common data contract adapted from our MIT-licensed phone profile reference.
def normalized_context(context: dict) -> dict:
    """Fill only documented optional fields; required values remain required."""
    c = deepcopy(context)
    p = c.setdefault("physical", {})
    p.setdefault("language", "")
    p.setdefault("language_code", "")
    p.setdefault("time_zone", "")
    c.setdefault("lines", [])
    c.setdefault("keys", [])
    retired = c.setdefault("retired", {})
    if not isinstance(retired, dict) or set(retired) - {"keys", "lines"}:
        raise ValueError("invalid_retired_slots")
    for kind in ("keys", "lines"):
        retired.setdefault(kind, [])
    for line in c["lines"]:
        for key, default in {
            "auth_username": "",
            "display_name": "",
            "expires": 3600,
            "outbound_proxy": "",
        }.items():
            line.setdefault(key, default)
    for key in c["keys"]:
        for field, default in {
            "dial_target": "",
            "subscription_target": "",
            "pickup_target": "",
            "event_package": "dialog",
            "label": "",
        }.items():
            key.setdefault(field, default)
    ldap = c.setdefault("directory", {}).setdefault("ldap", {})
    for key, default in {
        "host": "",
        "port": 389,
        "base_dn": "",
        "bind_dn": "",
        "password": "",
        "name_filter": "",
        "number_filter": "",
        "name_attributes": "cn sn displayName",
        "number_attributes": "telephonenumber mobile",
        "display_name": "LDAP",
        "display_attribute": "displayName",
    }.items():
        ldap.setdefault(key, default)
    c.setdefault("provisioning", {}).setdefault("url", "")
    return c


def validate_context(profile: dict, context: dict) -> dict:
    """Validate projection bounds before rendering; never trim a route or label."""
    c = normalized_context(context)
    caps = profile["capabilities"]
    if not re.fullmatch(r"[0-9a-f]{12}", c["physical"]["mac"]):
        raise ValueError("physical.mac must be twelve lowercase hex digits")
    time_zone = c["physical"]["time_zone"]
    time_zones = caps.get("time_zones", {})
    if not isinstance(time_zone, str) or not isinstance(time_zones, dict):
        raise ValueError("invalid time zone mapping")
    if time_zone and not isinstance(time_zones.get(time_zone), dict):
        raise ValueError("time zone has no declared vendor mapping")
    # Vendor clock data is owned by the verified profile, never by the caller.
    c["clock"] = deepcopy(time_zones[time_zone]) if time_zone else {}
    c["dnd_sync"] = caps.get("dnd_sync") is True

    def strings(value) -> None:
        if isinstance(value, str) and any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
            raise ValueError("control character in configuration input")
        if isinstance(value, dict):
            for item in value.values():
                strings(item)
        elif isinstance(value, list):
            for item in value:
                strings(item)

    strings(c)
    slots = []
    for line in c["lines"]:
        slot = line["slot"]
        if type(slot) is not int or not 1 <= slot <= caps["max_lines"] or slot in slots:
            raise ValueError("line slot outside the projection or duplicated")
        slots.append(slot)
        if not line["username"] or not line["password"] or not line["server"]:
            raise ValueError("SIP username, password and server are required")
        if type(line["port"]) is not int or not 1 <= line["port"] <= 65535:
            raise ValueError("invalid SIP port")
        if line["transport"] not in ("udp", "tcp", "tls"):
            raise ValueError("unsupported SIP transport")
        if profile["vendor"] == "snom" and line["auth_username"] not in ("", line["username"]):
            raise ValueError("this Snom subset requires auth username equal to SIP username")
    key_caps = {k["slot"]: k for k in caps["key_slots"]}
    seen = set()
    for key in c["keys"]:
        slot = key["slot"]
        if slot not in key_caps or slot in seen:
            raise ValueError("unknown or duplicated key slot")
        seen.add(slot)
        bound = key_caps[slot]
        if key["function"] not in bound["functions"]:
            raise ValueError("unsupported key function")
        if type(key["line"]) is not int or key["line"] not in slots:
            raise ValueError("key must reference a configured line")
        for name in ("dial_target", "subscription_target", "pickup_target"):
            if len(key.get(name, "").encode()) > bound["max_value_bytes"]:
                raise ValueError("key target exceeds byte limit")
        if len(key["label"].encode()) > bound["max_label_bytes"]:
            raise ValueError("key label exceeds byte limit")
        if key["function"] != "line" and not key["dial_target"]:
            raise ValueError("key dial target is required")
        if key["subscription_target"] and key["subscription_target"] != key["dial_target"]:
            raise ValueError("this subset requires one common dial and subscription target")
        if key["event_package"] not in ({"", "dialog"} if key["function"] in {"line", "speed_dial"} else {"dialog"}):
            raise ValueError("only the dialog event package is mapped")
        if key["pickup_target"] and profile["vendor"] != "yealink":
            raise ValueError("separate pickup target is not mapped for this subset")
    for kind, feature, allowed, active in (
        ("keys", "clear_keys", set(key_caps), seen),
        ("lines", "disable_lines", {str(slot) for slot in range(1, caps["max_lines"] + 1)},
         {str(slot) for slot in slots}),
    ):
        retired = c["retired"][kind]
        if (not isinstance(retired, list)
                or any(not isinstance(slot, str) or not re.fullmatch(r"[1-9][0-9]*", slot)
                       or slot not in allowed for slot in retired)
                or len(set(retired)) != len(retired)):
            raise ValueError(f"invalid_retired_{kind}")
        if set(retired) & active:
            raise ValueError(f"retired_{kind}_still_active")
        if retired and caps.get(feature, False) is not True:
            raise ValueError(f"profile_{feature}_unsupported")
    if c["physical"]["language"] not in ("", *caps["languages"].values()):
        raise ValueError("language has no declared vendor mapping")
    if c["directory"]["ldap"]["host"] and "ldap" not in caps["functions"]:
        raise ValueError("LDAP is not mapped for this subset")
    if not c["lines"] and c["directory"]["ldap"]["host"]:
        raise ValueError("unassigned hardware configuration cannot include LDAP credentials")
    if c["provisioning"]["url"] and "provision" not in caps["functions"]:
        raise ValueError("provisioning URL is not mapped for this subset")
    return c


def render_phone_bundle(profile, templates, context):
    """Render sparse templates only; downloaded Python is never imported."""
    context = validate_context(profile, context)
    environment = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False, keep_trailing_newline=True,
                                       loader=DictLoader(templates))
    # No unbounded helpers or ambient HA state are exposed to the template.
    environment.globals.clear()
    environment.globals["range"] = safe_range
    files = {}
    mac = str(context["physical"]["mac"])
    if not re.fullmatch(r"[0-9a-f]{12}", mac):
        raise ValueError("invalid_phone_mac")
    for output in profile.get("outputs", []):
        if "{" in str(output["path"]).replace("{mac}", "") or "}" in str(output["path"]).replace("{mac}", ""):
            raise ValueError("invalid_profile_path")
        path = safe_profile_path(str(output["path"]).replace("{mac}", mac))
        template_path = safe_profile_path(output["template"])
        if template_path not in templates:
            raise ValueError("missing_profile_template")
        environment.autoescape = output["format"] == "xml"
        value = environment.from_string(templates[template_path]).render(context).encode()
        if (len(value) > 256 * 1024 or b"PRIVATE KEY-----" in value
                or re.search(rb"\{\$[A-Za-z_]", value)):
            raise ValueError("unsafe_profile_output")
        if output["format"] == "xml":
            if b"<!DOCTYPE" in value.upper() or b"<!ENTITY" in value.upper():
                raise ValueError("unsafe_profile_xml")
            ElementTree.fromstring(value)
        elif output["format"] != "cfg":
            raise ValueError("unsupported_profile_format")
        if path in files:
            raise ValueError("duplicate_profile_output")
        files[path] = value
    if not files:
        raise ValueError("empty_profile_output")
    digest = hashlib.sha256()
    for name, value in sorted(files.items()):
        digest.update(name.encode() + b"\0" + value + b"\0")
    return files, digest.hexdigest()
