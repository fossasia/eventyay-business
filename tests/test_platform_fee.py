import pytest
from decimal import Decimal
from django.utils.timezone import now
from unittest.mock import MagicMock

from eventyay_business.models import (
    Subscription,
    Tier,
    TierEntitlement,
    TierVersion,
    UsageRecord,
)
from eventyay_business.signals import record_platform_fee_on_order_paid


@pytest.fixture
def organizer_with_fee_tier(db):
    from eventyay.base.models import Organizer

    org = Organizer.objects.create(name="Fee Org", slug="fee-org")

    tier = Tier.objects.create(slug="paid", name="Paid Tier")
    version = TierVersion.objects.create(tier=tier, version=1, published_at=now())

    TierEntitlement.objects.create(
        tier_version=version, capability="commerce.platform_fee_percent", value="5.0"
    )

    sub = Subscription.objects.get(organizer=org)
    sub.tier_version = version
    sub.save()

    return org


@pytest.mark.django_db
def test_platform_fee_calculation(organizer_with_fee_tier):
    from eventyay.base.models import Event

    event = Event.objects.create(
        organizer=organizer_with_fee_tier,
        name="Test Event",
        slug="test-event",
        currency="USD",
        date_from=now(),
    )

    order = MagicMock()
    order.code = "ABCXYZ"
    order.total = Decimal("120.00")

    pos1 = MagicMock()
    pos1.price = Decimal("100.00")
    pos1.tax_value = Decimal("10.00")  # net = 90

    pos2 = MagicMock()
    pos2.price = Decimal("20.00")
    pos2.tax_value = Decimal("0.00")  # net = 20

    order.positions.all.return_value = [pos1, pos2]

    record_platform_fee_on_order_paid(sender=event, order=order)

    assert UsageRecord.objects.count() == 1
    record = UsageRecord.objects.first()

    assert record.organizer == organizer_with_fee_tier
    assert record.event == event
    assert record.capability == "commerce.platform_fee_percent"
    assert record.unit == "USD"
    assert record.source_type == "order"
    assert record.source_id == "ABCXYZ"

    # Base = 90 + 20 = 110
    # Fee = 110 * 5.0% = 5.50
    assert record.quantity == Decimal("5.50")
    assert record.metadata["fee_base"] == "110.00"
    assert record.metadata["fee_percent"] == "5.0"
    assert record.metadata["order_total"] == "120.00"


@pytest.mark.django_db
def test_platform_fee_zero_percent(organizer_with_fee_tier):
    # Update fee to 0%
    sub = Subscription.objects.get(organizer=organizer_with_fee_tier)
    ent = sub.tier_version.entitlements.first()
    ent.value = "0.0"
    ent.save()

    from eventyay.base.models import Event

    event = Event.objects.create(
        organizer=organizer_with_fee_tier,
        name="Test Event 2",
        slug="test-event-2",
        currency="EUR",
        date_from=now(),
    )

    order = MagicMock()
    order.code = "ABCDEF"
    order.positions.all.return_value = [
        MagicMock(price=Decimal("100"), tax_value=Decimal("0"))
    ]

    record_platform_fee_on_order_paid(sender=event, order=order)

    # Should not create a usage record for 0% fee
    assert UsageRecord.objects.count() == 0


@pytest.mark.django_db
def test_platform_fee_currency_conversion(organizer_with_fee_tier):
    from eventyay.base.models import Event
    from eventyay.base.settings import GlobalSettingsObject

    gs = GlobalSettingsObject()
    gs.settings.ecb_rates_date = "2026-09-11"
    # Event is USD, Subscription is EUR
    # 1 EUR = 1.10 USD
    gs.settings.ecb_rates_dict = {"EUR": "1.0000", "USD": "1.1000"}

    sub = Subscription.objects.get(organizer=organizer_with_fee_tier)
    sub.currency = "EUR"
    sub.save()

    event = Event.objects.create(
        organizer=organizer_with_fee_tier,
        name="Test Event 3",
        slug="test-event-3",
        currency="USD",
        date_from=now(),
    )

    order = MagicMock()
    order.code = "CONVERT"
    order.total = Decimal("110.00")

    pos1 = MagicMock()
    pos1.price = Decimal("110.00")
    pos1.tax_value = Decimal("0.00")

    order.positions.all.return_value = [pos1]

    record_platform_fee_on_order_paid(sender=event, order=order)

    assert UsageRecord.objects.count() == 1
    record = UsageRecord.objects.first()

    assert record.unit == "USD"
    # Base = 110. Fee = 110 * 5% = 5.50 USD.
    assert record.quantity == Decimal("5.50")

    # 1 USD = 1/1.10 EUR = 0.9091 EUR
    # Converted fee = 5.50 * 0.9091 = 5.00 EUR
    assert record.metadata["billing_currency"] == "EUR"
    assert record.metadata["exchange_rate"] == "0.9091"
    assert record.metadata["exchange_rate_date"] == "2026-09-11"
    assert record.metadata["billing_currency_fee_amount"] == "5.00"


