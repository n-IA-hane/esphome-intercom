"""Parking slots are reservations owned by existing call sessions."""

import asyncio
from dataclasses import dataclass, field
from collections.abc import Awaitable, Callable

from .endpoint_session import CleanupStage, EndpointCallSession, TerminationIntent


class ParkingError(ValueError):
    pass


@dataclass(slots=True)
class ParkedCall:
    slot: str
    session: EndpointCallSession
    parker: str = ""
    return_destination: str = ""
    deadline: float | None = None
    timer: asyncio.Task | None = None
    claim: "PickupClaim | None" = None
    changed: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(slots=True)
class PickupClaim:
    lot: "ParkingLot"
    parked: ParkedCall
    call_id: str
    browser_endpoint_id: str = ""

    @property
    def current(self):
        return (self.lot.calls.get(self.parked.slot) is self.parked
                and self.parked.claim is self and self.parked.session.live)

    def authorizes_browser_answer(self, session, endpoint_id):
        return (self.current and self.parked.session is session
                and bool(self.browser_endpoint_id)
                and self.browser_endpoint_id == endpoint_id)

    def rollback(self):
        if self.current:
            self.parked.claim = None
            self.parked.changed.set()
            self.lot.changed()

    def commit(self):
        if not self.current:
            raise ParkingError("The parked call has ended or another pickup won")
        self.lot.release(self.parked)


class ParkingLot:
    def __init__(self, slots: tuple[str, ...], changed: Callable[[], None] | None = None):
        if not slots or any(not value or value.strip() != value for value in slots) or len(set(slots)) != len(slots):
            raise ParkingError("Choose unique, non-empty parking slots")
        self.slots = tuple(slots)
        self.calls: dict[str, ParkedCall] = {}
        self._changed = changed

    def changed(self):
        if self._changed is not None:
            self._changed()

    def reserve(self, session, *, slot="", parker="", return_destination="") -> ParkedCall:
        session.ensure_live()
        current = next((item for item in self.calls.values() if item.session is session), None)
        if current is not None:
            if slot and slot != current.slot:
                raise ParkingError(f"This call already occupies slot {current.slot}")
            return current
        if slot and slot not in self.slots:
            raise ParkingError("Unknown parking slot")
        selected = slot or next((value for value in self.slots if value not in self.calls), "")
        if not selected or selected in self.calls:
            raise ParkingError("The parking slot is occupied" if slot else "The parking lot is full")
        parked = ParkedCall(selected, session, parker, return_destination)
        async def cleanup(_reason):
            self.release(parked)
        session.add_resource(f"parking_slot:{selected}", parked, cleanup, stage=CleanupStage.RESERVATION)
        self.calls[selected] = parked
        self.changed()
        return parked

    def claim(self, slot: str, call_id: str, *, browser_endpoint_id: str = "") -> PickupClaim:
        parked = self.calls.get(slot)
        if parked is None or not parked.session.live:
            raise ParkingError("No call is parked in that slot")
        if not call_id:
            raise ParkingError("Pickup needs its own call identity")
        if parked.claim is not None:
            if parked.claim.call_id == call_id and parked.claim.browser_endpoint_id == browser_endpoint_id:
                return parked.claim
            raise ParkingError("Another phone is already picking up this call")
        claim = PickupClaim(self, parked, call_id, browser_endpoint_id)
        parked.claim = claim
        parked.changed.set()
        self.changed()
        return claim

    def release(self, parked: ParkedCall):
        if self.calls.get(parked.slot) is not parked:
            return
        self.calls.pop(parked.slot)
        parked.changed.set()
        if parked.session.live:
            parked.session.release_resource(f"parking_slot:{parked.slot}", value=parked)
        timer = parked.timer
        if parked.session.live and timer is not None and timer is not asyncio.current_task():
            timer.cancel()
        self.changed()

    def arm_return(self, parked: ParkedCall, timeout: float, callback: Callable[[PickupClaim], Awaitable[None]]):
        if self.calls.get(parked.slot) is not parked or not parked.session.live:
            raise ParkingError("The parked call has ended")
        if parked.timer is not None:
            return
        if timeout <= 0:
            raise ParkingError("Parking timeout must be positive")
        parked.deadline = asyncio.get_running_loop().time() + timeout
        async def expire():
            await asyncio.sleep(max(0, parked.deadline - asyncio.get_running_loop().time()))
            while self.calls.get(parked.slot) is parked and parked.session.live:
                parked.changed.clear()
                if parked.claim is None:
                    break
                await parked.changed.wait()
            else:
                return
            claim = self.claim(parked.slot, f"timeout:{parked.session.call_id}:{parked.session.generation}")
            try:
                await callback(claim)
            except asyncio.CancelledError:
                raise
            except Exception:
                await parked.session.terminate(TerminationIntent("parking_return_failed"))
                raise
        parked.timer = parked.session.create_task(expire(), name=f"voip-parking-return-{parked.slot}-{id(parked):x}")

    async def close(self):
        await asyncio.gather(*(item.session.terminate(TerminationIntent("parking_stopped"))
                               for item in tuple(self.calls.values())))
