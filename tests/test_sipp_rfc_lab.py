from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SIPP = ROOT / "tests" / "sipp"


def test_rfc_scenarios_use_real_dialog_messages() -> None:
    reliable = (SIPP / "reliable-provisional.xml").read_text()
    refresh = (SIPP / "session-refresh.xml").read_text()
    expiry = (SIPP / "session-expiry.xml").read_text()
    fork = (SIPP / "remote-fork-late-2xx.xml").read_text()

    assert "Require: 100rel" in reliable
    assert 'request="PRACK"' in reliable
    assert "RSeq: 41" in reliable
    assert "Session-Expires: 4;refresher=uac" in refresh
    assert 'request="UPDATE"' in refresh
    assert "Session-Expires: 4;refresher=uas" in expiry
    assert 'request="BYE" timeout="5000"' in expiry
    assert "tag=fork-a" in fork
    assert "tag=fork-b" in fork
    assert fork.count('request="ACK"') == 2
    assert fork.count('request="BYE"') == 2


def test_rfc_lab_runner_keeps_cases_data_driven() -> None:
    source = (ROOT / "scripts" / "run_sipp_rfc_lab.py").read_text()

    assert "for case in CASES" in source
    assert "runtime_quiescence" in source
    assert "FlowSnapshot.capture" in source
    assert "snapshot.apply(api)" in source
