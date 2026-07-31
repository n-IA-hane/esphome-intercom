"""PBX outbound forking with deterministic winner and stale-call rollback."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
import logging

from .endpoint_session import CallToken, EndpointCallSession
from .session_cleanup import async_wait_for_cleanup


_LOGGER = logging.getLogger(__name__)


class ForkStrategy(StrEnum):
    PARALLEL = "parallel"
    SEQUENTIAL = "sequential"


class DialDisposition(StrEnum):
    ANSWERED = "answered"
    BUSY = "busy"
    DND = "dnd"
    DECLINED = "declined"
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    MEDIA_INCOMPATIBLE = "media_incompatible"
    AUTH_FAILED = "auth_failed"
    CANCELLED = "cancelled"
    SOURCE_CANCELLED = "source_cancelled"
    REROUTE = "reroute"
    PROTOCOL_ERROR = "protocol_error"


class LegCloseMode(StrEnum):
    """Required signaling cleanup for a losing or abandoned branch."""

    CANCEL_OR_BYE = "cancel_or_bye"
    BYE = "bye"
    CLOSE = "close"


@dataclass(frozen=True, slots=True)
class DialOutcome:
    disposition: DialDisposition
    status: int = 0
    reason: str = ""

    @property
    def answered(self) -> bool:
        return self.disposition is DialDisposition.ANSWERED


DialOperation = Callable[[], Awaitable[DialOutcome]]
CloseOperation = Callable[[LegCloseMode], Awaitable[None]]
WinnerCommit = Callable[["DialCandidate", DialOutcome], bool]


@dataclass(frozen=True, slots=True)
class DialCandidate:
    candidate_id: str
    dial: DialOperation
    close: CloseOperation
    tier: int = 0
    order: int = 0
    endpoint_id: str = ""
    control: bool = False


@dataclass(frozen=True, slots=True)
class ForkResult:
    outcome: DialOutcome
    winner: DialCandidate | None = None
    attempted: tuple[str, ...] = ()


class DialForkController:
    """Run one bounded fork while a call-generation token remains current."""

    def __init__(
        self,
        session: EndpointCallSession,
        candidates: Iterable[DialCandidate],
        *,
        strategy: ForkStrategy = ForkStrategy.PARALLEL,
        tier_strategies: Mapping[int, ForkStrategy | str] | None = None,
        overall_timeout: float = 30.0,
        step_timeout: float = 15.0,
    ) -> None:
        self.session = session
        self.token: CallToken = session.token
        self.candidates = tuple(
            sorted(candidates, key=lambda item: (item.tier, item.order, item.candidate_id))
        )
        if len({candidate.candidate_id for candidate in self.candidates}) != len(
            self.candidates
        ):
            raise ValueError("dial candidate IDs must be unique")
        if float(overall_timeout) <= 0 or float(step_timeout) <= 0:
            raise ValueError("fork timeouts must be positive")
        self.strategy = ForkStrategy(strategy)
        self.tier_strategies = {
            int(tier): ForkStrategy(tier_strategy)
            for tier, tier_strategy in (tier_strategies or {}).items()
        }
        self.overall_timeout = float(overall_timeout)
        self.step_timeout = float(step_timeout)

    async def _close_candidates(
        self,
        candidates: Iterable[DialCandidate],
        mode: LegCloseMode,
    ) -> None:
        unique = tuple(dict.fromkeys(candidates))
        if not unique:
            return

        async def _close() -> None:
            results = await asyncio.gather(
                *(candidate.close(mode) for candidate in unique),
                return_exceptions=True,
            )
            for candidate, result in zip(unique, results, strict=True):
                if isinstance(result, BaseException):
                    _LOGGER.debug(
                        "PBX fork leg cleanup failed candidate=%s",
                        candidate.candidate_id,
                        exc_info=(type(result), result, result.__traceback__),
                    )

        cleanup = asyncio.create_task(_close(), name="voip-dial-fork-cleanup")
        await async_wait_for_cleanup(cleanup)

    async def _wait_for_cleanup_or_termination(
        self,
        cleanup: asyncio.Task[None],
    ) -> bool:
        """Return true when source termination began before cleanup committed."""

        if self.session.termination_started.is_set():
            await async_wait_for_cleanup(cleanup)
            return True
        termination = asyncio.create_task(
            self.session.termination_started.wait(),
            name="voip-dial-fork-source-termination",
        )
        try:
            done, _pending = await asyncio.wait(
                {cleanup, termination},
                return_when=asyncio.FIRST_COMPLETED,
            )
            source_terminated = termination in done and termination.result()
            await async_wait_for_cleanup(cleanup)
            return bool(source_terminated or not self.session.owns(self.token))
        finally:
            if not termination.done():
                termination.cancel()
            await asyncio.gather(termination, return_exceptions=True)

    @staticmethod
    def _reduce_failures(outcomes: list[DialOutcome]) -> DialOutcome:
        if not outcomes:
            return DialOutcome(DialDisposition.UNAVAILABLE, 480, "Temporarily Unavailable")
        dispositions = [outcome.disposition for outcome in outcomes]
        if all(item is DialDisposition.DECLINED for item in dispositions):
            return DialOutcome(DialDisposition.DECLINED, 603, "Decline")
        if any(item is DialDisposition.BUSY for item in dispositions):
            return DialOutcome(DialDisposition.BUSY, 486, "Busy Here")
        if any(item is DialDisposition.DND for item in dispositions):
            return DialOutcome(DialDisposition.DND, 486, "DND")
        if all(item is DialDisposition.MEDIA_INCOMPATIBLE for item in dispositions):
            return DialOutcome(
                DialDisposition.MEDIA_INCOMPATIBLE,
                488,
                "Not Acceptable Here",
            )
        if all(item is DialDisposition.TIMEOUT for item in dispositions):
            return DialOutcome(DialDisposition.TIMEOUT, 408, "Request Timeout")
        return DialOutcome(DialDisposition.UNAVAILABLE, 480, "Temporarily Unavailable")

    async def _dial_parallel_tier(
        self,
        candidates: tuple[DialCandidate, ...],
        deadline: float,
    ) -> tuple[DialCandidate | None, DialOutcome | None, list[DialOutcome]]:
        loop = asyncio.get_running_loop()
        tasks = {
            asyncio.create_task(
                candidate.dial(),
                name=f"voip-dial-fork-{candidate.candidate_id}",
            ): candidate
            for candidate in candidates
        }
        failures: list[DialOutcome] = []
        try:
            pending = set(tasks)
            while pending:
                if not self.session.owns(self.token):
                    return None, DialOutcome(DialDisposition.CANCELLED, 487), failures
                timeout = max(0.0, deadline - loop.time())
                if timeout <= 0:
                    failures.extend(
                        DialOutcome(DialDisposition.TIMEOUT, 408)
                        for _task in pending
                    )
                    return None, None, failures
                termination = asyncio.create_task(
                    self.session.termination_started.wait(),
                    name="voip-dial-fork-termination-wait",
                )
                try:
                    done, still_pending = await asyncio.wait(
                        {*pending, termination},
                        timeout=timeout,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    if not termination.done():
                        termination.cancel()
                    await asyncio.gather(termination, return_exceptions=True)
                if self.session.termination_started.is_set():
                    return None, DialOutcome(DialDisposition.CANCELLED, 487), failures
                completed = [
                    task for task in tasks if task in done and task is not termination
                ]
                batch: list[tuple[DialCandidate, DialOutcome]] = []
                pending = {task for task in still_pending if task is not termination}
                for task in completed:
                    candidate = tasks[task]
                    try:
                        outcome = task.result()
                    except asyncio.CancelledError:
                        outcome = DialOutcome(DialDisposition.CANCELLED, 487)
                    except Exception:
                        _LOGGER.debug(
                            "PBX fork dial failed candidate=%s",
                            candidate.candidate_id,
                            exc_info=True,
                        )
                        outcome = DialOutcome(DialDisposition.PROTOCOL_ERROR, 500)
                    batch.append((candidate, outcome))
                for control_disposition in (
                    DialDisposition.SOURCE_CANCELLED,
                    DialDisposition.REROUTE,
                ):
                    control = next(
                        (
                            outcome
                            for _candidate, outcome in batch
                            if outcome.disposition is control_disposition
                        ),
                        None,
                    )
                    if control is not None:
                        return None, control, failures
                for candidate, outcome in batch:
                    if outcome.answered:
                        return candidate, outcome, failures
                    failures.append(outcome)
            return None, None, failures
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _dial_sequential_tier(
        self,
        candidates: tuple[DialCandidate, ...],
        deadline: float,
    ) -> tuple[DialCandidate | None, DialOutcome | None, list[DialOutcome]]:
        loop = asyncio.get_running_loop()
        failures: list[DialOutcome] = []
        for candidate in candidates:
            if not self.session.owns(self.token):
                return None, DialOutcome(DialDisposition.CANCELLED, 487), failures
            remaining = max(0.0, deadline - loop.time())
            if remaining <= 0:
                break
            try:
                outcome = await asyncio.wait_for(
                    candidate.dial(),
                    timeout=min(self.step_timeout, remaining),
                )
            except asyncio.TimeoutError:
                outcome = DialOutcome(DialDisposition.TIMEOUT, 408)
                await self._close_candidates((candidate,), LegCloseMode.CANCEL_OR_BYE)
            except Exception:
                _LOGGER.debug(
                    "PBX sequential dial failed candidate=%s",
                    candidate.candidate_id,
                    exc_info=True,
                )
                outcome = DialOutcome(DialDisposition.PROTOCOL_ERROR, 500)
            if self.session.termination_started.is_set():
                await self._close_candidates((candidate,), LegCloseMode.CANCEL_OR_BYE)
                return None, DialOutcome(DialDisposition.CANCELLED, 487), failures
            if outcome.disposition in {
                DialDisposition.SOURCE_CANCELLED,
                DialDisposition.REROUTE,
            }:
                return None, outcome, failures
            if outcome.answered:
                return candidate, outcome, failures
            failures.append(outcome)
            await self._close_candidates((candidate,), LegCloseMode.CLOSE)
        return None, None, failures

    async def _run_candidates(self, commit_winner: WinnerCommit) -> ForkResult:
        """Run media branches which do not include route-control waiters."""

        self.session.ensure_live(self.token)
        if not self.candidates:
            return ForkResult(self._reduce_failures([]))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.overall_timeout
        attempted: list[str] = []
        failures: list[DialOutcome] = []
        all_candidates = set(self.candidates)
        winner: DialCandidate | None = None
        winner_outcome: DialOutcome | None = None

        tiers = sorted({candidate.tier for candidate in self.candidates})
        try:
            for tier in tiers:
                tier_candidates = tuple(
                    candidate for candidate in self.candidates if candidate.tier == tier
                )
                attempted.extend(candidate.candidate_id for candidate in tier_candidates)
                strategy = self.tier_strategies.get(tier, self.strategy)
                if strategy is ForkStrategy.PARALLEL:
                    winner, control, tier_failures = await self._dial_parallel_tier(
                        tier_candidates,
                        deadline,
                    )
                else:
                    winner, control, tier_failures = await self._dial_sequential_tier(
                        tier_candidates,
                        deadline,
                    )
                failures.extend(tier_failures)
                if control is not None and control.disposition in {
                    DialDisposition.CANCELLED,
                    DialDisposition.SOURCE_CANCELLED,
                    DialDisposition.REROUTE,
                }:
                    await self._close_candidates(
                        all_candidates,
                        LegCloseMode.CANCEL_OR_BYE,
                    )
                    return ForkResult(control, attempted=tuple(attempted))
                if winner is not None:
                    winner_outcome = control
                    break

            if winner is None or winner_outcome is None:
                await self._close_candidates(all_candidates, LegCloseMode.CLOSE)
                return ForkResult(
                    self._reduce_failures(failures),
                    attempted=tuple(attempted),
                )

            losers = all_candidates - {winner}
            cleanup = asyncio.create_task(
                self._close_candidates(losers, LegCloseMode.CANCEL_OR_BYE),
                name="voip-dial-fork-loser-barrier",
            )
            source_terminated = await self._wait_for_cleanup_or_termination(cleanup)
            if source_terminated or not self.session.owns(self.token):
                await self._close_candidates((winner,), LegCloseMode.BYE)
                return ForkResult(
                    DialOutcome(DialDisposition.CANCELLED, 487, "Request Terminated"),
                    attempted=tuple(attempted),
                )
            if not commit_winner(winner, winner_outcome):
                await self._close_candidates((winner,), LegCloseMode.BYE)
                return ForkResult(
                    DialOutcome(DialDisposition.CANCELLED, 487, "Request Terminated"),
                    attempted=tuple(attempted),
                )
            return ForkResult(
                winner_outcome,
                winner=winner,
                attempted=tuple(attempted),
            )
        except asyncio.CancelledError:
            await self._close_candidates(all_candidates, LegCloseMode.CANCEL_OR_BYE)
            raise

    async def run(self, commit_winner: WinnerCommit) -> ForkResult:
        """Run branches and route-control waiters under one arbitration point."""

        controls = tuple(candidate for candidate in self.candidates if candidate.control)
        branches = tuple(candidate for candidate in self.candidates if not candidate.control)
        if not controls:
            return await self._run_candidates(commit_winner)
        if not branches:
            return await self._run_candidates(commit_winner)

        branch_controller = DialForkController(
            self.session,
            branches,
            strategy=self.strategy,
            tier_strategies=self.tier_strategies,
            overall_timeout=self.overall_timeout,
            step_timeout=self.step_timeout,
        )
        branch_task = asyncio.create_task(
            branch_controller._run_candidates(
                lambda _candidate, _outcome: True
            ),
            name="voip-dial-fork-branches",
        )
        control_tasks = {
            asyncio.create_task(
                candidate.dial(),
                name=f"voip-dial-fork-control-{candidate.candidate_id}",
            ): candidate
            for candidate in controls
        }
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.overall_timeout
        attempted = tuple(
            candidate.candidate_id for candidate in self.candidates
        )
        pending_controls = set(control_tasks)

        async def _stop_branches() -> None:
            if branch_task.done():
                branch_result = branch_task.result()
                if branch_result.winner is not None:
                    await branch_controller._close_candidates(
                        (branch_result.winner,),
                        LegCloseMode.BYE,
                    )
                return
            branch_task.cancel()
            await asyncio.gather(branch_task, return_exceptions=True)

        try:
            while pending_controls and not branch_task.done():
                timeout = max(0.0, deadline - loop.time())
                if timeout <= 0:
                    break
                done, pending = await asyncio.wait(
                    {*pending_controls, branch_task},
                    timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                pending_controls = {
                    task for task in pending if task is not branch_task
                }
                completed_controls = [
                    task for task in control_tasks if task in done
                ]
                control_results: list[
                    tuple[DialCandidate, DialOutcome]
                ] = []
                for task in completed_controls:
                    try:
                        control_results.append(
                            (control_tasks[task], task.result())
                        )
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        _LOGGER.debug("PBX route-control candidate failed", exc_info=True)
                control = next(
                    (
                        (candidate, outcome)
                        for disposition in (
                            DialDisposition.SOURCE_CANCELLED,
                            DialDisposition.REROUTE,
                        )
                        for candidate, outcome in control_results
                        if outcome.disposition is disposition
                    ),
                    None,
                )
                if control is not None:
                    _control_candidate, control_outcome = control
                    await _stop_branches()
                    await self._close_candidates(
                        controls,
                        LegCloseMode.CLOSE,
                    )
                    return ForkResult(
                        control_outcome,
                        attempted=attempted,
                    )
                answered_control = next(
                    (
                        (candidate, outcome)
                        for candidate, outcome in control_results
                        if outcome.answered
                    ),
                    None,
                )
                if answered_control is not None:
                    winner, winner_outcome = answered_control
                    await _stop_branches()
                    await self._close_candidates(
                        (
                            candidate
                            for candidate in controls
                            if candidate is not winner
                        ),
                        LegCloseMode.CLOSE,
                    )
                    if not self.session.owns(self.token) or not commit_winner(
                        winner,
                        winner_outcome,
                    ):
                        await self._close_candidates(
                            (winner,),
                            LegCloseMode.CLOSE,
                        )
                        return ForkResult(
                            DialOutcome(
                                DialDisposition.CANCELLED,
                                487,
                                "Request Terminated",
                            ),
                            attempted=attempted,
                        )
                    return ForkResult(
                        winner_outcome,
                        winner=winner,
                        attempted=attempted,
                    )
            result = await branch_task
            await self._close_candidates(controls, LegCloseMode.CLOSE)
            if result.winner is not None and not commit_winner(
                result.winner,
                result.outcome,
            ):
                await branch_controller._close_candidates(
                    (result.winner,),
                    LegCloseMode.BYE,
                )
                return ForkResult(
                    DialOutcome(
                        DialDisposition.CANCELLED,
                        487,
                        "Request Terminated",
                    ),
                    attempted=result.attempted,
                )
            return result
        except asyncio.CancelledError:
            branch_task.cancel()
            for task in pending_controls:
                task.cancel()
            await asyncio.gather(
                branch_task,
                *pending_controls,
                return_exceptions=True,
            )
            await self._close_candidates(
                controls,
                LegCloseMode.CANCEL_OR_BYE,
            )
            raise
        finally:
            for task in pending_controls:
                if not task.done():
                    task.cancel()
            if pending_controls:
                await asyncio.gather(*pending_controls, return_exceptions=True)
