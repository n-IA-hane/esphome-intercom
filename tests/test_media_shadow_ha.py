"""The shadow comparison reports the declared differences and can never break a call."""

import json
import logging

import pytest

from custom_components.voip_stack.core import sdp
from custom_components.voip_stack.core.audio_capabilities import HA_AUDIO_CAPABILITIES
from custom_components.voip_stack.core.media import shadow
from tests.media_corpus_support import CORPUS, POLICIES, load_steps, run_legacy
from tests.test_media_leg_corpus_ha import NOT_YET

pytestmark = pytest.mark.ha

OFFERS = [step for step in load_steps() if step.op == "answer_offer"]


@pytest.fixture(autouse=True)
def shadow_mode():
    previous = shadow.engine_mode()
    shadow.set_engine_mode("shadow")
    yield
    shadow.set_engine_mode(previous)


def test_the_default_mode_runs_nothing(caplog):
    shadow.set_engine_mode("legacy")
    with caplog.at_level(logging.DEBUG, logger=shadow.__name__):
        run_legacy(OFFERS[0])
    assert not [record for record in caplog.records if record.name == shadow.__name__]
    with pytest.raises(ValueError):
        shadow.set_engine_mode("new")
LEDGER = json.loads((CORPUS / "ledger.json").read_text())


@pytest.mark.parametrize("step", OFFERS, ids=lambda step: step.name)
def test_listener_reports_exactly_the_declared_differences(step, caplog):
    """The real listener runs the shadow; its log names the ledger fields and nothing else."""
    declared = {row["field"] for row in LEDGER
                if row["step"] == step.name and row["cc"] not in NOT_YET and not row["field"].startswith("answer ")}
    with caplog.at_level(logging.DEBUG, logger=shadow.__name__):
        run_legacy(step)
    lines = [record.getMessage() for record in caplog.records if record.name == shadow.__name__]
    assert len(lines) == 1, lines
    reported = set()
    if "differs" in lines[0]:
        body = lines[0].split("entry=inbound-offer ", 1)[1].split(" trace=[", 1)[0]
        reported = {item.split(":", 1)[0] for item in body.split("; ")}
    assert reported == declared, lines[0]


def test_an_error_in_the_shadow_is_swallowed(monkeypatch, caplog):
    def explode(*args, **kwargs):
        raise RuntimeError("shadow bug")

    monkeypatch.setattr(shadow, "inbound_offer_differences", explode)
    with caplog.at_level(logging.DEBUG, logger=shadow.__name__):
        shadow.shadow_inbound_offer("call", b"v=0\r\n", None)
    assert "media shadow failed" in caplog.text


def test_unparseable_input_does_not_raise():
    shadow.shadow_inbound_offer("call", b"not a description", None)
    shadow.shadow_inbound_offer("call", b"", None)


ANSWERS = [step for step in load_steps() if step.op == "apply_answer"]


@pytest.mark.parametrize("step", ANSWERS, ids=lambda step: step.name)
def test_answer_shadow_reports_exactly_the_declared_differences(step):
    declared = {row["field"] for row in LEDGER
                if row["step"] == step.name and row["cc"] not in NOT_YET and not row["field"].startswith(("answer ", "dtmf"))}
    legacy = sdp.negotiate_answer_directional(
        step.sdp, list(POLICIES[step.policy][0]), list(POLICIES[step.policy][1]),
        local_offer_sdp=step.local_offer, allow_dahua_pcm=step.profile == "dahua_pcm",
        local_capabilities=HA_AUDIO_CAPABILITIES)
    differences, _trace = shadow.answer_differences(
        step.local_offer, step.sdp, legacy, dahua=step.profile == "dahua_pcm")
    assert {item.split(":", 1)[0] for item in differences} == declared, differences


def test_answer_shadow_is_silent_by_default_and_never_raises(caplog):
    shadow.set_engine_mode("legacy")
    with caplog.at_level(logging.DEBUG, logger=shadow.__name__):
        shadow.shadow_answer("call", ANSWERS[0].local_offer, ANSWERS[0].sdp, None)
    assert not caplog.records
    shadow.set_engine_mode("shadow")
    shadow.shadow_answer("call", b"garbage", b"garbage", None)
