"""Real optional model bundles interoperate with the production HA decoder.

Set VOIP_PHONE_PROFILE_CATALOG_PATH to a separate checkout of our asset repo.
No fixture replaces the catalog, bundle, decoder or renderer. Missing external
assets are explicitly skipped; an explicitly configured bad path is a failure.
"""

from copy import deepcopy
from hashlib import sha256
from importlib import util
from io import BytesIO
import json
import os
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest

from .test_groups import _load_module

phone_profile = _load_module("phone_profile")
catalog_module = _load_module("phone_profile_catalog")


@pytest.fixture(scope="module")
def real_assets():
    location = os.environ.get("VOIP_PHONE_PROFILE_CATALOG_PATH")
    if not location:
        pytest.skip("separate phone-profile catalog checkout not configured")
    root = Path(location)
    assert (root / "manifest.json").is_file(), "configured catalog manifest is missing"
    manifest = json.loads((root / "manifest.json").read_text())
    catalog_module.validate_catalog(manifest)
    spec = util.spec_from_file_location("asset_reference_render", root / "phone_profiles/render.py")
    reference = util.module_from_spec(spec)
    spec.loader.exec_module(reference)
    context = json.loads((root / "examples/context.json").read_text())
    return root, manifest, reference, context


def decoded(assets, identity):
    root, manifest, _, _ = assets
    metadata = next(item for item in manifest["profiles"] if item["id"] == identity)
    raw = (root / metadata["bundle"]["path"]).read_bytes()
    profile, templates = catalog_module.decode_profile_bundle(raw, metadata)
    return metadata, profile, templates


def context_for(assets, profile):
    _, _, reference, example = assets
    context = deepcopy(example)
    caps = profile["capabilities"]
    context["physical"].update(name='È & <Casa "test">', language="", language_code="")
    if caps["languages"]:
        code = "it" if "it" in caps["languages"] else next(iter(caps["languages"]))
        context["physical"].update(language=caps["languages"][code], language_code=code)
    if not caps["key_slots"]:
        context["keys"] = []
    else:
        context["keys"][0]["label"] = "È & P4"
    return reference.normalized_context(context)


def test_real_manifest_all_98_bundles_decode_without_catalog_widening(real_assets):
    _, manifest, _, _ = real_assets
    ready = [item for item in manifest["profiles"] if item["status"] == "software_validated"]
    assert len(ready) == 98
    for metadata in ready:
        _, profile, templates = decoded(real_assets, metadata["id"])
        assert profile["id"] == metadata["id"]
        assert profile["capabilities"] == metadata["capabilities"]
        assert templates and all(name.startswith("templates/") for name in templates)
    for metadata in manifest["profiles"]:
        if metadata["status"] == "imported":
            assert "bundle" not in metadata and metadata["reason"]


def test_all_real_bundles_root_and_mit_reference_render_identical_bytes(real_assets):
    _, manifest, reference, _ = real_assets
    no_language = 0
    for metadata in manifest["profiles"]:
        if metadata["status"] != "software_validated":
            continue
        _, profile, templates = decoded(real_assets, metadata["id"])
        context = context_for(real_assets, profile)
        expected = reference.render(profile, templates, context)
        actual, digest = phone_profile.render_phone_bundle(profile, templates, context)
        assert actual == expected, metadata["id"]
        if not profile["capabilities"]["languages"]:
            no_language += 1
            assert context["physical"]["language"] == ""
        check = sha256()
        for name, body in sorted(actual.items()):
            check.update(name.encode() + b"\0" + body + b"\0")
        assert digest == check.hexdigest()
        assert all(b"PRIVATE KEY-----" not in value for value in actual.values())
    assert no_language > 0


def test_real_bundles_accept_actual_minimal_context_with_ldap_disabled(real_assets):
    _, manifest, reference, _ = real_assets
    # The runtime legitimately emits directory={} when LDAP is disabled. Do not
    # pre-normalize this input in the test, or StrictUndefined regressions hide.
    raw = {"physical": {"name": "Fixture", "mac": "020000000001"}, "directory": {},
           "lines": [{"slot": 1, "username": "fixture480", "password": "fixture-only",
                      "server": "192.0.2.10", "port": 5060, "transport": "udp"}]}
    for metadata in manifest["profiles"]:
        if metadata["status"] != "software_validated":
            continue
        _, profile, templates = decoded(real_assets, metadata["id"])
        expected = reference.render(profile, templates, raw)
        actual, _ = phone_profile.render_phone_bundle(profile, templates, raw)
        assert actual == expected, metadata["id"]


