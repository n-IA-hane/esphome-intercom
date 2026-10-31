"""Exercise the qualification client through HTTP and a real JPEG decoder.

The local HTTP fixture tests the download oracle, not an ESP worker or panel.
"""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.util import module_from_spec, spec_from_file_location
from io import BytesIO
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
import asyncio
import hashlib
import os

from PIL import Image
import pytest


ROOT = Path(__file__).resolve().parents[1]
CAPTURE = "aabbccdd-00000002"


def witness_module():
    source = Path(os.environ.get("VOIP_STACK_REPO", ROOT.parent / "esphome-voip-stack"))
    spec = spec_from_file_location("screenshot_http_qualification", source / "tools/qualify_sip_registration_esp.py")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def jpeg_bytes(size=(24, 16)):
    image = Image.new("RGB", size, (12, 44, 98))
    image.paste((200, 30, 10), (0, 0, size[0] // 2, size[1]))
    stream = BytesIO()
    image.save(stream, format="JPEG", quality=85)
    return stream.getvalue()


@contextmanager
def image_origin(payload, *, mime="image/jpeg", etag=f'"{CAPTURE}"'):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", mime)
            if etag is not None:
                self.send_header("ETag", etag)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


def ready_witness(module):
    witness = module.EspWitness(SimpleNamespace())
    entity = type("TextSensorInfo", (), {"object_id": "screenshot_id", "key": 7})()
    witness.entities = [entity]
    witness.values[("TextSensorState", 7)] = "aabbccdd-00000001"
    witness.services = {"take_screenshot": "capture-action"}

    async def capture(service, data):
        assert service == "capture-action" and data == {}
        asyncio.get_running_loop().call_soon(
            witness.values.__setitem__, ("TextSensorState", 7), CAPTURE,
        )
        asyncio.get_running_loop().call_soon(witness.changed.set)

    witness.client.execute_service = capture
    return witness


async def test_screenshot_downloads_http_jpeg_with_capture_identity(tmp_path):
    module = witness_module()
    witness = ready_witness(module)
    payload = jpeg_bytes()
    output = tmp_path / "screen.jpg"
    with image_origin(payload) as (address, requests):
        result = await witness.screenshot(address, output)
    assert requests == [f"/esp_screenshot/screen/image.jpg?capture={CAPTURE}"]
    assert output.read_bytes() == payload
    assert result["capture_id"] == CAPTURE and result["etag"] == f'"{CAPTURE}"'
    assert (result["width"], result["height"]) == (24, 16)
    assert result["sha256"] == hashlib.sha256(payload).hexdigest()


@pytest.mark.parametrize("failure", ["wrong-etag", "missing-etag", "wrong-mime", "html", "truncated", "broken-jpeg", "png"])
async def test_screenshot_rejects_invalid_jpeg_or_capture_identity(tmp_path, failure):
    module = witness_module()
    witness = ready_witness(module)
    payload, mime, etag = jpeg_bytes(), "image/jpeg", f'"{CAPTURE}"'
    if failure == "wrong-etag":
        etag = '"another-boot-00000002"'
    elif failure == "missing-etag":
        etag = None
    elif failure == "wrong-mime":
        mime = "text/html"
    elif failure == "html":
        payload = b"<html>success page from the wrong HTTP handler</html>"
    elif failure == "truncated":
        payload = payload[:-10]
    elif failure == "broken-jpeg":
        payload = b"\xff\xd8broken image\xff\xd9"
    elif failure == "png":
        stream = BytesIO()
        Image.new("RGB", (24, 16)).save(stream, format="PNG")
        payload = stream.getvalue()
    output = tmp_path / "screen.jpg"
    output.write_bytes(b"previous successful capture")
    with image_origin(payload, mime=mime, etag=etag) as (address, _requests):
        with pytest.raises(RuntimeError, match="Screenshot"):
            await witness.screenshot(address, output)
    assert output.read_bytes() == b"previous successful capture"


async def test_screenshot_rejects_wrong_physical_dimensions(tmp_path):
    witness = ready_witness(witness_module())
    output = tmp_path / "screen.jpg"
    with image_origin(jpeg_bytes((16, 24))) as (address, _requests):
        with pytest.raises(RuntimeError, match="dimensions"):
            await witness.screenshot(address, output, expected_size=(24, 16))
    assert not output.exists()
