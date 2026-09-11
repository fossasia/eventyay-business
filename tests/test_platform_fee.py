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
