from dataclasses import dataclass
from enum import Enum


class CapabilityType(Enum):
    BOOLEAN = "boolean"
    INTEGER = "integer"
    DECIMAL = "decimal"
    MONEY = "money"


class CapabilityAlreadyRegistered(ValueError):
    pass


class UnknownCapability(KeyError):
    pass


@dataclass(frozen=True, slots=True)
class Capability:
    name: str
    value_type: CapabilityType
    unit: str = ""


class CapabilityRegistry:
    def __init__(self) -> None:
        self._capabilities: dict[str, Capability] = {}

    def register(
        self,
        name: str,
        value_type: CapabilityType,
        unit: str = "",
    ) -> Capability:
        if not name or name != name.strip():
            raise ValueError("Capability name must be a non-empty identifier.")

        capability = Capability(name=name, value_type=value_type, unit=unit)
        existing = self._capabilities.get(name)
        if existing is None:
            self._capabilities[name] = capability
            return capability
        if existing != capability:
            raise CapabilityAlreadyRegistered(
                f"Capability {name!r} is already registered as "
                f"{existing.value_type.value}"
                + (f" ({existing.unit})" if existing.unit else "")
                + "."
            )
        return existing

    def get(self, name: str) -> Capability:
        try:
            return self._capabilities[name]
        except KeyError as exc:
            raise UnknownCapability(name) from exc

    def is_registered(self, name: str) -> bool:
        return name in self._capabilities

    def all(self) -> tuple[Capability, ...]:
        return tuple(self._capabilities[name] for name in sorted(self._capabilities))


registry = CapabilityRegistry()

# Issue #6 catalogue plus the remaining initial capabilities from epic #1.
BUILTIN_CAPABILITIES: tuple[tuple[str, CapabilityType, str], ...] = (
    ("video.youtube", CapabilityType.BOOLEAN, ""),
    ("video.announcements", CapabilityType.BOOLEAN, ""),
    ("video.chat", CapabilityType.BOOLEAN, ""),
    ("video.jitsi", CapabilityType.BOOLEAN, ""),
    ("video.jitsi.concurrent_rooms", CapabilityType.INTEGER, "rooms"),
    ("video.jitsi.participant_limit", CapabilityType.INTEGER, "participants"),
    ("video.jitsi.room_hours", CapabilityType.DECIMAL, "hours"),
    ("video.loungemesh", CapabilityType.BOOLEAN, ""),
    ("video.loungemesh.spaces", CapabilityType.INTEGER, "spaces"),
    ("email.bulk.monthly", CapabilityType.INTEGER, "emails"),
    ("organizer.full_admins", CapabilityType.INTEGER, "seats"),
    ("api.read", CapabilityType.BOOLEAN, ""),
    ("api.write", CapabilityType.BOOLEAN, ""),
    ("api.webhooks", CapabilityType.BOOLEAN, ""),
    ("commerce.platform_fee_percent", CapabilityType.DECIMAL, "percent"),
    ("registration.free_allowance_per_event", CapabilityType.INTEGER, "registrations"),
    ("registration.free_overage_price", CapabilityType.MONEY, ""),
    ("support.priority", CapabilityType.BOOLEAN, ""),
)


def register_capability(
    name: str,
    value_type: CapabilityType,
    unit: str = "",
    *,
    target: CapabilityRegistry | None = None,
) -> Capability:
    """Register a capability. Other plugins should call this from AppConfig.ready()."""
    return (target or registry).register(name, value_type, unit=unit)


def get_capability(
    name: str, *, target: CapabilityRegistry | None = None
) -> Capability:
    return (target or registry).get(name)


def get_capabilities(
    *, target: CapabilityRegistry | None = None
) -> tuple[Capability, ...]:
    return (target or registry).all()


def is_registered(name: str, *, target: CapabilityRegistry | None = None) -> bool:
    return (target or registry).is_registered(name)


def register_builtin_capabilities(
    target: CapabilityRegistry | None = None,
) -> None:
    destination = target or registry
    for name, value_type, unit in BUILTIN_CAPABILITIES:
        destination.register(name, value_type, unit=unit)
