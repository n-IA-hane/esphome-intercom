"""UI/schema fixtures reuse authentic, hash-verified wake-word models."""

import json

import pytest

from tests import test_package_composition as composition


@pytest.mark.parametrize("word,size", [("alexa", 55856), ("stop", 45744)])
def test_ui_model_fixture_contains_real_model_and_rejects_corruption(word, size):
    files = dict(composition._ui_model_files(word))
    model_name = word + ".tflite"
    assert json.loads(files["manifest.json"])["model"] == model_name
    assert len(files[model_name]) == size and files[model_name][4:8] == b"TFL3"
    _, _, manifest_hash, data_hash = composition._MODEL_SOURCES[word]
    for name, digest in (("manifest.json", manifest_hash), (model_name, data_hash)):
        assert composition._verify_model_bytes(files[name], digest) is files[name]
        corrupted = files[name][:-1] + bytes([files[name][-1] ^ 1])
        with pytest.raises(ValueError, match="SHA256 mismatch"):
            composition._verify_model_bytes(corrupted, digest)


def test_ui_model_fixture_reuses_verified_bytes_without_another_download(tmp_path, monkeypatch):
    verified = {word: dict(composition._ui_model_files(word)) for word in ("alexa", "stop")}

    def unexpected_download(*_args, **_kwargs):
        raise AssertionError("Verified model bytes must be reused")

    monkeypatch.setattr(composition, "urlopen", unexpected_download)
    for directory in (tmp_path / "one/.esphome", tmp_path / "two/data"):
        composition.seed_ui_schema_model_cache(directory)
        for word, key in (("alexa", "a5f625a9"), ("stop", "23a3a6c4")):
            cache = directory / "micro_wake_word" / key
            assert {path.name: path.read_bytes() for path in cache.iterdir()} == verified[word]
