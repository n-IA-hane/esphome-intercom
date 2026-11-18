"""The native HTTP command is authenticated, scoped and independently observed."""

from hashlib import md5
import asyncio
from base64 import b64encode
from copy import deepcopy
from datetime import timedelta
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp import ClientSession, CookieJar, web
import pytest

from .test_managed_phones_ha import custom_integration_path as custom_integration_path
from .test_physical_phone_runtime_ha import (
    cached_physical_config as cached_physical_config, hass_config_dir as hass_config_dir,
    hass_storage as hass_storage, phone_lab as phone_lab,
)

pytestmark = pytest.mark.ha
SERVER = "https://ha.example.test/private-fixture"


@pytest.fixture
async def fanvil_http(aiohttp_server, hass, socket_enabled):
    from custom_components.voip_stack.physical_phone_apply import async_apply_fanvil

    state = SimpleNamespace(mac="00:11:22:33:44:55", model="X3U Pro", firmware="2.12.20",
        calls=0, posts=[], gets=[], revoked=False, late_busy=False, auth_ok=True, redirect=False, server=SERVER,
        preauthenticated=False, unknown_landing=False, information_status=200, final_login=False,
        password="fixture-secret", on_trigger=None, basic_calls=False, basic_password="fixture-secret",
        basic_call_requests=[], provision_auth=[], final_status=200, on_challenge=None,
        language_cookie="", gui_logged_in=False, commands=[], command_auth=[], on_calls=None,
        username="fixture-admin", disconnect_command=False)
    login_html = ('<form id="login" method="post"><input id="username"><input id="password" type="password">'
        '<input id="encoded" name="encoded"><input name="CurLanguage" value="it">'
        '<input id="logonButton" type="button" value="Login"></form>')

    async def root(request):
        if request.method == "GET":
            state.gets.append(request.path)
            if state.preauthenticated:
                response = web.Response(text='<frameset><frame name="main" src="/information.htm"></frameset>')
                if state.language_cookie:
                    response.set_cookie("CUR_LANG", state.language_cookie)
                return response
            if state.unknown_landing:
                return web.Response(text='<html>Unknown landing</html>')
            return web.Response(text=login_html)
        form = dict(await request.post())
        state.posts.append((request.path, form))
        expected = state.username + ":" + md5((state.username + ":" + state.password + ":fixture-nonce").encode(), usedforsecurity=False).hexdigest()
        language = state.language_cookie if state.preauthenticated or state.unknown_landing else "it"
        assert form == {"encoded": expected, "CurLanguage": language, "ReturnPage": "/"}
        assert request.cookies.get("auth") == "fixture-nonce"
        if state.redirect:
            raise web.HTTPFound("http://outside.invalid/")
        response = web.Response(text="Authenticated frames")
        if state.auth_ok:
            state.gui_logged_in = True
            response.set_cookie("session", "fixture-session")
        return response

    async def nonce(request):
        state.gets.append(request.path)
        assert request.query.get("now", "").isdigit()
        if state.preauthenticated:
            return web.Response(text="")
        return web.Response(text="fixture-nonce")

    async def information(request):
        state.gets.append(request.path)
        if state.information_status != 200:
            return web.Response(status=state.information_status)
        if not state.preauthenticated and request.cookies.get("session") != "fixture-session":
            return web.Response(text=login_html)
        if state.late_busy:
            state.calls = 1
        return web.Response(text=f"Fanvil {state.model} Firmware {state.firmware} MAC {state.mac}")

    async def calls(request):
        state.gets.append(request.path)
        if state.on_calls is not None:
            state.on_calls()
        if state.basic_calls:
            authorization = request.headers.get("Authorization", "")
            state.basic_call_requests.append(bool(authorization))
            expected = "Basic " + b64encode((state.username + ":" + state.basic_password).encode()).decode()
            if authorization != expected:
                if state.on_challenge is not None:
                    state.on_challenge()
                return web.Response(status=401, headers={"WWW-Authenticate": 'Basic realm="Fanvil"'})
        if not state.preauthenticated and request.cookies.get("session") != "fixture-session":
            return web.Response(status=401, text="Authentication required")
        return web.Response(text=f"<callInfo><calls>{state.calls}</calls></callInfo>")

    async def provision(request):
        state.provision_auth.append(bool(request.headers.get("Authorization")))
        state.gets.append(request.path)
        return web.Response(text=f'<input name="FTP_AutoServer_R" value="{state.server}">'
            '<input name="FTP_AutoFileName_R" value="001122334455.cfg">'
            '<select name="FTP_AutoDownloadMode"><option value="0">Disabled</option>'
            '<option value="1" selected>Update after reboot</option></select>')

    async def command(request):
        assert request.method == "GET" and dict(request.query) == {"key": "AutoP"}
        state.commands.append(dict(request.query))
        authorization = request.headers.get("Authorization", "")
        expected = "Basic " + b64encode((state.username + ":" + state.basic_password).encode()).decode()
        state.command_auth.append(authorization == expected)
        if authorization != expected:
            return web.Response(status=401, headers={"WWW-Authenticate": 'Basic realm="Fanvil"'})
        if state.on_trigger is not None:
            await state.on_trigger()
        if state.disconnect_command:
            request.transport.abort()
            return web.Response(text="Unacknowledged command")
        if state.final_login:
            return web.Response(text=login_html)
        if state.final_status != 200:
            return web.Response(status=state.final_status, headers={"WWW-Authenticate": 'Basic realm="Fanvil"'})
        return web.Response(text="Requested")

    app = web.Application()
    app.router.add_route("*", "/", root)
    app.router.add_get("/key==nonce", nonce)
    app.router.add_get("/information.htm", information)
    app.router.add_get("/callInfo.xml", calls)
    app.router.add_get("/provision.htm", provision)
    app.router.add_get("/cgi-bin/ConfigManApp.com", command)
    server = await aiohttp_server(app)
    state.origin = str(server.make_url("/"))
    state.phone = {"mac": "001122334455", "admin_username": "fixture-admin", "admin_password": "fixture-secret"}
    state.profile = {"capabilities": {"apply": {"method": "fanvil_native_http", "model": "X3U Pro", "firmwares": ["2.12.20"]}},
        "outputs": [{"path": "{mac}.cfg"}]}

    async def run():
        async def current():
            return not state.revoked
        async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as session:
            return await async_apply_fanvil(session, state.origin, state.phone, state.profile, SERVER, still_current=current)
    state.run = run
    return state


