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
    TerminationIntent,
)
from .fsm import CallState
from .session_cleanup import async_wait_for_cleanup

if TYPE_CHECKING:
    from .call_registry import CallRuntimeApi


_LOGGER = logging.getLogger(__name__)

SendFinalResponse: TypeAlias = Callable[[int, str, str], bool]
ClaimAnswer: TypeAlias = Callable[[], bool]


def bind_final_response(
    send: Callable[..., bool],
    context: object,
    call_id: str,
) -> SendFinalResponse:
    """Bind the common HA final-response adapter once."""

    return lambda status, reason, sdp: bool(
        send(context, call_id, status, reason, answer_sdp=sdp)
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
            claimed = self.session.claim_answer(self.token, claim)
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

    transaction = AnswerTransaction(
        session,
        (lambda _status, _reason, _sdp: True)
        if response_already_sent
        else send_final_response,
    )
    return await transaction.commit(answer_sdp, claim=claim)


async def async_commit_runtime_answer(
    runtime: CallRuntimeApi,
    call_id: str,
    answer_sdp: str,
    *,
    send_final_response: SendFinalResponse,
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
    return await async_commit_answer(
        session,
        answer_sdp,
        claim=lambda: runtime.transition(
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
        is not None,
        send_final_response=send_final_response,
        response_already_sent=response_already_sent,
    )
