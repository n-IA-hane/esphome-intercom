"""Opt-in, pinned and hash-verified phone profile assets, absent from HACS."""

from __future__ import annotations

import asyncio
import hashlib
from io import BytesIO
import json
from pathlib import Path
import re
import shutil
import zipfile

from .phone_profile import safe_profile_path

CATALOG_REPOSITORY = "n-IA-hane/voip-stack-phone-profiles"
CATALOG_SCHEMA = 1
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_BUNDLE_BYTES = 8 * 1024 * 1024


def catalog_channel():
    from .const import INTEGRATION_VERSION
    return "dev" if "dev" in INTEGRATION_VERSION else "main"


def validate_catalog(data):
    if not isinstance(data, dict) or data.get("schema_version") != CATALOG_SCHEMA:
        raise ValueError("phone_catalog_schema_unsupported")
    profiles = data.get("profiles")
    if not isinstance(profiles, list) or len(profiles) > 3000:
        raise ValueError("invalid_phone_catalog")
    declared_revision = data.get("catalog_revision", "")
    calculated = hashlib.sha256(json.dumps({key: value for key, value in data.items()
        if key != "catalog_revision"}, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    if declared_revision != calculated:
        raise ValueError("phone_catalog_revision_mismatch")
    identities = set()
    for profile in profiles:
        identity = str(profile.get("id") or "")
        if not re.fullmatch(r"[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+", identity) or identity in identities:
            raise ValueError("invalid_phone_profile_id")
        identities.add(identity)
        if profile.get("status") not in {"imported", "software_validated", "protocol_verified", "hardware_qualified"}:
            raise ValueError("invalid_phone_profile_status")
        bundle = profile.get("bundle")
        if bundle is not None:
            safe_profile_path(bundle["path"])
            if (not re.fullmatch(r"[0-9a-f]{64}", str(bundle.get("sha256")))
                    or not 0 < int(bundle.get("size", 0)) <= MAX_BUNDLE_BYTES):
                raise ValueError("invalid_phone_profile_bundle")
    return data


def decode_profile_bundle(raw, metadata):
    expected = metadata["bundle"]
    if len(raw) != expected["size"] or hashlib.sha256(raw).hexdigest() != expected["sha256"]:
        raise ValueError("phone_profile_digest_mismatch")
    files = {}
    with zipfile.ZipFile(BytesIO(raw)) as archive:
        if len(archive.infolist()) > 256 or sum(item.file_size for item in archive.infolist()) > MAX_BUNDLE_BYTES:
            raise ValueError("phone_profile_bundle_too_large")
        for item in archive.infolist():
            name = safe_profile_path(item.filename)
            if item.is_dir():
                continue
            if ((item.external_attr >> 16) & 0o170000) == 0o120000 or name in files:
                raise ValueError("invalid_phone_profile_member")
            value = archive.read(item)
            if b"PRIVATE KEY-----" in value:
                raise ValueError("unsafe_phone_profile_asset")
            files[name] = value
    if "profile.json" not in files or "LICENSES/MPL-1.1.txt" not in files or "NOTICE" not in files:
        raise ValueError("phone_profile_notices_missing")
    profile = json.loads(files["profile.json"])
    if profile.get("schema_version") != CATALOG_SCHEMA or profile.get("id") != metadata["id"]:
        raise ValueError("phone_profile_identity_mismatch")
    # Capabilities originate from the catalog; model bundles cannot widen them.
    if profile.get("capabilities") != metadata.get("capabilities"):
        raise ValueError("phone_profile_capabilities_mismatch")
    templates = {name: value.decode("utf-8") for name, value in files.items()
                 if name.startswith("templates/") and name.endswith(".jinja2")}
    if not templates:
        raise ValueError("phone_profile_templates_missing")
    for output in profile.get("outputs", []):
        safe_profile_path(output["path"].replace("{mac}", "000000000000"))
        if output.get("template") not in templates or output.get("format") not in {"xml", "cfg"}:
            raise ValueError("invalid_phone_profile_output")
    return profile, templates


def _private_write(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as stream:
        stream.write(data)
    temporary.chmod(0o600)
    temporary.replace(path)


class PhoneProfileCatalog:
    """No instance, I/O or background task exists until the feature is enabled."""

    def __init__(self, hass):
        self.hass = hass
        self.root = Path(hass.config.path(".storage", "voip_stack", "phone_profiles"))
        self.commit = ""
        self.manifest = None
        self._lock = asyncio.Lock()

    async def _fetch(self, url, limit):
        from homeassistant.helpers.aiohttp_client import async_get_clientsession
        session = async_get_clientsession(self.hass)
        async with asyncio.timeout(25):
            async with session.get(url) as response:
                response.raise_for_status()
                chunks, size = [], 0
                async for chunk in response.content.iter_chunked(65536):
                    size += len(chunk)
                    if size > limit:
                        raise ValueError("phone_catalog_download_too_large")
                    chunks.append(chunk)
        return b"".join(chunks)

    async def async_manifest(self, *, refresh=False, commit=""):
        async with self._lock:
            if commit and not re.fullmatch(r"[0-9a-f]{40}", commit):
                raise ValueError("invalid_phone_catalog_revision")
            current_path = self.root / "current.json"
            if not commit and not refresh:
                try:
                    raw = await self.hass.async_add_executor_job(current_path.read_bytes)
                    cached = json.loads(raw)
                    if not re.fullmatch(r"[0-9a-f]{40}", str(cached["commit"])):
                        raise ValueError("invalid_phone_catalog_revision")
                    self.manifest = validate_catalog(cached["manifest"])
                    self.commit = cached["commit"]
                    return self.manifest
                except (OSError, ValueError, KeyError, TypeError):
                    pass
            selected = commit
            if not selected:
                ref = json.loads(await self._fetch(
                    f"https://api.github.com/repos/{CATALOG_REPOSITORY}/git/ref/heads/{catalog_channel()}",
                    MAX_MANIFEST_BYTES))
                selected = str(ref.get("object", {}).get("sha") or "")
            if not re.fullmatch(r"[0-9a-f]{40}", selected):
                raise ValueError("invalid_phone_catalog_revision")
            cached_path = self.root / (selected + ".json")
            try:
                raw = await self.hass.async_add_executor_job(cached_path.read_bytes)
            except OSError:
                raw = await self._fetch(
                    f"https://raw.githubusercontent.com/{CATALOG_REPOSITORY}/{selected}/manifest.json",
                    MAX_MANIFEST_BYTES)
            manifest = validate_catalog(json.loads(raw))
            await self.hass.async_add_executor_job(_private_write, cached_path, raw)
            if not commit or not self.commit:
                self.commit, self.manifest = selected, manifest
            if not commit:
                current = json.dumps({"commit": selected, "manifest": manifest}, sort_keys=True).encode()
                await self.hass.async_add_executor_job(_private_write, current_path, current)
            return manifest

    async def async_profile(self, identity, *, commit=""):
        manifest = await self.async_manifest(commit=commit)
        metadata = next((item for item in manifest["profiles"] if item["id"] == identity), None)
        if metadata is None or metadata.get("status") == "imported" or not metadata.get("bundle"):
            raise ValueError("phone_profile_not_ready")
        selected = commit or self.commit
        bundle = metadata["bundle"]
        path = self.root / "bundles" / (bundle["sha256"] + ".zip")
        try:
            raw = await self.hass.async_add_executor_job(path.read_bytes)
        except OSError:
            raw = await self._fetch(
                f"https://raw.githubusercontent.com/{CATALOG_REPOSITORY}/{selected}/{safe_profile_path(bundle['path'])}",
                MAX_BUNDLE_BYTES)
        decoded = await self.hass.async_add_executor_job(decode_profile_bundle, raw, metadata)
        await self.hass.async_add_executor_job(_private_write, path, raw)
        return decoded

    async def async_clear(self):
        def clear():
            if self.root.is_symlink():
                raise ValueError("invalid_phone_catalog_cache")
            if self.root.exists():
                shutil.rmtree(self.root)
        await self.hass.async_add_executor_job(clear)
        self.commit, self.manifest = "", None