async def test_native_nonce_cookie_digest_and_single_AutoP_command(fanvil_http):
    state = fanvil_http
    result = await state.run()
    assert result == {"method": "fanvil_native_http", "request_status": "accepted", "firmware": "2.12.20"}
    assert [path for path, _ in state.posts] == ["/"]
    assert state.commands == [{"key": "AutoP"}] and state.command_auth == [True]
    assert state.gets.count("/callInfo.xml") == 2
    assert "fixture-secret" not in str(result) and SERVER not in str(result)


async def test_two_fresh_frameset_operations_use_AutoP_without_GUI_nonce(fanvil_http):
    state = fanvil_http
    state.preauthenticated = state.basic_calls = True
    for _ in range(2):
        result = await state.run()
        assert result["request_status"] == "accepted"
    assert state.gets == ["/", "/information.htm", "/callInfo.xml", "/provision.htm", "/callInfo.xml"] * 2
    assert not state.posts and "/key==nonce" not in state.gets
    assert state.commands == [{"key": "AutoP"}] * 2
    assert state.command_auth == [True, True]
    assert state.basic_call_requests == [True] * 4


async def test_public_reads_do_not_remove_separate_Basic_API_authentication(fanvil_http):
    state = fanvil_http
    state.preauthenticated = state.basic_calls = True
    state.language_cookie = "it"
    result = await state.run()
    assert result["request_status"] == "accepted"
    assert state.basic_call_requests == [True, True]
    assert state.provision_auth == [True]
    assert not state.posts and state.commands == [{"key": "AutoP"}]
    assert "/key==nonce" not in state.gets


async def test_bad_Basic_credentials_are_tried_once_and_never_dispatch_a_command(fanvil_http):
    state = fanvil_http
    state.preauthenticated = state.basic_calls = True
    state.basic_password = "different-fixture-password"
    with pytest.raises(ValueError, match="phone_apply_authentication_failed"):
        await state.run()
    assert state.basic_call_requests == [True]
    assert not state.posts and not state.provision_auth and not state.commands


async def test_API_guard_rechecks_current_before_sending_command(fanvil_http):
    state = fanvil_http
    state.preauthenticated = state.basic_calls = True
    def revoke():
        state.revoked = True
    state.on_calls = revoke
    with pytest.raises(ValueError, match="phone_configuration_changed"):
        await state.run()
    assert state.basic_call_requests == [True] and not state.posts and not state.commands


@pytest.mark.parametrize("status", [401, 503])
async def test_final_AutoP_error_is_never_replayed(fanvil_http, status):
    state = fanvil_http
    state.preauthenticated = True
    state.final_status = status
    with pytest.raises(ValueError, match="phone_apply_http_error"):
        await state.run()
    assert not state.posts and state.commands == [{"key": "AutoP"}]


@pytest.mark.parametrize("field,value,code", [
    ("mac", "00:11:22:33:44:99", "phone_apply_identity_mismatch"),
    ("information_status", 401, "phone_apply_http_error"),
    ("server", "https://other.invalid/", "phone_apply_provisioning_not_configured"),
])
async def test_preauthenticated_landing_never_bypasses_independent_guards(fanvil_http, field, value, code):
    state = fanvil_http
    state.preauthenticated = True
    setattr(state, field, value)
    with pytest.raises(ValueError, match=code):
        await state.run()
    assert not state.posts and not state.commands and "/key==nonce" not in state.gets
    if field in {"mac", "information_status"}:
        assert not state.basic_call_requests and not state.provision_auth and not state.command_auth


async def test_unknown_landing_is_not_proof_of_access(fanvil_http):
    state = fanvil_http
    state.unknown_landing = True
    with pytest.raises(ValueError, match="phone_apply_authentication_failed"):
        await state.run()
    assert not state.posts and not state.commands and state.gets == ["/", "/information.htm"]


async def test_final_command_login_form_is_not_an_accepted_application(fanvil_http):
    state = fanvil_http
    state.final_login = True
    with pytest.raises(ValueError, match="phone_apply_authentication_failed"):
        await state.run()
    assert [path for path, _ in state.posts] == ["/"]
    assert state.commands == [{"key": "AutoP"}]


@pytest.mark.parametrize("field,value,code", [
    ("mac", "00:11:22:33:44:99", "phone_apply_identity_mismatch"),
    ("model", "X6U", "phone_apply_identity_mismatch"),
    ("firmware", "2.12.20.1", "phone_apply_identity_mismatch"),
    ("auth_ok", False, "phone_apply_authentication_failed"),
    ("server", "https://other.invalid/", "phone_apply_provisioning_not_configured"),
    ("redirect", True, "phone_apply_authentication_failed"),
])
async def test_unverified_identity_auth_or_provisioning_never_dispatches_apply(fanvil_http, field, value, code):
    state = fanvil_http
    setattr(state, field, value)
    with pytest.raises(ValueError, match=code):
        await state.run()
    assert not state.commands


@pytest.mark.parametrize("late", [False, True])
async def test_busy_native_phone_is_deferred_before_application(fanvil_http, late):
    state = fanvil_http
    state.late_busy = late
    state.calls = 0 if late else 1
    with pytest.raises(ValueError, match="phone_apply_waiting_idle"):
        await state.run()
    assert not state.commands and [path for path, _ in state.posts] == ["/"]


