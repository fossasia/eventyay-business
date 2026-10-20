import logging
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils.timezone import is_aware, make_aware, now
from eventyay.base.models import Organizer
from eventyay.base.models.organizer import OrganizerBillingModel
from eventyay.base.settings import GlobalSettingsObject

from .invoice_presentation import current_invoice_presentation_snapshot
from .models import (
    AddonPricingMode,
    AddonStatus,
    BillingInterval,
    BillingNotificationEventType,
    BusinessInvoice,
    BusinessInvoiceLine,
    BusinessInvoiceStatus,
    EventAddon,
    InvoiceLineType,
    InvoicePaymentState,
    OrganizerAddon,
    PlatformFeeLedgerEntry,
    Subscription,
    SubscriptionStatus,
    UsageRecord,
)
from .notifications import queue_invoice_notification
from .vouchers import build_voucher_credit_lines

logger = logging.getLogger(__name__)

CENT = Decimal("0.01")


class InvoiceCalculationError(ValueError):
    pass


def next_calendar_month_range(
    period_end: datetime,
) -> tuple[datetime, datetime]:
    current_month = period_end.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if current_month.month == 12:
        start = current_month.replace(year=current_month.year + 1, month=1)
    else:
        start = current_month.replace(month=current_month.month + 1)
    if start.month == 12:
        following = start.replace(year=start.year + 1, month=1)
    else:
        following = start.replace(month=start.month + 1)
    return start, following - timedelta(microseconds=1)


def prorated_amount(
    amount: Decimal,
    period_start: datetime,
    period_end: datetime,
    active_start: datetime,
    active_end: datetime,
) -> Decimal:
    period_seconds = Decimal(
        str((period_end - period_start + timedelta(microseconds=1)).total_seconds())
    )
    active_seconds = Decimal(
        str((active_end - active_start + timedelta(microseconds=1)).total_seconds())
    )
    return (amount * active_seconds / period_seconds).quantize(
        CENT, rounding=ROUND_HALF_UP
    )


def effective_service_period(
    starts_at: datetime,
    ends_at: datetime | None,
    cancel_at: datetime | None,
    canceled_at: datetime | None,
    period_start: datetime,
    period_end: datetime,
) -> tuple[datetime, datetime] | None:
    service_start = max(starts_at, period_start)
    boundaries = [value for value in (ends_at, cancel_at, canceled_at) if value]
    service_end = min([period_end, *boundaries])
    if service_start > service_end:
        return None
    return service_start, service_end


def invoice_source_claimed(source_key: str) -> bool:
    return BusinessInvoiceLine.objects.filter(usage_reference=source_key).exists()


def build_subscription_invoice_line(
    subscription: Subscription,
    service_period_start: datetime,
    service_period_end: datetime,
    full_period_start: datetime,
    full_period_end: datetime,
    billing_currency: str,
    rates_dict: dict | None,
    rates_date: date | None,
    prorate: bool,
) -> dict | None:
    tier_version = subscription.tier_version
    tier = tier_version.tier
    interval = subscription.billing_interval or BillingInterval.MONTHLY
    price = (
        tier_version.prices.filter(
            active=True,
            billing_interval=interval,
            currency=billing_currency,
        ).first()
        or tier_version.prices.filter(
            active=True,
            billing_interval=interval,
        ).first()
    )
    if not price:
        logger.warning(
            "No active price found for tier %s matching billing interval %s. Skipping subscription line.",
            tier.slug,
            interval,
        )
        return None

    exchange_rate = None
    if price.currency == billing_currency:
        unit_price = price.amount
    elif (
        rates_dict
        and rates_date
        and price.currency in rates_dict
        and billing_currency in rates_dict
    ):
        try:
            rate = Decimal(str(rates_dict[billing_currency])) / Decimal(
                str(rates_dict[price.currency])
            )
            unit_price = (price.amount * rate).quantize(CENT)
            exchange_rate = str(rate.quantize(Decimal("0.0001")))
        except (InvalidOperation, ArithmeticError, TypeError) as exc:
            raise InvoiceCalculationError(
                f"Could not convert the {tier.slug} subscription price from "
                f"{price.currency} to {billing_currency}."
            ) from exc
    else:
        raise InvoiceCalculationError(
            f"No exchange rate is available to convert the {tier.slug} subscription "
            f"price from {price.currency} to {billing_currency}."
        )

    amount = unit_price
    if prorate:
        amount = prorated_amount(
            unit_price,
            full_period_start,
            full_period_end,
            service_period_start,
            service_period_end,
        )
    if amount <= Decimal("0.00"):
        return None

    source_key = (
        f"subscription:{subscription.pk}:{service_period_start.isoformat()}:"
        f"{service_period_end.isoformat()}"
    )
    if invoice_source_claimed(source_key):
        return None

    return {
        "line_type": InvoiceLineType.SUBSCRIPTION,
        "event": None,
        "description": f"Plan Subscription: {tier.name} ({price.get_billing_interval_display()})",
        "quantity": Decimal("1.00"),
        "unit_price": unit_price,
        "amount": amount,
        "tier_version": tier_version,
        "addon": None,
        "usage_reference": source_key,
        "calculation_metadata": {
            "subscription_id": subscription.pk,
            "tier_slug": tier.slug,
            "tier_name": tier.name,
            "tier_version": tier_version.version,
            "billing_interval": str(interval),
            "price_currency": price.currency,
            "price_amount": str(price.amount),
            "service_period_start": service_period_start.isoformat(),
            "service_period_end": service_period_end.isoformat(),
            "exchange_rate": exchange_rate,
            "exchange_rate_date": rates_date.isoformat() if exchange_rate else None,
            "prorated": prorate,
            "charged_amount": str(amount),
        },
        "service_period_start": service_period_start,
        "service_period_end": service_period_end,
    }


