"""Resources and cancellation-safe cleanup for outbound SIP dial attempts."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from .media_ports import RtpPortReservation
from .session_cleanup import async_cleanup_sip_runtime, async_wait_for_cleanup
from .sip_client import SipCallClient
from .sip_video_relay import SipVideoRtpRelay


@dataclass(slots=True)
class OutboundLeg:
    """One physical SIP candidate in a dial fork."""

    member: str
    uri: object
    client: SipCallClient
    ports: RtpPortReservation
    bridge_to_softphone: bool = False
    endpoint_id: str = ""
    candidate_id: str = ""
    tier: int = 0
    order: int = 0
    video_relay: SipVideoRtpRelay | None = None
    video_failure_reason: str = ""


@dataclass(frozen=True, slots=True)
class BrowserLeg:
    """One logical browser candidate in a dial fork."""

    member: str
    endpoint_id: str
    name: str
    device_id: str


async def async_apply_outbound_video_answer(
    attempt: OutboundLeg,
    answer: Any | None,
) -> Any | None:
    """Keep an accepted video relay or release a rejected reservation."""

    relay = attempt.video_relay
    if relay is None or answer is not None:
        return answer
    await relay.stop()
    attempt.video_relay = None
    attempt.video_failure_reason = "remote_video_rejected"
    return None


async def async_close_client_and_release(
    client: SipCallClient,
    ports: RtpPortReservation,
    *,
    bye: bool = False,
) -> None:
    """Terminate one client and release its ports after signaling cleanup."""

    async def cleanup() -> None:
        try:
            await async_cleanup_sip_runtime(
                client=client,
                terminate_client=bye,
            )
        finally:
            ports.release()

    task = asyncio.create_task(
        cleanup(),
        name=f"voip-outbound-client-close-{client.dialog_ids.call_id}",
    )
    await async_wait_for_cleanup(task)


async def async_close_outbound_leg(
    attempt: OutboundLeg,
    *,
    cancel: bool = False,
    bye_or_cancel: bool = False,
) -> None:
    """Terminate and fully release one outbound dial leg."""

    async def cleanup() -> None:
        try:
            await async_cleanup_sip_runtime(
                client=attempt.client,
                terminate_client=bool(cancel or bye_or_cancel),
            )
        finally:
            try:
                if attempt.video_relay is not None:
                    await attempt.video_relay.stop()
                    attempt.video_relay = None
            finally:
                # Late final responses may still produce RTP during signaling
                # teardown. Releasing earlier could route that RTP into a new
                # unrelated call using the same bounded pool.
                attempt.ports.release()

    task = asyncio.create_task(
        cleanup(),
        name=f"voip-outbound-leg-close-{attempt.client.dialog_ids.call_id}",
    )
    await async_wait_for_cleanup(task)


async def async_cancel_and_join_tasks(tasks: list[asyncio.Task]) -> None:
    """Cancel a dial fork's tasks and join every terminal callback."""

    async def cleanup() -> None:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    task = asyncio.create_task(
        cleanup(),
        name="voip-outbound-dial-task-cleanup",
    )
    await async_wait_for_cleanup(task)


async def async_cleanup_outbound_attempts(
    tasks: list[asyncio.Task],
    attempts: list[OutboundLeg],
) -> None:
    """Cancel a fork and close every unsuccessful physical candidate."""

    async def cleanup() -> None:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if attempts:
            await asyncio.gather(
                *(
                    async_close_outbound_leg(attempt, bye_or_cancel=True)
                    for attempt in attempts
                ),
                return_exceptions=True,
            )

    task = asyncio.create_task(
        cleanup(),
        name="voip-outbound-attempt-cleanup",
    )
    await async_wait_for_cleanup(task)
