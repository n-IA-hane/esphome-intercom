"""Runtime contracts for the custom-card locale boundary."""

from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "custom_components/voip_stack/frontend/voip-stack-i18n.js"


def test_frontend_translates_supported_languages_and_falls_back_to_english() -> None:
    script = f"""
import {{ voipStackTranslate }} from {MODULE.as_uri()!r};
const checks = [
  [{{ language: "pt-BR" }}, "Answer", "Atender"],
  [{{ language: "pt" }}, "Extension:", "Extensão:"],
  [{{ language: "de-DE" }}, "Answer", "Annehmen"],
  [{{ language: "de" }}, "Extension:", "Nebenstelle:"],
  [{{ language: "it" }}, "Answer", "Answer"],
  [{{ language: "pt-BR" }}, "Incoming: Sala", "Recebendo: Sala"],
  [{{ language: "de-DE" }}, "Calling Büro...", "Büro wird angerufen..."],
  [{{ language: "pt-BR" }}, "Call with Sala ended.", "Chamada com Sala encerrada."],
  [{{ language: "de-DE" }}, "Call with Büro ended.", "Gespräch mit Büro beendet."],
];
for (const [hass, source, expected] of checks) {{
  const actual = voipStackTranslate(hass, source);
  if (actual !== expected) throw new Error(`${{source}}: ${{actual}} !== ${{expected}}`);
}}
"""
    subprocess.run(["node", "--input-type=module", "--eval", script], check=True)
