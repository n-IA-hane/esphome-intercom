"""Small disposable qualification outputs, independent of compiler TMPDIR."""

from pathlib import Path

# Host/SDK builds may set TMPDIR to persistent disk. Captures stay in /tmp.
CAPTURE_ROOT = Path("/tmp/voip-tests")
