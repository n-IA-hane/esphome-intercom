"""Only schema-test ESPHome subprocesses replace the model HTTP boundary.

The real provider/parser/codegen runs unchanged. Downloads of the recorded
official models return hash-verified original bytes, including on a cold cache.
No installed dependency or production configuration is patched.
"""

import os
import sys

if os.environ.get("VOIP_SCHEMA_MODEL_FIXTURES") and "esphome" in sys.orig_argv:
    import base64
    import hashlib
    import json
    from pathlib import Path

    from esphome import external_files

    _root = Path(os.environ["VOIP_SCHEMA_MODEL_FIXTURES"])
    _sources = json.loads((_root / "sources.json").read_text())
    _download = external_files.download_content

    def _fixture_download(url, path, *args, **kwargs):
        entry = _sources.get(url)
        if entry is None:
            return _download(url, path, *args, **kwargs)
        encoded = (_root / entry["file"]).read_bytes()
        data = base64.b64decode(b"".join(encoded.split()), validate=True)
        if hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise ValueError(f"Offline model fixture SHA256 mismatch: {url}")
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return data if kwargs.get("return_content", True) else b""

    external_files.download_content = _fixture_download
