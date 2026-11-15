"""Bounded vendor HTTP application through the phone's existing native form."""

from __future__ import annotations

from hashlib import md5
from html.parser import HTMLParser
import re
import time
from xml.etree import ElementTree

from yarl import URL

MAX_PAGE_BYTES = 256 * 1024
SAFE_PHONE_ERROR_CODES = frozenset({
    "phone_provisioning_error", "phone_ldap_mode_unsupported", "phone_configuration_changed",
    "phone_apply_target_unverified", "phone_apply_unsupported", "phone_apply_credentials_required",
    "phone_apply_authentication_failed", "phone_apply_http_error", "phone_apply_response_invalid",
    "phone_apply_identity_mismatch", "phone_apply_provisioning_not_configured",
    "phone_apply_waiting_idle", "phone_apply_result_uncertain",
})


def is_safe_phone_error(value):
    """Keep domain codes and legacy exception class labels, never messages."""
    return isinstance(value, str) and (value == "" or value in SAFE_PHONE_ERROR_CODES
        or re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}(?:Error|Exception)", value) is not None)


def safe_phone_error(error):
    """Project an exception without HTTP bodies, URLs or credential arguments."""
    code = str(error)
    if code in SAFE_PHONE_ERROR_CODES:
        return code
    code = type(error).__name__
    return code if is_safe_phone_error(code) else "phone_provisioning_error"


class PhoneApplyError(ValueError):
    """A safe code, never an HTTP body or credential-bearing exception."""


class PhoneApplyDeferred(PhoneApplyError):
    """The existing owner will reconsider the latest revision when idle."""


