from __future__ import annotations

from typing import Any

from dataclasses import dataclass, field
from django.utils.translation import gettext_lazy as _
from enum import Enum


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

    def register_all(
        self, capabilities: list[Capability], override: bool = False
    ) -> None:
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
        return [
            (cap.name, f"{cap.name} ({cap.label})")
            for cap in sorted(self._capabilities.values(), key=lambda c: c.name)
        ]

    def grouped_choices(self) -> list[tuple[str, list[tuple[str, str]]]]:
        """
        Returns choices grouped by category for Django select optgroups.
        Format: [('Category', [(value, label), ...]), ...]
        """
        grouped = self.by_category()
        result = []
        for category in sorted(grouped.keys()):
            items = [
                (cap.name, f"{cap.label} ({cap.name})")
                for cap in sorted(grouped[category], key=lambda c: str(c.label))
            ]
            result.append((category, items))
        return result

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
        description=_(
            "Yes/no access to YouTube streaming rooms and stream schedules. "
            "YouTube rooms are not metered and do not consume the Jitsi room allowance."
        ),
        value_type=CapabilityValueType.BOOLEAN,
        category="Video",
        default_value=True,
        metadata={"audience": "organizer"},
    ),
    Capability(
        name="video.jitsi",
        label=_("Jitsi Video Rooms"),
        description=_(
            "Access to Jitsi video rooms."
        ),
        value_type=CapabilityValueType.BOOLEAN,
        category="Video",
        default_value=True,
        metadata={"audience": "organizer"},
    ),
    Capability(
        name="video.jitsi.concurrent_rooms",
        label=_("Simultaneous Video Rooms (Beta)"),
        description=_(
            "Planned limit on simultaneously running Jitsi rooms, excluding YouTube streams. "
            "Running rooms are not yet counted or limited by this entitlement."
        ),
        value_type=CapabilityValueType.INTEGER,
        category="Video",
        unit="rooms",
        default_value=1,
        metadata={"audience": "organizer"},
    ),
    Capability(
        name="video.loungemesh",
        label=_("Spatial Networking Lounge"),
        description=_(
            "Access to the spatial networking lounge."
        ),
        value_type=CapabilityValueType.BOOLEAN,
        category="Video",
        default_value=False,
        metadata={"audience": "organizer"},
    ),
    # Email Communications
    Capability(
        name="email.bulk.monthly",
        label=_("Monthly Bulk Emails"),
        description=_(
            "Number of announcement emails you can send to your attendees each month"
        ),
        value_type=CapabilityValueType.INTEGER,
        category="Email",
        unit="emails",
        default_value=1000,
        metadata={"audience": "organizer"},
    ),
    # Team & Organization
    Capability(
        name="organizer.full_admins",
        label=_("Team Admin Seats"),
        description=_(
            "Maximum number of team members who can be granted full administrator access"
        ),
        value_type=CapabilityValueType.INTEGER,
        category="Organisation",
        unit="admins",
        default_value=2,
        metadata={"audience": "organizer"},
    ),
    # Developer & API
    Capability(
        name="api.read",
        label=_("API Read Access"),
        description=_(
            "Plan-based access to REST API reads using API tokens, devices and OAuth applications. "
            "Normal API authentication and permissions still apply."
        ),
        value_type=CapabilityValueType.BOOLEAN,
        category="Developer & API",
        default_value=True,
        metadata={"audience": "developer"},
    ),
    Capability(
        name="api.write",
        label=_("API Write Access"),
        description=_(
            "Plan-based access to REST API writes using API tokens, devices and OAuth applications. "
            "Normal API authentication and permissions still apply."
        ),
        value_type=CapabilityValueType.BOOLEAN,
        category="Developer & API",
        default_value=False,
        metadata={"audience": "developer"},
    ),
    Capability(
        name="api.webhooks",
        label=_("Webhook Delivery"),
        description=_(
            "Plan-based control of webhook delivery. "
            "Webhooks otherwise continue to use their existing configuration and permissions."
        ),
        value_type=CapabilityValueType.BOOLEAN,
        category="Developer & API",
        default_value=False,
        metadata={"audience": "developer"},
    ),
    # Commerce & Fees
    Capability(
        name="commerce.platform_fee_percent",
        label=_("Platform Fee"),
        description=_(
            "Percentage fee applied by the platform on each paid ticket transaction"
        ),
        value_type=CapabilityValueType.DECIMAL,
        category="Commerce",
        unit="%",
        default_value=0.0,
        metadata={"audience": "organizer"},
    ),
    # Registration & Ticketing
    Capability(
        name="registration.free_allowance_per_event",
        label=_("Free Ticket Allowance (Beta)"),
        description=_(
            "Free registrations included for each event within a billing period. Billing is implemented, "
            "but the allowance is not tracked over the event's lifetime and does not include add-on allowances. "
            "This is not a checkout limit."
        ),
        value_type=CapabilityValueType.INTEGER,
        category="Registration",
        unit="registrations",
        default_value=100,
        metadata={"audience": "organizer"},
    ),
    Capability(
        name="registration.free_overage_price",
        label=_("Free Ticket Overage Price (Beta)"),
        description=_(
            "Price per free registration above the tier allowance within a billing period. "
            "Billing is implemented, but follows the Beta free-ticket allowance calculation; "
            "add-on allowances are not included."
        ),
        value_type=CapabilityValueType.MONEY,
        category="Registration",
        unit="per registration",
        default_value=0.0,
        metadata={"audience": "organizer"},
    ),
    # Customer Support
    Capability(
        name="support.priority",
        label=_("Priority Support (Beta)"),
        description=_(
            "Planned priority-support benefit. This entitlement does not yet automate support routing "
            "or enforce response-time guarantees."
        ),
        value_type=CapabilityValueType.BOOLEAN,
        category="Support",
        default_value=False,
        metadata={"audience": "organizer"},
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


def get_grouped_capability_choices() -> list[tuple[str, list[tuple[str, str]]]]:
    return default_registry.grouped_choices()


def get_capabilities_dict() -> dict[str, dict[str, Any]]:
    return default_registry.as_dict()
