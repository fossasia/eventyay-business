import pytest
from datetime import timedelta
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.db.models import Sum
from django.utils.timezone import now
from django_scopes import scope
from eventyay.base.models import (
    Event,
    Order,
    OrderPayment,
    OrderPosition,
    OrderRefund,
    Organizer,
    Product,
)

from eventyay_business.billing_overview import build_organizer_billing_overview
from eventyay_business.invoicing_service import generate_business_invoice_for_organizer
from eventyay_business.models import (
    CountryFeeSetting,
    InvoiceLineType,
    PlatformFeeLedgerEntry,
    PlatformFeeLedgerEntryType,
    Subscription,
    Tier,
    TierEntitlement,
    TierVersion,
    UsageRecord,
)
from eventyay_business.platform_fee_ledger import (
    BILLING_CUTOVER_SETTING,
    organizer_billing_cutover_at,
    organizer_consolidated_billing_enabled,
)
from eventyay_business.signals import (
    record_platform_fee_on_order_canceled,
    record_platform_fee_on_order_paid,
    record_platform_fee_on_refund,
)


@pytest.fixture
def organizer_with_fee_tier(db):
    organizer = Organizer.objects.create(name="Fee Org", slug="fee-org")
    organizer.settings.set(
        BILLING_CUTOVER_SETTING, (now() - timedelta(days=60)).isoformat()
    )
    tier = Tier.objects.create(slug="paid", name="Paid Tier")
    version = TierVersion.objects.create(tier=tier, version=1, published_at=now())
    TierEntitlement.objects.create(
        tier_version=version,
        capability="commerce.platform_fee_percent",
        value="5.0",
    )
    subscription = Subscription.objects.get(organizer=organizer)
    subscription.tier_version = version
    subscription.starts_at = now() - timedelta(days=30)
    subscription.save()
    return organizer


def create_paid_order(
    organizer,
    *,
    code="ORDER1",
    currency="USD",
    position_price=Decimal("110.00"),
    tax_value=Decimal("10.00"),
    payment_amounts=(Decimal("110.00"),),
    application_fees=(),
):
    event = Event.objects.create(
        organizer=organizer,
        name=f"Event {code}",
        slug=code.lower(),
        currency=currency,
        date_from=now(),
    )
    with scope(organizer=organizer, event=event):
        product = Product.objects.create(
            event=event,
            name="Ticket",
            category=None,
            default_price=position_price,
            admission=True,
        )
        order = Order.objects.create(
            code=code,
            event=event,
            email="buyer@example.com",
            status=Order.STATUS_PAID,
            datetime=now(),
            expires=now() + timedelta(days=10),
            total=sum(payment_amounts, Decimal("0.00")),
            locale="en",
        )
        OrderPosition.objects.create(
            order=order,
            product=product,
            variation=None,
            price=position_price,
            tax_value=tax_value,
            tax_rate=Decimal("0.00"),
            attendee_name_parts={"full_name": "Buyer"},
        )
        payments = []
        for index, amount in enumerate(payment_amounts, start=1):
            payment = OrderPayment.objects.create(
                order=order,
                local_id=index,
                state=OrderPayment.PAYMENT_STATE_CONFIRMED,
                amount=amount,
                payment_date=now() + timedelta(seconds=index),
                provider="stripe",
            )
            if index <= len(application_fees):
                payment.info_data = {
                    "application_fee_amount": application_fees[index - 1]
                }
                payment.save(update_fields=["info"])
            payments.append(payment)
    return event, order, payments


@pytest.mark.django_db
def test_paid_order_records_reproducible_tier_fee(organizer_with_fee_tier):
    event, order, payments = create_paid_order(organizer_with_fee_tier)

    record_platform_fee_on_order_paid(sender=event, order=order)

    entry = PlatformFeeLedgerEntry.objects.get()
    assert entry.payment == payments[0]
    assert entry.fee_base == Decimal("100.00")
    assert entry.percentage == Decimal("5.0000")
    assert entry.calculated_fee == Decimal("5.00")
    assert entry.amount == Decimal("5.00")
    assert entry.policy_source == "tier:paid:v1"
    assert entry.billing_period == payments[0].payment_date.date().replace(day=1)


