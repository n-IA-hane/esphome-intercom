"""RFC 3891 input validation and initial-dialog transition ownership."""

from .voip_phase1_support import asyncio, audio_format, sdp, sip, sip_listener, unittest


class InitialTransitionTest(unittest.IsolatedAsyncioTestCase):
    def request(self, method="INVITE", *, replaces=(), body=None):
        pcm = audio_format.AudioFormat(16000, "s16le", 1, 20)
        if body is None:
            body = sdp.build_offer_directional("127.0.0.2", "127.0.0.2", 42000, [pcm], [pcm]).encode()
        return sip.build_request(method, "sip:HA@127.0.0.1", [
            ("Via", "SIP/2.0/UDP 127.0.0.2:5060;branch=z9hG4bKreplace"),
            ("From", "<sip:alice@127.0.0.2>;tag=alice"),
            ("To", "<sip:HA@127.0.0.1>"),
            ("Call-ID", "replacement"), ("CSeq", f"1 {method}"),
            ("Contact", "<sip:alice@127.0.0.2:5060>"),
            ("Content-Type", "application/sdp"),
            *(("Replaces", value) for value in replaces),
        ], body)

    def endpoint(self, handler, send):
        return sip_listener.SipUdpEndpoint(
            local_ip="127.0.0.1", local_sip_port=5060, local_rtp_port=41000,
            supported_formats=[audio_format.AudioFormat(16000, "s16le", 1, 20)],
            on_invite=handler, send_override=send,
        )

    async def test_invalid_replaces_is_rejected_before_routing(self):
        async def route(_invite):
            self.fail("invalid Replaces reached routing")
        for method, headers in (
            ("INVITE", ("old;to-tag=t;from-tag=f", "old;to-tag=t;from-tag=f")),
            ("OPTIONS", ("old;to-tag=t;from-tag=f",)),
            ("INVITE", ("old;to-tag=t",)),
        ):
            with self.subTest(method=method, headers=headers):
                sent = []
                endpoint = self.endpoint(route, lambda raw, _addr: not sent.append(sip.parse_message(raw)))
                try:
                    await endpoint._handle_datagram(self.request(method, replaces=headers), ("127.0.0.2", 5060))
                    self.assertEqual(sent[-1].status_code, 400)
                    self.assertFalse(endpoint.pending_invites)
                finally:
                    endpoint.connection_lost(None)

    async def test_initial_transition_commits_once_after_successful_response(self):
        for deliver in (True, False):
            with self.subTest(deliver=deliver):
                sent, changes = [], []
                async def commit():
                    self.assertEqual(sent[-1].status_code, 200)
                    changes.append("commit")
                async def rollback():
                    changes.append("rollback")
                async def route(invite):
                    self.assertEqual(invite.replaces.call_id, "old%41")
                    return sip_listener.SipInviteResult(200, "OK", answer_sdp=sdp.build_answer_directional(
                        "127.0.0.1", "127.0.0.1", 41000, invite.send_format, invite.recv_format,
                        recv_formats=invite.recv_formats,
                        dtmf_direction=invite.dtmf_direction,
                        remote_sdp=invite.remote_sdp,
                    ), commit=commit, rollback=rollback)
                def send(raw, _addr):
                    response = sip.parse_message(raw)
                    sent.append(response)
                    return deliver or response.status_code != 200
                endpoint = self.endpoint(route, send)
                request = self.request(replaces=("old%41;to-tag=t;from-tag=f",))
                try:
                    await endpoint._handle_datagram(request, ("127.0.0.2", 5060))
                    await endpoint._handle_datagram(request, ("127.0.0.2", 5060))
                    self.assertEqual(changes, ["commit" if deliver else "rollback"])
                    self.assertEqual(sent[-1].status_code, 200 if deliver else 500)
                finally:
                    endpoint.connection_lost(None)

    async def test_cancel_during_initial_preparation_rolls_back_without_accepting(self):
        started, release = asyncio.Event(), asyncio.Event()
        changes, sent = [], []
        async def commit():
            changes.append("commit")
        async def rollback():
            changes.append("rollback")
        async def route(invite):
            started.set()
            await release.wait()
            return sip_listener.SipInviteResult(200, "OK", answer_sdp=sdp.build_answer_directional(
                "127.0.0.1", "127.0.0.1", 41000, invite.send_format, invite.recv_format,
                recv_formats=invite.recv_formats,
                dtmf_direction=invite.dtmf_direction,
                remote_sdp=invite.remote_sdp,
            ), commit=commit, rollback=rollback)
        endpoint = self.endpoint(route, lambda raw, _addr: not sent.append(sip.parse_message(raw)))
        task = asyncio.create_task(endpoint._handle_datagram(self.request(), ("127.0.0.2", 5060)))
        try:
            await started.wait()
            await endpoint._handle_datagram(self.request("CANCEL", body=b""), ("127.0.0.2", 5060))
            release.set()
            await task
            assert changes == ["rollback"]
            assert not endpoint.active_dialogs
            assert not any(item.status_code == 200 and item.header("CSeq") == "1 INVITE" for item in sent)
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            endpoint.connection_lost(None)

    async def test_cancellation_after_acceptance_rolls_back_and_retires_dialog(self):
        started = asyncio.Event()
        rollback_started, rollback_release = asyncio.Event(), asyncio.Event()
        changes = []
        async def commit():
            started.set()
            await asyncio.Future()
        async def rollback():
            changes.append("rollback")
            rollback_started.set()
            await rollback_release.wait()
        async def route(invite):
            return sip_listener.SipInviteResult(200, "OK", answer_sdp=sdp.build_answer_directional(
                "127.0.0.1", "127.0.0.1", 41000, invite.send_format, invite.recv_format,
                recv_formats=invite.recv_formats,
                dtmf_direction=invite.dtmf_direction,
                remote_sdp=invite.remote_sdp,
            ), commit=commit, rollback=rollback)
        responses = []
        bye_received = asyncio.Event()

        def cooperative_peer(raw, address):
            message = sip.parse_message(raw)
            if message.method == "BYE":
                bye_received.set()
                response = sip.build_uas_response(message, 200, "OK")
                responses.append(asyncio.create_task(endpoint._handle_datagram(response, address)))
            return True

        endpoint = self.endpoint(route, cooperative_peer)
        task = asyncio.create_task(endpoint._handle_datagram(self.request(), ("127.0.0.2", 5060)))
        try:
            await asyncio.wait_for(started.wait(), 2)
            dialog = endpoint.active_dialogs["replacement"]
            task.cancel()
            await asyncio.wait_for(rollback_started.wait(), 2)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            rollback_release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(changes, ["rollback"])
            if dialog.termination_task is not None:
                await asyncio.wait_for(asyncio.shield(dialog.termination_task), 2)
            self.assertTrue(bye_received.is_set())
            self.assertFalse(endpoint.active_dialogs)
            self.assertFalse(endpoint.pending_invites)
        finally:
            rollback_release.set()
            endpoint.connection_lost(None)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.gather(*responses, return_exceptions=True)
