import logging
from datetime import datetime
from decimal import Decimal
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils.timezone import is_aware, make_aware, now
from eventyay.base.models import Organizer
from eventyay.base.settings import GlobalSettingsObject

from .models import (
    AddonStatus,
    BillingInterval,
    BusinessInvoice,
    BusinessInvoiceLine,
    BusinessInvoiceStatus,
    EventAddon,
    InvoiceLineType,
    OrganizerAddon,
    Subscription,
    SubscriptionStatus,
    UsageRecord,
)

logger = logging.getLogger(__name__)


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
    # 1. Check idempotency: if invoice already exists for this exact cycle, return it.
    existing = BusinessInvoice.objects.filter(
        organizer=organizer,
        billing_period_start=start_date,
        billing_period_end=end_date,
    ).first()
    if existing:
        return existing

    # 2. Determine billing currency and active subscription
    active_sub = (
        Subscription.objects.filter(
            organizer=organizer,
            starts_at__lte=end_date,
            status__in=[SubscriptionStatus.ACTIVE, SubscriptionStatus.PAST_DUE],
        )
        .filter(
            Q(ends_at__isnull=True)
            | Q(ends_at__gte=start_date)
            | Q(status=SubscriptionStatus.PAST_DUE)
        )
        .filter(Q(cancel_at__isnull=True) | Q(cancel_at__gte=start_date))
        .select_related("tier_version__tier")
        .order_by("-starts_at")
        .first()
    )

    billing_currency = (
        currency or (active_sub.currency if active_sub else None) or "EUR"
    )

    lines_data = []

    # 3. Aggregate Subscription Plan Fee
    if active_sub and active_sub.tier_version:
        tier_ver = active_sub.tier_version
        tier = tier_ver.tier

        # Find matching active price
        interval = active_sub.billing_interval or BillingInterval.MONTHLY
        matching_price = (
            tier_ver.prices.filter(
                active=True,
                billing_interval=interval,
                currency=billing_currency,
            ).first()
            or tier_ver.prices.filter(active=True, billing_interval=interval).first()
            or tier_ver.prices.filter(active=True).first()
        )

        if matching_price and matching_price.amount > Decimal("0.00"):
            lines_data.append(
                {
                    "line_type": InvoiceLineType.SUBSCRIPTION,
                    "event": None,
                    "description": f"Plan Subscription: {tier.name} ({matching_price.get_billing_interval_display()})",
                    "quantity": Decimal("1.00"),
                    "unit_price": matching_price.amount,
                    "amount": matching_price.amount,
                    "tier_version": tier_ver,
                    "addon": None,
                    "usage_reference": f"subscription_{active_sub.pk}",
                    "calculation_metadata": {
                        "subscription_id": active_sub.pk,
                        "tier_slug": tier.slug,
                        "tier_name": tier.name,
                        "tier_version": tier_ver.version,
                        "billing_interval": str(interval),
                        "price_currency": matching_price.currency,
                        "price_amount": str(matching_price.amount),
                    },
                }
            )

    # 4. Aggregate Active Add-ons
    # 4a. Organizer-level add-ons
    org_addons = (
        OrganizerAddon.objects.filter(
            organizer=organizer,
            status=AddonStatus.ACTIVE,
            starts_at__lte=end_date,
        )
        .filter(Q(ends_at__isnull=True) | Q(ends_at__gte=start_date))
        .filter(Q(cancel_at__isnull=True) | Q(cancel_at__gte=start_date))
        .filter(Q(canceled_at__isnull=True) | Q(canceled_at__gte=start_date))
        .select_related("addon")
    )

    for oa in org_addons:
        price = oa.price if oa.price is not None else oa.addon.price
        qty = Decimal(oa.quantity or 1)
        if price and price > Decimal("0.00"):
            line_amount = (price * qty).quantize(Decimal("0.01"))
            lines_data.append(
                {
                    "line_type": InvoiceLineType.ADDON,
                    "event": None,
                    "description": f"Add-on: {oa.addon.name} (Organisation)",
                    "quantity": qty,
                    "unit_price": price,
                    "amount": line_amount,
                    "tier_version": None,
                    "addon": oa.addon,
                    "usage_reference": f"organizer_addon_{oa.pk}",
                    "calculation_metadata": {
                        "organizer_addon_id": oa.pk,
                        "addon_id": oa.addon.pk,
                        "addon_name": oa.addon.name,
                        "pricing_mode": str(oa.addon.pricing_mode),
                        "quantity": int(qty),
                        "unit_price": str(price),
                    },
                }
            )

    # 4b. Event-level add-ons
    event_addons = (
        EventAddon.objects.filter(
            event__organizer=organizer,
            status=AddonStatus.ACTIVE,
            starts_at__lte=end_date,
        )
        .filter(Q(ends_at__isnull=True) | Q(ends_at__gte=start_date))
        .filter(Q(cancel_at__isnull=True) | Q(cancel_at__gte=start_date))
        .filter(Q(canceled_at__isnull=True) | Q(canceled_at__gte=start_date))
        .select_related("addon", "event")
    )

    for ea in event_addons:
        price = ea.price if ea.price is not None else ea.addon.price
        qty = Decimal(ea.quantity or 1)
        if price and price > Decimal("0.00"):
            line_amount = (price * qty).quantize(Decimal("0.01"))
            lines_data.append(
                {
                    "line_type": InvoiceLineType.ADDON,
                    "event": ea.event,
                    "description": f"Add-on: {ea.addon.name} (Event: {ea.event.name})",
                    "quantity": qty,
                    "unit_price": price,
                    "amount": line_amount,
                    "tier_version": None,
                    "addon": ea.addon,
                    "usage_reference": f"event_addon_{ea.pk}",
                    "calculation_metadata": {
                        "event_addon_id": ea.pk,
                        "event_slug": ea.event.slug,
                        "addon_id": ea.addon.pk,
                        "addon_name": ea.addon.name,
                        "pricing_mode": str(ea.addon.pricing_mode),
                        "quantity": int(qty),
                        "unit_price": str(price),
                    },
                }
            )

    # 5. Aggregate Paid-Order Platform Fees
    fee_records = UsageRecord.objects.filter(
        organizer=organizer,
        capability="commerce.platform_fee_percent",
        occurred_at__gte=start_date,
        occurred_at__lte=end_date,
    ).select_related("event")

    events_fee_map = {}
    for rec in fee_records:
        ev = rec.event
        events_fee_map.setdefault(ev, []).append(rec)

    gs = GlobalSettingsObject()
    rates_dict = gs.settings.get("ecb_rates_dict", as_type=dict)

    for ev, recs in events_fee_map.items():
        total_event_fee = Decimal("0.00")
        record_details = []

        for r in recs:
            meta = r.metadata or {}
            rec_currency = meta.get("currency", r.unit)
            converted = meta.get("billing_currency_fee_amount")

            if converted and meta.get("billing_currency") == billing_currency:
                fee_val = Decimal(str(converted))
            elif rec_currency == billing_currency:
                fee_val = Decimal(str(r.quantity))
            elif (
                rates_dict
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
                except Exception as exc:
                    logger.warning(
                        "Failed to convert platform fee %s from %s to %s: %s",
                        r.source_id,
                        rec_currency,
                        billing_currency,
                        exc,
                    )
                    continue
            else:
                logger.warning(
                    "Exchange rate unavailable to convert platform fee %s from %s to %s. Skipping record.",
                    r.source_id,
                    rec_currency,
                    billing_currency,
                )
                continue

            total_event_fee += fee_val
            record_details.append(
                {
                    "source_id": r.source_id,
                    "fee_amount": str(fee_val),
                    "currency": meta.get("currency", r.unit),
                    "fee_base": meta.get("fee_base"),
                    "fee_percent": meta.get("fee_percent"),
                }
            )

        if total_event_fee > Decimal("0.00") and record_details:
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
                    "tier_version": active_sub.tier_version if active_sub else None,
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
    free_reg_records = UsageRecord.objects.filter(
        organizer=organizer,
        capability="registration.free_allowance_per_event",
        occurred_at__gte=start_date,
        occurred_at__lte=end_date,
    ).select_related("event")

    events_reg_map = {}
    for rec in free_reg_records:
        ev = rec.event
        events_reg_map.setdefault(ev, []).append(rec)

    allowance = 100
    overage_price = Decimal("0.00")
    if active_sub and active_sub.tier_version:
        t_ver = active_sub.tier_version
        allowance_ent = t_ver.entitlements.filter(
            capability="registration.free_allowance_per_event"
        ).first()
        if allowance_ent and allowance_ent.value:
            try:
                allowance = int(allowance_ent.value)
            except (ValueError, TypeError):
                allowance = 100

        overage_ent = t_ver.entitlements.filter(
            capability="registration.free_overage_price"
        ).first()
        if overage_ent and overage_ent.value:
            try:
                overage_price = Decimal(str(overage_ent.value))
            except Exception:
                overage_price = Decimal("0.00")

    for ev, recs in events_reg_map.items():
        total_free_registrations = sum(Decimal(str(r.quantity)) for r in recs)
        overage_quantity = max(
            Decimal("0.00"), total_free_registrations - Decimal(allowance)
        )

        if overage_quantity > Decimal("0.00") and overage_price > Decimal("0.00"):
            overage_amount = (overage_quantity * overage_price).quantize(
                Decimal("0.01")
            )
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

    # 7. If no lines and empty invoices not permitted, return None
    if not lines_data and not allow_empty:
        return None

    # 8. Compute invoice totals
    subtotal = sum((item["amount"] for item in lines_data), Decimal("0.00"))
    tax = Decimal("0.00")
    total = subtotal + tax

    # 9. Persist Invoice and Lines atomically
    try:
        with transaction.atomic():
            Organizer.objects.select_for_update().filter(pk=organizer.pk).first()

            # Re-check idempotency inside lock to prevent race conditions
            existing = BusinessInvoice.objects.filter(
                organizer=organizer,
                billing_period_start=start_date,
                billing_period_end=end_date,
            ).first()
            if existing:
                return existing

            invoice_number = generate_invoice_number(organizer, start_date)

            invoice = BusinessInvoice.objects.create(
                organizer=organizer,
                invoice_number=invoice_number,
                billing_period_start=start_date,
                billing_period_end=end_date,
                currency=billing_currency,
                status=(
                    BusinessInvoiceStatus.OPEN
                    if total > Decimal("0.00")
                    else BusinessInvoiceStatus.PAID
                ),
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
                    tier_version=data["tier_version"],
                    addon=data["addon"],
                    usage_reference=data["usage_reference"],
                    calculation_metadata=data["calculation_metadata"],
                )
                for data in lines_data
            ]
            BusinessInvoiceLine.objects.bulk_create(line_objects)

        return invoice
    except IntegrityError:
        existing = BusinessInvoice.objects.filter(
            organizer=organizer,
            billing_period_start=start_date,
            billing_period_end=end_date,
        ).first()
        if existing:
            return existing
        raise


def generate_all_business_invoices(
    start_date: datetime = None, end_date: datetime = None
) -> list[BusinessInvoice]:
    """
    Generates invoices for all organizers with active activity in the given billing period.
    Defaults to previous calendar month if dates not provided.
    """
    from eventyay.base.models import Organizer

    if start_date is None or end_date is None:
        start_date, end_date = get_previous_calendar_month_range()

    invoices = []
    for organizer in Organizer.objects.all():
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