@pytest.mark.django_db
def test_zero_policy_creates_no_ledger_debt(organizer_with_fee_tier):
    subscription = Subscription.objects.get(organizer=organizer_with_fee_tier)
    entitlement = subscription.tier_version.entitlements.get(
        capability="commerce.platform_fee_percent"
    )
    entitlement.value = "0"
    entitlement.save(update_fields=["value"])
    event, order, _ = create_paid_order(organizer_with_fee_tier, code="ZEROFEE")

    record_platform_fee_on_order_paid(sender=event, order=order)

    assert not PlatformFeeLedgerEntry.objects.exists()


@pytest.mark.django_db
def test_country_override_caps_order_fee(organizer_with_fee_tier):
    CountryFeeSetting.objects.create(
        country="FR",
        currency="EUR",
        service_fee_percent=Decimal("2.00"),
        maximum_fee=Decimal("15.00"),
    )
    event, order, _ = create_paid_order(
        organizer_with_fee_tier,
        code="CAPPED",
        currency="EUR",
        position_price=Decimal("2000.00"),
        tax_value=Decimal("0.00"),
        payment_amounts=(Decimal("2000.00"),),
    )
    event.settings.set("invoice_address_from_country", "FR")

    record_platform_fee_on_order_paid(sender=event, order=order)

    entry = PlatformFeeLedgerEntry.objects.get()
    assert entry.amount == Decimal("15.00")
    assert entry.maximum_fee == Decimal("15.00")
    assert entry.is_override is True
    assert entry.policy_source == "country_override"


@pytest.mark.django_db
def test_partial_payments_share_fee_and_respect_order_cap(organizer_with_fee_tier):
    CountryFeeSetting.objects.create(
        country="US",
        currency="USD",
        service_fee_percent=Decimal("10.00"),
        maximum_fee=Decimal("8.00"),
    )
    event, order, payments = create_paid_order(
        organizer_with_fee_tier,
        code="PARTIAL",
        position_price=Decimal("100.00"),
        tax_value=Decimal("0.00"),
        payment_amounts=(Decimal("40.00"), Decimal("60.00")),
    )
    event.settings.set("invoice_address_from_country", "US")

    record_platform_fee_on_order_paid(sender=event, order=order)
    record_platform_fee_on_order_paid(sender=event, order=order)

    entries = list(PlatformFeeLedgerEntry.objects.order_by("payment_id"))
    assert [entry.payment for entry in entries] == payments
    assert [entry.fee_base for entry in entries] == [
        Decimal("40.00"),
        Decimal("60.00"),
    ]
    assert [entry.amount for entry in entries] == [
        Decimal("4.00"),
        Decimal("4.00"),
    ]
    assert sum((entry.amount for entry in entries), Decimal("0.00")) == Decimal("8.00")


@pytest.mark.django_db
def test_historical_application_fee_is_not_accrued_again(organizer_with_fee_tier):
    event, order, _ = create_paid_order(
        organizer_with_fee_tier,
        code="HISTORICAL",
        application_fees=(500,),
    )

    record_platform_fee_on_order_paid(sender=event, order=order)

    entry = PlatformFeeLedgerEntry.objects.get()
    assert entry.calculated_fee == Decimal("5.00")
    assert entry.collected_at_checkout == Decimal("5.00")
    assert entry.amount == Decimal("0.00")
    assert entry.metadata["collection_path"] == "historical_immediate"


@pytest.mark.django_db
def test_pre_cutover_payment_without_fee_is_not_accrued(organizer_with_fee_tier):
    cutover_at = now() + timedelta(days=1)
    organizer_with_fee_tier.settings.set(
        BILLING_CUTOVER_SETTING, cutover_at.isoformat()
    )
    event, order, _ = create_paid_order(
        organizer_with_fee_tier,
        code="PRECUTOVER",
    )

    record_platform_fee_on_order_paid(sender=event, order=order)

    entry = PlatformFeeLedgerEntry.objects.get()
    assert entry.calculated_fee == Decimal("5.00")
    assert entry.amount == Decimal("0.00")
    assert entry.metadata["collection_path"] == "historical_pre_cutover"
    assert entry.metadata["cutover_relation"] == "pre_cutover"
    assert entry.metadata["billing_cutover_at"] == cutover_at.isoformat()
    assert entry.metadata["seconds_from_cutover"] < 0
    assert entry.metadata["unreconciled_historical_amount"] == "5.00"


