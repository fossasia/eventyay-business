from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from django.db.models import Prefetch, Q
from django.utils.timezone import is_aware, make_aware, now
from django.utils.translation import gettext as _
from eventyay.base.models.organizer import OrganizerBillingModel

from .models import (
    AddonPricingMode,
    AddonStatus,
    BillingInterval,
    EventAddon,
    OrganizerAddon,
    PlatformFeeLedgerEntry,
    PlatformFeeLedgerEntryType,
    Subscription,
    SubscriptionStatus,
    UsageRecord,
)

ZERO = Decimal("0.00")


def calendar_month_range(reference: datetime | None = None):
    current = reference or now()
    start = datetime(current.year, current.month, 1)
    if current.month == 12:
        next_start = datetime(current.year + 1, 1, 1)
    else:
        next_start = datetime(current.year, current.month + 1, 1)
    if is_aware(current):
        start = make_aware(start, current.tzinfo)
        next_start = make_aware(next_start, current.tzinfo)
    return start, next_start - timedelta(microseconds=1), next_start


def parse_month(value: str | None, reference: datetime | None = None):
    if value:
        try:
            parsed = datetime.strptime(value, "%Y-%m")
        except ValueError:
            pass
        else:
            current = reference or now()
            if is_aware(current):
                parsed = make_aware(parsed, current.tzinfo)
            return calendar_month_range(parsed)
    return calendar_month_range(reference)


def _active_subscription(organizer, period_end):
    return (
        Subscription.objects.filter(
            organizer=organizer,
            status__in=[SubscriptionStatus.ACTIVE, SubscriptionStatus.PAST_DUE],
            starts_at__lte=period_end,
        )
        .filter(Q(ends_at__isnull=True) | Q(ends_at__gte=period_end))
        .select_related("tier_version__tier")
        .prefetch_related("tier_version__prices", "tier_version__entitlements")
        .order_by("-starts_at", "-id")
        .first()
    )


def _add_amount(totals, currency, category, amount):
    currency = currency or "EUR"
    totals[currency][category] += amount
    totals[currency]["total"] += amount


def _fixed_charge_estimate(organizer, period_start, period_end, totals):
    subscription = _active_subscription(organizer, period_end)
    plan = None
    if subscription:
        interval = subscription.billing_interval or BillingInterval.MONTHLY
        currency = subscription.currency or "EUR"
        is_billable_period = interval == BillingInterval.MONTHLY
        if interval == BillingInterval.ANNUAL:
            for year in (
                period_start.year - 1,
                period_start.year,
                period_start.year + 1,
            ):
                try:
                    anniversary = subscription.starts_at.replace(year=year)
                except ValueError:
                    anniversary = subscription.starts_at.replace(year=year, day=28)
                if period_start <= anniversary <= period_end:
                    is_billable_period = True
                    break
        price = next(
            (
                item
                for item in subscription.tier_version.prices.all()
                if item.active
                and item.billing_interval == interval
                and item.currency == currency
            ),
            None,
        )
        if price and is_billable_period:
            plan = {
                "name": subscription.tier_version.tier.name,
                "interval": subscription.get_billing_interval_display(),
                "amount": price.amount,
                "currency": price.currency,
            }
            _add_amount(totals, price.currency, "plan", price.amount)

    recurring_filter = (
        Q(addon__pricing_mode=AddonPricingMode.RECURRING)
        & Q(status__in=[AddonStatus.ACTIVE, AddonStatus.PAST_DUE])
        & Q(starts_at__lte=period_end)
        & (Q(ends_at__isnull=True) | Q(ends_at__gte=period_start))
        & (Q(cancel_at__isnull=True) | Q(cancel_at__gte=period_start))
    )
    one_time_filter = (
        Q(addon__pricing_mode=AddonPricingMode.ONE_TIME)
        & Q(starts_at__gte=period_start)
        & Q(starts_at__lte=period_end)
        & ~Q(status=AddonStatus.CANCELED)
    )
    addon_filter = recurring_filter | one_time_filter
    assignments = list(
        OrganizerAddon.objects.filter(organizer=organizer)
        .filter(addon_filter)
        .select_related("addon")
    )
    assignments.extend(
        EventAddon.objects.filter(event__organizer=organizer)
        .filter(addon_filter)
        .select_related("addon", "event")
    )

    addons = []
    for assignment in assignments:
        price = assignment.price
        if price is None:
            price = assignment.addon.price
        currency = assignment.currency or assignment.addon.currency or "EUR"
        amount = (price * Decimal(assignment.quantity or 1)).quantize(Decimal("0.01"))
        addons.append(
            {
                "name": assignment.addon.name,
                "event": getattr(assignment, "event", None),
                "pricing_mode": assignment.addon.get_pricing_mode_display(),
                "quantity": assignment.quantity,
                "amount": amount,
                "currency": currency,
            }
        )
        _add_amount(totals, currency, "addons", amount)
    return subscription, plan, addons


