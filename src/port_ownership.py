"""Application-wide serial-port ownership coordination.

Every workbench must acquire a lease before opening or probing a serial port.
The token stored by a lease prevents an old callback from releasing a newer
owner's claim after a reconnect or process retry.
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from uuid import uuid4


def _canonical_port(port: str) -> str:
    return str(port or "").strip().casefold()


@dataclass(frozen=True)
class PortClaim:
    port: str
    owner: str
    purpose: str
    token: str


class PortLease:
    def __init__(self, registry: "PortOwnershipRegistry", claim: PortClaim) -> None:
        self._registry = registry
        self.claim = claim
        self._released = False

    @property
    def port(self) -> str:
        return self.claim.port

    @property
    def released(self) -> bool:
        return self._released

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._registry._release(self.claim)

    def __enter__(self) -> "PortLease":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.release()


class PortOwnershipRegistry:
    """Thread-safe, non-blocking exclusive leases keyed by serial-port name."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._claims: dict[str, PortClaim] = {}

    def acquire(self, port: str, owner: str, purpose: str) -> PortLease | None:
        key = _canonical_port(port)
        if not key:
            return None
        with self._lock:
            if key in self._claims:
                return None
            claim = PortClaim(
                port=str(port).strip(),
                owner=str(owner).strip() or "unknown",
                purpose=str(purpose).strip() or "serial operation",
                token=uuid4().hex,
            )
            self._claims[key] = claim
        return PortLease(self, claim)

    def claim_for(self, port: str) -> PortClaim | None:
        with self._lock:
            return self._claims.get(_canonical_port(port))

    def is_available(self, port: str) -> bool:
        return self.claim_for(port) is None

    def _release(self, claim: PortClaim) -> None:
        key = _canonical_port(claim.port)
        with self._lock:
            current = self._claims.get(key)
            if current is not None and current.token == claim.token:
                self._claims.pop(key, None)


GLOBAL_PORT_OWNERSHIP = PortOwnershipRegistry()