@pytest.mark.django_db
def test_missing_cutover_does_not_enable_accrual(organizer_with_fee_tier):
    organizer_with_fee_tier.settings.set(BILLING_CUTOVER_SETTING, "")
    event, order, _ = create_paid_order(
        organizer_with_fee_tier,
        code="NOCUTOVER",
    )

    record_platform_fee_on_order_paid(sender=event, order=order)

    entry = PlatformFeeLedgerEntry.objects.get()
    assert entry.amount == Decimal("0.00")
    assert entry.metadata["collection_path"] == "migration_not_enabled"
    assert entry.metadata["cutover_relation"] == "not_configured"


@pytest.mark.django_db
def test_cutover_enables_consolidated_billing(organizer_with_fee_tier):
    cutover_at = now() - timedelta(minutes=1)
    organizer_with_fee_tier.settings.set(
        BILLING_CUTOVER_SETTING,
        cutover_at.isoformat(),
    )

    assert organizer_consolidated_billing_enabled(organizer_with_fee_tier) is True


@pytest.mark.django_db
def test_malformed_cutover_timestamp_is_treated_as_not_configured(
    organizer_with_fee_tier,
):
    organizer_with_fee_tier.settings.set(
        BILLING_CUTOVER_SETTING,
        "2026-99-99T25:61:00",
    )

    assert organizer_billing_cutover_at(organizer_with_fee_tier) is None
    assert organizer_consolidated_billing_enabled(organizer_with_fee_tier) is False


@pytest.mark.django_db
def test_legacy_usage_debt_is_not_accrued_again(organizer_with_fee_tier):
    event, order, _ = create_paid_order(
        organizer_with_fee_tier,
        code="LEGACYDEBT",
    )
    UsageRecord.objects.create(
        organizer=organizer_with_fee_tier,
        event=event,
        capability="commerce.platform_fee_percent",
        quantity=Decimal("5.00"),
        unit="USD",
        occurred_at=now(),
        source_type="order",
        source_id=order.code,
        idempotency_key=f"order_{order.code}_platform_fee",
    )

    record_platform_fee_on_order_paid(sender=event, order=order)

    entry = PlatformFeeLedgerEntry.objects.get()
    assert entry.calculated_fee == Decimal("5.00")
    assert entry.amount == Decimal("0.00")
    assert entry.metadata["collection_path"] == "legacy_usage_record"


@pytest.mark.django_db
def test_partial_and_full_refunds_create_linked_negative_entries(
    organizer_with_fee_tier,
):
    event, order, payments = create_paid_order(
        organizer_with_fee_tier,
        code="REFUNDS",
        position_price=Decimal("100.00"),
        tax_value=Decimal("0.00"),
        payment_amounts=(Decimal("100.00"),),
    )
    record_platform_fee_on_order_paid(sender=event, order=order)
    charge = PlatformFeeLedgerEntry.objects.get(
        entry_type=PlatformFeeLedgerEntryType.CHARGE
    )

    with scope(organizer=organizer_with_fee_tier, event=event):
        partial = OrderRefund.objects.create(
            order=order,
            payment=payments[0],
            state=OrderRefund.REFUND_STATE_DONE,
            source=OrderRefund.REFUND_SOURCE_ADMIN,
            amount=Decimal("25.00"),
            execution_date=now(),
            provider="stripe",
        )
        final = OrderRefund.objects.create(
            order=order,
            payment=payments[0],
            state=OrderRefund.REFUND_STATE_DONE,
            source=OrderRefund.REFUND_SOURCE_ADMIN,
            amount=Decimal("75.00"),
            execution_date=now(),
            provider="stripe",
        )

    record_platform_fee_on_refund(sender=OrderRefund, instance=partial)
    record_platform_fee_on_refund(sender=OrderRefund, instance=final)

    refunds = PlatformFeeLedgerEntry.objects.filter(
        entry_type=PlatformFeeLedgerEntryType.REFUND
    )
    assert refunds.count() == 2
    assert all(entry.related_entry == charge for entry in refunds)
    assert refunds.aggregate(total=Sum("amount"))["total"] == Decimal("-5.00")