def _usage_estimate(organizer, subscription, period_start, period_end, totals):
    records = (
        UsageRecord.objects.filter(
            organizer=organizer,
            occurred_at__gte=period_start,
            occurred_at__lte=period_end,
        )
        .exclude(capability="commerce.platform_fee_percent")
        .select_related("event")
    )
    grouped = defaultdict(lambda: {"quantity": ZERO, "records": []})
    for record in records:
        key = (record.capability, record.event_id, record.unit)
        grouped[key]["quantity"] += record.quantity
        grouped[key]["records"].append(record)

    entitlements = {}
    if subscription:
        entitlements = {
            entitlement.capability: entitlement
            for entitlement in subscription.tier_version.entitlements.all()
        }

    usage = []
    for (capability, _event_id, unit), data in grouped.items():
        entitlement = entitlements.get(capability)
        allowance = ZERO
        price = ZERO
        currency = ""
        if entitlement:
            try:
                allowance = Decimal(str(entitlement.value or "0"))
            except InvalidOperation:
                allowance = ZERO
            price = entitlement.overage_price or ZERO
            currency = entitlement.currency or ""
        if capability == "registration.free_allowance_per_event" and subscription:
            price_entitlement = entitlements.get("registration.free_overage_price")
            if price_entitlement:
                try:
                    price = Decimal(str(price_entitlement.value or "0"))
                except InvalidOperation:
                    price = ZERO
                currency = price_entitlement.currency or subscription.currency or "EUR"
        overage = max(data["quantity"] - allowance, ZERO)
        amount = (overage * price).quantize(Decimal("0.01"))
        event = data["records"][0].event
        usage.append(
            {
                "capability": capability,
                "event": event,
                "quantity": data["quantity"],
                "unit": unit,
                "allowance": allowance,
                "overage": overage,
                "amount": amount,
                "currency": currency,
            }
        )
        if amount and currency:
            _add_amount(totals, currency, "usage", amount)
    return usage


