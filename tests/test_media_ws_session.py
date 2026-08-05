#!/usr/bin/env python3
"""Shared audio/video WebSocket lifecycle contracts."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

from aiohttp import web


ROOT = Path(__file__).resolve().parents[1]
PKG_NAME = "custom_components.voip_stack"
PKG_DIR = ROOT / "custom_components" / "voip_stack"


def _load_module(name: str):
    if "custom_components" not in sys.modules:
        root = types.ModuleType("custom_components")
        root.__path__ = [str(ROOT / "custom_components")]
        sys.modules["custom_components"] = root
    if PKG_NAME not in sys.modules:
        package = types.ModuleType(PKG_NAME)
        package.__path__ = [str(PKG_DIR)]
        sys.modules[PKG_NAME] = package
    full_name = f"{PKG_NAME}.{name}"
    if full_name in sys.modules:
        return sys.modules[full_name]
    spec = importlib.util.spec_from_file_location(full_name, PKG_DIR / f"{name}.py")
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {full_name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


_load_module("session_cleanup")
websocket_owner = _load_module("websocket_owner")
runtime_data = types.ModuleType(f"{PKG_NAME}.runtime_data")
runtime_data.call_projection = lambda hass: hass.data.get("voip_stack", {}).get(
    "call_registry"
)
runtime_data.sip_trunk = lambda hass: hass.data.get("voip_stack", {}).get(
    "sip_trunk"
)
sys.modules[runtime_data.__name__] = runtime_data
media_ws_session = _load_module("media_ws_session")


class _Registry:
    sessions: dict = {}
    softphone_media: dict = {}

    @staticmethod
    def resolve_session_id(call_id: str) -> str:
        return call_id


class _Lease:
    endpoint_id = "phone"
    call_id = "call-1"
    token = object()


class _Bridge:
    def __init__(self) -> None:
        self.releases: list[tuple[str, str, object]] = []
        self.calls: list[str] = []

    def release_media(self, call_id: str, endpoint_id: str, token: object) -> bool:
        self.releases.append((call_id, endpoint_id, token))
        return True

    def get_call(self, call_id: str) -> object:
        self.calls.append(call_id)
        return {"call_id": call_id, "generation": len(self.calls)}


class _LocalState:
    value = "in_call"


class _LocalCall:
    def __init__(self, *, video_enabled: bool = True) -> None:
        self.video_enabled = video_enabled

    @staticmethod
    def state_for(endpoint_id: str) -> _LocalState:
        assert endpoint_id == "kitchen"
        return _LocalState()


class _Request:
    def __init__(self, hass: object) -> None:
        self.app = {"hass": hass}
        self.query = {
            "client_id": "browser-document-1234",
            "endpoint_id": "kitchen",
            "device_id": "",
            "call_id": "call-1",
        }
        self._user = object()

    def get(self, key: str) -> object | None:
        if key == "hass_user":
            return self._user
        return None


class MediaWebSocketSessionTest(unittest.IsolatedAsyncioTestCase):
    def test_prepared_request_rechecks_local_video_capability(self) -> None:
        context = media_ws_session.MediaWebSocketRequestContext(
            hass=object(),
            user_id="user-1",
            client_id="browser-document-1234",
            endpoint_id="kitchen",
            call_id="call-1",
            local_bridge=types.SimpleNamespace(
                get_call=lambda _call_id: _LocalCall(video_enabled=False)
            ),
        )
        prepared = media_ws_session.PreparedMediaWebSocketRequest(
            context=context,
            registry=_Registry(),
            local_call=None,
            active_session_resolver=lambda _hass, _endpoint_id: None,
            missing_dialog_text="missing video dialog",
            require_local_video=True,
        )
        with self.assertRaises(web.HTTPConflict) as raised:
            prepared.resolve_current()
        self.assertEqual(raised.exception.text, "local phone call is audio-only")

    def test_prepared_request_rejects_missing_remote_dialog(self) -> None:
        context = media_ws_session.MediaWebSocketRequestContext(
            hass=object(),
            user_id="user-1",
            client_id="browser-document-1234",
            endpoint_id="kitchen",
            call_id="call-1",
            local_bridge=None,
        )
        prepared = media_ws_session.PreparedMediaWebSocketRequest(
            context=context,
            registry=_Registry(),
            local_call=None,
            active_session_resolver=lambda _hass, _endpoint_id: None,
            missing_dialog_text="missing media dialog",
        )
        with self.assertRaises(web.HTTPConflict) as raised:
            prepared.resolve_current()
        self.assertEqual(raised.exception.text, "missing media dialog")

    def test_request_resolution_is_shared_and_reloads_local_call(self) -> None:
        hass = types.SimpleNamespace(data={})
        bridge = _Bridge()
        request = _Request(hass)
        authorization = types.ModuleType(f"{PKG_NAME}.authorization")
        authorization.require_http_control = lambda _request: "user-1"
        authorization.require_media_client_id = (
            lambda _request: "browser-document-1234"
        )
        local_runtime = types.ModuleType(f"{PKG_NAME}.local_softphone_runtime")
        local_runtime.local_softphone_bridge = lambda _hass: bridge
        websocket_api = types.ModuleType(f"{PKG_NAME}.websocket_api")
        websocket_api._endpoint_id_from_selector = (
            lambda _hass, *, endpoint_id, device_id: endpoint_id or device_id
        )

        with patch.dict(
            sys.modules,
            {
                authorization.__name__: authorization,
                local_runtime.__name__: local_runtime,
                websocket_api.__name__: websocket_api,
            },
        ):
            context = media_ws_session.resolve_media_websocket_request(request)

        self.assertIs(context.hass, hass)
        self.assertEqual(context.user_id, "user-1")
        self.assertEqual(context.client_id, "browser-document-1234")
        self.assertEqual(context.endpoint_id, "kitchen")
        self.assertEqual(context.call_id, "call-1")
        self.assertEqual(context.current_local_call()["generation"], 1)
        self.assertEqual(context.current_local_call()["generation"], 2)
        self.assertEqual(bridge.calls, ["call-1", "call-1"])

    def test_request_resolution_rejects_invalid_media_identity(self) -> None:
        request = _Request(types.SimpleNamespace(data={}))
        authorization = types.ModuleType(f"{PKG_NAME}.authorization")
        authorization.require_http_control = lambda _request: "user-1"

        def invalid_client_id(_request) -> str:
            raise ValueError("invalid client")

        authorization.require_media_client_id = invalid_client_id
        with patch.dict(sys.modules, {authorization.__name__: authorization}):
            with self.assertRaises(web.HTTPBadRequest) as raised:
                media_ws_session.resolve_media_websocket_request(request)
        self.assertEqual(raised.exception.text, "invalid client")

    async def test_controller_authorization_runs_after_channel_validation(self) -> None:
        calls: list[tuple[object, ...]] = []

        class CallRegistry:
            pass

        registry = CallRegistry()
        hass = types.SimpleNamespace(
            data={"voip_stack": {"call_registry": registry}}
        )
        request = _Request(hass)
        context = media_ws_session.MediaWebSocketRequestContext(
            hass=hass,
            user_id="user-1",
            client_id="browser-document-1234",
            endpoint_id="kitchen",
            call_id="call-1",
            local_bridge=None,
        )
        authorization = types.ModuleType(f"{PKG_NAME}.authorization")

        async def authorize(
            authorized_hass,
            authorized_registry,
            call_id,
            user,
            *,
            endpoint_id,
        ) -> None:
            calls.append(
                (
                    authorized_hass,
                    authorized_registry,
                    call_id,
                    user,
                    endpoint_id,
                )
            )

        authorization.async_require_media_controller = authorize
        call_registry = types.ModuleType(f"{PKG_NAME}.call_registry")
        call_registry.CallRegistry = CallRegistry
        const = types.ModuleType(f"{PKG_NAME}.const")
        const.DOMAIN = "voip_stack"
        with patch.dict(
            sys.modules,
            {
                authorization.__name__: authorization,
                call_registry.__name__: call_registry,
                const.__name__: const,
            },
        ):
            result = (
                await media_ws_session.async_authorize_media_websocket_request(
                    context,
                    request,
                )
            )

        self.assertIs(result, registry)
        self.assertEqual(
            calls,
            [(hass, registry, "call-1", request._user, "kitchen")],
        )

    async def test_context_claims_and_releases_audio_owner_then_publishes(self) -> None:
        bucket: dict = {}
        owner = websocket_owner.MediaWebSocketOwner(user_id="u", client_id="c")
        published: list[str] = []

        async with media_ws_session.async_media_websocket_session(
            bucket,
            _Registry(),
            "call-1",
            "phone",
            owner,
            channel="audio",
            timeout=0.1,
            shutdown_event=None,
            pin_client_identity=False,
            local_bridge=None,
            publish_state=lambda: published.append("published"),
        ):
            self.assertIs(bucket["audio_ws_owners"]["phone|call-1"], owner)

        self.assertEqual(bucket["audio_ws_owners"], {})
        self.assertTrue(owner.released.is_set())
        self.assertEqual(published, ["published"])

    async def test_shared_claim_builds_the_channel_websocket_and_owner(self) -> None:
        hass = types.SimpleNamespace(data={"voip_stack": {}})
        context = media_ws_session.MediaWebSocketRequestContext(
            hass=hass,
            user_id="user-1",
            client_id="browser-document-1234",
            endpoint_id="phone",
            call_id="call-1",
            local_bridge=None,
        )
        request = types.SimpleNamespace(transport=object())
        const = types.ModuleType(f"{PKG_NAME}.const")
        const.DOMAIN = "voip_stack"
        published: list[str] = []

        with patch.dict(sys.modules, {const.__name__: const}):
            async with media_ws_session.async_claimed_media_websocket(
                request,
                context,
                _Registry(),
                channel="audio",
                max_msg_size=4096,
                timeout=0.1,
                local_call=object(),
                publish_state=lambda: published.append("published"),
            ) as claimed:
                self.assertIsInstance(claimed.websocket, web.WebSocketResponse)
                self.assertEqual(claimed.owner.user_id, "user-1")
                self.assertEqual(
                    claimed.owner.client_id,
                    "browser-document-1234",
                )
                self.assertIs(
                    hass.data["voip_stack"]["audio_ws_owners"][
                        "phone|call-1"
                    ],
                    claimed.owner,
                )

        self.assertEqual(
            hass.data["voip_stack"]["audio_ws_owners"],
            {},
        )
        self.assertEqual(published, ["published"])

    async def test_local_lease_releases_only_after_both_media_owners_are_gone(self) -> None:
        bucket: dict = {}
        bridge = _Bridge()
        audio_owner = websocket_owner.MediaWebSocketOwner(user_id="u", client_id="c")
        video_owner = websocket_owner.MediaWebSocketOwner(user_id="u", client_id="c")
        bucket["video_ws_owners"] = {"phone|call-1": video_owner}

        async with media_ws_session.async_media_websocket_session(
            bucket,
            _Registry(),
            "call-1",
            "phone",
            audio_owner,
            channel="audio",
            timeout=0.1,
            shutdown_event=None,
            pin_client_identity=False,
            local_bridge=None,
            publish_state=lambda: None,
        ) as session:
            session.own_local_lease(bridge, _Lease())

        self.assertEqual(bridge.releases, [])
        bucket["video_ws_owners"].clear()
        video_session = media_ws_session.MediaWebSocketSession(
            bucket,
            bucket["video_ws_owners"],
            asyncio.Lock(),
            "phone|call-1",
            video_owner,
            lambda: None,
            local_bridge=bridge,
            local_lease=_Lease(),
        )
        await video_session.close()
        self.assertEqual(len(bridge.releases), 1)

    async def test_cancelled_close_waiter_cannot_skip_release_or_publication(self) -> None:
        bucket = {"audio_ws_owners": {}}
        owner_lock = asyncio.Lock()
        await owner_lock.acquire()
        owner = websocket_owner.MediaWebSocketOwner(user_id="u", client_id="c")
        bucket["audio_ws_owners"]["phone|call-1"] = owner
        published: list[str] = []
        session = media_ws_session.MediaWebSocketSession(
            bucket,
            bucket["audio_ws_owners"],
            owner_lock,
            "phone|call-1",
            owner,
            lambda: published.append("published"),
        )
        waiter = asyncio.create_task(session.close())
        await asyncio.sleep(0)
        waiter.cancel()
        await asyncio.sleep(0)
        self.assertFalse(waiter.done())

        owner_lock.release()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertEqual(bucket["audio_ws_owners"], {})
        self.assertEqual(published, ["published"])

    async def test_publication_failure_does_not_restore_released_owner(self) -> None:
        bucket: dict = {}
        owner = websocket_owner.MediaWebSocketOwner(user_id="u", client_id="c")

        async with media_ws_session.async_media_websocket_session(
            bucket,
            _Registry(),
            "call-1",
            "phone",
            owner,
            channel="video",
            timeout=0.1,
            shutdown_event=None,
            pin_client_identity=False,
            local_bridge=None,
            publish_state=lambda: (_ for _ in ()).throw(RuntimeError("observer")),
        ):
            pass

        self.assertEqual(bucket["video_ws_owners"], {})