@pytest.fixture
async def rapidlogic_http(socket_enabled):
    """Raw peer with the observed explicit-Connection framing behavior."""
    from urllib.parse import parse_qs, urlsplit

    state = SimpleNamespace(requests=[], errors=[], handlers=set(), writers=set(),
                            connections=0, auth_posts=0, triggers=0, drop_command=False)

    async def handle(reader, writer):
        task = asyncio.current_task()
        state.handlers.add(task)
        state.writers.add(writer)
        state.connections += 1
        connection = state.connections
        try:
            while True:
                try:
                    head = await reader.readuntil(b"\r\n\r\n")
                except asyncio.IncompleteReadError:
                    break
                lines = head.decode().split("\r\n")
                method, target, _version = lines[0].split(" ")
                headers = dict(line.lower().split(": ", 1) for line in lines[1:] if ": " in line)
                payload = await reader.readexactly(int(headers.get("content-length", "0")))
                path = urlsplit(target).path
                state.requests.append((method, path, headers.get("connection"), connection))
                cookie = ""
                if path == "/" and method == "GET":
                    body = (b'<form id="login"><input id="username"><input id="password" type="password">'
                            b'<input name="CurLanguage" value="it"><input id="logonButton" type="button"></form>')
                elif path == "/key==nonce":
                    body = b"raw-nonce"
                elif path == "/" and method == "POST":
                    form = parse_qs(payload.decode())
                    expected = "fixture-admin:" + md5(b"fixture-admin:fixture-secret:raw-nonce", usedforsecurity=False).hexdigest()
                    assert form["encoded"] == [expected]
                    assert "cur_lang=it" in headers.get("cookie", "")
                    state.auth_posts += 1
                    cookie = "Set-Cookie: session=raw-session; Path=/\r\n"
                    body = b"Authenticated"
                elif path == "/information.htm":
                    body = b"Fanvil X3U Pro 2.12.20 MAC 00:11:22:33:44:55"
                elif path == "/callInfo.xml":
                    body = b"<callInfo><calls>0</calls></callInfo>"
                elif path == "/provision.htm" and method == "GET":
                    body = (f'<input name="FTP_AutoServer_R" value="{SERVER}">'
                        '<input name="FTP_AutoFileName_R" value="001122334455.cfg">'
                        '<select name="FTP_AutoDownloadMode"><option value="1" selected>Enabled</option></select>').encode()
                else:
                    assert path == "/cgi-bin/ConfigManApp.com" and method == "GET"
                    assert parse_qs(urlsplit(target).query) == {"key": ["AutoP"]}
                    assert headers.get("authorization") == "basic " + b64encode(b"fixture-admin:fixture-secret").decode().lower()
                    state.triggers += 1
                    if state.drop_command:
                        break  # Side effect occurred; no response reaches client.
                    body = b"Requested"
                # With no explicit policy, the real server advertises chunking
                # but writes unframed HTML. Both explicit policies frame it.
                framed = headers.get("connection") in {"close", "keep-alive"}
                wire = (f"{len(body):x}\r\n".encode() + body + b"\r\n0\r\n\r\n") if framed else body
                writer.write(("HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n" + cookie + "\r\n").encode() + wire)
                await writer.drain()
                # Match the captured close branch: no response Connection
                # header, followed by FIN after the correctly framed body.
                if not framed or headers.get("connection") == "close":
                    break
        except (ConnectionResetError, BrokenPipeError):
            pass
        except Exception as error:
            state.errors.append(error)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionResetError, BrokenPipeError):
                pass
            state.writers.discard(writer)
            state.handlers.discard(task)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    state.origin = "http://127.0.0.1:" + str(server.sockets[0].getsockname()[1])
    try:
        yield state
    finally:
        server.close()
        await server.wait_closed()
        for writer in tuple(state.writers):
            writer.close()
        if state.handlers:
            await asyncio.gather(*tuple(state.handlers))
        assert not state.errors


async def test_rapidlogic_implicit_policy_has_the_observed_invalid_chunk_framing(rapidlogic_http):
    from aiohttp import ClientError
    state = rapidlogic_http
    async with ClientSession() as session:
        with pytest.raises(ClientError, match="chunk"):
            async with session.get(state.origin) as response:
                await response.read()


async def test_rapidlogic_explicit_keepalive_completes_one_connection_auth_and_trigger(rapidlogic_http):
    from custom_components.voip_stack.physical_phone_apply import async_apply_fanvil

    state = rapidlogic_http
    phone = {"mac": "001122334455", "admin_username": "fixture-admin", "admin_password": "fixture-secret"}
    profile = {"capabilities": {"apply": {"method": "fanvil_native_http", "model": "X3U Pro", "firmwares": ["2.12.20"]}},
        "outputs": [{"path": "{mac}.cfg"}]}
    async def current():
        return True
    async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as session:
        result = await async_apply_fanvil(session, state.origin, phone, profile, SERVER, still_current=current)
    assert result["request_status"] == "accepted"
    assert state.connections == 1 and state.auth_posts == 1 and state.triggers == 1
    assert all(policy == "keep-alive" for _method, _path, policy, _connection in state.requests)
    assert [(method, path) for method, path, _policy, _conn in state.requests] == [
        ("GET", "/"), ("GET", "/key==nonce"), ("POST", "/"),
        ("GET", "/information.htm"), ("GET", "/callInfo.xml"), ("GET", "/provision.htm"),
        ("GET", "/callInfo.xml"), ("GET", "/cgi-bin/ConfigManApp.com")]


async def test_AutoP_disconnect_after_receipt_cannot_trigger_aiohttp_automatic_GET_replay(rapidlogic_http):
    from custom_components.voip_stack.physical_phone_apply import async_apply_fanvil

    state = rapidlogic_http
    state.drop_command = True
    phone = {"mac": "001122334455", "admin_username": "fixture-admin", "admin_password": "fixture-secret"}
    profile = {"capabilities": {"apply": {"method": "fanvil_native_http", "model": "X3U Pro", "firmwares": ["2.12.20"]}},
        "outputs": [{"path": "{mac}.cfg"}]}
    async def current():
        return True
    async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as session:
        with pytest.raises(ValueError, match="phone_apply_result_uncertain"):
            await async_apply_fanvil(session, state.origin, phone, profile, SERVER, still_current=current)
    assert state.triggers == 1 and state.connections == 1