@pytest.mark.django_db
def test_platform_fee_idempotent_repeated_delivery(organizer_with_fee_tier):
    from eventyay.base.models import Event

    event = Event.objects.create(
        organizer=organizer_with_fee_tier,
        name="Test Event Repeat",
        slug="test-event-repeat",
        currency="USD",
        date_from=now(),
    )

    order = MagicMock()
    order.code = "REPEAT123"
    order.total = Decimal("100.00")
    pos = MagicMock(price=Decimal("100.00"), tax_value=Decimal("0.00"))
    order.positions.all.return_value = [pos]

    # Invoke twice to test idempotent processing
    record_platform_fee_on_order_paid(sender=event, order=order)
    record_platform_fee_on_order_paid(sender=event, order=order)

    assert (
        UsageRecord.objects.filter(
            idempotency_key="order_REPEAT123_platform_fee"
        ).count()
        == 1
    )


@pytest.mark.django_db
def test_platform_fee_currency_conversion_missing_rates_stops_persistence(
    organizer_with_fee_tier,
):
    from eventyay.base.models import Event
    from eventyay.base.settings import GlobalSettingsObject

    gs = GlobalSettingsObject()
    gs.settings.ecb_rates_date = "2026-09-11"
    # Event is JPY, Subscription is EUR, but JPY is missing from rates
    gs.settings.ecb_rates_dict = {"EUR": "1.0000", "USD": "1.1000"}

    sub = Subscription.objects.get(organizer=organizer_with_fee_tier)
    sub.currency = "EUR"
    sub.save()

    event = Event.objects.create(
        organizer=organizer_with_fee_tier,
        name="Test Event JPY",
        slug="test-event-jpy",
        currency="JPY",
        date_from=now(),
    )

    order = MagicMock()
    order.code = "MISSINGRATE"
    order.total = Decimal("10000.00")
    pos = MagicMock(price=Decimal("10000.00"), tax_value=Decimal("0.00"))
    order.positions.all.return_value = [pos]

    record_platform_fee_on_order_paid(sender=event, order=order)

    # Persistence must stop when exchange rate is unavailable
    assert (
        UsageRecord.objects.filter(
            idempotency_key="order_MISSINGRATE_platform_fee"
        ).count()
        == 0
    )


@pytest.mark.django_db
def test_platform_fee_uses_payment_timestamp(organizer_with_fee_tier):
    import datetime
    from django.utils.timezone import make_aware
    from eventyay.base.models import Event

    event = Event.objects.create(
        organizer=organizer_with_fee_tier,
        name="Test Event Payment Date",
        slug="test-event-payment-date",
        currency="USD",
        date_from=now(),
    )

    confirmed_date = make_aware(datetime.datetime(2026, 8, 15, 10, 0, 0))

    sub = Subscription.objects.get(organizer=organizer_with_fee_tier)
    sub.starts_at = confirmed_date - datetime.timedelta(days=1)
    sub.save()

    order = MagicMock()
    order.code = "PAYDATE"
    order.total = Decimal("100.00")
    pos = MagicMock(price=Decimal("100.00"), tax_value=Decimal("0.00"))
    order.positions.all.return_value = [pos]

    payment = MagicMock()
    payment.payment_date = confirmed_date

    record_platform_fee_on_order_paid(sender=event, order=order, payment=payment)

    record = UsageRecord.objects.get(idempotency_key="order_PAYDATE_platform_fee")
    assert record.occurred_at == confirmed_date
