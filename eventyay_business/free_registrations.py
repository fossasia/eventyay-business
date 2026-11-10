"""
Free-registration allowance accounting shared by invoicing and billing estimates.

The allowance is counted over an event's lifetime: registrations from earlier
billing periods consume it, so only the part of a period's usage that exceeds
what is still left is billable. Add-ons that grant
``registration.free_allowance_per_event`` raise the allowance for the periods
they overlap.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from django.db.models import Q, Sum

from .models import AddonStatus, EventAddon, OrganizerAddon, UsageRecord

FREE_REGISTRATION_ALLOWANCE = "registration.free_allowance_per_event"
FREE_REGISTRATION_OVERAGE_PRICE = "registration.free_overage_price"

ZERO = Decimal("0")


@dataclass(frozen=True)
class FreeRegistrationUsage:
    """Allowance position of one event (or the organizer bucket) for a period."""

    period_quantity: Decimal
    previous_quantity: Decimal
    tier_allowance: int
    addon_allowance: int

    @property
    def allowance(self) -> int:
        return self.tier_allowance + self.addon_allowance

    @property
    def remaining_allowance(self) -> Decimal:
        """Allowance still unused when the period starts."""
        return max(Decimal(self.allowance) - self.previous_quantity, ZERO)

    @property
    def overage_quantity(self) -> Decimal:
        return max(self.period_quantity - self.remaining_allowance, ZERO)


def tier_free_registration_terms(tier_version) -> tuple[int, Decimal]:
    """Return ``(allowance, overage price)`` configured on a tier version."""
    allowance = 0
    price = Decimal("0.00")
    if not tier_version:
        return allowance, price

    values = dict(
        tier_version.entitlements.filter(
            capability__in=[
                FREE_REGISTRATION_ALLOWANCE,
                FREE_REGISTRATION_OVERAGE_PRICE,
            ]
        ).values_list("capability", "value")
    )
    if values.get(FREE_REGISTRATION_ALLOWANCE):
        try:
            allowance = max(int(values[FREE_REGISTRATION_ALLOWANCE]), 0)
        except (ValueError, TypeError):
            allowance = 0
    if values.get(FREE_REGISTRATION_OVERAGE_PRICE):
        try:
            price = Decimal(str(values[FREE_REGISTRATION_OVERAGE_PRICE]))
        except InvalidOperation:
            price = Decimal("0.00")
    return allowance, price


def _addon_period_filter(period_start: datetime, period_end: datetime) -> Q:
    """Granted add-ons whose service overlaps the billing period."""
    return (
        Q(addon__active=True, starts_at__lte=period_end)
        & (
            Q(capability=FREE_REGISTRATION_ALLOWANCE)
            | Q(capability="", addon__capability=FREE_REGISTRATION_ALLOWANCE)
        )
        & ~Q(status=AddonStatus.PENDING)
        & (Q(ends_at__isnull=True) | Q(ends_at__gte=period_start))
        & (Q(cancel_at__isnull=True) | Q(cancel_at__gte=period_start))
        & (Q(canceled_at__isnull=True) | Q(canceled_at__gte=period_start))
    )


def _assignment_allowance(assignment) -> int:
    value = assignment.get_typed_value()
    if value is None:
        value = assignment.addon.quantity or 1
    return assignment.quantity * int(value)


def addon_free_registration_allowances(
    organizer, period_start: datetime, period_end: datetime
) -> tuple[int, dict[int, int]]:
    """
    Return the add-on allowance for a billing period.

    The first item applies to every event of the organizer, the second maps
    event primary keys to the extra allowance of event-level add-ons.
    """
    period_filter = _addon_period_filter(period_start, period_end)
    organizer_allowance = sum(
        _assignment_allowance(assignment)
        for assignment in OrganizerAddon.objects.filter(
            period_filter, organizer=organizer
        ).select_related("addon")
    )
    event_allowances: dict[int, int] = {}
    for assignment in EventAddon.objects.filter(
        period_filter, event__organizer=organizer
    ).select_related("addon"):
        event_allowances[assignment.event_id] = event_allowances.get(
            assignment.event_id, 0
        ) + _assignment_allowance(assignment)
    return organizer_allowance, event_allowances


def previous_free_registrations(
    organizer, period_start: datetime
) -> dict[int | None, Decimal]:
    """Free registrations recorded per event before the billing period."""
    rows = (
        UsageRecord.objects.filter(
            organizer=organizer,
            capability=FREE_REGISTRATION_ALLOWANCE,
            occurred_at__lt=period_start,
        )
        .values("event_id")
        .annotate(total=Sum("quantity"))
    )
    return {row["event_id"]: row["total"] or ZERO for row in rows}


def free_registration_usage_by_event(
    organizer,
    tier_version,
    period_quantities: dict[int | None, Decimal],
    period_start: datetime,
    period_end: datetime,
) -> dict[int | None, FreeRegistrationUsage]:
    """
    Resolve the allowance position for each event with usage in the period.

    ``period_quantities`` maps event primary keys (``None`` for usage without
    an event) to the free registrations recorded within the period.
    """
    if not period_quantities:
        return {}
    tier_allowance, _price = tier_free_registration_terms(tier_version)
    organizer_allowance, event_allowances = addon_free_registration_allowances(
        organizer, period_start, period_end
    )
    previous = previous_free_registrations(organizer, period_start)
    return {
        event_id: FreeRegistrationUsage(
            period_quantity=quantity,
            previous_quantity=previous.get(event_id, ZERO),
            tier_allowance=tier_allowance,
            addon_allowance=organizer_allowance + event_allowances.get(event_id, 0),
        )
        for event_id, quantity in period_quantities.items()
    }
