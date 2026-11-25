# Offline schema model assets

These are the authentic Alexa and Stop model assets by Kevin Ahrendt, encoded
as base64 without changing their bytes. They are test fixtures, not runtime
model forks. Their source URLs and decoded SHA256 hashes are in `sources.json`.
The Alexa files are fixed to ESPHome's official models revision
`40ff33f57f8fc6ad71a75ef085abbf742495b225`; Stop files are the recorded official
OHF Voice release assets, fixed by their content hashes.

Both upstream repositories distribute these assets under Apache License 2.0;
see `licenses/APACHE-2.0.txt`. The original model manifests retain the author's
name, website, language and model metadata.

Tests seed ESPHome's normal model cache and verify the hashes. Schema-test
subprocesses also substitute only these exact model HTTP URLs through
`tests/fixtures/offline_schema/sitecustomize.py`, because official ESPHome can
revalidate an otherwise seeded cache. Parser, provider and code generation
remain real. No download, synthetic TFLite file or manual model preparation is
needed. Production YAMLs continue to use their normal public model sources.
