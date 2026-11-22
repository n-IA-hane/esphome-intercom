"""Native HA test HTTP boundary and external real Chromium owner.

Playwright belongs to the ordinary test environment, not the immutable HA
runner. The browser talks to production HA websocket commands and media views.
"""

import asyncio
import json
import os
from pathlib import Path
import wave

from aiohttp import web, WSMsgType
from aiohttp.test_utils import TestServer

from custom_components.voip_stack.audio_ws_view import VoipAudioWebSocketView
from custom_components.voip_stack.video_ws_view import VoipVideoWebSocketView
from tests.test_browser_logical_availability_ha import open_browser_ws
from .shared_codec_peer import tone_pcm


class Card:
    def __init__(self):
        self.controls = []
        self.http_errors = []
        self.requests = set()
        self.process = self.server = None

    async def start(
        self, lab, hass_ws_client, directory, identity, *, browser_python=None
    ):
        browser_python = browser_python or os.environ.get("VOIP_BROWSER_PYTHON")
        if not browser_python:
            raise RuntimeError(
                "Set VOIP_BROWSER_PYTHON to the browser lane's Playwright interpreter"
            )
        directory.mkdir(parents=True, exist_ok=True)
        self.directory = directory
        self.controls = []
        root = (
            Path(__file__).resolve().parents[2]
            / "custom_components/voip_stack/frontend"
        )

        @web.middleware
        async def authenticated(request, handler):
            request["hass_user"] = identity
            task = asyncio.current_task()
            self.requests.add(task)
            try:
                return await handler(request)
            except web.HTTPException as error:
                self.http_errors.append((request.path, error.status, error.text))
                raise
            finally:
                self.requests.discard(task)

        app = web.Application(middlewares=[authenticated])
        app["hass"] = lab.hass
        app.router.add_get("/api/voip_stack/ws", VoipAudioWebSocketView().get)
        app.router.add_get("/api/voip_stack/video_ws", VoipVideoWebSocketView().get)

        async def control(request):
            browser = web.WebSocketResponse()
            await browser.prepare(request)
            native = await open_browser_ws(lab, hass_ws_client)
            from homeassistant.components.config import device_registry, entity_registry

            device_registry.async_setup(lab.hass)
            entity_registry.async_setup(lab.hass)
            self.controls.append(native)

            async def browser_to_ha():
                async for message in browser:
                    if message.type != WSMsgType.TEXT:
                        continue
                    value = json.loads(message.data)
                    if value.get("type") == "auth/sign_path":
                        # TestClient-style HTTP identity is already supplied by
                        # this fixture; all production media authorization stays.
                        await browser.send_json(
                            {
                                "id": value["id"],
                                "type": "result",
                                "success": True,
                                "result": {"path": value["path"]},
                            }
                        )
                    else:
                        await native.send_json(value)

            async def ha_to_browser():
                async for message in native:
                    if message.type == WSMsgType.TEXT:
                        await browser.send_str(message.data)

            tasks = [
                asyncio.create_task(browser_to_ha()),
                asyncio.create_task(ha_to_browser()),
            ]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await native.close()
                await browser.close()
            return browser

        app.router.add_get("/control/ws", control)

        async def asset(request):
            path = root / request.match_info["name"]
            if not path.is_file() or path.parent != root:
                raise web.HTTPNotFound()
            return web.Response(body=path.read_bytes(), content_type="text/javascript")

        app.router.add_get("/voip-stack/{name}", asset)

        async def index(request):
            return web.Response(
                text='<html><body style="background:#142234;color:white"><main id="card"></main></body></html>',
                content_type="text/html",
            )

        app.router.add_get("/", index)
        self.server = TestServer(app)
        await self.server.start_server()
        wav = directory / "microphone-650.wav"
        with wave.open(str(wav), "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(48000)
            writer.writeframes(tone_pcm(48000, 650))
        driver = Path(__file__).with_name("shared_codec_chromium.py")
        self.process = await asyncio.create_subprocess_exec(
            browser_python,
            str(driver),
            str(self.server.make_url("/")),
            str(wav),
            str(directory),
            identity.id,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            limit=4 * 1024 * 1024,
            stderr=(directory / "driver.log").open("wb"),
        )
        await self.reply()
        return self

    async def reply(self):
        line = await asyncio.wait_for(self.process.stdout.readline(), 20)
        if not line:
            raise RuntimeError(
                "Chromium driver stopped: "
                + (self.directory / "driver.log").read_text()
            )
        response = json.loads(line)
        if "error" in response:
            raise RuntimeError(response["error"])
        return response.get("result")

    async def command(self, action, **values):
        self.process.stdin.write(
            (json.dumps({"action": action, **values}) + "\n").encode()
        )
        await self.process.stdin.drain()
        return await self.reply()

    async def close(self):
        if (
            getattr(self, "process", None) is not None
            and self.process.returncode is None
        ):
            try:
                await asyncio.wait_for(self.command("close"), 3)
            except (TimeoutError, RuntimeError, BrokenPipeError):
                pass
            finally:
                if self.process.returncode is None:
                    try:
                        await asyncio.wait_for(self.process.wait(), 3)
                    except TimeoutError:
                        self.process.kill()
                        await self.process.wait()
        for native in self.controls:
            await native.close()
        pending = [task for task in self.requests if not task.done()]
        if pending:
            done, remaining = await asyncio.wait(pending, timeout=1)
            for task in remaining:
                task.cancel()
            await asyncio.gather(*remaining, return_exceptions=True)
        if getattr(self, "server", None):
            await asyncio.wait_for(self.server.close(), 3)