async def test_rapidlogic_close_branch_ends_the_stream_before_a_next_auth_request(rapidlogic_http):
    """The captured FIN is a transport boundary, not a password verdict."""
    from urllib.parse import urlsplit
    state = rapidlogic_http
    url = urlsplit(state.origin)
    reader, writer = await asyncio.open_connection(url.hostname, url.port)
    try:
        writer.write(b"GET / HTTP/1.1\r\nHost: fixture\r\nConnection: close\r\n\r\n")
        await writer.drain()
        head = await reader.readuntil(b"\r\n\r\n")
        assert b"Transfer-Encoding: chunked" in head and b"Connection:" not in head
        size = int((await reader.readline()).strip(), 16)
        await reader.readexactly(size + 2)
        assert await reader.readuntil(b"\r\n\r\n") == b"0\r\n\r\n"
        assert await reader.read() == b""
        assert state.auth_posts == 0 and state.triggers == 0
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.fixture
async def apply_owner(cached_physical_config, monkeypatch, request):
    from custom_components.voip_stack import physical_phone_apply

    state = cached_physical_config
    profile, templates = await state.manager.catalog.async_profile(state.data["profile_id"], commit="")
    profile = deepcopy(profile)
    profile["capabilities"]["apply"] = {"method": "fanvil_native_http", "qualified": True,
        "model": "X3U Pro", "firmwares": ["2.12.20"]}
    options = getattr(request, "param", {})
    if options.get("unqualified"):
        profile["capabilities"]["apply"]["qualified"] = False
    templates = {name: text + "name={{ physical.name }}\n" for name, text in templates.items()}
    state.data = {**state.data, "host": "192.168.1.85", "catalog_commit": "f" * 40,
                  "admin_username": "fixture-admin", "admin_password": "fixture-admin-private"}
    if options.get("no_credentials"):
        state.data.update(admin_username="", admin_password="")
    state.lab.hass.config_entries.async_update_subentry(state.lab.entry, state.subentry, data=state.data)
    state.manager.catalog.async_profile.return_value = profile, templates
    await state.manager.async_prepare(state.data)
    state.real_apply = physical_phone_apply.async_apply_fanvil
    state.apply = AsyncMock(return_value={"method": "fanvil_native_http"})
    monkeypatch.setattr(physical_phone_apply, "async_apply_fanvil", state.apply)
    state.profile = profile
    return state


@pytest.mark.parametrize("apply_owner", [{"unqualified": True}, {"no_credentials": True}], indirect=True)
async def test_auto_apply_requires_both_qualified_profile_and_explicit_credentials(apply_owner):
    state = apply_owner
    state.manager.schedule({state.data["phone_id"]})
    await state.lab.hass.async_block_till_done()
    state.apply.assert_not_awaited()
    assert state.manager.public_state(state.data["phone_id"])["state"] == "ready"


async def test_save_events_coalesce_one_request_and_never_render_admin_credentials(apply_owner):
    state = apply_owner
    for _ in range(5):
        state.manager.schedule({state.data["phone_id"]})
    await state.lab.hass.async_block_till_done()
    assert state.apply.await_count == 1
    await state.manager.async_reconcile()
    await state.lab.hass.async_block_till_done()
    assert state.apply.await_count == 1
    public = state.manager.public_state(state.data["phone_id"])
    assert public["apply_status"] == "requested" and public["apply_attempted_revision"] == public["desired_revision"]
    prepared = state.manager.files[state.data["phone_id"]]
    assert "fixture-admin-private" not in str(public) + str(prepared.dependencies.phone) + str(prepared.dependencies.context)
    assert all(b"fixture-admin-private" not in body for body in prepared.files.values())


async def test_auto_apply_defers_busy_and_reuses_the_existing_idle_signal(apply_owner):
    state = apply_owner
    state.lab.runtime.endpoints.claim_call("sip:first", "busy-fixture")
    state.manager.schedule({state.data["phone_id"]})
    await state.lab.hass.async_block_till_done()
    state.apply.assert_not_awaited()
    state.lab.runtime.endpoints.release_call("sip:first", "busy-fixture")
    state.manager._wake_idle()
    await state.lab.hass.async_block_till_done()
    assert state.apply.await_count == 1


async def test_a_new_revision_retires_the_inflight_candidate_before_native_post(apply_owner):
    from custom_components.voip_stack.physical_phone_apply import PhoneApplyDeferred

    state = apply_owner
    entered, release = asyncio.Event(), asyncio.Event()
    posted = []

    async def apply(_session, _origin, phone, _profile, _server, *, still_current, on_dispatch):
        entered.set()
        await release.wait()
        if not await still_current():
            raise PhoneApplyDeferred("phone_configuration_changed")
        on_dispatch()
        posted.append(phone["name"])
        return {"method": "fanvil_native_http"}

    state.apply.side_effect = apply
    state.manager.schedule({state.data["phone_id"]})
    await asyncio.wait_for(entered.wait(), 2)
    state.data = {**state.data, "name": "Latest revision"}
    state.lab.hass.config_entries.async_update_subentry(state.lab.entry, state.subentry, data=state.data)
    await state.manager.async_reconcile()
    release.set()
    await state.lab.hass.async_block_till_done()
    assert posted == ["Latest revision"] and state.apply.await_count == 2


async def test_failed_authentication_is_not_retried_until_credentials_are_changed(apply_owner):
    from custom_components.voip_stack.physical_phone_apply import PhoneApplyError

    state = apply_owner
    state.apply.side_effect = PhoneApplyError("phone_apply_authentication_failed")
    await state.manager.async_reconcile()
    await state.lab.hass.async_block_till_done()
    for _ in range(3):
        state.manager.schedule({state.data["phone_id"]})
    await state.lab.hass.async_block_till_done()
    assert state.apply.await_count == 1
    state.data = {**state.data, "admin_password": "corrected-fixture-private"}
    state.lab.hass.config_entries.async_update_subentry(state.lab.entry, state.subentry, data=state.data)
    state.apply.side_effect = None
    await state.manager.async_reconcile()
    await state.lab.hass.async_block_till_done()
    assert state.apply.await_count == 2
    assert not state.manager.public_state(state.data["phone_id"])["apply_error"]


