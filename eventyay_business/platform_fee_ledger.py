import datetime
import logging
from decimal import ROUND_HALF_UP, Decimal
from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils.dateparse import parse_datetime
from django.utils.timezone import is_naive, make_aware
from django_scopes import scope
from eventyay.base.models import Order, OrderPayment, OrderRefund

from .models import (
    PlatformFeeLedgerEntry,
    PlatformFeeLedgerEntryType,
    Subscription,
    UsageRecord,
)
from .services import resolve_fee_settings

logger = logging.getLogger(__name__)

CENT = Decimal("0.01")
ZERO = Decimal("0.00")
BILLING_CUTOVER_SETTING = "business_billing_cutover_at"


def resolve_payment_date(order, payment=None, payment_date=None):
    if not isinstance(payment_date, (datetime.date, datetime.datetime)):
        payment_date = None

    if not payment_date and payment is not None:
        candidate = payment.payment_date
        if isinstance(candidate, (datetime.date, datetime.datetime)):
            payment_date = candidate

    if not payment_date:
        payment_date = order.datetime

    if isinstance(payment_date, datetime.date) and not isinstance(
        payment_date, datetime.datetime
    ):
        payment_date = datetime.datetime.combine(payment_date, datetime.time.min)
    if is_naive(payment_date):
        payment_date = make_aware(payment_date)
    return payment_date


def organizer_billing_cutover_at(organizer) -> datetime.datetime | None:
    value = organizer.settings.get(BILLING_CUTOVER_SETTING, as_type=str)
    if not value:
        return None

    cutover_at = parse_datetime(value)
    if cutover_at is None:
        logger.error(
            "Invalid billing cutover timestamp configured for organizer %s",
            organizer.pk,
        )
        return None
    if is_naive(cutover_at):
        cutover_at = make_aware(cutover_at)
    return cutover_at


def checkout_application_fee(payment, currency_places: int) -> Decimal:
    info = payment.info_data
    if not isinstance(info, dict):
        return ZERO
    amount = info.get("application_fee_amount")
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        return ZERO
    return (Decimal(amount) / (Decimal(10) ** currency_places)).quantize(CENT)


def active_subscription_at(organizer, payment_date):
    return (
        Subscription.objects.filter(
            organizer=organizer,
            status="active",
            starts_at__lte=payment_date,
        )
        .exclude(ends_at__lt=payment_date)
        .select_related("tier_version__tier")
        .first()
    )


def policy_source(subscription, is_override: bool) -> str:
    if is_override:
        return "country_override"
    if subscription and subscription.tier_version:
        tier = subscription.tier_version.tier
        return f"tier:{tier.slug}:v{subscription.tier_version.version}"
    return "none"


