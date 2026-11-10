"""Persistent paging groups projected into the existing phone directory."""

from __future__ import annotations

from uuid import uuid4

from .roster import RosterEntry

MAX_PAGING_TARGETS = 16


def normalize_paging_group(data, *, identity=""):
    name = str(data.get("name") or "").strip()
    extension = str(data.get("extension") or "").strip()
    members = tuple(dict.fromkeys(str(value).strip() for value in data.get("members", ()) if str(value).strip()))
    if not name or len(name) > 128 or (extension and (not extension.isdecimal() or len(extension) > 32)):
        raise ValueError("A paging group needs a name and an optional numeric extension")
    if not members or len(members) > MAX_PAGING_TARGETS:
        raise ValueError("Select between one and sixteen paging phones")
    answer_timeout = float(data.get("answer_timeout", 5))
    timeout = float(data.get("timeout", 120))
    if not 1 <= answer_timeout <= 120 or not 1 <= timeout <= 600:
        raise ValueError("Paging timeouts exceed the supported limits")
    return {"paging_group_id": str(identity or data.get("paging_group_id") or uuid4().hex),
            "name": name, "extension": extension, "members": list(members),
            "answer_timeout": answer_timeout, "timeout": timeout,
            "enabled": bool(data.get("enabled", True))}


def paging_group_configs(entry):
    return {str(item.data.get("paging_group_id") or item.subentry_id): dict(item.data)
            for item in entry.subentries.values() if item.subentry_type == "paging"}


def paging_group_entries(entry):
    return [RosterEntry(id=f"paging:{identity}", name=data["name"],
                        extension=str(data.get("extension") or ""), ha_bridge=True,
                        enabled=bool(data.get("enabled", True)),
                        metadata={"virtual_endpoint": "paging", "paging_group_id": identity,
                                  "endpoint_id": f"service:paging:{identity}", "endpoint_kind": "service"})
            for identity, data in paging_group_configs(entry).items()]