async def test_dhcp_target_follows_the_paired_registration_instance(apply_owner, monkeypatch):
    state = apply_owner
    state.manager.states[state.data["phone_id"]]["apply_target_instance"] = "urn:uuid:paired"
    contacts = [SimpleNamespace(instance_id="urn:uuid:other", source_host="192.168.1.85"),
                SimpleNamespace(instance_id="urn:uuid:paired", source_host="192.168.1.191")]
    monkeypatch.setattr(state.lab.registrar, "registered_contacts", lambda _username: contacts)
    origin, instance = await state.manager._apply_target(state.data, state.profile["capabilities"]["apply"])
    assert origin == "http://192.168.1.191" and instance == "urn:uuid:paired"
    assert state.data["host"] == "192.168.1.85"


async def test_ambiguous_unpaired_contacts_do_not_choose_a_physical_target(apply_owner, monkeypatch):
    state = apply_owner
    contacts = [SimpleNamespace(instance_id="first", source_host="192.168.1.85"),
                SimpleNamespace(instance_id="second", source_host="192.168.1.191")]
    monkeypatch.setattr(state.lab.registrar, "registered_contacts", lambda _username: contacts)
    with pytest.raises(ValueError, match="phone_apply_target_unverified"):
        await state.manager._apply_target({**state.data, "host": ""}, state.profile["capabilities"]["apply"])


async def test_native_diagnostics_redact_administrative_credentials(hass):
    from homeassistant.components.diagnostics import async_redact_data
    from custom_components.voip_stack.diagnostics import TO_REDACT

    result = async_redact_data({"subentries": {"record": {"data": {
        "admin_username": "fixture-admin", "admin_password": "fixture-private",
        "provisioning_token": "fixture-delivery-private", "enabled": True}}}}, TO_REDACT)
    assert result["subentries"]["record"]["data"] == {
        "admin_username": "**REDACTED**", "admin_password": "**REDACTED**",
        "provisioning_token": "**REDACTED**", "enabled": True}


async def test_idle_wakeup_uses_real_HA_bus_on_the_event_loop(apply_owner, monkeypatch):
    from custom_components.voip_stack.physical_phone_discovery import PhoneDiscovery
    from custom_components.voip_stack.websocket_api import CALL_EVENT

    state = apply_owner
    monkeypatch.setattr(PhoneDiscovery, "async_start", AsyncMock(side_effect=OSError("fixture no multicast")))
    await state.manager.async_start()
    await state.lab.hass.async_block_till_done()
    threads = []
    monkeypatch.setattr(state.manager, "schedule", lambda _ids=None: threads.append(threading.get_ident()))
    state.manager.states[state.data["phone_id"]]["apply_status"] = "waiting_idle"
    state.lab.hass.bus.async_fire(CALL_EVENT, {"state": "idle"})
    await state.lab.hass.async_block_till_done()
    assert threads == [threading.get_ident()]


def use_real_native_HTTP(state, peer, monkeypatch):
    """Replace only target resolution; execute the actual adapter and HTTP I/O."""
    from custom_components.voip_stack import physical_phone_apply
    from custom_components.voip_stack.physical_phone_provisioning import phone_provisioning_url

    peer.password = state.data["admin_password"]
    peer.basic_password = state.data["admin_password"]
    peer.server = phone_provisioning_url(state.lab.hass, state.lab.entry, state.data)
    monkeypatch.setattr(physical_phone_apply, "async_apply_fanvil", state.real_apply)
    monkeypatch.setattr(state.manager, "_apply_target", AsyncMock(return_value=(peer.origin, "")))


async def test_HTTP_AutoP_received_before_busy_ACK_is_never_replayed(apply_owner, fanvil_http, monkeypatch):
    state, peer = apply_owner, fanvil_http
    use_real_native_HTTP(state, peer, monkeypatch)
    received, reply = asyncio.Event(), asyncio.Event()

    async def received_trigger():
        if len(peer.commands) == 1:
            state.lab.runtime.endpoints.claim_call("sip:first", "owned-ACK-race")
            received.set()
            await reply.wait()

    peer.on_trigger = received_trigger
    try:
        state.manager.schedule({state.data["phone_id"]})
        await asyncio.wait_for(received.wait(), 3)
        revision = state.manager.public_state(state.data["phone_id"])["desired_revision"]
        reply.set()
        await asyncio.wait_for(state.manager._task, 3)
        state.lab.runtime.endpoints.release_call("sip:first", "owned-ACK-race")
        state.manager.schedule({state.data["phone_id"]})
        await state.lab.hass.async_block_till_done()
        assert len(peer.commands) == 1
        public = state.manager.public_state(state.data["phone_id"])
        assert public["apply_attempted_revision"] == revision
        assert public["apply_status"] == "requested"
    finally:
        reply.set()
        state.lab.runtime.endpoints.release_call("sip:first", "owned-ACK-race")


@pytest.mark.parametrize("outcome", ["acknowledged", "disconnect", "cancel"])
async def test_admin_credentials_changed_during_AutoP_ACK_do_not_orphan_requesting(
        apply_owner, fanvil_http, monkeypatch, hass_admin_user, outcome):
    from homeassistant.core import Context, ServiceCall
    from custom_components.voip_stack.physical_phone_services import async_provision_phone

    state, peer = apply_owner, fanvil_http
    use_real_native_HTTP(state, peer, monkeypatch)
    received, reply = asyncio.Event(), asyncio.Event()
    async def held_reply():
        received.set()
        await reply.wait()
    peer.on_trigger = held_reply
    peer.disconnect_command = outcome == "disconnect"
    try:
        state.manager.schedule()
        await asyncio.wait_for(received.wait(), 3)
        revision = state.manager.public_state(state.data["phone_id"])["desired_revision"]
        prepared = state.manager.files[state.data["phone_id"]]
        state.data = {**state.data, "admin_username": "replacement-admin", "admin_password": "replacement-private"}
        peer.username, peer.password, peer.basic_password = "replacement-admin", "replacement-private", "replacement-private"
        state.lab.hass.config_entries.async_update_subentry(state.lab.entry, state.subentry, data=state.data)
        await state.manager.async_reconcile()
        assert state.manager.files[state.data["phone_id"]] is prepared
        assert state.manager.public_state(state.data["phone_id"])["desired_revision"] == revision
        # An administrator pressing Retry during the actual active request may
        # enqueue work, but cannot clear the dispatched revision or replay it.
        call = ServiceCall(state.lab.hass, "voip_stack", "provision_phone", {"phone_id": state.data["phone_id"]},
            context=Context(user_id=hass_admin_user.id))
        await async_provision_phone(call)
        assert state.manager.public_state(state.data["phone_id"])["apply_attempted_revision"] == revision
        if outcome == "cancel":
            state.manager._task.cancel()
            await asyncio.gather(state.manager._task, return_exceptions=True)
        else:
            reply.set()
            await asyncio.wait_for(state.manager._task, 3)
        public = state.manager.public_state(state.data["phone_id"])
        assert public["apply_status"] == ("requested" if outcome == "acknowledged" else "uncertain")
        assert public["apply_attempted_revision"] == revision and len(peer.commands) == 1
        assert getattr(state.manager, "_active_apply", None) is None
        state.manager.schedule()
        await state.lab.hass.async_block_till_done()
        assert len(peer.commands) == 1
    finally:
        reply.set()