@pytest.mark.django_db
def test_current_charge_estimate_uses_ledger_refund_adjustments(
    organizer_with_fee_tier,
):
    event, order, payments = create_paid_order(
        organizer_with_fee_tier,
        code="CURRENTCHARGES",
        position_price=Decimal("100.00"),
        tax_value=Decimal("0.00"),
        payment_amounts=(Decimal("100.00"),),
    )
    record_platform_fee_on_order_paid(sender=event, order=order)
    with scope(organizer=organizer_with_fee_tier, event=event):
        refund = OrderRefund.objects.create(
            order=order,
            payment=payments[0],
            state=OrderRefund.REFUND_STATE_DONE,
            source=OrderRefund.REFUND_SOURCE_ADMIN,
            amount=Decimal("40.00"),
            execution_date=now(),
            provider="stripe",
        )
    record_platform_fee_on_refund(sender=OrderRefund, instance=refund)

    overview = build_organizer_billing_overview(organizer_with_fee_tier)

    assert len(overview["fee_rows"]) == 1
    assert overview["fee_rows"][0]["calculated_fee"] == Decimal("5.00")
    assert overview["fee_rows"][0]["adjustment"] == Decimal("-2.00")
    assert overview["fee_rows"][0]["net"] == Decimal("3.00")
    usd_total = next(
        total for total in overview["totals"] if total["currency"] == "USD"
    )
    assert usd_total["ticket_fees"] == Decimal("3.00")


@pytest.mark.django_db
def test_payment_and_order_refunds_track_separate_targets(organizer_with_fee_tier):
    event, order, payments = create_paid_order(
        organizer_with_fee_tier,
        code="MIXEDREFUNDS",
        position_price=Decimal("200.00"),
        tax_value=Decimal("0.00"),
        payment_amounts=(Decimal("100.00"), Decimal("100.00")),
    )
    record_platform_fee_on_order_paid(sender=event, order=order)
    charges = list(
        PlatformFeeLedgerEntry.objects.filter(
            entry_type=PlatformFeeLedgerEntryType.CHARGE
        ).order_by("payment_id")
    )

    with scope(organizer=organizer_with_fee_tier, event=event):
        payment_refund = OrderRefund.objects.create(
            order=order,
            payment=payments[0],
            state=OrderRefund.REFUND_STATE_DONE,
            source=OrderRefund.REFUND_SOURCE_ADMIN,
            amount=Decimal("40.00"),
            execution_date=now(),
            provider="stripe",
        )
        order_refund = OrderRefund.objects.create(
            order=order,
            state=OrderRefund.REFUND_STATE_DONE,
            source=OrderRefund.REFUND_SOURCE_ADMIN,
            amount=Decimal("40.00"),
            execution_date=now(),
            provider="stripe",
        )

    record_platform_fee_on_refund(sender=OrderRefund, instance=order_refund)
    record_platform_fee_on_refund(sender=OrderRefund, instance=payment_refund)

    assert -charges[0].adjustments.aggregate(total=Sum("amount"))["total"] == Decimal(
        "3.00"
    )
    assert -charges[1].adjustments.aggregate(total=Sum("amount"))["total"] == Decimal(
        "1.00"
    )


@pytest.mark.django_db
def test_order_refund_uses_remaining_charge_capacity(organizer_with_fee_tier):
    event, order, payments = create_paid_order(
        organizer_with_fee_tier,
        code="FULLMIXEDREFUND",
        position_price=Decimal("200.00"),
        tax_value=Decimal("0.00"),
        payment_amounts=(Decimal("100.00"), Decimal("100.00")),
    )
    record_platform_fee_on_order_paid(sender=event, order=order)
    charges = list(
        PlatformFeeLedgerEntry.objects.filter(
            entry_type=PlatformFeeLedgerEntryType.CHARGE
        ).order_by("payment_id")
    )

    with scope(organizer=organizer_with_fee_tier, event=event):
        payment_refund = OrderRefund.objects.create(
            order=order,
            payment=payments[0],
            state=OrderRefund.REFUND_STATE_DONE,
            source=OrderRefund.REFUND_SOURCE_ADMIN,
            amount=Decimal("100.00"),
            execution_date=now(),
            provider="stripe",
        )
    record_platform_fee_on_refund(sender=OrderRefund, instance=payment_refund)

    with scope(organizer=organizer_with_fee_tier, event=event):
        order_refund = OrderRefund.objects.create(
            order=order,
            state=OrderRefund.REFUND_STATE_DONE,
            source=OrderRefund.REFUND_SOURCE_ADMIN,
            amount=Decimal("100.00"),
            execution_date=now(),
            provider="stripe",
        )
    record_platform_fee_on_refund(sender=OrderRefund, instance=order_refund)

    for charge in charges:
        reversed_amount = -(
            charge.adjustments.aggregate(total=Sum("amount"))["total"] or Decimal("0")
        )
        assert reversed_amount == charge.amount


