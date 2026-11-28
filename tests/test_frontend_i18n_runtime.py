"""Runtime contracts for the custom-card locale boundary."""

from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "custom_components/voip_stack/frontend/voip-stack-i18n.js"
CARD_MODEL = ROOT / "custom_components/voip_stack/frontend/voip-stack-card-model.js"


def test_frontend_translates_supported_languages_and_falls_back_to_english() -> None:
    script = f"""
import {{ voipStackTranslate }} from {MODULE.as_uri()!r};
const checks = [
  [{{ language: "pt-BR" }}, "Answer", "Atender"],
  [{{ language: "pt" }}, "Extension:", "Extensão:"],
  [{{ language: "de-DE" }}, "Answer", "Annehmen"],
  [{{ language: "de" }}, "Extension:", "Nebenstelle:"],
  [{{ language: "it" }}, "Answer", "Rispondi"],
  [{{ language: "it-IT" }}, "Call", "Chiama"],
  [{{ language: "it" }}, "Decline", "Rifiuta"],
  [{{ language: "it" }}, "Hangup", "Riaggancia"],
  [{{ language: "it" }}, "Options", "Opzioni"],
  [{{ language: "it-IT" }}, "Call popups", "Popup chiamate"],
  [{{ language: "it" }}, "Save", "Salva"],
  [{{ language: "it" }}, "Incoming: Casa", "In arrivo: Casa"],
  [{{ language: "it" }}, "Calling Answer...", "Chiamata a Answer..."],
  [{{ language: "it-IT" }}, "In Call: Reception@Hotel", "In chiamata con Reception@Hotel"],
  [{{ language: "it" }}, "Call with *42 $& {{name}} ended.", "Chiamata con *42 $& {{name}} terminata."],
  [{{ language: "en" }}, "Call popups", "Call popups"],
  [{{ language: "pt-BR" }}, "Incoming: Sala", "Recebendo: Sala"],
  [{{ language: "de-DE" }}, "Calling Büro...", "Büro wird angerufen..."],
  [{{ language: "pt-BR" }}, "Call with Sala ended.", "Chamada com Sala encerrada."],
  [{{ language: "de-DE" }}, "Call with Büro ended.", "Gespräch mit Büro beendet."],
  [{{ language: "it" }}, "Record call", "Registra chiamata"],
  [{{ language: "it" }}, "Reason: Busy", "Motivo: Occupato"],
  [{{ language: "it" }}, "Video unavailable: Timeout", "Video non disponibile: Tempo scaduto"],
  [{{ language: "de-DE" }}, "Call popups", "Anruf-Popups"],
  [{{ language: "de" }}, "Minimize call", "Anruf minimieren"],
  [{{ language: "pt-PT" }}, "Call popups", "Pop-ups de chamadas"],
  [{{ language: "pt-BR" }}, "Reopen call", "Reabrir chamada"],
  [{{ language: "cs-CZ" }}, "Answer", "Přijmout"],
  [{{ language: "cs" }}, "Call popups", "Vyskakovací okna hovorů"],
  [{{ language: "cs" }}, "SIP account", "Účet SIP"],
  [{{ language: "cs" }}, "No messages", "Žádné zprávy"],
  [{{ language: "cs" }}, "Calling Answer...", "Volání Answer..."],
  [{{ language: "cs" }}, "Reason: Busy", "Důvod: Obsazeno"],
  [{{ language: "cs" }}, "Call with *42 $& {{name}} ended.", "Hovor s *42 $& {{name}} skončil."],
  [{{ language: "de" }}, "Call with *42 $& {{name}} ended.", "Gespräch mit *42 $& {{name}} beendet."],
  [{{ language: "pt" }}, "Call with *42 $& {{name}} ended.", "Chamada com *42 $& {{name}} encerrada."],
  [{{ language: "fr" }}, "Call popups", "Call popups"],
  [{{ language: "cs" }}, "Unknown provider response", "Unknown provider response"],
];
for (const [hass, source, expected] of checks) {{
  const actual = voipStackTranslate(hass, source);
  if (actual !== expected) throw new Error(`${{source}}: ${{actual}} !== ${{expected}}`);
}}
"""
    subprocess.run(["node", "--input-type=module", "--eval", script], check=True)


def test_frontend_translates_end_reasons_and_wrapped_errors() -> None:
    script = f"""
import {{ voipStackTranslate }} from {MODULE.as_uri()!r};
import {{ formatEndReason }} from {CARD_MODEL.as_uri()!r};
const check = (language, source, expected) => {{
  const actual = voipStackTranslate({{ language }}, source);
  if (actual !== expected) throw new Error(`${{language}} ${{source}}: ${{actual}} !== ${{expected}}`);
}};
// SIP text from the peer must survive translation untouched.
const detail = "Busy Here $& {{name}}";

const endReasons = [
  ["declined", "self", "", "Recusa local"],
  ["declined", "self", detail, `Recusa local: "${{detail}}"`],
  ["declined", "dest", detail, `O destinatário recusou: "${{detail}}"`],
  ["declined", "", detail, `Recusada: "${{detail}}"`],
  ["error", "self", "486", "Erro local (código 486)"],
  ["error", "self", detail, `Erro local: "${{detail}}"`],
  ["error", "remote", detail, `Erro no outro lado: "${{detail}}"`],
  ["error", "source", "", "Erro no chamador"],
  ["error", "", "500", "Erro (código 500)"],
  ["idle", "remote", "remote_hangup", "Desligamento remoto"],
];
for (const [kind, origin, reason, expected] of endReasons) {{
  check("pt-BR", `Reason: ${{formatEndReason({{ kind, origin, reason }})}}`, `Motivo: ${{expected}}`);
}}

check("pt-BR", "Auto Answer was not enabled: Browser camera access was not granted.",
  "O atendimento automático não foi ativado: O acesso à câmera no navegador não foi permitido.");
check("pt-BR", `Audio media update failed: ${{detail}}`, `Falha ao atualizar a mídia de áudio: ${{detail}}`);
check("pt-BR", "Video unavailable: SIP video negotiation timed out",
  "Vídeo indisponível: Tempo esgotado na negociação do vídeo SIP");
check("pt-BR", "Browser cannot encode negotiated SIP video H264",
  "O navegador não consegue codificar o vídeo SIP negociado H264");
check("pt-BR", "Camera 2", "Câmera 2");
check("pt-BR", "Speaker (Realtek Audio)", "Speaker (Realtek Audio)");

// Without the new keys, the English source is returned unchanged.
for (const language of ["en", "fr", "de", "it", "cs"]) {{
  for (const source of [
    `Local decline: "${{detail}}"`,
    "Error (code 500)",
    `Auto Answer was not enabled: ${{detail}}`,
    `Audio media update failed: ${{detail}}`,
    "Browser cannot encode negotiated SIP video H264",
    "Camera 2",
    "Phone media error",
  ]) check(language, source, source);
}}
// A head that a language already has is now translated; the detail stays literal.
for (const language of ["de", "it", "cs"]) {{
  const head = voipStackTranslate({{ language }}, "Local error");
  if (head === "Local error") throw new Error(`${{language}}: missing "Local error"`);
  check(language, `Local error: "${{detail}}"`, `${{head}}: "${{detail}}"`);
}}
"""
    subprocess.run(["node", "--input-type=module", "--eval", script], check=True)