@pytest.mark.parametrize("normalise_first", [False, True])
async def test_orphan_requesting_uses_task_liveness_and_explicit_admin_retry(
        apply_owner, fanvil_http, monkeypatch, hass_admin_user, normalise_first):
    from homeassistant.core import Context, ServiceCall
    from custom_components.voip_stack.physical_phone_services import async_provision_phone

    state, peer = apply_owner, fanvil_http
    use_real_native_HTTP(state, peer, monkeypatch)
    state.manager.schedule()
    await state.lab.hass.async_block_till_done()
    assert len(peer.commands) == 1 and state.manager._task.done()
    # Legacy/orphan diagnostics are not an active task. The preceding actual
    # command has finished, but a stored status can still read requesting.
    state.manager.states[state.data["phone_id"]]["apply_status"] = "requesting"
    if normalise_first:
        state.manager.schedule()
        await state.lab.hass.async_block_till_done()
        public = state.manager.public_state(state.data["phone_id"])
        assert public["apply_status"] == "uncertain" and public["apply_error"] == "phone_apply_result_uncertain"
        assert len(peer.commands) == 1
    call = ServiceCall(state.lab.hass, "voip_stack", "provision_phone", {"phone_id": state.data["phone_id"]},
        context=Context(user_id=hass_admin_user.id))
    await async_provision_phone(call)
    await state.lab.hass.async_block_till_done()
    assert len(peer.commands) == 2
    assert state.manager.public_state(state.data["phone_id"])["apply_status"] == "requested"


async def test_uncertain_HTTP_result_requires_explicit_admin_retry(apply_owner, fanvil_http, monkeypatch, hass_admin_user):
    from homeassistant.core import Context, ServiceCall
    from homeassistant.util import dt as dt_util
    from pytest_homeassistant.common import async_fire_time_changed_exact
    from custom_components.voip_stack.physical_phone_services import async_provision_phone, async_provisioning_state

    state, peer = apply_owner, fanvil_http
    use_real_native_HTTP(state, peer, monkeypatch)
    peer.final_login = True
    await state.manager.async_reconcile()
    await state.lab.hass.async_block_till_done()
    revision = state.manager.public_state(state.data["phone_id"])["desired_revision"]
    public = state.manager.public_state(state.data["phone_id"])
    assert public["apply_status"] == "uncertain"
    assert public["apply_error"] == "phone_apply_result_uncertain"
    assert public["apply_attempted_revision"] == revision
    assert state.manager._idle_retry_cancel is None
    # Neither unrelated events, elapsed time nor credential edits authorize a
    # replay of a command that the real HTTP server has already received.
    peer.final_login = False
    for _ in range(2):
        state.manager.schedule()
    async_fire_time_changed_exact(state.lab.hass, dt_util.utcnow() + timedelta(seconds=120))
    state.data = {**state.data, "admin_password": "changed-private-fixture"}
    peer.password = state.data["admin_password"]
    peer.basic_password = state.data["admin_password"]
    state.lab.hass.config_entries.async_update_subentry(state.lab.entry, state.subentry, data=state.data)
    await state.manager.async_reconcile()
    await state.lab.hass.async_block_till_done()
    assert len(peer.commands) == 1
    call = ServiceCall(state.lab.hass, "voip_stack", "get_phone_provisioning_state",
        {"phone_id": state.data["phone_id"]}, context=Context(user_id=hass_admin_user.id))
    report = await async_provisioning_state(call)
    assert report["apply_status"] == "uncertain" and report["apply_attempted_revision"] == revision
    await async_provision_phone(call)
    await state.lab.hass.async_block_till_done()
    assert len(peer.commands) == 2
    assert state.manager.public_state(state.data["phone_id"])["apply_status"] == "requested"


async def test_external_phone_busy_is_rechecked_every_60s_until_idle_without_call_event(apply_owner, fanvil_http, monkeypatch):
    from homeassistant.util import dt as dt_util
    from pytest_homeassistant.common import async_fire_time_changed_exact

    state, peer = apply_owner, fanvil_http
    use_real_native_HTTP(state, peer, monkeypatch)
    peer.calls = 1  # A native external line has no HA account call event.
    await state.manager.async_reconcile()
    await state.lab.hass.async_block_till_done()
    assert state.manager.public_state(state.data["phone_id"])["apply_status"] == "waiting_idle"
    first_timer = state.manager._idle_retry_cancel
    assert first_timer is not None
    now = dt_util.utcnow()
    async_fire_time_changed_exact(state.lab.hass, now + timedelta(seconds=59))
    await state.lab.hass.async_block_till_done()
    assert state.manager._idle_retry_cancel is first_timer
    for seconds in (61, 122, 183):
        previous_checks = peer.gets.count("/callInfo.xml")
        async_fire_time_changed_exact(state.lab.hass, now + timedelta(seconds=seconds))
        await state.lab.hass.async_block_till_done()
        assert peer.gets.count("/callInfo.xml") > previous_checks
        assert state.manager._idle_retry_cancel is not None
        assert not peer.commands
    peer.calls = 0
    async_fire_time_changed_exact(state.lab.hass, now + timedelta(seconds=244))
    await state.lab.hass.async_block_till_done()
    assert len(peer.commands) == 1
    assert state.manager.public_state(state.data["phone_id"])["apply_status"] == "requested"
    assert state.manager._idle_retry_cancel is None


