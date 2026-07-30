"""Project text style contracts."""

from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def test_tracked_text_avoids_typographic_dashes() -> None:
    """Keep project-owned text free from em dash and en dash punctuation."""

    tracked = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout.split(b"\0")
    forbidden = {"\u2013": "en dash", "\u2014": "em dash"}
    violations: list[str] = []
    for raw_path in tracked:
        if not raw_path:
            continue
        path = ROOT / raw_path.decode()
        if not path.is_file():
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            found = sorted(name for character, name in forbidden.items() if character in line)
            if found:
                violations.append(
                    f"{path.relative_to(ROOT)}:{line_number}: {', '.join(found)}"
                )

    assert not violations, (
        "Use commas, colons or periods instead of typographic dashes:\n"
        + "\n".join(violations)
    )
