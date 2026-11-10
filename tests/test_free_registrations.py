import pytest
from datetime import datetime
from decimal import Decimal
from django.utils.timezone import make_aware, now
from eventyay.base.models import Event, Organizer

from eventyay_business.billing_overview import build_organizer_billing_overview
from eventyay_business.free_registrations import FreeRegistrationUsage
from eventyay_business.invoicing_service import generate_business_invoice_for_organizer
from eventyay_business.models import (
    AddonDefinition,
    AddonStatus,
    EventAddon,
    InvoiceLineType,
    OrganizerAddon,
    Subscription,
    Tier,
    TierEntitlement,
    TierPrice,
    TierVersion,
    UsageRecord,
)

CAPABILITY = "registration.free_allowance_per_event"
JULY = (
    make_aware(datetime(2026, 7, 1, 0, 0, 0)),
    make_aware(datetime(2026, 7, 31, 23, 59, 59)),
)
AUGUST = (
    make_aware(datetime(2026, 8, 1, 0, 0, 0)),
    make_aware(datetime(2026, 8, 31, 23, 59, 59)),
)


@pytest.fixture
def setup(db):
    """Tier with 50 free registrations per event and 0.50 EUR overage."""
    org = Organizer.objects.create(name="Acme Corp", slug="acme")
    tier = Tier.objects.create(slug="pro", name="Pro Tier")
    version = TierVersion.objects.create(tier=tier, version=1, published_at=now())
    TierPrice.objects.create(
        tier_version=version,
        billing_interval="monthly",
        amount=Decimal("49.00"),
        currency="EUR",
    )
    TierEntitlement.objects.create(
        tier_version=version, capability=CAPABILITY, value="50"
    )
    TierEntitlement.objects.create(
        tier_version=version,
        capability="registration.free_overage_price",
        value="0.50",
    )
    sub = Subscription.objects.get(organizer=org)
    sub.tier_version = version
    sub.currency = "EUR"
    sub.billing_interval = "monthly"
    sub.starts_at = make_aware(datetime(2026, 1, 1, 0, 0, 0))
    sub.save()
    event = Event.objects.create(
        organizer=org, name="Acme Conf", slug="conf", currency="EUR", date_from=now()
    )
    return org, event


def record_usage(org, event, quantity, occurred_at, key):
    return UsageRecord.objects.create(
        organizer=org,
        event=event,
        capability=CAPABILITY,
        quantity=Decimal(quantity),
        unit="registrations",
        source_type="order",
        source_id=key,
        idempotency_key=key,
        occurred_at=occurred_at,
    )


def allowance_addon(slug, scope, value="25"):
    return AddonDefinition.objects.create(
        slug=slug,
        name="Extra free registrations",
        assignment_scope=scope,
        pricing_mode="one_time",
        price=Decimal("0.00"),
        currency="EUR",
        capability=CAPABILITY,
        entitlement_value=value,
        active=True,
    )


def overage_line(invoice, event=None):
    lines = invoice.lines.filter(line_type=InvoiceLineType.REGISTRATION_OVERAGE)
    if event:
        lines = lines.filter(event=event)
    return lines.first()


@pytest.mark.parametrize(
    "period,previous,tier,addon,remaining,overage",
    [
        (80, 0, 50, 0, 50, 30),
        (30, 40, 50, 0, 10, 20),
        (30, 60, 50, 0, 0, 30),
        (30, 40, 50, 25, 35, 0),
        (0, 500, 50, 0, 0, 0),
    ],
)
def test_usage_position(period, previous, tier, addon, remaining, overage):
    usage = FreeRegistrationUsage(
        period_quantity=Decimal(period),
        previous_quantity=Decimal(previous),
        tier_allowance=tier,
        addon_allowance=addon,
    )
    assert usage.remaining_allowance == remaining
    assert usage.overage_quantity == overage


@pytest.mark.django_db
def test_allowance_is_consumed_over_the_event_lifetime(setup):
    org, event = setup
    record_usage(org, event, 40, make_aware(datetime(2026, 7, 10, 10, 0)), "JUL")
    record_usage(org, event, 30, make_aware(datetime(2026, 8, 12, 10, 0)), "AUG")

    july = generate_business_invoice_for_organizer(org, *JULY)
    assert overage_line(july) is None

    august = generate_business_invoice_for_organizer(org, *AUGUST)
    line = overage_line(august)
    # 40 of 50 were used in July, so 10 remain and 20 of August's 30 are billable.
    assert line.quantity == Decimal("20")
    assert line.amount == Decimal("10.00")
    assert line.calculation_metadata["total_free_registrations"] == 30
    assert line.calculation_metadata["previous_free_registrations"] == 40
    assert line.calculation_metadata["remaining_allowance"] == 10
    assert line.calculation_metadata["included_allowance"] == 50