@pytest.mark.django_db
def test_invoice_claims_ledger_entry_once(organizer_with_fee_tier):
    event, order, _ = create_paid_order(
        organizer_with_fee_tier,
        code="INVOICED",
        position_price=Decimal("100.00"),
        tax_value=Decimal("0.00"),
        payment_amounts=(Decimal("100.00"),),
    )
    record_platform_fee_on_order_paid(sender=event, order=order)
    entry = PlatformFeeLedgerEntry.objects.get()
    period_start = entry.occurred_at.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    next_month = (period_start + timedelta(days=32)).replace(day=1)
    period_end = next_month - timedelta(microseconds=1)

    invoice = generate_business_invoice_for_organizer(
        organizer_with_fee_tier,
        period_start,
        period_end,
        currency="USD",
    )

    entry.refresh_from_db()
    assert entry.invoice == invoice
    assert invoice.lines.get(line_type=InvoiceLineType.PLATFORM_FEE).amount == Decimal(
        "5.00"
    )
    assert (
        generate_business_invoice_for_organizer(
            organizer_with_fee_tier,
            period_start,
            period_end,
            currency="USD",
        )
        == invoice
    )


@pytest.mark.django_db
def test_invoice_carries_forward_unassigned_older_entry(organizer_with_fee_tier):
    event, order, _ = create_paid_order(
        organizer_with_fee_tier,
        code="LATEENTRY",
        position_price=Decimal("100.00"),
        tax_value=Decimal("0.00"),
        payment_amounts=(Decimal("100.00"),),
    )
    record_platform_fee_on_order_paid(sender=event, order=order)
    entry = PlatformFeeLedgerEntry.objects.get()
    period_start = entry.occurred_at + timedelta(days=1)
    period_end = period_start + timedelta(days=31)

    invoice = generate_business_invoice_for_organizer(
        organizer_with_fee_tier,
        period_start,
        period_end,
        currency="USD",
    )

    entry.refresh_from_db()
    assert entry.invoice == invoice
    assert invoice.lines.get(line_type=InvoiceLineType.PLATFORM_FEE).amount == Decimal(
        "5.00"
    )


@pytest.mark.django_db
def test_order_cancellation_reverses_remaining_fee_once(organizer_with_fee_tier):
    event, order, _ = create_paid_order(
        organizer_with_fee_tier,
        code="CANCELED",
        position_price=Decimal("100.00"),
        tax_value=Decimal("0.00"),
        payment_amounts=(Decimal("100.00"),),
    )
    record_platform_fee_on_order_paid(sender=event, order=order)
    order.status = Order.STATUS_CANCELED
    order.cancellation_date = now()
    order.save(update_fields=["status", "cancellation_date"])

    record_platform_fee_on_order_canceled(sender=event, order=order)
    record_platform_fee_on_order_canceled(sender=event, order=order)

    cancellation = PlatformFeeLedgerEntry.objects.get(
        entry_type=PlatformFeeLedgerEntryType.CANCELLATION
    )
    assert cancellation.amount == Decimal("-5.00")
    assert cancellation.related_entry.amount == Decimal("5.00")


@pytest.mark.django_db
def test_ledger_financial_fields_are_immutable(organizer_with_fee_tier):
    event, order, _ = create_paid_order(
        organizer_with_fee_tier,
        code="IMMUTABLE",
        position_price=Decimal("100.00"),
        tax_value=Decimal("0.00"),
        payment_amounts=(Decimal("100.00"),),
    )
    record_platform_fee_on_order_paid(sender=event, order=order)
    entry = PlatformFeeLedgerEntry.objects.get()
    entry.amount = Decimal("1.00")

    with pytest.raises(ValidationError):
        entry.save(update_fields=["amount"])
