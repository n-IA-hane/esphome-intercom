"""Optional independent Asterisk process for loopback PBX interoperability tests."""

import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pytest


@asynccontextmanager
async def asterisk_middle(directory, *, port, port_a, port_b, transport, password, force_caller=False):
    """Run extracted test dependencies without installing or changing the host."""
    root_value = os.environ.get("VOIP_TEST_ASTERISK_ROOT")
    if not root_value:
        pytest.skip("Set VOIP_TEST_ASTERISK_ROOT to an extracted Asterisk test runtime")
    root = Path(root_value)
    binary = root / "usr/sbin/asterisk"
    if not binary.is_file():
        pytest.skip("The configured Asterisk test executable is unavailable")
    for name in ("etc", "run", "log", "db", "lib", "spool"):
        (directory / name).mkdir(parents=True, exist_ok=True)
    # Unix control socket paths have a much smaller limit than pytest's
    # evidence directory names. Keep only that ephemeral socket path short.
    control = TemporaryDirectory(prefix="voip-asterisk-")
    control_path = Path(control.name)
    config = directory / "etc/asterisk.conf"
    config.write_text("\n".join([
        "[directories]", f"astetcdir={directory / 'etc'}", f"astrundir={control_path}",
        f"astlogdir={directory / 'log'}", f"astdbdir={directory / 'db'}",
        f"astvarlibdir={directory / 'lib'}", f"astspooldir={directory / 'spool'}",
        f"astmoddir={root / 'usr/lib/asterisk/modules'}",
        f"astdatadir={root / 'usr/share/asterisk'}", "[options]", "verbose=3", "nocolor=yes", "",
    ]))
    modules = (
        "res_pjproject", "res_sorcery_config", "res_sorcery_memory", "res_sorcery_astdb",
        "res_pjsip", "res_pjsip_pubsub", "res_pjsip_authenticator_digest", "res_pjsip_outbound_authenticator_digest",
        "res_pjsip_endpoint_identifier_user", "res_pjsip_session", "res_pjsip_sdp_rtp",
        "res_pjsip_caller_id", "res_pjsip_dtmf_info", "res_pjsip_rfc3326", "res_pjsip_logger",
        "res_rtp_asterisk", "res_timing_timerfd", "chan_pjsip", "pbx_config", "app_dial",
        "bridge_simple", "bridge_native_rtp", "bridge_builtin_features", "codec_alaw", "codec_ulaw",
    )
    (directory / "etc/modules.conf").write_text(
        "[modules]\nautoload=no\n" + "\n".join(f"load={name}.so" for name in modules) + "\n")
    (directory / "etc/logger.conf").write_text("[logfiles]\nconsole=notice,warning,error,verbose\n")
    for filename in ("stasis", "features", "acl", "pjproject", "udptl", "ccss", "indications"):
        sample = root / f"usr/share/asterisk/conf/samples/{filename}.conf.sample"
        (directory / f"etc/{filename}.conf").write_text(sample.read_text() if sample.exists() else "[general]\n")
    for filename in ("cdr", "cel", "manager", "http"):
        option = "enable" if filename in {"cdr", "cel"} else "enabled"
        (directory / f"etc/{filename}.conf").write_text(f"[general]\n{option}=no\n")
    (directory / "etc/rtp.conf").write_text("[general]\nrtpstart=25000\nrtpend=25100\nstrictrtp=no\nicesupport=no\n")
    (directory / "etc/extensions.conf").write_text(
        "[general]\nstatic=yes\nwriteprotect=yes\n[from-ha]\n"
        "exten => _1XX,1,Dial(PJSIP/${EXTEN}@from_101,10)\n"
        "same => n,Hangup()\n"
        "exten => _2XX,1,Dial(PJSIP/${EXTEN}@from_202,10)\n"
        "same => n,Hangup()\n")
    pjsip = [
        "[global]", "type=global", "endpoint_identifier_order=auth_username,username",
        "user_agent=VoIP-Stack-independent-Asterisk-fixture", "",
        "[transport-test]", "type=transport", f"protocol={transport.lower()}", f"bind=127.0.0.1:{port}", "",
    ]
    for local, target, remote in (("from_101", port_a, "from_202"), ("from_202", port_b, "from_101")):
        pjsip += [
            f"[{local}]", "type=endpoint", "transport=transport-test", "context=from-ha",
            "disallow=all", "allow=alaw", "direct_media=no", "identify_by=auth_username",
            f"auth=in-{local}", f"outbound_auth=out-{local}", f"aors={local}",
            "trust_id_inbound=no", "send_pai=no", "send_rpid=no", "contact_user=middle-pbx",
            *( ["from_user=central-pbx"] if force_caller else [] ), "",
            f"[{local}]", "type=aor", f"contact=sip:127.0.0.1:{target};transport={transport.lower()}", "",
            f"[in-{local}]", "type=auth", "auth_type=userpass", f"username={local}", f"password={password}", "",
            f"[out-{local}]", "type=auth", "auth_type=userpass", f"username={remote}", f"password={password}", "",
        ]
    (directory / "etc/pjsip.conf").write_text("\n".join(pjsip))
    environment = dict(os.environ)
    library_paths = (root / "usr/lib", root / "usr/lib/x86_64-linux-gnu")
    environment["LD_LIBRARY_PATH"] = os.pathsep.join(map(str, library_paths)) + (
        os.pathsep + environment["LD_LIBRARY_PATH"] if environment.get("LD_LIBRARY_PATH") else "")
    log_path = directory / "console.log"
    log = log_path.open("wb")
    process = await asyncio.create_subprocess_exec(str(binary), "-f", "-g", "-c", "-C", str(config),
        stdin=asyncio.subprocess.PIPE, stdout=log, stderr=asyncio.subprocess.STDOUT, env=environment)

    async def command(value):
        # Foreground CLI also works where optional AF_UNIX control sockets
        # are unavailable. The version command is an ordered output barrier.
        offset = log_path.stat().st_size
        process.stdin.write((value + "\n" + ("" if value == "core stop now" else "core show version\n")).encode())
        await process.stdin.drain()
        if value == "core stop now":
            return 0, ""
        async with asyncio.timeout(3):
            while True:
                with log_path.open("rb") as reader:
                    reader.seek(offset)
                    output = reader.read().decode(errors="replace")
                if "built by" in output:
                    return 0, output
                assert process.returncode is None, output
                await asyncio.sleep(.02)

    try:
        async with asyncio.timeout(10):
            while True:
                assert process.returncode is None, (directory / "console.log").read_text()
                if "Asterisk Ready." in log_path.read_text():
                    status, output = await command("pjsip show endpoints")
                    if status == 0 and "from_101" in output and "from_202" in output:
                        break
                await asyncio.sleep(.05)
        await command("pjsip set logger on")
        yield SimpleNamespace(process=process, command=command, directory=directory, port=port)
    finally:
        if process.returncode is None:
            await command("core stop now")
            try:
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                process.kill()
                await process.wait()
        log.close()
        control.cleanup()