def billing_address_snapshot(organizer) -> dict:
    billing = OrganizerBillingModel.objects.filter(organizer=organizer).first()
    if not billing:
        return {"organization_name": organizer.name}
    return {
        "contact_name": billing.primary_contact_name,
        "contact_email": billing.primary_contact_email,
        "organization_name": billing.company_or_organization_name,
        "address_line_1": billing.address_line_1,
        "address_line_2": billing.address_line_2,
        "city": billing.city,
        "postal_code": billing.zip_code,
        "country": billing.country,
        "tax_id": billing.tax_id,
    }


def notify_invoice_issued(invoice: BusinessInvoice) -> None:
    event_type = (
        BillingNotificationEventType.CREDIT_ISSUED
        if invoice.corrects_invoice_id or invoice.total < Decimal("0.00")
        else BillingNotificationEventType.INVOICE_ISSUED
    )
    queue_invoice_notification(
        invoice,
        event_type,
    )


def finalize_business_invoice(
    invoice: BusinessInvoice, finalized_by=None, due_days: int = 14
) -> BusinessInvoice:
    with transaction.atomic():
        invoice = BusinessInvoice.objects.select_for_update().get(pk=invoice.pk)
        if invoice.finalized_at:
            return invoice

        line_total = sum((line.amount for line in invoice.lines.all()), Decimal("0.00"))
        tax = sum(
            (
                line.amount
                for line in invoice.lines.filter(line_type=InvoiceLineType.TAX)
            ),
            Decimal("0.00"),
        )
        finalized_at = now()
        invoice.subtotal = line_total - tax
        invoice.tax = tax
        invoice.total = line_total
        invoice.issue_date = finalized_at.date()
        invoice.due_date = invoice.issue_date + timedelta(days=due_days)
        invoice.finalized_at = finalized_at
        invoice.finalized_by = finalized_by
        invoice.billing_address_snapshot = billing_address_snapshot(invoice.organizer)
        invoice.presentation_snapshot = current_invoice_presentation_snapshot()
        if invoice.status == BusinessInvoiceStatus.PAID or invoice.total <= Decimal(
            "0.00"
        ):
            invoice.status = BusinessInvoiceStatus.PAID
            invoice.payment_state = InvoicePaymentState.SUCCEEDED
            invoice.paid_at = finalized_at
        else:
            invoice.status = BusinessInvoiceStatus.OPEN
            invoice.payment_state = InvoicePaymentState.NOT_STARTED
        invoice.save()
        transaction.on_commit(lambda: notify_invoice_issued(invoice), robust=True)
        return invoice


def generate_invoice_number(organizer, period_start: datetime) -> str:
    """
    Generates a unique, sequential invoice number for the organizer and period.
    Format: INV-{YEAR}{MONTH}-{ORG_PK:04d}-{SEQ:02d}
    """
    period_str = period_start.strftime("%Y%m")
    prefix = f"INV-{period_str}-{organizer.pk:04d}-"

    with transaction.atomic():
        Organizer.objects.select_for_update().filter(pk=organizer.pk).first()
        last_invoice = (
            BusinessInvoice.objects.filter(invoice_number__startswith=prefix)
            .order_by("-invoice_number")
            .first()
        )
        if last_invoice:
            try:
                last_seq = int(last_invoice.invoice_number.split("-")[-1])
                seq = last_seq + 1
            except (ValueError, IndexError):
                seq = 1
        else:
            seq = 1

        return f"{prefix}{seq:02d}"


def get_previous_calendar_month_range(reference_date: datetime = None):
    """
    Returns (start_datetime, end_datetime) aware datetimes for the previous calendar month.
    """
    current = reference_date or now()
    year = current.year
    month = current.month

    if month == 1:
        prev_month = 12
        prev_year = year - 1
    else:
        prev_month = month - 1
        prev_year = year

    start_date = datetime(prev_year, prev_month, 1, 0, 0, 0)
    if prev_month == 12:
        next_month_start = datetime(prev_year + 1, 1, 1, 0, 0, 0)
    else:
        next_month_start = datetime(prev_year, prev_month + 1, 1, 0, 0, 0)

    from datetime import timedelta

    end_date = next_month_start - timedelta(microseconds=1)

    if not is_aware(start_date):
        start_date = make_aware(start_date)
    if not is_aware(end_date):
        end_date = make_aware(end_date)

    return start_date, end_date


