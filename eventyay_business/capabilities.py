from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from django.utils.translation import gettext_lazy as _


class CapabilityValueType(str, Enum):
    BOOLEAN = "boolean"
    INTEGER = "integer"
    DECIMAL = "decimal"
    MONEY = "money"
    STRING = "string"


@dataclass
class Capability:
    """
    Represents a platform capability or entitlement quota that can be assigned to tiers.
    """

    name: str
    label: str
    description: str = ""
    value_type: CapabilityValueType = CapabilityValueType.BOOLEAN
    category: str = "General"
    unit: str = ""
    default_value: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """
        Serialize capability into a dictionary for API/signal responses.
        """
        return {
            "name": self.name,
            "label": str(self.label),
            "description": str(self.description),
            "value_type": self.value_type.value,
            "category": self.category,
            "unit": self.unit,
            "default_value": self.default_value,
            "metadata": dict(self.metadata),
        }


class CapabilityRegistry:
    """
    Registry that stores and provides capabilities across the Eventyay platform.
    Designed for dynamic extension by first-party and third-party plugins.
    """

    def __init__(self) -> None:
        self._capabilities: dict[str, Capability] = {}

    def register(self, capability: Capability, override: bool = False) -> None:
        """
        Register a capability. Raises ValueError if already registered unless override=True.
        """
        if not isinstance(capability, Capability):
            raise TypeError(f"Expected Capability instance, got {type(capability)}")
        if not capability.name:
            raise ValueError("Capability name cannot be empty")
        if capability.name in self._capabilities and not override:
            raise ValueError(f"Capability '{capability.name}' is already registered.")
        self._capabilities[capability.name] = capability

    def register_all(self, capabilities: list[Capability], override: bool = False) -> None:
        for cap in capabilities:
            self.register(cap, override=override)

    def unregister(self, name: str) -> None:
        self._capabilities.pop(name, None)

    def get(self, name: str) -> Capability | None:
        return self._capabilities.get(name)

    def all(self) -> list[Capability]:
        return list(self._capabilities.values())

    def by_category(self) -> dict[str, list[Capability]]:
        grouped: dict[str, list[Capability]] = {}
        for cap in self._capabilities.values():
            grouped.setdefault(cap.category, []).append(cap)
        return grouped

    def choices(self) -> list[tuple[str, str]]:
        """
        Returns choices suitable for Django form fields grouped by category or sorted by name.
        """
        return [(cap.name, f"{cap.name} ({cap.label})") for cap in sorted(self._capabilities.values(), key=lambda c: c.name)]

    def as_dict(self) -> dict[str, dict[str, Any]]:
        """
        Serialize all registered capabilities into a dict keyed by capability name.
        """
        return {name: cap.to_dict() for name, cap in self._capabilities.items()}


# Standard platform capability catalogue
STANDARD_CAPABILITIES = [
    # Video & Streaming
    Capability(
        name="video.youtube",
        label=_("YouTube Streaming"),
        description=_("Enable YouTube live streaming integration for events"),
        value_type=CapabilityValueType.BOOLEAN,
        category="Video",
        default_value=True,
    ),
    Capability(
        name="video.jitsi",
        label=_("Jitsi Video"),
        description=_("Enable integrated Jitsi video meeting rooms"),
        value_type=CapabilityValueType.BOOLEAN,
        category="Video",
        default_value=True,
    ),
    Capability(
        name="video.jitsi.concurrent_rooms",
        label=_("Jitsi Concurrent Rooms"),
        description=_("Maximum number of concurrent Jitsi video breakout rooms"),
        value_type=CapabilityValueType.INTEGER,
        category="Video",
        unit="rooms",
        default_value=1,
    ),
    Capability(
        name="video.loungemesh",
        label=_("Loungemesh Networking"),
        description=_("Enable Loungemesh interactive spatial networking"),
        value_type=CapabilityValueType.BOOLEAN,
        category="Video",
        default_value=False,
    ),
    # Email Communications
    Capability(
        name="email.bulk.monthly",
        label=_("Monthly Bulk Emails"),
        description=_("Monthly allowance of organizer-initiated bulk announcement emails"),
        value_type=CapabilityValueType.INTEGER,
        category="Email",
        unit="emails",
        default_value=1000,
    ),
    # Team & Organization
    Capability(
        name="organizer.full_admins",
        label=_("Full Administrator Seats"),
        description=_("Maximum number of full team administrators allowed for the organizer"),
        value_type=CapabilityValueType.INTEGER,
        category="Organization",
        unit="admins",
        default_value=2,
    ),
    # Developer & API
    Capability(
        name="api.read",
        label=_("API Read Access"),
        description=_("Access to read data via REST APIs"),
        value_type=CapabilityValueType.BOOLEAN,
        category="API",
        default_value=True,
    ),
    Capability(
        name="api.write",
        label=_("API Write Access"),
        description=_("Access to create and modify data via REST APIs"),
        value_type=CapabilityValueType.BOOLEAN,
        category="API",
        default_value=False,
    ),
    Capability(
        name="api.webhooks",
        label=_("Webhooks Delivery"),
        description=_("Real-time webhook notifications for order and ticket events"),
        value_type=CapabilityValueType.BOOLEAN,
        category="API",
        default_value=False,
    ),
    # Commerce & Fees
    Capability(
        name="commerce.platform_fee_percent",
        label=_("Platform Fee Percentage"),
        description=_("Percentage platform fee applied to paid ticket transactions"),
        value_type=CapabilityValueType.DECIMAL,
        category="Commerce",
        unit="%",
        default_value=0.0,
    ),
    # Registration & Ticketing
    Capability(
        name="registration.free_allowance_per_event",
        label=_("Free Registrations Allowance"),
        description=_("Included number of free ticket registrations per event"),
        value_type=CapabilityValueType.INTEGER,
        category="Registration",
        unit="registrations",
        default_value=100,
    ),
    Capability(
        name="registration.free_overage_price",
        label=_("Free Registration Overage Price"),
        description=_("Price charged per free registration exceeding the included allowance"),
        value_type=CapabilityValueType.MONEY,
        category="Registration",
        unit="per registration",
        default_value=0.0,
    ),
    # Customer Support
    Capability(
        name="support.priority",
        label=_("Priority Support"),
        description=_("Access to dedicated priority support and expedited SLA response"),
        value_type=CapabilityValueType.BOOLEAN,
        category="Support",
        default_value=False,
    ),
]


# Global singleton registry
default_registry = CapabilityRegistry()
default_registry.register_all(STANDARD_CAPABILITIES)


# Global helper functions for external plugins and application code
def register_capability(capability: Capability, override: bool = False) -> None:
    """
    Hook allowing any Eventyay plugin or app to register capabilities dynamically.
    """
    default_registry.register(capability, override=override)


def get_capability(name: str) -> Capability | None:
    return default_registry.get(name)


def get_all_capabilities() -> list[Capability]:
    return default_registry.all()


def get_capability_choices() -> list[tuple[str, str]]:
    return default_registry.choices()