@pytest.mark.parametrize("field", ["username", "password", "server", "display_name"])
def test_real_cfg_control_character_injection_is_rejected_by_both_renderers(real_assets, field):
    _, profile, templates = decoded(real_assets, "yealink/t46s")
    _, _, reference, _ = real_assets
    for injection in ("ok\nnetwork.primary_dns=203.0.113.250", "ok\r\naccount.2.enable=1", "ok\0bad"):
        context = context_for(real_assets, profile)
        context["lines"][0][field] = injection
        with pytest.raises(ValueError):
            reference.render(profile, templates, context)
        with pytest.raises(ValueError):
            phone_profile.render_phone_bundle(profile, templates, context)


def test_real_fanvil_xml_escapes_user_values_and_preserves_directory_filters(real_assets):
    _, profile, templates = decoded(real_assets, "fanvil/x3u-pro")
    _, _, reference, _ = real_assets
    context = context_for(real_assets, profile)
    context["directory"]["ldap"].update(host="192.0.2.10", port=1389,
        base_dn="ou=phonebook,dc=voip", bind_dn="uid=fixture,dc=voip", password="fixture-only-<&",
        name_filter="(&(objectClass=person)(cn=%))", number_filter="(|(telephonenumber=%)(mobile=%))")
    expected = reference.render(profile, templates, context)
    actual, _ = phone_profile.render_phone_bundle(profile, templates, context)
    assert actual == expected
    body = next(iter(actual.values()))
    assert b"&lt;Casa" in body and b"&amp;" in body
    assert b"(&(objectClass" not in body


@pytest.mark.parametrize("function,target", [("line", ""), ("speed_dial", "*123")])
def test_real_core_resolved_non_dialog_keys_match_asset_reference(real_assets, function, target):
    _, profile, templates = decoded(real_assets, "fanvil/x3u-pro")
    _, _, reference, _ = real_assets
    runtime = SimpleNamespace(queues=None, endpoints=SimpleNamespace(resolve=lambda _value: None))
    phone = {"lines": [{"slot": "1", "endpoint_id": "sip:fixture"}], "keys": [
        {"slot": "1", "function": function, "line": "1", "target": target, "label": "SIP1"}]}
    context = context_for(real_assets, profile)
    context["keys"] = phone_profile.resolve_phone_keys(runtime, phone, profile)
    assert context["keys"][0]["event_package"] == ""
    assert phone_profile.render_phone_bundle(profile, templates, context)[0] == reference.render(profile, templates, context)


def minimal_bundle(files):
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path, value in files.items():
            archive.writestr(path, value)
    raw = buffer.getvalue()
    return raw, {"id": "fanvil/fixture", "capabilities": {},
                 "bundle": {"size": len(raw), "sha256": sha256(raw).hexdigest()}}


def test_decoder_checks_digest_before_parsing_archive():
    raw, metadata = minimal_bundle({"ignored": b"test"})
    with pytest.raises(ValueError, match="digest_mismatch"):
        catalog_module.decode_profile_bundle(raw + b"tampered", metadata)


@pytest.mark.parametrize("path", ["../escape", "/absolute", "templates/../../escape", "bad\\path"])
def test_decoder_rejects_archive_path_escape_before_member_use(path):
    raw, metadata = minimal_bundle({path: b"not executable"})
    with pytest.raises(ValueError, match="invalid_profile_path"):
        catalog_module.decode_profile_bundle(raw, metadata)


def test_decoder_bundle_cannot_widen_manifest_capabilities():
    profile = {"schema_version": 1, "id": "fanvil/fixture", "capabilities": {"max_lines": 999}}
    raw, metadata = minimal_bundle({"profile.json": json.dumps(profile), "NOTICE": "fixture",
        "LICENSES/MPL-1.1.txt": "fixture", "templates/config.jinja2": "fixture"})
    with pytest.raises(ValueError, match="capabilities_mismatch"):
        catalog_module.decode_profile_bundle(raw, metadata)


def test_catalog_revision_covers_metadata_tampering():
    value = {"schema_version": 1, "profiles": [], "catalog_revision": "0"*64}
    with pytest.raises(ValueError, match="revision_mismatch"):
        catalog_module.validate_catalog(value)
