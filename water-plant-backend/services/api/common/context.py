"""Request context: business, branch, actor, role and device resolved from
authentication -- never from a client-selected tenant identifier."""

from dataclasses import dataclass, field

from .errors import Forbidden


@dataclass(frozen=True)
class RequestContext:
    business: object
    branch: object | None
    user: object | None
    role: str | None
    capabilities: frozenset = field(default_factory=frozenset)
    device: object | None = None
    session_id: str | None = None
    request_id: str = "-"

    @property
    def business_id(self):
        return self.business.id

    @property
    def user_id(self):
        return self.user.id if self.user else None

    @property
    def client_id(self) -> str:
        if self.device is not None:
            return f"device:{self.device.client_identifier}"
        if self.user is not None:
            return f"user:{self.user.id}"
        return "system"

    def has(self, capability: str) -> bool:
        return "*" in self.capabilities or capability in self.capabilities

    def require(self, *capabilities: str) -> None:
        """Require at least one of the capabilities."""
        if not any(self.has(c) for c in capabilities):
            raise Forbidden()

    @property
    def is_owner(self) -> bool:
        return self.role == "OWNER"

    @property
    def is_customer(self) -> bool:
        return self.role == "CUSTOMER"