def _build_addon_invoice_line(
    addon_assignment,
    description: str,
    usage_reference: str,
    scope_meta: dict,
    event=None,
    billing_currency: str = "EUR",
    rates_dict: dict | None = None,
    rates_date: date | None = None,
    service_period_start: datetime | None = None,
    service_period_end: datetime | None = None,
    full_period_start: datetime | None = None,
    full_period_end: datetime | None = None,
) -> dict | None:
    raw_price = (
        addon_assignment.price
        if addon_assignment.price is not None
        else addon_assignment.addon.price
    )
    addon_currency = addon_assignment.currency or (
        addon_assignment.addon.currency if addon_assignment.addon else billing_currency
    )
    rate_applied = None

    if raw_price is None or raw_price <= Decimal("0.00"):
        return None

    if addon_currency == billing_currency:
        price = raw_price
    elif (
        rates_dict
        and rates_date
        and addon_currency in rates_dict
        and billing_currency in rates_dict
    ):
        try:
            rate = Decimal(str(rates_dict[billing_currency])) / Decimal(
                str(rates_dict[addon_currency])
            )
            price = (raw_price * rate).quantize(Decimal("0.01"))
            rate_applied = str(rate.quantize(Decimal("0.0001")))
        except (InvalidOperation, ArithmeticError, TypeError) as exc:
            raise InvoiceCalculationError(
                f"Could not convert add-on {addon_assignment.addon.slug} from "
                f"{addon_currency} to {billing_currency}."
            ) from exc
    else:
        raise InvoiceCalculationError(
            f"No exchange rate is available to convert add-on "
            f"{addon_assignment.addon.slug} from {addon_currency} to "
            f"{billing_currency}."
        )

    qty = Decimal(addon_assignment.quantity or 1)
    line_amount = (price * qty).quantize(Decimal("0.01"))
    if (
        addon_assignment.addon.pricing_mode == AddonPricingMode.RECURRING
        and service_period_start
        and service_period_end
        and full_period_start
        and full_period_end
    ):
        line_amount = prorated_amount(
            line_amount,
            full_period_start,
            full_period_end,
            service_period_start,
            service_period_end,
        )

    calc_meta = {
        **scope_meta,
        "addon_id": addon_assignment.addon.pk,
        "addon_name": addon_assignment.addon.name,
        "pricing_mode": str(addon_assignment.addon.pricing_mode),
        "quantity": int(qty),
        "unit_price": str(price),
        "original_currency": addon_currency,
        "original_price": str(raw_price),
        "exchange_rate": rate_applied,
        "exchange_rate_date": rates_date.isoformat() if rate_applied else None,
        "service_period_start": (
            service_period_start.isoformat() if service_period_start else None
        ),
        "service_period_end": (
            service_period_end.isoformat() if service_period_end else None
        ),
    }

    return {
        "line_type": InvoiceLineType.ADDON,
        "event": event,
        "description": description,
        "quantity": qty,
        "unit_price": price,
        "amount": line_amount,
        "tier_version": None,
        "addon": addon_assignment.addon,
        "usage_reference": usage_reference,
        "calculation_metadata": calc_meta,
        "service_period_start": service_period_start,
        "service_period_end": service_period_end,
    }