async def test_new_save_behind_old_requested_revision_retains_the_idle_timer(apply_owner):
    from homeassistant.util import dt as dt_util
    from pytest_homeassistant.common import async_fire_time_changed_exact

    state = apply_owner
    await state.manager.async_reconcile()
    await state.lab.hass.async_block_till_done()
    old_revision = state.manager.public_state(state.data["phone_id"])["desired_revision"]
    state.lab.runtime.endpoints.claim_call("sip:first", "long-call")
    try:
        state.data = {**state.data, "name": "New settings during long call"}
        state.lab.hass.config_entries.async_update_subentry(state.lab.entry, state.subentry, data=state.data)
        await state.manager.async_reconcile()
        await state.lab.hass.async_block_till_done()
        public = state.manager.public_state(state.data["phone_id"])
        assert public["state"] == "waiting_idle" and public["apply_status"] == "requested"
        assert public["desired_revision"] == public["apply_attempted_revision"] == old_revision
        assert state.manager._idle_retry_cancel is not None
        async_fire_time_changed_exact(state.lab.hass, dt_util.utcnow() + timedelta(seconds=61))
        await state.lab.hass.async_block_till_done()
        assert state.apply.await_count == 1  # No replay of the old revision.
        state.lab.runtime.endpoints.release_call("sip:first", "long-call")
        async_fire_time_changed_exact(state.lab.hass, dt_util.utcnow() + timedelta(seconds=122))
        await state.lab.hass.async_block_till_done()
        public = state.manager.public_state(state.data["phone_id"])
        assert public["desired_revision"] != old_revision
        assert public["apply_attempted_revision"] == public["desired_revision"]
        assert state.apply.await_count == 2 and state.manager._idle_retry_cancel is None
    finally:
        state.lab.runtime.endpoints.release_call("sip:first", "long-call")


@pytest.mark.parametrize("change", ["new_revision", "delete"])
async def test_old_HTTP_ack_does_not_mark_new_revision_or_revive_deleted_phone(apply_owner, fanvil_http, monkeypatch, change):
    state, peer = apply_owner, fanvil_http
    use_real_native_HTTP(state, peer, monkeypatch)
    received, reply = asyncio.Event(), asyncio.Event()

    async def held_reply():
        received.set()
        await reply.wait()

    peer.on_trigger = held_reply
    task = state.lab.hass.async_create_task(state.manager._async_apply_if_needed(state.data))
    try:
        await asyncio.wait_for(received.wait(), 3)
        old_revision = state.manager.public_state(state.data["phone_id"])["desired_revision"]
        if change == "new_revision":
            state.data = {**state.data, "name": "A newer revision"}
            state.lab.hass.config_entries.async_update_subentry(state.lab.entry, state.subentry, data=state.data)
            await state.manager.async_prepare(state.data)
            new_revision = state.manager.public_state(state.data["phone_id"])["desired_revision"]
            assert new_revision != old_revision
        else:
            state.lab.hass.config_entries.async_remove_subentry(state.lab.entry, state.subentry.subentry_id)
            await state.manager.async_reconcile()
        reply.set()
        await asyncio.wait_for(task, 3)
        public = state.manager.public_state(state.data["phone_id"])
        data = await state.manager.async_file(state.data["phone_id"], state.data["provisioning_token"], state.filename)
        if change == "new_revision":
            assert public["desired_revision"] == new_revision and public["apply_attempted_revision"] == old_revision
            assert public["apply_status"] != "requested"
            assert b"A newer revision" in data
        else:
            assert not public and data is None and state.data["phone_id"] not in state.manager.files
        assert len(peer.commands) == 1
    finally:
        reply.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_unload_after_HTTP_dispatch_preserves_uncertainty_and_rejects_late_GET(apply_owner, fanvil_http, monkeypatch):
    state, peer = apply_owner, fanvil_http
    use_real_native_HTTP(state, peer, monkeypatch)
    received, reply = asyncio.Event(), asyncio.Event()

    async def held_reply():
        received.set()
        await reply.wait()

    peer.on_trigger = held_reply
    try:
        state.manager.schedule()
        await asyncio.wait_for(received.wait(), 3)
        await state.manager.async_close()
        public = state.manager.public_state(state.data["phone_id"])
        assert public["apply_status"] == "uncertain" and public["apply_error"] == "phone_apply_result_uncertain"
        assert public["apply_attempted_revision"] == public["desired_revision"]
        assert state.manager._idle_retry_cancel is None and not state.manager.files
        assert await state.manager.async_file(state.data["phone_id"], state.data["provisioning_token"], state.filename) is None
    finally:
        reply.set()


@pytest.mark.parametrize("change", ["delete", "unload"])
async def test_pending_idle_timer_is_cancelled_when_owner_is_removed(apply_owner, fanvil_http, monkeypatch, change):
    from homeassistant.util import dt as dt_util
    from pytest_homeassistant.common import async_fire_time_changed_exact

    state, peer = apply_owner, fanvil_http
    use_real_native_HTTP(state, peer, monkeypatch)
    peer.calls = 1
    await state.manager.async_reconcile()
    await state.lab.hass.async_block_till_done()
    assert state.manager._idle_retry_cancel is not None
    requests = len(peer.gets)
    if change == "delete":
        state.lab.hass.config_entries.async_remove_subentry(state.lab.entry, state.subentry.subentry_id)
        await state.manager.async_reconcile()
    else:
        await state.manager.async_close()
    assert state.manager._idle_retry_cancel is None
    async_fire_time_changed_exact(state.lab.hass, dt_util.utcnow() + timedelta(seconds=120))
    await state.lab.hass.async_block_till_done()
    assert len(peer.gets) == requests