def record_platform_fees_for_paid_order(event, order) -> list[PlatformFeeLedgerEntry]:
    organizer = event.organizer
    created_entries = []
    cutover_at = organizer_billing_cutover_at(organizer)

    with transaction.atomic(), scope(organizer=organizer, event=event):
        locked_order = (
            Order.objects.select_for_update()
            .select_related("event__organizer")
            .get(pk=order.pk)
        )
        payments = list(
            locked_order.payments.filter(
                state__in=(
                    OrderPayment.PAYMENT_STATE_CONFIRMED,
                    OrderPayment.PAYMENT_STATE_REFUNDED,
                ),
                amount__gt=ZERO,
            ).order_by("payment_date", "pk")
        )
        if not payments or locked_order.total <= ZERO:
            return created_entries

        fee_base = sum(
            (
                position.price - (position.tax_value or ZERO)
                for position in locked_order.positions.all()
            ),
            ZERO,
        )
        if fee_base <= ZERO:
            return created_entries

        currency_places = settings.CURRENCY_PLACES.get(event.currency, 2)
        checkout_fee_remaining = sum(
            (
                checkout_application_fee(payment, currency_places)
                for payment in payments
            ),
            ZERO,
        )
        legacy_debt_remaining = (
            UsageRecord.objects.filter(
                organizer=organizer,
                capability="commerce.platform_fee_percent",
                source_type="order",
                source_id=locked_order.code,
            ).aggregate(total=Sum("quantity"))["total"]
            or ZERO
        )
        remaining_base = fee_base
        cumulative_calculated_fee = ZERO

        for index, payment in enumerate(payments):
            payment_date = resolve_payment_date(locked_order, payment=payment)
            subscription = active_subscription_at(organizer, payment_date)
            tier_version = subscription.tier_version if subscription else None
            percentage, maximum_fee, is_override = resolve_fee_settings(
                event=event,
                order=locked_order,
                tier_version=tier_version,
            )

            if index == len(payments) - 1:
                payment_fee_base = remaining_base
            else:
                ratio = min(payment.amount / locked_order.total, Decimal("1"))
                payment_fee_base = min(
                    (fee_base * ratio).quantize(CENT, rounding=ROUND_HALF_UP),
                    remaining_base,
                )
            remaining_base -= payment_fee_base

            if percentage <= ZERO:
                continue

            calculated_fee = (payment_fee_base * percentage / Decimal("100")).quantize(
                CENT, rounding=ROUND_HALF_UP
            )
            if maximum_fee > ZERO:
                calculated_fee = min(
                    calculated_fee,
                    max(maximum_fee - cumulative_calculated_fee, ZERO),
                )
            cumulative_calculated_fee += calculated_fee
            if calculated_fee <= ZERO:
                continue

            checkout_offset = min(calculated_fee, checkout_fee_remaining)
            checkout_fee_remaining -= checkout_offset
            legacy_offset = min(calculated_fee - checkout_offset, legacy_debt_remaining)
            legacy_debt_remaining -= legacy_offset
            uncollected_amount = calculated_fee - checkout_offset - legacy_offset
            is_post_cutover = cutover_at is not None and payment_date >= cutover_at
            accrued_amount = uncollected_amount if is_post_cutover else ZERO
            if checkout_offset > ZERO:
                collection_path = "historical_immediate"
            elif legacy_offset > ZERO:
                collection_path = "legacy_usage_record"
            elif cutover_at is None:
                collection_path = "migration_not_enabled"
            elif is_post_cutover:
                collection_path = "monthly_accrual"
            else:
                collection_path = "historical_pre_cutover"
            source_key = f"payment:{payment.pk}"
            metadata = {
                "order_code": locked_order.code,
                "order_total": str(locked_order.total),
                "payment_amount": str(payment.amount),
                "payment_provider": payment.provider,
                "collection_path": collection_path,
                "legacy_accrued_amount": str(legacy_offset),
                "tier_version_id": tier_version.pk if tier_version else None,
                "billing_cutover_at": cutover_at.isoformat() if cutover_at else None,
                "cutover_relation": (
                    "post_cutover"
                    if is_post_cutover
                    else "pre_cutover" if cutover_at else "not_configured"
                ),
                "seconds_from_cutover": (
                    int((payment_date - cutover_at).total_seconds())
                    if cutover_at
                    else None
                ),
                "unreconciled_historical_amount": str(
                    uncollected_amount if not is_post_cutover else ZERO
                ),
            }

            try:
                entry, created = PlatformFeeLedgerEntry.objects.get_or_create(
                    organizer=organizer,
                    source_key=source_key,
                    defaults={
                        "event": event,
                        "order": locked_order,
                        "payment": payment,
                        "entry_type": PlatformFeeLedgerEntryType.CHARGE,
                        "occurred_at": payment_date,
                        "billing_period": payment_date.date().replace(day=1),
                        "currency": event.currency,
                        "fee_base": payment_fee_base,
                        "percentage": percentage,
                        "maximum_fee": maximum_fee,
                        "calculated_fee": calculated_fee,
                        "collected_at_checkout": checkout_offset,
                        "amount": accrued_amount,
                        "policy_source": policy_source(subscription, is_override),
                        "is_override": is_override,
                        "metadata": metadata,
                    },
                )
            except IntegrityError:
                continue
            if created:
                created_entries.append(entry)

    return created_entries