def generate_business_invoice_for_organizer(
    organizer,
    start_date: datetime,
    end_date: datetime,
    currency: str = None,
    allow_empty: bool = False,
) -> BusinessInvoice | None:
    """
    Aggregates subscriptions, add-ons, paid-order platform fees, and free registration
    overages for an organizer across a billing period into a BusinessInvoice with
    itemized BusinessInvoiceLine records.
    """
    if end_date.microsecond == 0:
        end_date = end_date.replace(microsecond=999999)

    billing_currency = currency
    try:
        with transaction.atomic():
            # Acquire exclusive row-level lock on organizer to serialize concurrent requests per organizer
            Organizer.objects.select_for_update().filter(pk=organizer.pk).first()

            gs = GlobalSettingsObject()
            rates_dict = gs.settings.get("ecb_rates_dict", as_type=dict)
            rates_date = gs.settings.get("ecb_rates_date", as_type=date)

            # Determine the subscription for variable charges in the closed period.
            sub_filter = (
                Q(status__in=[SubscriptionStatus.ACTIVE, SubscriptionStatus.PAST_DUE])
                & (Q(ends_at__isnull=True) | Q(ends_at__gte=start_date))
                & (Q(cancel_at__isnull=True) | Q(cancel_at__gte=start_date))
            ) | (
                Q(status__in=[SubscriptionStatus.CANCELED, SubscriptionStatus.EXPIRED])
                & (
                    (Q(ends_at__isnull=False) & Q(ends_at__gte=start_date))
                    | (Q(cancel_at__isnull=False) & Q(cancel_at__gte=start_date))
                )
            )
            active_sub = (
                Subscription.objects.filter(
                    organizer=organizer,
                    starts_at__lte=end_date,
                )
                .filter(sub_filter)
                .select_related("tier_version__tier")
                .order_by("-starts_at")
                .first()
            )

            advance_start, advance_end = next_calendar_month_range(end_date)
            fixed_sub_filter = (
                Q(status__in=[SubscriptionStatus.ACTIVE, SubscriptionStatus.PAST_DUE])
                & (Q(ends_at__isnull=True) | Q(ends_at__gte=advance_start))
                & (Q(cancel_at__isnull=True) | Q(cancel_at__gte=advance_start))
            ) | (
                Q(status__in=[SubscriptionStatus.CANCELED, SubscriptionStatus.EXPIRED])
                & (
                    (Q(ends_at__isnull=False) & Q(ends_at__gte=advance_start))
                    | (Q(cancel_at__isnull=False) & Q(cancel_at__gte=advance_start))
                )
            )
            fixed_sub = (
                Subscription.objects.filter(
                    organizer=organizer,
                    starts_at__lte=advance_end,
                )
                .filter(fixed_sub_filter)
                .select_related("tier_version__tier")
                .order_by("-starts_at")
                .first()
            )

            billing_currency = (
                currency
                or (fixed_sub.currency if fixed_sub else None)
                or (active_sub.currency if active_sub else None)
                or "EUR"
            )

            existing = BusinessInvoice.objects.filter(
                organizer=organizer,
                billing_period_start=start_date,
                billing_period_end=end_date,
                currency=billing_currency,
            ).first()
            if existing:
                return existing

            lines_data = []

            # Charge the closed month once when an existing monthly subscription
            # first moves from arrears billing to advance billing. On later runs,
            # the preceding invoice has already claimed this service period.
            if (
                active_sub
                and active_sub.tier_version
                and (active_sub.billing_interval or BillingInterval.MONTHLY)
                == BillingInterval.MONTHLY
            ):
                catch_up_period = effective_service_period(
                    active_sub.starts_at,
                    active_sub.ends_at,
                    active_sub.cancel_at,
                    None,
                    start_date,
                    end_date,
                )
                if catch_up_period:
                    line = build_subscription_invoice_line(
                        active_sub,
                        catch_up_period[0],
                        catch_up_period[1],
                        start_date,
                        end_date,
                        billing_currency,
                        rates_dict,
                        rates_date,
                        prorate=catch_up_period != (start_date, end_date),
                    )
                    if line:
                        lines_data.append(line)

            # Add the fixed plan charge for the upcoming period in advance.
            if fixed_sub and fixed_sub.tier_version:
                interval = fixed_sub.billing_interval or BillingInterval.MONTHLY
                if interval == BillingInterval.MONTHLY:
                    service_period = effective_service_period(
                        fixed_sub.starts_at,
                        fixed_sub.ends_at,
                        fixed_sub.cancel_at,
                        None,
                        advance_start,
                        advance_end,
                    )
                    if service_period:
                        line = build_subscription_invoice_line(
                            fixed_sub,
                            service_period[0],
                            service_period[1],
                            advance_start,
                            advance_end,
                            billing_currency,
                            rates_dict,
                            rates_date,
                            prorate=service_period != (advance_start, advance_end),
                        )
                        if line:
                            lines_data.append(line)
                else:
                    for test_year in (
                        advance_start.year,
                        advance_start.year - 1,
                        advance_start.year + 1,
                    ):
                        try:
                            anniversary = fixed_sub.starts_at.replace(year=test_year)
                        except ValueError:
                            anniversary = fixed_sub.starts_at.replace(
                                year=test_year, day=28
                            )
                        if not (
                            advance_start <= anniversary <= advance_end
                            and anniversary >= fixed_sub.starts_at
                        ):
                            continue
                        try:
                            annual_end = anniversary.replace(year=anniversary.year + 1)
                        except ValueError:
                            annual_end = anniversary.replace(
                                year=anniversary.year + 1, day=28
                            )
                        annual_period = effective_service_period(
                            fixed_sub.starts_at,
                            fixed_sub.ends_at,
                            fixed_sub.cancel_at,
                            None,
                            anniversary,
                            annual_end,
                        )
                        if not annual_period or annual_period[1] <= annual_period[0]:
                            break
                        line = build_subscription_invoice_line(
                            fixed_sub,
                            annual_period[0],
                            annual_period[1],
                            anniversary,
                            annual_end,
                            billing_currency,
                            rates_dict,
                            rates_date,
                            prorate=annual_period != (anniversary, annual_end),
                        )
                        if line:
                            lines_data.append(line)
                        break

            # Include a new subscription's first partial period when it began
            # after the preceding calendar-boundary invoice was generated.
            if (
                active_sub
                and active_sub.tier_version
                and start_date < active_sub.starts_at <= end_date
            ):
                interval = active_sub.billing_interval or BillingInterval.MONTHLY
                if interval == BillingInterval.ANNUAL:
                    try:
                        initial_end = active_sub.starts_at.replace(
                            year=active_sub.starts_at.year + 1
                        )
                    except ValueError:
                        initial_end = active_sub.starts_at.replace(
                            year=active_sub.starts_at.year + 1, day=28
                        )
                    initial_period = effective_service_period(
                        active_sub.starts_at,
                        active_sub.ends_at,
                        active_sub.cancel_at,
                        None,
                        active_sub.starts_at,
                        initial_end,
                    )
                    if initial_period and initial_period[1] > initial_period[0]:
                        line = build_subscription_invoice_line(
                            active_sub,
                            initial_period[0],
                            initial_period[1],
                            active_sub.starts_at,
                            initial_end,
                            billing_currency,
                            rates_dict,
                            rates_date,
                            prorate=initial_period
                            != (active_sub.starts_at, initial_end),
                        )
                        if line:
                            lines_data.append(line)

            # Add recurring add-ons for the upcoming period and one-time add-ons
            # activated during the closed period.
            advance_recurring_filter = (
                Q(addon__pricing_mode=AddonPricingMode.RECURRING)
                & (
                    (
                        Q(status__in=[AddonStatus.ACTIVE, AddonStatus.PAST_DUE])
                        & (Q(ends_at__isnull=True) | Q(ends_at__gte=advance_start))
                        & (Q(cancel_at__isnull=True) | Q(cancel_at__gte=advance_start))
                        & (
                            Q(canceled_at__isnull=True)
                            | Q(canceled_at__gte=advance_start)
                        )
                    )
                    | (
                        Q(status__in=[AddonStatus.CANCELED, AddonStatus.EXPIRED])
                        & (
                            (Q(ends_at__isnull=False) & Q(ends_at__gte=advance_start))
                            | (
                                Q(cancel_at__isnull=False)
                                & Q(cancel_at__gte=advance_start)
                            )
                            | (
                                Q(canceled_at__isnull=False)
                                & Q(canceled_at__gte=advance_start)
                            )
                        )
                    )
                )
                & Q(starts_at__lte=advance_end)
            )
            closed_recurring_filter = (
                Q(addon__pricing_mode=AddonPricingMode.RECURRING)
                & (
                    (
                        Q(status__in=[AddonStatus.ACTIVE, AddonStatus.PAST_DUE])
                        & (Q(ends_at__isnull=True) | Q(ends_at__gte=start_date))
                        & (Q(cancel_at__isnull=True) | Q(cancel_at__gte=start_date))
                        & (Q(canceled_at__isnull=True) | Q(canceled_at__gte=start_date))
                    )
                    | (
                        Q(status__in=[AddonStatus.CANCELED, AddonStatus.EXPIRED])
                        & (
                            (Q(ends_at__isnull=False) & Q(ends_at__gte=start_date))
                            | (
                                Q(cancel_at__isnull=False)
                                & Q(cancel_at__gte=start_date)
                            )
                            | (
                                Q(canceled_at__isnull=False)
                                & Q(canceled_at__gte=start_date)
                            )
                        )
                    )
                )
                & Q(starts_at__lte=end_date)
            )
            onetime_filter = (
                Q(addon__pricing_mode=AddonPricingMode.ONE_TIME)
                & Q(starts_at__gte=start_date)
                & Q(starts_at__lte=end_date)
                & ~Q(status=AddonStatus.CANCELED)
            )
            addon_filter = (
                advance_recurring_filter | closed_recurring_filter | onetime_filter
            )

            # 4a. Organizer-level add-ons
            org_addons = (
                OrganizerAddon.objects.filter(organizer=organizer)
                .filter(addon_filter)
                .select_related("addon")
            )

            for oa in org_addons:
                if oa.addon.pricing_mode == AddonPricingMode.RECURRING:
                    initial_period = effective_service_period(
                        oa.starts_at,
                        oa.ends_at,
                        oa.cancel_at,
                        oa.canceled_at,
                        start_date,
                        end_date,
                    )
                    if initial_period:
                        initial_source_key = (
                            f"organizer-addon:{oa.pk}:"
                            f"{initial_period[0].isoformat()}:"
                            f"{initial_period[1].isoformat()}"
                        )
                        if not invoice_source_claimed(initial_source_key):
                            initial_line = _build_addon_invoice_line(
                                addon_assignment=oa,
                                description=f"Add-on: {oa.addon.name} (Organisation)",
                                usage_reference=initial_source_key,
                                scope_meta={"organizer_addon_id": oa.pk},
                                event=None,
                                billing_currency=billing_currency,
                                rates_dict=rates_dict,
                                rates_date=rates_date,
                                service_period_start=initial_period[0],
                                service_period_end=initial_period[1],
                                full_period_start=start_date,
                                full_period_end=end_date,
                            )
                            if initial_line:
                                lines_data.append(initial_line)
                    service_period = effective_service_period(
                        oa.starts_at,
                        oa.ends_at,
                        oa.cancel_at,
                        oa.canceled_at,
                        advance_start,
                        advance_end,
                    )
                    if not service_period:
                        continue
                    service_period_start, service_period_end = service_period
                    source_key = (
                        f"organizer-addon:{oa.pk}:{service_period_start.isoformat()}:"
                        f"{service_period_end.isoformat()}"
                    )
                else:
                    service_period_start = oa.starts_at
                    service_period_end = oa.starts_at
                    source_key = f"organizer-addon:{oa.pk}:one-time"
                if invoice_source_claimed(source_key):
                    continue
                line = _build_addon_invoice_line(
                    addon_assignment=oa,
                    description=f"Add-on: {oa.addon.name} (Organisation)",
                    usage_reference=source_key,
                    scope_meta={"organizer_addon_id": oa.pk},
                    event=None,
                    billing_currency=billing_currency,
                    rates_dict=rates_dict,
                    rates_date=rates_date,
                    service_period_start=service_period_start,
                    service_period_end=service_period_end,
                    full_period_start=advance_start,
                    full_period_end=advance_end,
                )
                if line:
                    lines_data.append(line)

            # 4b. Event-level add-ons
            event_addons = (
                EventAddon.objects.filter(event__organizer=organizer)
                .filter(addon_filter)
                .select_related("addon", "event")
            )

            for ea in event_addons:
                if ea.addon.pricing_mode == AddonPricingMode.RECURRING:
                    initial_period = effective_service_period(
                        ea.starts_at,
                        ea.ends_at,
                        ea.cancel_at,
                        ea.canceled_at,
                        start_date,
                        end_date,
                    )
                    if initial_period:
                        initial_source_key = (
                            f"event-addon:{ea.pk}:"
                            f"{initial_period[0].isoformat()}:"
                            f"{initial_period[1].isoformat()}"
                        )
                        if not invoice_source_claimed(initial_source_key):
                            initial_line = _build_addon_invoice_line(
                                addon_assignment=ea,
                                description=(
                                    f"Add-on: {ea.addon.name} "
                                    f"(Event: {ea.event.name})"
                                ),
                                usage_reference=initial_source_key,
                                scope_meta={
                                    "event_addon_id": ea.pk,
                                    "event_slug": ea.event.slug,
                                },
                                event=ea.event,
                                billing_currency=billing_currency,
                                rates_dict=rates_dict,
                                rates_date=rates_date,
                                service_period_start=initial_period[0],
                                service_period_end=initial_period[1],
                                full_period_start=start_date,
                                full_period_end=end_date,
                            )
                            if initial_line:
                                lines_data.append(initial_line)
                    service_period = effective_service_period(
                        ea.starts_at,
                        ea.ends_at,
                        ea.cancel_at,
                        ea.canceled_at,
                        advance_start,
                        advance_end,
                    )
                    if not service_period:
                        continue
                    service_period_start, service_period_end = service_period
                    source_key = (
                        f"event-addon:{ea.pk}:{service_period_start.isoformat()}:"
                        f"{service_period_end.isoformat()}"
                    )
                else:
                    service_period_start = ea.starts_at
                    service_period_end = ea.starts_at
                    source_key = f"event-addon:{ea.pk}:one-time"
                if invoice_source_claimed(source_key):
                    continue
                line = _build_addon_invoice_line(
                    addon_assignment=ea,
                    description=f"Add-on: {ea.addon.name} (Event: {ea.event.name})",
                    usage_reference=source_key,
                    scope_meta={
                        "event_addon_id": ea.pk,
                        "event_slug": ea.event.slug,
                    },
                    event=ea.event,
                    billing_currency=billing_currency,
                    rates_dict=rates_dict,
                    rates_date=rates_date,
                    service_period_start=service_period_start,
                    service_period_end=service_period_end,
                    full_period_start=advance_start,
                    full_period_end=advance_end,
                )
                if line:
                    lines_data.append(line)

            # 5. Aggregate Paid-Order Platform Fees
            fee_records = (
                UsageRecord.objects.select_for_update(of=("self",))
                .filter(
                    organizer=organizer,
                    capability="commerce.platform_fee_percent",
                    invoice__isnull=True,
                    occurred_at__gte=start_date,
                    occurred_at__lte=end_date,
                )
                .select_related("event")
            )
            ledger_entries = (
                PlatformFeeLedgerEntry.objects.select_for_update()
                .filter(
                    organizer=organizer,
                    invoice__isnull=True,
                    occurred_at__lte=end_date,
                )
                .select_related("event")
            )

            events_fee_map = {}
            for rec in fee_records:
                ev = rec.event
                events_fee_map.setdefault(ev, []).append(("usage", rec))
            for entry in ledger_entries:
                events_fee_map.setdefault(entry.event, []).append(("ledger", entry))

            ledger_entry_ids_for_invoice = []
            usage_record_ids_for_invoice = []

            for ev, recs in events_fee_map.items():
                total_event_fee = Decimal("0.00")
                record_details = []

                for record_type, r in recs:
                    if record_type == "ledger":
                        exchange_rate = None
                        if r.currency == billing_currency:
                            fee_val = r.amount
                        elif (
                            rates_dict
                            and rates_date
                            and r.currency in rates_dict
                            and billing_currency in rates_dict
                        ):
                            try:
                                rate = Decimal(
                                    str(rates_dict[billing_currency])
                                ) / Decimal(str(rates_dict[r.currency]))
                                fee_val = (r.amount * rate).quantize(Decimal("0.01"))
                                exchange_rate = str(rate.quantize(Decimal("0.0001")))
                            except (
                                InvalidOperation,
                                ArithmeticError,
                                TypeError,
                            ) as exc:
                                raise InvoiceCalculationError(
                                    f"Could not convert platform fee {r.source_key} "
                                    f"from {r.currency} to {billing_currency}."
                                ) from exc
                        else:
                            raise InvoiceCalculationError(
                                f"No exchange rate is available to convert platform fee "
                                f"{r.source_key} from {r.currency} to {billing_currency}."
                            )
                        total_event_fee += fee_val
                        record_details.append(
                            {
                                "source_id": r.source_key,
                                "fee_amount": str(fee_val),
                                "currency": r.currency,
                                "fee_base": str(r.fee_base),
                                "fee_percent": str(r.percentage),
                                "entry_type": r.entry_type,
                                "exchange_rate": exchange_rate,
                                "exchange_rate_date": (
                                    rates_date.isoformat() if exchange_rate else None
                                ),
                            }
                        )
                        ledger_entry_ids_for_invoice.append(r.pk)
                        continue

                    meta = r.metadata or {}
                    rec_currency = meta.get("currency", r.unit)
                    converted = meta.get("billing_currency_fee_amount")
                    exchange_rate = meta.get("exchange_rate")
                    exchange_rate_date = meta.get("exchange_rate_date")

                    if converted and meta.get("billing_currency") == billing_currency:
                        fee_val = Decimal(str(converted))
                    elif rec_currency == billing_currency:
                        fee_val = Decimal(str(r.quantity))
                    elif (
                        rates_dict
                        and rates_date
                        and rec_currency in rates_dict
                        and billing_currency in rates_dict
                    ):
                        try:
                            rate = Decimal(str(rates_dict[billing_currency])) / Decimal(
                                str(rates_dict[rec_currency])
                            )
                            fee_val = (Decimal(str(r.quantity)) * rate).quantize(
                                Decimal("0.01")
                            )
                            exchange_rate = str(rate.quantize(Decimal("0.0001")))
                            exchange_rate_date = rates_date.isoformat()
                        except (
                            InvalidOperation,
                            ArithmeticError,
                            TypeError,
                        ) as exc:
                            raise InvoiceCalculationError(
                                f"Could not convert platform fee usage record {r.pk} "
                                f"from {rec_currency} to {billing_currency}."
                            ) from exc
                    else:
                        raise InvoiceCalculationError(
                            f"No exchange rate is available to convert platform fee "
                            f"{r.source_id} from {rec_currency} to {billing_currency}."
                        )

                    total_event_fee += fee_val
                    record_details.append(
                        {
                            "source_id": r.source_id,
                            "fee_amount": str(fee_val),
                            "currency": meta.get("currency", r.unit),
                            "fee_base": meta.get("fee_base"),
                            "fee_percent": meta.get("fee_percent"),
                            "exchange_rate": exchange_rate,
                            "exchange_rate_date": exchange_rate_date,
                        }
                    )
                    usage_record_ids_for_invoice.append(r.pk)

                if total_event_fee != Decimal("0.00") and record_details:
                    event_name = ev.name if ev else "General"
                    lines_data.append(
                        {
                            "line_type": InvoiceLineType.PLATFORM_FEE,
                            "event": ev,
                            "description": f"Ticket platform transaction fees ({event_name})",
                            "quantity": Decimal(len(record_details)),
                            "unit_price": (
                                total_event_fee / Decimal(len(record_details))
                            ).quantize(Decimal("0.01")),
                            "amount": total_event_fee.quantize(Decimal("0.01")),
                            "tier_version": (
                                active_sub.tier_version if active_sub else None
                            ),
                            "addon": None,
                            "usage_reference": f"platform_fees_event_{ev.slug if ev else 'org'}",
                            "calculation_metadata": {
                                "orders_count": len(record_details),
                                "total_fees": str(total_event_fee),
                                "billing_currency": billing_currency,
                                "fee_records": record_details,
                            },
                        }
                    )

            # 6. Aggregate Free Registration Overages
            free_reg_records = (
                UsageRecord.objects.select_for_update(of=("self",))
                .filter(
                    organizer=organizer,
                    capability="registration.free_allowance_per_event",
                    invoice__isnull=True,
                    occurred_at__gte=start_date,
                    occurred_at__lte=end_date,
                )
                .select_related("event")
            )

            events_reg_map = {}
            for rec in free_reg_records:
                ev = rec.event
                events_reg_map.setdefault(ev, []).append(rec)

            allowance = 0
            overage_price = Decimal("0.00")
            if active_sub and active_sub.tier_version:
                allowance_ent = active_sub.tier_version.entitlements.filter(
                    capability="registration.free_allowance_per_event"
                ).first()
                if allowance_ent and allowance_ent.value:
                    try:
                        allowance = int(allowance_ent.value)
                    except (ValueError, TypeError):
                        allowance = 0

                overage_ent = active_sub.tier_version.entitlements.filter(
                    capability="registration.free_overage_price"
                ).first()
                if overage_ent and overage_ent.value:
                    try:
                        overage_price = Decimal(str(overage_ent.value))
                    except Exception:
                        overage_price = Decimal("0.00")

            for ev, recs in events_reg_map.items():
                total_free_registrations = sum(r.quantity for r in recs)
                usage_record_ids_for_invoice.extend(r.pk for r in recs)
                if total_free_registrations <= allowance:
                    continue

                overage_quantity = total_free_registrations - allowance
                overage_amount = (overage_quantity * overage_price).quantize(
                    Decimal("0.01")
                )

                if overage_amount <= Decimal("0.00"):
                    continue

                event_name = ev.name if ev else "General"
                lines_data.append(
                    {
                        "line_type": InvoiceLineType.REGISTRATION_OVERAGE,
                        "event": ev,
                        "description": (
                            f"Free ticket registration overage ({event_name}): "
                            f"{int(overage_quantity)} registrations beyond {allowance} allowance"
                        ),
                        "quantity": overage_quantity,
                        "unit_price": overage_price,
                        "amount": overage_amount,
                        "tier_version": active_sub.tier_version if active_sub else None,
                        "addon": None,
                        "usage_reference": f"registration_overage_event_{ev.slug if ev else 'org'}",
                        "calculation_metadata": {
                            "total_free_registrations": int(total_free_registrations),
                            "included_allowance": allowance,
                            "overage_units": int(overage_quantity),
                            "unit_overage_price": str(overage_price),
                        },
                    }
                )

            # Discount plan and add-on lines covered by a redeemed voucher.
            lines_data.extend(build_voucher_credit_lines(organizer, lines_data))

            # 7. If no lines and empty invoices not permitted, return None
            if not lines_data and not allow_empty:
                return None

            # 8. Compute invoice totals
            subtotal = sum((item["amount"] for item in lines_data), Decimal("0.00"))
            tax = Decimal("0.00")
            total = subtotal + tax

            # 9. Persist Invoice and Lines atomically
            invoice_number = generate_invoice_number(organizer, start_date)

            invoice = BusinessInvoice.objects.create(
                organizer=organizer,
                invoice_number=invoice_number,
                billing_period_start=start_date,
                billing_period_end=end_date,
                currency=billing_currency,
                status=BusinessInvoiceStatus.DRAFT,
                subtotal=subtotal,
                tax=tax,
                total=total,
            )

            line_objects = [
                BusinessInvoiceLine(
                    invoice=invoice,
                    event=data["event"],
                    line_type=data["line_type"],
                    description=data["description"],
                    quantity=data["quantity"],
                    unit_price=data["unit_price"],
                    amount=data["amount"],
                    service_period_start=data.get("service_period_start", start_date),
                    service_period_end=data.get("service_period_end", end_date),
                    tier_version=data["tier_version"],
                    addon=data["addon"],
                    usage_reference=data["usage_reference"],
                    calculation_metadata=data["calculation_metadata"],
                )
                for data in lines_data
            ]
            BusinessInvoiceLine.objects.bulk_create(line_objects)
            PlatformFeeLedgerEntry.objects.filter(
                pk__in=ledger_entry_ids_for_invoice,
                invoice__isnull=True,
            ).update(invoice=invoice)
            UsageRecord.objects.filter(
                pk__in=usage_record_ids_for_invoice,
                invoice__isnull=True,
            ).update(invoice=invoice)

            return finalize_business_invoice(invoice)
    except IntegrityError:
        existing_query = BusinessInvoice.objects.filter(
            organizer=organizer,
            billing_period_start=start_date,
            billing_period_end=end_date,
        )
        if billing_currency:
            existing_query = existing_query.filter(currency=billing_currency)
        existing = existing_query.first()
        if existing:
            return existing
        raise


def generate_all_business_invoices(
    start_date: datetime = None,
    end_date: datetime = None,
    organizers=None,
) -> list[BusinessInvoice]:
    """
    Generates invoices for all organizers with active activity in the given billing period.
    Defaults to previous calendar month if dates not provided.
    """
    from eventyay.base.models import Organizer

    if start_date is None or end_date is None:
        start_date, end_date = get_previous_calendar_month_range()

    invoices = []
    for organizer in organizers if organizers is not None else Organizer.objects.all():
        try:
            inv = generate_business_invoice_for_organizer(
                organizer=organizer,
                start_date=start_date,
                end_date=end_date,
            )
            if inv:
                invoices.append(inv)
        except Exception as exc:
            logger.exception(
                "Failed to generate business invoice for organizer %s (%s - %s): %s",
                organizer.slug,
                start_date,
                end_date,
                exc,
            )

    return invoices