@pytest.mark.django_db
def test_earlier_overage_is_not_billed_again(setup):
    org, event = setup
    record_usage(org, event, 80, make_aware(datetime(2026, 7, 10, 10, 0)), "JUL")
    record_usage(org, event, 5, make_aware(datetime(2026, 8, 12, 10, 0)), "AUG")

    july = generate_business_invoice_for_organizer(org, *JULY)
    assert overage_line(july).quantity == Decimal("30")

    august = generate_business_invoice_for_organizer(org, *AUGUST)
    assert overage_line(august).quantity == Decimal("5")


@pytest.mark.django_db
def test_each_event_has_its_own_allowance(setup):
    org, event = setup
    other = Event.objects.create(
        organizer=org, name="Other", slug="other", currency="EUR", date_from=now()
    )
    record_usage(org, event, 60, make_aware(datetime(2026, 7, 10, 10, 0)), "JUL")
    record_usage(org, other, 45, make_aware(datetime(2026, 8, 12, 10, 0)), "AUG")

    august = generate_business_invoice_for_organizer(org, *AUGUST)
    assert overage_line(august) is None


@pytest.mark.django_db
def test_organizer_addon_raises_the_allowance_of_every_event(setup):
    org, event = setup
    OrganizerAddon.objects.create(
        organizer=org,
        addon=allowance_addon("org-free", "organizer"),
        status=AddonStatus.ACTIVE,
        quantity=2,
        starts_at=AUGUST[0],
    )
    record_usage(org, event, 110, make_aware(datetime(2026, 8, 12, 10, 0)), "AUG")

    invoice = generate_business_invoice_for_organizer(org, *AUGUST)
    line = overage_line(invoice)
    # 50 from the tier plus 2 x 25 from the add-on.
    assert line.quantity == Decimal("10")
    assert line.calculation_metadata["tier_allowance"] == 50
    assert line.calculation_metadata["addon_allowance"] == 50
    assert line.calculation_metadata["included_allowance"] == 100


@pytest.mark.django_db
def test_event_addon_only_raises_the_allowance_of_its_event(setup):
    org, event = setup
    other = Event.objects.create(
        organizer=org, name="Other", slug="other", currency="EUR", date_from=now()
    )
    EventAddon.objects.create(
        event=event,
        addon=allowance_addon("event-free", "event"),
        status=AddonStatus.ACTIVE,
        quantity=1,
        starts_at=AUGUST[0],
    )
    record_usage(org, event, 70, make_aware(datetime(2026, 8, 12, 10, 0)), "A")
    record_usage(org, other, 70, make_aware(datetime(2026, 8, 12, 10, 0)), "B")

    invoice = generate_business_invoice_for_organizer(org, *AUGUST)
    assert overage_line(invoice, event) is None
    assert overage_line(invoice, other).quantity == Decimal("20")


@pytest.mark.django_db
@pytest.mark.parametrize(
    "fields",
    [
        {"status": AddonStatus.PENDING, "starts_at": AUGUST[0]},
        {
            "status": AddonStatus.CANCELED,
            "starts_at": make_aware(datetime(2026, 6, 1)),
            "cancel_at": make_aware(datetime(2026, 7, 15)),
            "canceled_at": make_aware(datetime(2026, 7, 15)),
        },
        {
            "status": AddonStatus.EXPIRED,
            "starts_at": make_aware(datetime(2026, 6, 1)),
            "ends_at": make_aware(datetime(2026, 7, 31)),
        },
        {"status": AddonStatus.ACTIVE, "starts_at": make_aware(datetime(2026, 9, 1))},
    ],
)
def test_addon_outside_the_period_grants_no_allowance(setup, fields):
    org, event = setup
    OrganizerAddon.objects.create(
        organizer=org, addon=allowance_addon("org-free", "organizer"), **fields
    )
    record_usage(org, event, 60, make_aware(datetime(2026, 8, 12, 10, 0)), "AUG")

    invoice = generate_business_invoice_for_organizer(org, *AUGUST)
    line = overage_line(invoice)
    assert line.quantity == Decimal("10")
    assert line.calculation_metadata["addon_allowance"] == 0


@pytest.mark.django_db
def test_current_charges_estimate_matches_invoice_calculation(setup):
    org, event = setup
    OrganizerAddon.objects.create(
        organizer=org,
        addon=allowance_addon("org-free", "organizer"),
        status=AddonStatus.ACTIVE,
        starts_at=JULY[0],
    )
    record_usage(org, event, 40, make_aware(datetime(2026, 7, 10, 10, 0)), "JUL")
    record_usage(org, event, 45, make_aware(datetime(2026, 8, 12, 10, 0)), "AUG")

    overview = build_organizer_billing_overview(
        org, reference=make_aware(datetime(2026, 8, 20, 12, 0))
    )
    (item,) = [row for row in overview["usage"] if row["capability"] == CAPABILITY]
    # Allowance 50 + 25, of which 40 were used before August.
    assert item["quantity"] == Decimal("45")
    assert item["allowance"] == Decimal("35")
    assert item["overage"] == Decimal("10")
    assert item["amount"] == Decimal("5.00")
    assert item["currency"] == "EUR"

    invoice = generate_business_invoice_for_organizer(org, *AUGUST)
    assert overage_line(invoice).amount == item["amount"]