class _NativeForm(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.values = {}
        self.ids = set()
        self._select = ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("id"):
            self.ids.add(attrs["id"])
        if tag == "input" and attrs.get("name"):
            self.values[attrs["name"]] = attrs.get("value", "")
        elif tag == "select":
            self._select = attrs.get("name") or attrs.get("id", "")
        elif tag == "option" and self._select:
            if "selected" in attrs or self._select not in self.values:
                self.values[self._select] = attrs.get("value", "")

    def handle_endtag(self, tag):
        if tag == "select":
            self._select = ""


async def async_apply_fanvil(session, origin, phone, profile, expected_server, *, still_current,
                             on_dispatch=None):
    """Request one download, after native login, identity and idle validation.

    The caller owns the HTTP session and the revision. A successful response
    confirms the request only; file delivery and device adoption remain distinct.
    """
    base = URL(origin)
    if (base.scheme not in {"http", "https"} or not base.host or base.user is not None
            or base.query_string or base.fragment or base.path not in {"", "/"}):
        raise PhoneApplyError("phone_apply_target_unverified")
    capability = profile["capabilities"].get("apply", {})
    if not isinstance(capability, dict) or capability.get("method") != "fanvil_native_http":
        raise PhoneApplyError("phone_apply_unsupported")
    versions = capability.get("firmwares", [])
    model = capability.get("model", "")
    if (not isinstance(model, str) or not model or len(model) > 100
            or not isinstance(versions, list) or not 1 <= len(versions) <= 8
            or any(not isinstance(version, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", version) for version in versions)):
        raise PhoneApplyError("phone_apply_unsupported")
    username = phone.get("admin_username", "")
    password = phone.get("admin_password", "")
    if not username or not password:
        raise PhoneApplyError("phone_apply_credentials_required")

    async def current():
        if not await still_current():
            raise PhoneApplyDeferred("phone_configuration_changed")

    async def request(method, path, *, data=None, final_command=False):
        await current()
        url = base.join(URL(path))
        if final_command and on_dispatch is not None:
            on_dispatch()
        async with session.request(method, url, data=data, headers={"Connection": "keep-alive"}, allow_redirects=False) as response:
            if response.status in {301, 302, 303} and method == "POST" and path == "/":
                redirect = url.join(URL(response.headers.get("Location", "")))
                if redirect.origin() != base.origin() or redirect.path not in {"/", "/index.htm", "/information.htm"}:
                    raise PhoneApplyError("phone_apply_authentication_failed")
                return await request("GET", str(redirect.relative()))
            if response.status != 200:
                raise PhoneApplyError("phone_apply_http_error")
            content = bytearray()
            async for chunk in response.content.iter_chunked(8192):
                content.extend(chunk)
                if len(content) > MAX_PAGE_BYTES:
                    raise PhoneApplyError("phone_apply_response_invalid")
        # The command might already have taken effect. A call or configuration
        # change during its ACK cannot turn it back into a safe deferred request.
        if not final_command:
            await current()
        return bytes(content).decode("utf-8", errors="replace")

    async def idle():
        body = await request("GET", "/callInfo.xml")
        if "<!DOCTYPE" in body.upper() or "<!ENTITY" in body.upper():
            raise PhoneApplyError("phone_apply_response_invalid")
        try:
            calls = ElementTree.fromstring(body).findtext(".//calls")
        except ElementTree.ParseError as error:
            raise PhoneApplyError("phone_apply_response_invalid") from error
        if calls != "0":
            raise PhoneApplyDeferred("phone_apply_waiting_idle")

    login = _NativeForm()
    login.feed(await request("GET", "/"))
    # The native UI skips login when the landing is already an authorized
    # frameset. Identity and all command guards below are still mandatory.
    if "logonButton" in login.ids:
        language = login.values.get("CurLanguage") or login.values.get("langSelect", "")
        session.cookie_jar.update_cookies({"CUR_LANG": language}, response_url=base)
        nonce = await request("GET", "/key==nonce?now=" + str(int(time.time() * 1000)))
        if not nonce or len(nonce) > 256 or any(ord(char) < 33 or ord(char) == 127 for char in nonce):
            raise PhoneApplyError("phone_apply_authentication_failed")
        session.cookie_jar.update_cookies({"auth": nonce, "CUR_LANGCLICK": "false"}, response_url=base)
        encoded = username + ":" + md5((username + ":" + password + ":" + nonce).encode(), usedforsecurity=False).hexdigest()
        await request("POST", "/", data={"encoded": encoded,
            "CurLanguage": language, "ReturnPage": "/"})
    information = await request("GET", "/information.htm")
    information_form = _NativeForm()
    information_form.feed(information)
    if "logonButton" in information_form.ids:
        raise PhoneApplyError("phone_apply_authentication_failed")
    macs = {re.sub(r"[:-]", "", match).lower() for match in re.findall(
        r"(?i)(?<![0-9a-f])(?:(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}|[0-9a-f]{12})(?![0-9a-f])", information)}
    model = re.sub(r"[^a-z0-9]", "", model.lower())
    normalized = re.sub(r"[^a-z0-9]", "", information.lower())
    firmware = next((version for version in versions if re.search(r"(?<![\d.])" + re.escape(version) + r"(?![\d.])", information)), "")
    if phone["mac"] not in macs or not model or model not in normalized or not firmware:
        raise PhoneApplyError("phone_apply_identity_mismatch")
    await idle()
    form = _NativeForm()
    form.feed(await request("GET", "/provision.htm"))
    expected_filename = profile["outputs"][0]["path"].replace("{mac}", phone["mac"])
    if (form.values.get("FTP_AutoServer_R", "").rstrip("/") != expected_server.rstrip("/")
            or form.values.get("FTP_AutoFileName_R") != expected_filename
            or form.values.get("FTP_AutoDownloadMode") not in {"1", "2"}):
        raise PhoneApplyError("phone_apply_provisioning_not_configured")
    await idle()
    result = _NativeForm()
    result.feed(await request("POST", "/provision.htm", data={"autoProvisionNow": "autopNow"},
                              final_command=True))
    if "logonButton" in result.ids:
        raise PhoneApplyError("phone_apply_authentication_failed")
    return {"method": "fanvil_native_http", "request_status": "accepted", "firmware": firmware}