async def test_worker_and_service_redact_unexpected_exception_messages(apply_owner, hass_admin_user):
    from homeassistant.core import Context, ServiceCall
    from custom_components.voip_stack.physical_phone_services import async_provisioning_state

    state = apply_owner
    state.apply.side_effect = RuntimeError("secret-password at https://private.invalid/token")
    state.manager.schedule()
    await state.lab.hass.async_block_till_done()
    call = ServiceCall(state.lab.hass, "voip_stack", "get_phone_provisioning_state",
        {"phone_id": state.data["phone_id"]}, context=Context(user_id=hass_admin_user.id))
    public = await async_provisioning_state(call)
    assert public["apply_status"] == "error" and public["apply_error"] == "RuntimeError"
    assert not any(word in str(public) for word in ("secret-password", "private.invalid", "https://"))
    assert state.manager._idle_retry_cancel is None


@pytest.mark.parametrize("outcome", [200, 403, 489, None])
async def test_snom_resync_targets_downloaded_handset_once_after_idle(cached_physical_config, monkeypatch, outcome):
    from custom_components.voip_stack import physical_phone_enrollment
    from custom_components.voip_stack.sip_registrar import SipRegistration
    from .test_physical_phone_dhcp_ha import native_cache
    from .test_physical_phone_runtime_ha import add, phone

    fixture = cached_physical_config
    manager, lab = fixture.manager, fixture.lab
    host = "192.168.10.67"
    data = phone(profile_id="snom/360", mac="000413000001")
    record = add(lab, data)
    profile, _ = manager.catalog.async_profile.return_value
    profile = {**profile, "id": "snom/360", "vendor": "snom",
               "capabilities": {**profile["capabilities"], "resync": {"method": "snom_check_sync",
                   "qualification": "documented_request", "firmwares": ["8.7.3.25.9"]}}}
    manager.catalog.async_profile.return_value = (profile, {"templates/config.jinja2": "name={{ physical.name }}\n"})
    watcher = native_cache(lab)
    watcher.async_process_client(host, "snom360-000001", data["mac"])
    monkeypatch.setattr(physical_phone_enrollment, "async_same_phone_lan", AsyncMock(return_value=True))
    def registration(ip, agent):
        return SipRegistration("first", f"sip:first@{ip}:5060", ip, 5060, "UDP", float("inf"), user_agent=agent)
    other = registration("192.168.10.71", "Other Phone")
    contact = registration(host, "snom360/8.7.3.25.9")
    contacts = [other]
    monkeypatch.setattr(lab.registrar, "registered_contacts", lambda username: contacts if username == "first" else [])

    async def send(uri, address, transport, *, active, on_sent):
        assert uri == contact.contact_uri and address == (host, 5060) and transport == "UDP"
        assert active()
        on_sent()
        return SimpleNamespace(status_code=outcome) if outcome else None

    sender = SimpleNamespace(async_snom_check_sync=AsyncMock(side_effect=send))
    lab.runtime.sip.adopt_component("udp_listener", sender)
    await manager.async_prepare(data)
    identity = data["phone_id"]
    state = manager.states[identity]
    async def read(peer):
        return await manager.async_file(identity, data["provisioning_token"], data["mac"] + ".cfg", peer_host=peer)

    # A manual preview on another host cannot enroll the phone for periodic pushes.
    assert await read("192.168.10.71")
    await manager._async_apply_if_needed(data)
    assert state["apply_status"] == "manual" and not state.get("device_download_revision")
    assert await read(host)
    downloaded = state["device_download_revision"]
    await manager._async_apply_if_needed(data)
    sender.async_snom_check_sync.assert_not_awaited()
    data = {**data, "name": "New display name"}
    lab.hass.config_entries.async_update_subentry(lab.entry, record, data=data)
    lab.runtime.endpoints.claim_call("sip:first", "old-revision-busy")
    await manager.async_prepare(data)
    await manager._async_apply_if_needed(data)
    assert state["state"] == state["apply_status"] == "waiting_idle"
    assert manager._idle_retry_cancel is not None
    lab.runtime.endpoints.release_call("sip:first", "old-revision-busy")
    await manager.async_prepare(data)
    assert state["desired_revision"] != downloaded
    await manager._async_apply_if_needed(data)
    assert state["apply_status"] == "pending"
    contacts.append(contact)
    contact.user_agent = "snom360/8.7.3.25.90"
    await manager._async_apply_if_needed(data)
    sender.async_snom_check_sync.assert_not_awaited()
    contact.user_agent = "snom360/8.7.3.25.9"
    lab.runtime.endpoints.claim_call("sip:first", "busy")
    await manager._async_apply_if_needed(data)
    assert state["apply_status"] == "waiting_idle"
    sender.async_snom_check_sync.assert_not_awaited()
    lab.runtime.endpoints.release_call("sip:first", "busy")
    await manager._async_apply_if_needed(data)
    await manager._async_apply_if_needed(data)
    sender.async_snom_check_sync.assert_awaited_once()
    assert state["apply_status"] == ("requested" if outcome == 200 else "error" if outcome else "uncertain")
    assert state["apply_attempted_revision"] == state["desired_revision"]
    assert state["device_download_revision"] == downloaded
    reservation = manager.store.async_save.await_args.args[0][identity]
    assert reservation["apply_status"] == "requesting" and reservation["apply_attempted_revision"] == state["desired_revision"]
    if outcome == 200:
        assert state["apply_method"] == "snom_check_sync"
        assert await read(host) and state["device_download_revision"] == state["desired_revision"]
        data = {**data, "provisioning_token": "rotated-fixture-token", "name": "New credential"}
        lab.hass.config_entries.async_update_subentry(lab.entry, record, data=data)
        await manager.async_prepare(data)
        await manager._async_apply_if_needed(data)
        assert state["apply_status"] == "manual"
    elif outcome is None:
        # Process recovery can only see the pre-send reservation after power loss.
        state.update(reservation)
        await manager._async_apply_if_needed(data)
        assert state["apply_status"] == "uncertain"
    sender.async_snom_check_sync.assert_awaited_once()