def platform_fee_rows(
    *,
    organizer=None,
    period_start=None,
    period_end=None,
    state="open",
    currency="",
    event_query="",
    include_carry_forward=False,
):
    adjustments = PlatformFeeLedgerEntry.objects.exclude(
        entry_type=PlatformFeeLedgerEntryType.CHARGE
    ).select_related("invoice", "refund")
    charges = (
        PlatformFeeLedgerEntry.objects.filter(
            entry_type=PlatformFeeLedgerEntryType.CHARGE
        )
        .select_related("organizer", "event", "order", "payment", "invoice")
        .prefetch_related(Prefetch("adjustments", queryset=adjustments))
    )
    if organizer:
        charges = charges.filter(organizer=organizer)
    if currency:
        charges = charges.filter(currency=currency.upper())
    if event_query:
        charges = charges.filter(
            Q(event__name__icontains=event_query)
            | Q(event__slug__icontains=event_query)
        )
    if period_end:
        if include_carry_forward and state == "open":
            charges = charges.filter(
                Q(invoice__isnull=True, occurred_at__lte=period_end)
                | Q(
                    adjustments__invoice__isnull=True,
                    adjustments__occurred_at__lte=period_end,
                )
            )
        elif period_start:
            charges = charges.filter(
                Q(occurred_at__range=(period_start, period_end))
                | Q(adjustments__occurred_at__range=(period_start, period_end))
            )
    charges = charges.distinct().order_by("-occurred_at", "-id")

    rows = []
    for charge in charges:
        components = [charge, *charge.adjustments.all()]
        if period_start and not include_carry_forward:
            components = [
                item
                for item in components
                if period_start <= item.occurred_at <= period_end
            ]
        elif period_end:
            components = [item for item in components if item.occurred_at <= period_end]
        if state == "open":
            selected = [item for item in components if item.invoice_id is None]
        elif state == "invoiced":
            selected = [item for item in components if item.invoice_id is not None]
        else:
            selected = components
        if not selected:
            continue

        selected_ids = {item.pk for item in selected}
        gross = charge.amount if charge.pk in selected_ids else ZERO
        calculated_fee = charge.calculated_fee if charge.pk in selected_ids else ZERO
        excluded_amount = calculated_fee - gross
        exclusion_reasons = []
        if excluded_amount:
            if charge.collected_at_checkout:
                exclusion_reasons.append(
                    _("Includes fees already collected at checkout.")
                )
            collection_path = charge.metadata.get("collection_path", "")
            if collection_path == "legacy_usage_record":
                exclusion_reasons.append(
                    _("Fees were recorded in the previous billing system.")
                )
            cutover_relation = charge.metadata.get("cutover_relation", "")
            if cutover_relation == "not_configured":
                exclusion_reasons.append(
                    _("Monthly billing was not activated for this payment.")
                )
            elif cutover_relation == "pre_cutover":
                exclusion_reasons.append(
                    _("Payment was made before monthly billing activation.")
                )
            if not exclusion_reasons:
                exclusion_reasons.append(
                    _("This amount was not recorded for monthly billing.")
                )
        adjustment = sum(
            (
                item.amount
                for item in selected
                if item.entry_type != PlatformFeeLedgerEntryType.CHARGE
            ),
            ZERO,
        )
        states = {
            "open" if item.invoice_id is None else "invoiced" for item in selected
        }
        rows.append(
            {
                "organizer": charge.organizer,
                "event": charge.event,
                "order_code": charge.order.code,
                "payment_reference": (
                    charge.payment.local_id if charge.payment else None
                ),
                "occurred_at": charge.occurred_at,
                "currency": charge.currency,
                "fee_base": charge.fee_base,
                "percentage": charge.percentage,
                "maximum_fee": charge.maximum_fee,
                "calculated_fee": calculated_fee,
                "excluded_amount": excluded_amount,
                "exclusion_reason": " ".join(exclusion_reasons),
                "not_billed_here": bool(
                    excluded_amount
                    and not gross
                    and len(selected) == 1
                    and charge.invoice_id is None
                ),
                "adjustment": adjustment,
                "net": gross + adjustment,
                "state": "mixed" if len(states) > 1 else states.pop(),
                "policy_source": charge.policy_source,
                "is_override": charge.is_override,
                "source_key": charge.source_key,
                "collection_path": charge.metadata.get("collection_path", ""),
                "cutover_relation": charge.metadata.get("cutover_relation", ""),
                "adjustment_count": sum(
                    item.entry_type != PlatformFeeLedgerEntryType.CHARGE
                    for item in selected
                ),
            }
        )
    return rows


def build_organizer_billing_overview(organizer, reference: datetime | None = None):
    period_start, period_end, issue_date = calendar_month_range(reference)
    totals = defaultdict(
        lambda: {
            "plan": ZERO,
            "addons": ZERO,
            "ticket_fees": ZERO,
            "usage": ZERO,
            "tax": ZERO,
            "total": ZERO,
        }
    )
    subscription, plan, addons = _fixed_charge_estimate(
        organizer, period_start, period_end, totals
    )
    usage = _usage_estimate(organizer, subscription, period_start, period_end, totals)
    fee_rows = platform_fee_rows(
        organizer=organizer,
        period_start=period_start,
        period_end=period_end,
        state="open",
        include_carry_forward=True,
    )
    for row in fee_rows:
        _add_amount(totals, row["currency"], "ticket_fees", row["net"])

    billing = OrganizerBillingModel.objects.filter(organizer=organizer).first()
    authorization = getattr(organizer, "business_billing_authorization", None)
    customer_ready = bool(billing and billing.stripe_customer_id)
    payment_method_ready = bool(billing and billing.stripe_payment_method_id)
    collection_authorized = bool(
        authorization
        and authorization.is_active
        and billing
        and authorization.stripe_customer_id == billing.stripe_customer_id
    )

    return {
        "period_start": period_start,
        "period_end": period_end,
        "expected_issue_date": issue_date,
        "subscription": subscription,
        "plan": plan,
        "addons": addons,
        "usage": usage,
        "fee_rows": fee_rows,
        "totals": [
            {"currency": currency, **amounts}
            for currency, amounts in sorted(totals.items())
        ],
        "billing_customer_ready": customer_ready,
        "billing_payment_method_ready": payment_method_ready,
        "billing_collection_authorized": collection_authorized,
        "billing_profile_ready": customer_ready
        and payment_method_ready
        and collection_authorized,
    }
