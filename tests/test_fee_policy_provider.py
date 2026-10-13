import pytest
from decimal import Decimal
from unittest.mock import MagicMock

pytest.importorskip("eventyay.base.platform_fees")

from django.utils.timezone import now
from eventyay.base.models import Event, Organizer
from eventyay.base.platform_fees import PlatformFeePolicy, resolve_platform_fee_policy

from eventyay_business.models import Subscription, Tier, TierEntitlement, TierVersion


@pytest.mark.django_db
def test_business_provides_tier_platform_fee_policy():
    organizer = Organizer.objects.create(name="Fee organizer", slug="fee-organizer")
    tier = Tier.objects.create(slug="fee-tier", name="Fee tier")
    version = TierVersion.objects.create(tier=tier, version=1, published_at=now())
    TierEntitlement.objects.create(
        tier_version=version,
        capability="commerce.platform_fee_percent",
        value="3.50",
    )
    subscription = Subscription.objects.get(organizer=organizer)
    subscription.tier_version = version
    subscription.save(update_fields=["tier_version"])
    event = Event.objects.create(
        organizer=organizer,
        name="Fee event",
        slug="fee-event",
        currency="EUR",
        date_from=now(),
    )
    payment = MagicMock(payment_date=now())
    order = MagicMock()

    policy = resolve_platform_fee_policy(
        event=event,
        order=order,
        payment=payment,
    )

    assert policy == PlatformFeePolicy(
        percentage=Decimal("3.50"),
        maximum_fee=Decimal("0.00"),
        is_override=False,
        source="eventyay-business",
    )