def record_platform_fee_refund(refund) -> list[PlatformFeeLedgerEntry]:
    event = refund.order.event
    organizer = event.organizer
    created_entries = []

    with transaction.atomic(), scope(organizer=organizer, event=event):
        locked_refund = (
            OrderRefund.objects.select_for_update()
            .select_related("order__event__organizer")
            .get(pk=refund.pk)
        )
        if locked_refund.state != OrderRefund.REFUND_STATE_DONE:
            return created_entries

        charges = PlatformFeeLedgerEntry.objects.select_for_update().filter(
            organizer=organizer,
            order=locked_refund.order,
            entry_type=PlatformFeeLedgerEntryType.CHARGE,
            amount__gt=ZERO,
        )
        if locked_refund.payment_id:
            charges = charges.filter(payment=locked_refund.payment)
            denominator = locked_refund.payment.amount
            refunded_total = (
                locked_refund.payment.refunds.filter(
                    state=OrderRefund.REFUND_STATE_DONE
                ).aggregate(total=Sum("amount"))["total"]
                or ZERO
            )
        else:
            denominator = locked_refund.order.total
            refunded_total = (
                locked_refund.order.refunds.filter(
                    state=OrderRefund.REFUND_STATE_DONE,
                    payment__isnull=True,
                ).aggregate(total=Sum("amount"))["total"]
                or ZERO
            )

        if denominator <= ZERO:
            return created_entries
        refund_ratio = min(refunded_total / denominator, Decimal("1"))
        occurred_at = locked_refund.execution_date or locked_refund.created
        charges = list(charges.order_by("payment_id", "pk"))
        total_reversed_by_charge = {
            charge.pk: -(
                charge.adjustments.filter(amount__lt=ZERO).aggregate(
                    total=Sum("amount")
                )["total"]
                or ZERO
            )
            for charge in charges
        }
        order_refund_allocations = {}

        if not locked_refund.payment_id:
            scoped_reversed_by_charge = {
                charge.pk: -(
                    charge.adjustments.filter(
                        entry_type=PlatformFeeLedgerEntryType.REFUND,
                        refund__payment__isnull=True,
                    ).aggregate(total=Sum("amount"))["total"]
                    or ZERO
                )
                for charge in charges
            }
            target_reversal = (
                sum((charge.amount for charge in charges), ZERO) * refund_ratio
            ).quantize(CENT, rounding=ROUND_HALF_UP)
            remaining_target = max(
                target_reversal - sum(scoped_reversed_by_charge.values(), ZERO),
                ZERO,
            )

            for charge in charges:
                charge_target = (charge.amount * refund_ratio).quantize(
                    CENT, rounding=ROUND_HALF_UP
                )
                remaining_charge = max(
                    charge.amount - total_reversed_by_charge[charge.pk], ZERO
                )
                allocation = min(
                    max(charge_target - scoped_reversed_by_charge[charge.pk], ZERO),
                    remaining_charge,
                    remaining_target,
                )
                order_refund_allocations[charge.pk] = allocation
                remaining_target -= allocation

            for charge in charges:
                if remaining_target <= ZERO:
                    break
                remaining_charge = max(
                    charge.amount
                    - total_reversed_by_charge[charge.pk]
                    - order_refund_allocations[charge.pk],
                    ZERO,
                )
                allocation = min(remaining_charge, remaining_target)
                order_refund_allocations[charge.pk] += allocation
                remaining_target -= allocation

        for charge in charges:
            if locked_refund.payment_id:
                target_reversal = (charge.amount * refund_ratio).quantize(
                    CENT, rounding=ROUND_HALF_UP
                )
                scoped_reversed_amount = -(
                    charge.adjustments.filter(
                        entry_type=PlatformFeeLedgerEntryType.REFUND,
                        refund__payment=locked_refund.payment,
                    ).aggregate(total=Sum("amount"))["total"]
                    or ZERO
                )
                adjustment_amount = min(
                    max(target_reversal - scoped_reversed_amount, ZERO),
                    charge.amount - total_reversed_by_charge[charge.pk],
                )
            else:
                adjustment_amount = order_refund_allocations[charge.pk]
            if adjustment_amount <= ZERO:
                continue

            source_key = f"refund:{locked_refund.pk}:entry:{charge.pk}"
            try:
                entry, created = PlatformFeeLedgerEntry.objects.get_or_create(
                    organizer=organizer,
                    source_key=source_key,
                    defaults={
                        "event": event,
                        "order": locked_refund.order,
                        "payment": charge.payment,
                        "refund": locked_refund,
                        "related_entry": charge,
                        "entry_type": PlatformFeeLedgerEntryType.REFUND,
                        "occurred_at": occurred_at,
                        "billing_period": occurred_at.date().replace(day=1),
                        "currency": charge.currency,
                        "fee_base": ZERO,
                        "percentage": charge.percentage,
                        "maximum_fee": charge.maximum_fee,
                        "calculated_fee": -adjustment_amount,
                        "amount": -adjustment_amount,
                        "policy_source": charge.policy_source,
                        "is_override": charge.is_override,
                        "metadata": {
                            "order_code": locked_refund.order.code,
                            "refund_amount": str(locked_refund.amount),
                            "refund_ratio": str(refund_ratio),
                        },
                    },
                )
            except IntegrityError:
                continue
            if created:
                created_entries.append(entry)

    return created_entries


def record_platform_fee_cancellation(event, order) -> list[PlatformFeeLedgerEntry]:
    organizer = event.organizer
    created_entries = []

    with transaction.atomic(), scope(organizer=organizer, event=event):
        locked_order = Order.objects.select_for_update().get(pk=order.pk)
        charges = PlatformFeeLedgerEntry.objects.select_for_update().filter(
            organizer=organizer,
            order=locked_order,
            entry_type=PlatformFeeLedgerEntryType.CHARGE,
            amount__gt=ZERO,
        )
        occurred_at = locked_order.cancellation_date or locked_order.last_modified

        for charge in charges:
            reversed_amount = -(
                charge.adjustments.filter(amount__lt=ZERO).aggregate(
                    total=Sum("amount")
                )["total"]
                or ZERO
            )
            adjustment_amount = charge.amount - reversed_amount
            if adjustment_amount <= ZERO:
                continue

            source_key = f"cancellation:{locked_order.pk}:entry:{charge.pk}"
            try:
                entry, created = PlatformFeeLedgerEntry.objects.get_or_create(
                    organizer=organizer,
                    source_key=source_key,
                    defaults={
                        "event": event,
                        "order": locked_order,
                        "payment": charge.payment,
                        "related_entry": charge,
                        "entry_type": PlatformFeeLedgerEntryType.CANCELLATION,
                        "occurred_at": occurred_at,
                        "billing_period": occurred_at.date().replace(day=1),
                        "currency": charge.currency,
                        "fee_base": ZERO,
                        "percentage": charge.percentage,
                        "maximum_fee": charge.maximum_fee,
                        "calculated_fee": -adjustment_amount,
                        "amount": -adjustment_amount,
                        "policy_source": charge.policy_source,
                        "is_override": charge.is_override,
                        "metadata": {
                            "order_code": locked_order.code,
                            "reason": "order_canceled",
                        },
                    },
                )
            except IntegrityError:
                continue
            if created:
                created_entries.append(entry)

    return created_entries
