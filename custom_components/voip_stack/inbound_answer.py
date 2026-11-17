"""Transactional ownership boundary for final inbound SIP answers."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Any, TypeAlias

from .endpoint_session import (
    AsyncCloser,
    CallToken,
    CleanupStage,
    EndpointCallSession,
    ManagedResource,
    LegPhase,
    SessionHandoff,
    TerminationIntent,
)
from .fsm import CallState
from .session_cleanup import async_wait_for_cleanup

if TYPE_CHECKING:
    from .call_registry import CallRuntimeApi


_LOGGER = logging.getLogger(__name__)

SendFinalResponse: TypeAlias = Callable[[int, str, str], bool]
ClaimAnswer: TypeAlias = Callable[[], bool]


class _AnswerClaimRejected(Exception):
    """Abort state mutation before transferring resources or sending success."""


def bind_final_response(
    send: Callable[..., bool],
    context: object,
    token: CallToken,
) -> SendFinalResponse:
    """Bind the common HA final-response adapter once."""

    return lambda status, reason, sdp: bool(
        send(
            context,
            token.call_id,
            status,
            reason,
            answer_sdp=sdp,
            expected_generation=token.generation,
        )
    )


@dataclass(frozen=True, slots=True)
class AnswerCommitResult:
    committed: bool
    response_sent: bool
    reason: str = ""


class AnswerTransaction:
    """Prepare media first, then atomically claim and answer one live call."""

    def __init__(
        self,
        session: EndpointCallSession,
        send_final_response: SendFinalResponse,
    ) -> None:
        self.session = session
        self.token: CallToken = session.token
        self._send_final_response = send_final_response
        self._prepared: list[ManagedResource] = []
        self._finished = False

    def add_resource(
        self,
        name: str,
        value: Any,
        closer: AsyncCloser,
        *,
        stage: CleanupStage = CleanupStage.RESERVATION,
    ) -> Any:
        if self._finished:
            raise RuntimeError("answer transaction has already finished")
        if not str(name or "").strip():
            raise ValueError("resource name must not be empty")
        self._prepared.append(ManagedResource(str(name), value, closer, stage))
        return value

    async def rollback(self, reason: str) -> None:
        """Release every still-prepared resource in reverse ownership order."""

        if self._finished:
            return
        self._finished = True

        async def _rollback() -> None:
            for resource in reversed(self._prepared):
                try:
                    await resource.close(reason)
                except BaseException:
                    _LOGGER.debug(
                        "Inbound answer rollback failed resource=%s call_id=%s",
                        resource.name,
                        self.session.call_id,
                        exc_info=True,
                    )
            self._prepared.clear()

        cleanup = asyncio.create_task(
            _rollback(),
            name=f"voip-answer-rollback-{self.session.call_id}",
        )
        await async_wait_for_cleanup(cleanup)

    async def commit(
        self,
        answer_sdp: str,
        *,
        claim: ClaimAnswer,
        status: int = 200,
        reason: str = "OK",
        response_already_sent: bool = False,
    ) -> AnswerCommitResult:
        """Publish only the completed answer or its completed compensation."""
        with self.session.defer_state_notifications():
            return await self._commit(
                answer_sdp, claim=claim, status=status, reason=reason,
                response_already_sent=response_already_sent,
            )

    async def _commit(
        self,
        answer_sdp: str,
        *,
        claim: ClaimAnswer,
        status: int = 200,
        reason: str = "OK",
        response_already_sent: bool = False,
    ) -> AnswerCommitResult:
        """Claim without yielding, transfer resources, then send final response."""

        if self._finished:
            raise RuntimeError("answer transaction has already finished")
        if not self.session.owns(self.token):
            await self.rollback("stale_call")
            return AnswerCommitResult(False, False, "stale_call")

        prepared = tuple(self._prepared)
        prepared_names = [resource.name for resource in prepared]
        owned_names = {resource.name for resource in self.session.resources}
        if len(prepared_names) != len(set(prepared_names)) or any(
            name in owned_names for name in prepared_names
        ):
            await self.rollback("resource_conflict")
            return AnswerCommitResult(False, False, "resource_conflict")

        # No await is allowed between the generation check, authoritative
        # state claim, resource transfer, and final response.  This makes the
        # sequence atomic relative to asyncio termination callbacks.
        try:
            with SessionHandoff(self.session.state_runtime, self.session):
                claimed = (
                    claim()
                    if response_already_sent and self.session.answer_committed
                    else self.session.claim_answer(self.token, claim)
                )
                if not claimed or not self.session.owns(self.token):
                    raise _AnswerClaimRejected
        except _AnswerClaimRejected:
            await self.rollback("stale_call")
            return AnswerCommitResult(False, False, "stale_call")
        except Exception:
            _LOGGER.exception(
                "Inbound answer state claim failed call_id=%s",
                self.session.call_id,
            )
            await self.rollback("claim_failed")
            return AnswerCommitResult(False, False, "claim_failed")
        if not claimed or not self.session.owns(self.token):
            await self.rollback("stale_call")
            return AnswerCommitResult(False, False, "stale_call")

        self._prepared.clear()
        transferred: list[ManagedResource] = []
        try:
            for resource in prepared:
                self.session.add_resource(
                    resource.name,
                    resource.value,
                    resource.closer,
                    stage=resource.stage,
                )
                transferred.append(resource)
        except Exception:
            _LOGGER.exception(
                "Inbound answer resource transfer failed call_id=%s",
                self.session.call_id,
            )
            for resource in transferred:
                self.session.release_resource(resource.name, value=resource.value)
            self._prepared.extend(prepared)
            await self.rollback("resource_transfer_failed")
            await self.session.terminate(TerminationIntent("resource_transfer_failed"))
            return AnswerCommitResult(False, False, "resource_transfer_failed")
        self._finished = True
        if response_already_sent:
            return AnswerCommitResult(True, False)
        try:
            response_sent = bool(
                self._send_final_response(int(status), str(reason), str(answer_sdp))
            )
        except Exception:
            _LOGGER.exception(
                "Inbound final response failed call_id=%s",
                self.session.call_id,
            )
            response_sent = False
        if response_sent:
            return AnswerCommitResult(True, True)

        await self.session.terminate(TerminationIntent("final_response_failed"))
        return AnswerCommitResult(False, False, "final_response_failed")


async def async_commit_answer(
    session: EndpointCallSession,
    answer_sdp: str,
    *,
    claim: ClaimAnswer,
    send_final_response: SendFinalResponse,
    response_already_sent: bool = False,
) -> AnswerCommitResult:
    """Commit one answer through the session generation owner."""

    transaction = AnswerTransaction(session, send_final_response)
    return await transaction.commit(
        answer_sdp, claim=claim, response_already_sent=response_already_sent
    )


def confirm_existing_caller_leg(runtime: CallRuntimeApi, session: EndpointCallSession, wire_call_id: str) -> None:
    """Confirm only the existing source SIP leg inside the answer claim."""
    leg = session.legs.get(wire_call_id)
    if (leg is not None and leg.role == "caller" and leg.sip_call_id == wire_call_id
            and leg.endpoint_id == session.metadata.get("source_endpoint_id")
            and leg.phase in {LegPhase.NEW, LegPhase.INVITING, LegPhase.RINGING}):
        runtime.observe_leg(session.call_id, leg.leg_id, role=leg.role,
            state=CallState.IN_CALL.value, generation=session.generation)


async def async_commit_runtime_answer(
    runtime: CallRuntimeApi,
    call_id: str,
    answer_sdp: str,
    *,
    send_final_response: Callable[..., bool],
    response_context: object,
    owner: str,
    caller: str = "",
    callee: str = "",
    route_kind: str = "",
    endpoint_id: str = "",
    source_endpoint_id: str = "",
    dest_endpoint_id: str = "",
    media_client_id: str = "",
    response_already_sent: bool = False,
) -> AnswerCommitResult:
    """Atomically publish and signal one ordinary runtime answer."""

    session = runtime.get_session(call_id)
    if session is None:
        return AnswerCommitResult(False, False, "stale_call")
    def claim() -> bool:
        claimed = runtime.transition(
            call_id,
            state=CallState.IN_CALL.value,
            owner=owner,
            caller=caller,
            callee=callee,
            route_kind=route_kind,
            expected_generation=session.generation,
            endpoint_id=endpoint_id,
            source_endpoint_id=source_endpoint_id,
            dest_endpoint_id=dest_endpoint_id,
            media_client_id=media_client_id,
        )
        if claimed is None:
            return False
        confirm_existing_caller_leg(runtime, claimed, call_id)
        return True

    return await async_commit_answer(
        session,
        answer_sdp,
        claim=claim,
        send_final_response=bind_final_response(
            send_final_response,
            response_context,
            session.token,
        ),
        response_already_sent=response_already_sent,
    )
