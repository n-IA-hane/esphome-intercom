"""UI/schema fixtures reuse the authentic, hash-verified Alexa model."""

import json

import pytest

from tests import test_package_composition as composition


def test_ui_model_fixture_contains_real_model_and_rejects_corruption():
    files = dict(composition._ui_model_files())
    assert json.loads(files["manifest.json"])["model"] == "alexa.tflite"
    model = files["alexa.tflite"]
    assert len(model) == 55856 and model[4:8] == b"TFL3"
    for name, _url, digest in composition._UI_MODEL_FILES:
        assert composition._verify_model_bytes(files[name], digest) is files[name]
        corrupted = files[name][:-1] + bytes([files[name][-1] ^ 1])
        with pytest.raises(ValueError, match="SHA256 mismatch"):
            composition._verify_model_bytes(corrupted, digest)


def test_ui_model_fixture_reuses_verified_bytes_without_another_download(tmp_path, monkeypatch):
    verified = dict(composition._ui_model_files())

    def unexpected_download(*_args, **_kwargs):
        raise AssertionError("Verified model bytes must be reused")

    monkeypatch.setattr(composition, "urlopen", unexpected_download)
    for directory in (tmp_path / "one/.esphome", tmp_path / "two/data"):
        composition.seed_ui_schema_model_cache(directory)
        cache = directory / "micro_wake_word/a5f625a9"
        assert {path.name: path.read_bytes() for path in cache.iterdir()} == verified
