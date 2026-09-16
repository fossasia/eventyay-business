import pytest
from datetime import datetime, time
from decimal import Decimal
from django.test import override_settings
from django.utils.timezone import make_aware, now
from eventyay.base.models import Event, Organizer, User
from eventyay.base.models.auth import StaffSession
from eventyay.base.settings import GlobalSettingsObject
from unittest.mock import patch

from eventyay_business.invoicing_service import (
    generate_all_business_invoices,
    generate_business_invoice_for_organizer,
    generate_invoice_number,
    get_previous_calendar_month_range,
)
from eventyay_business.models import (
    AddonDefinition,
    AddonPricingMode,
    AddonStatus,
    BillingInterval,
    BusinessInvoice,
    BusinessInvoiceStatus,
    EventAddon,
    InvoiceLineType,
    OrganizerAddon,
    Subscription,
    SubscriptionStatus,
    Tier,
    TierEntitlement,
    TierPrice,
    TierVersion,
    UsageRecord,
)
from eventyay_business.tasks import generate_monthly_business_invoices


@pytest.fixture
def business_admin_client(admin_client, admin_user):
    session = admin_client.session
    session.save()
    StaffSession.objects.create(
        user=admin_user,
        session_key=session.session_key,
        comment="test",
    )
    admin_user.is_staff = True
    admin_user.save()
    return admin_client


@pytest.fixture
def organizer_user(db):
    user = User.objects.create_user("orguser@example.com", "secret")
    return user


@pytest.fixture
def test_setup(db):
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
        tier_version=version,
        capability="registration.free_allowance_per_event",
        value="50",
    )
    TierEntitlement.objects.create(
        tier_version=version,
        capability="registration.free_overage_price",
        value="0.50",
    )
    TierEntitlement.objects.create(
        tier_version=version,
        capability="commerce.platform_fee_percent",
        value="2.5",
    )

    sub = Subscription.objects.get(organizer=org)
    sub.tier_version = version
    sub.currency = "EUR"
    sub.billing_interval = "monthly"
    sub.starts_at = make_aware(datetime(2026, 1, 1, 0, 0, 0))
    sub.save()

    event = Event.objects.create(
        organizer=org,
        name="Acme Conf",
        slug="conf",
        currency="EUR",
        date_from=now(),
    )

    return org, tier, version, event


@pytest.mark.django_db
def test_generate_invoice_number(test_setup):
    org, _, _, _ = test_setup
    period_start = make_aware(datetime(2026, 8, 1, 0, 0, 0))
    num1 = generate_invoice_number(org, period_start)
    assert num1.startswith("INV-202608-")
    assert f"{org.pk:04d}-01" in num1

    # Create dummy invoice with num1 to test sequence increment
    BusinessInvoice.objects.create(
        organizer=org,
        invoice_number=num1,
        billing_period_start=period_start,
        billing_period_end=make_aware(datetime(2026, 8, 31, 23, 59, 59)),
        currency="EUR",
        subtotal=Decimal("0.00"),
        total=Decimal("0.00"),
    )
    num2 = generate_invoice_number(org, period_start)
    assert f"{org.pk:04d}-02" in num2


@pytest.mark.django_db
def test_get_previous_calendar_month_range():
    # Test on a known date: 2026-09-15 -> previous month is August 2026
    ref_dt = make_aware(datetime(2026, 9, 15, 12, 0, 0))
    start, end = get_previous_calendar_month_range(ref_dt)
    assert start.year == 2026
    assert start.month == 8
    assert start.day == 1
    assert start.time() == time(0, 0, 0)
    assert end.year == 2026
    assert end.month == 8
    assert end.day == 31
    assert end.hour == 23 and end.minute == 59 and end.second == 59

    # Test January rollover: 2026-01-10 -> previous month is December 2025
    jan_dt = make_aware(datetime(2026, 1, 10, 12, 0, 0))
    jan_start, jan_end = get_previous_calendar_month_range(jan_dt)
    assert jan_start.year == 2025
    assert jan_start.month == 12
    assert jan_start.day == 1
    assert jan_end.year == 2025
    assert jan_end.month == 12
    assert jan_end.day == 31

    # Test default (now)
    cur_start, cur_end = get_previous_calendar_month_range()
    assert cur_start < cur_end


@pytest.mark.django_db
def test_invoice_subscription_fee(test_setup):
    org, _, _, _ = test_setup
    period_start = make_aware(datetime(2026, 8, 1, 0, 0, 0))
    period_end = make_aware(datetime(2026, 8, 31, 23, 59, 59))

    invoice = generate_business_invoice_for_organizer(org, period_start, period_end)
    assert invoice is not None
    assert invoice.status == BusinessInvoiceStatus.OPEN
    assert invoice.currency == "EUR"
    assert invoice.subtotal == Decimal("49.00")
    assert invoice.total == Decimal("49.00")

    lines = invoice.lines.all()
    assert lines.count() == 1
    sub_line = lines.first()
    assert sub_line.line_type == InvoiceLineType.SUBSCRIPTION
    assert sub_line.unit_price == Decimal("49.00")
    assert sub_line.amount == Decimal("49.00")
    assert "Pro Tier" in sub_line.description
    assert sub_line.calculation_metadata["tier_name"] == "Pro Tier"


@pytest.mark.django_db
def test_invoice_addons_aggregation(test_setup):
    org, _, _, event = test_setup
    period_start = make_aware(datetime(2026, 8, 1, 0, 0, 0))
    period_end = make_aware(datetime(2026, 8, 31, 23, 59, 59))

    # Add-on 1: Org scope
    addon1 = AddonDefinition.objects.create(
        name="Extra Storage",
        slug="extra-storage",
        assignment_scope="organizer",
        pricing_mode="recurring",
        price=Decimal("15.00"),
        currency="EUR",
        capability="storage.extra_gb",
        active=True,
    )
    OrganizerAddon.objects.create(
        organizer=org,
        addon=addon1,
        status="active",
        quantity=2,
        price=Decimal("15.00"),
        currency="EUR",
        starts_at=period_start,
    )

    # Add-on 2: Event scope
    addon2 = AddonDefinition.objects.create(
        name="Custom Domain",
        slug="custom-domain",
        assignment_scope="event",
        pricing_mode="recurring",
        price=Decimal("10.00"),
        currency="EUR",
        capability="event.custom_domain",
        active=True,
    )
    EventAddon.objects.create(
        event=event,
        addon=addon2,
        status="active",
        quantity=1,
        price=Decimal("10.00"),
        currency="EUR",
        starts_at=period_start,
    )

    invoice = generate_business_invoice_for_organizer(org, period_start, period_end)
    assert invoice is not None
    # Sub: 49.00, Org Addon: 2 * 15.00 = 30.00, Event Addon: 1 * 10.00 = 10.00. Total = 89.00
    assert invoice.subtotal == Decimal("89.00")
    assert invoice.total == Decimal("89.00")

    addon_lines = invoice.lines.filter(line_type=InvoiceLineType.ADDON)
    assert addon_lines.count() == 2

    org_line = addon_lines.filter(event__isnull=True).first()
    assert org_line.quantity == Decimal("2")
    assert org_line.unit_price == Decimal("15.00")
    assert org_line.amount == Decimal("30.00")

    ev_line = addon_lines.filter(event=event).first()
    assert ev_line.quantity == Decimal("1")
    assert ev_line.unit_price == Decimal("10.00")
    assert ev_line.amount == Decimal("10.00")


@pytest.mark.django_db
def test_invoice_platform_fees(test_setup):
    org, _, _, event = test_setup
    period_start = make_aware(datetime(2026, 8, 1, 0, 0, 0))
    period_end = make_aware(datetime(2026, 8, 31, 23, 59, 59))

    # Create UsageRecords for platform fees
    UsageRecord.objects.create(
        organizer=org,
        event=event,
        capability="commerce.platform_fee_percent",
        quantity=Decimal("12.50"),
        unit="EUR",
        source_type="order",
        source_id="ORD-001",
        idempotency_key="ORD-001-fee",
        occurred_at=make_aware(datetime(2026, 8, 10, 10, 0, 0)),
        metadata={
            "billing_currency_fee_amount": 12.50,
            "billing_currency": "EUR",
            "gross_sales": 500.00,
            "tickets_count": 5,
            "fee_percent": 2.5,
            "currency": "EUR",
        },
    )
    UsageRecord.objects.create(
        organizer=org,
        event=event,
        capability="commerce.platform_fee_percent",
        quantity=Decimal("7.50"),
        unit="EUR",
        source_type="order",
        source_id="ORD-002",
        idempotency_key="ORD-002-fee",
        occurred_at=make_aware(datetime(2026, 8, 20, 15, 0, 0)),
        metadata={
            "billing_currency_fee_amount": 7.50,
            "billing_currency": "EUR",
            "gross_sales": 300.00,
            "tickets_count": 3,
            "fee_percent": 2.5,
            "currency": "EUR",
        },
    )
    # Record outside the period should NOT be included
    UsageRecord.objects.create(
        organizer=org,
        event=event,
        capability="commerce.platform_fee_percent",
        quantity=Decimal("50.00"),
        unit="EUR",
        source_type="order",
        source_id="ORD-OUTSIDE",
        idempotency_key="ORD-OUTSIDE-fee",
        occurred_at=make_aware(datetime(2026, 7, 25, 10, 0, 0)),
    )

    invoice = generate_business_invoice_for_organizer(org, period_start, period_end)
    assert invoice is not None
    fee_lines = invoice.lines.filter(line_type=InvoiceLineType.PLATFORM_FEE)
    assert fee_lines.count() == 1
    fee_line = fee_lines.first()
    assert fee_line.amount == Decimal("20.00")
    assert fee_line.calculation_metadata["orders_count"] == 2
    assert fee_line.calculation_metadata["total_fees"] == "20.00"


@pytest.mark.django_db
def test_invoice_free_registration_overage(test_setup):
    org, _, _, event = test_setup
    period_start = make_aware(datetime(2026, 8, 1, 0, 0, 0))
    period_end = make_aware(datetime(2026, 8, 31, 23, 59, 59))

    # Allowance is 50, rate is 0.50.
    # Record 80 free registrations for this event
    UsageRecord.objects.create(
        organizer=org,
        event=event,
        capability="registration.free_allowance_per_event",
        quantity=Decimal("80"),
        unit="count",
        source_type="order",
        source_id="FREE-001",
        idempotency_key="FREE-001",
        occurred_at=make_aware(datetime(2026, 8, 12, 10, 0, 0)),
    )

    invoice = generate_business_invoice_for_organizer(org, period_start, period_end)
    assert invoice is not None
    overage_lines = invoice.lines.filter(line_type=InvoiceLineType.REGISTRATION_OVERAGE)
    assert overage_lines.count() == 1
    line = overage_lines.first()
    # 80 - 50 = 30 overage * 0.50 = 15.00
    assert line.quantity == Decimal("30")
    assert line.unit_price == Decimal("0.50")
    assert line.amount == Decimal("15.00")
    assert line.calculation_metadata["included_allowance"] == 50
    assert line.calculation_metadata["total_free_registrations"] == 80
    assert line.calculation_metadata["overage_units"] == 30


@pytest.mark.django_db
def test_invoice_free_registration_within_allowance(test_setup):
    org, _, _, event = test_setup
    period_start = make_aware(datetime(2026, 8, 1, 0, 0, 0))
    period_end = make_aware(datetime(2026, 8, 31, 23, 59, 59))

    # Allowance is 50. Record 40 free registrations -> no overage line created
    UsageRecord.objects.create(
        organizer=org,
        event=event,
        capability="registration.free_allowance_per_event",
        quantity=Decimal("40"),
        unit="count",
        source_type="order",
        source_id="FREE-002",
        idempotency_key="FREE-002",
        occurred_at=make_aware(datetime(2026, 8, 12, 10, 0, 0)),
    )

    invoice = generate_business_invoice_for_organizer(org, period_start, period_end)
    assert invoice is not None
    overage_lines = invoice.lines.filter(line_type=InvoiceLineType.REGISTRATION_OVERAGE)
    assert overage_lines.count() == 0


@pytest.mark.django_db
def test_invoice_idempotency(test_setup):
    org, _, _, _ = test_setup
    period_start = make_aware(datetime(2026, 8, 1, 0, 0, 0))
    period_end = make_aware(datetime(2026, 8, 31, 23, 59, 59))

    inv1 = generate_business_invoice_for_organizer(org, period_start, period_end)
    line_count_1 = inv1.lines.count()

    # Call again with same parameters
    inv2 = generate_business_invoice_for_organizer(org, period_start, period_end)
    assert inv1.pk == inv2.pk
    assert inv2.lines.count() == line_count_1
    assert BusinessInvoice.objects.filter(organizer=org).count() == 1


@pytest.mark.django_db
def test_generate_all_business_invoices_and_task(test_setup):
    org, _, _, _ = test_setup
    period_start = make_aware(datetime(2026, 8, 1, 0, 0, 0))
    period_end = make_aware(datetime(2026, 8, 31, 23, 59, 59))

    invoices = generate_all_business_invoices(period_start, period_end)
    assert len(invoices) >= 1
    assert any(inv.organizer == org for inv in invoices)

    # Test celery task call
    with patch(
        "eventyay_business.invoicing_service.generate_all_business_invoices"
    ) as mock_gen:
        mock_gen.return_value = []
        res = generate_monthly_business_invoices()
        assert mock_gen.called
        assert res["generated_invoices_count"] == 0


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_organizer_invoice_views(client, organizer_user, test_setup):
    org, _, _, _ = test_setup
    period_start = make_aware(datetime(2026, 8, 1, 0, 0, 0))
    period_end = make_aware(datetime(2026, 8, 31, 23, 59, 59))

    invoice = generate_business_invoice_for_organizer(org, period_start, period_end)

    # Without login/permission -> redirect or 403
    url_list = f"/control/organizer/{org.slug}/business/invoices/"
    url_detail = f"/control/organizer/{org.slug}/business/invoices/{invoice.pk}/"

    resp = client.get(url_list)
    assert resp.status_code in (302, 403)

    # Grant user permission on organizer
    team = org.teams.create(
        name="Admins",
        all_events=True,
        can_change_organizer_settings=True,
    )
    team.members.add(organizer_user)
    client.force_login(organizer_user)

    # Test list view
    resp = client.get(url_list)
    assert resp.status_code == 200
    assert invoice.invoice_number.encode() in resp.content

    # Test detail view
    resp = client.get(url_detail)
    assert resp.status_code == 200
    assert invoice.invoice_number.encode() in resp.content
    assert b"Pro Tier" in resp.content

    # Another organizer's invoice should not be accessible
    other_org = Organizer.objects.create(name="Other Org", slug="other")
    other_inv = BusinessInvoice.objects.create(
        organizer=other_org,
        invoice_number="INV-202608-9999-01",
        billing_period_start=period_start,
        billing_period_end=period_end,
        currency="EUR",
        subtotal=Decimal("10.00"),
        total=Decimal("10.00"),
    )
    resp = client.get(
        f"/control/organizer/{org.slug}/business/invoices/{other_inv.pk}/"
    )
    assert resp.status_code == 404


@pytest.mark.django_db
def test_admin_invoice_views(business_admin_client, test_setup):
    org, _, _, _ = test_setup
    period_start = make_aware(datetime(2026, 8, 1, 0, 0, 0))
    period_end = make_aware(datetime(2026, 8, 31, 23, 59, 59))

    invoice = generate_business_invoice_for_organizer(org, period_start, period_end)

    # Admin List View
    url_list = "/admin/global/business/invoices/"
    resp = business_admin_client.get(url_list)
    assert resp.status_code == 200
    assert invoice.invoice_number.encode() in resp.content
    assert org.name.encode() in resp.content

    # Search filter
    resp = business_admin_client.get(f"{url_list}?q={invoice.invoice_number}")
    assert resp.status_code == 200
    assert invoice.invoice_number.encode() in resp.content

    resp = business_admin_client.get(f"{url_list}?q=nonexistent")
    assert resp.status_code == 200
    assert invoice.invoice_number.encode() not in resp.content

    # Status filter
    resp = business_admin_client.get(f"{url_list}?status=open")
    assert resp.status_code == 200
    assert invoice.invoice_number.encode() in resp.content

    resp = business_admin_client.get(f"{url_list}?status=paid")
    assert resp.status_code == 200
    assert invoice.invoice_number.encode() not in resp.content

    # Admin Detail View
    url_detail = f"/admin/global/business/invoices/{invoice.pk}/"
    resp = business_admin_client.get(url_detail)
    assert resp.status_code == 200
    assert invoice.invoice_number.encode() in resp.content
    assert b"Pro Tier" in resp.content


@pytest.mark.django_db
def test_canceled_and_inactive_subscription_and_addons_excluded(test_setup):
    org, tier, version, event = test_setup
    period_start = make_aware(datetime(2026, 8, 1, 0, 0, 0))
    period_end = make_aware(datetime(2026, 8, 31, 23, 59, 59))

    # 1. Canceled subscription should not be billed
    sub = Subscription.objects.get(organizer=org)
    sub.status = SubscriptionStatus.CANCELED
    sub.save()

    # 2. Inactive/canceled addon should not be billed
    addon_def = AddonDefinition.objects.create(
        slug="canceled-storage",
        name="Canceled Storage",
        price=Decimal("15.00"),
        currency="EUR",
        capability="storage.extra_gb",
    )
    OrganizerAddon.objects.create(
        organizer=org,
        addon=addon_def,
        status=AddonStatus.CANCELED,
        starts_at=make_aware(datetime(2026, 7, 1, 0, 0, 0)),
        canceled_at=make_aware(datetime(2026, 7, 15, 0, 0, 0)),
    )

    # 3. Addon canceled before the billing period start should not be billed
    addon_def2 = AddonDefinition.objects.create(
        slug="old-addon",
        name="Old Addon",
        price=Decimal("20.00"),
        currency="EUR",
        capability="custom_domain",
    )
    OrganizerAddon.objects.create(
        organizer=org,
        addon=addon_def2,
        status=AddonStatus.ACTIVE,
        starts_at=make_aware(datetime(2026, 6, 1, 0, 0, 0)),
        cancel_at=make_aware(datetime(2026, 7, 31, 23, 59, 59)),
    )

    inv = generate_business_invoice_for_organizer(org, period_start, period_end)
    assert inv is None


@pytest.mark.django_db
def test_platform_fee_ecb_conversion_and_missing_rate(test_setup):
    org, tier, version, event = test_setup
    period_start = make_aware(datetime(2026, 8, 1, 0, 0, 0))
    period_end = make_aware(datetime(2026, 8, 31, 23, 59, 59))

    # Remove subscription fee to isolate platform fee lines
    sub = Subscription.objects.get(organizer=org)
    sub.status = SubscriptionStatus.CANCELED
    sub.save()

    # Create event with USD
    usd_event = Event.objects.create(
        organizer=org,
        name="USD Summit",
        slug="usd-summit",
        currency="USD",
        date_from=now(),
    )

    # Usage record for platform fee: 12.00 USD
    UsageRecord.objects.create(
        organizer=org,
        event=usd_event,
        capability="commerce.platform_fee_percent",
        quantity=Decimal("12.00"),
        unit="USD",
        occurred_at=make_aware(datetime(2026, 8, 10, 12, 0, 0)),
        source_type="order",
        source_id="ORD-USD-1",
        idempotency_key="fee-usd-1",
        metadata={"currency": "USD", "fee_base": "120.00", "fee_percent": "10.0"},
    )

    gs = GlobalSettingsObject()

    # Case A: Missing exchange rate -> record skipped, no invoice generated
    gs.settings.ecb_rates_dict = {"EUR": "1.0000"}  # USD missing
    inv_none = generate_business_invoice_for_organizer(
        org, period_start, period_end, currency="EUR"
    )
    assert inv_none is None

    # Case B: Exchange rate present: 1 EUR = 1.20 USD -> 12.00 USD = 10.00 EUR
    gs.settings.ecb_rates_dict = {"EUR": "1.0000", "USD": "1.2000"}
    inv = generate_business_invoice_for_organizer(
        org, period_start, period_end, currency="EUR"
    )
    assert inv is not None
    assert inv.currency == "EUR"
    assert inv.total == Decimal("10.00")
    line = inv.lines.first()
    assert line.line_type == InvoiceLineType.PLATFORM_FEE
    assert line.amount == Decimal("10.00")


@pytest.mark.django_db
def test_concurrent_invoice_generation_integrity_handling(test_setup):
    from django.db import IntegrityError

    org, _, _, _ = test_setup
    period_start = make_aware(datetime(2026, 8, 1, 0, 0, 0))
    period_end = make_aware(datetime(2026, 8, 31, 23, 59, 59))

    # Pre-create invoice
    existing_inv = generate_business_invoice_for_organizer(
        org, period_start, period_end
    )
    assert existing_inv is not None

    # Simulate concurrency: during the second call, the pre-existing invoice
    # does not satisfy the idempotency check (simulating concurrent execution before commit),
    # while preserving the conflicting record needed for IntegrityError recovery.
    orig_filter = BusinessInvoice.objects.filter
    filter_calls = 0

    def mock_filter(*args, **kwargs):
        nonlocal filter_calls
        if kwargs.get("billing_period_start") == period_start:
            filter_calls += 1
            if filter_calls == 1:
                return orig_filter(*args, **kwargs).none()
        return orig_filter(*args, **kwargs)

    with patch.object(
        BusinessInvoice.objects, "filter", side_effect=mock_filter
    ), patch(
        "eventyay_business.invoicing_service.BusinessInvoice.objects.create"
    ) as mock_create:
        mock_create.side_effect = IntegrityError(
            "duplicate key value violates unique constraint"
        )
        raced_inv = generate_business_invoice_for_organizer(
            org, period_start, period_end
        )
        mock_create.assert_called_once()
        assert raced_inv == existing_inv


@pytest.mark.django_db
def test_subscription_line_skipped_when_no_interval_price_match(test_setup):
    org, tier, version, event = test_setup
    period_start = make_aware(datetime(2026, 8, 1, 0, 0, 0))
    period_end = make_aware(datetime(2026, 8, 31, 23, 59, 59))

    # Tier only has MONTHLY price
    assert version.prices.filter(billing_interval=BillingInterval.MONTHLY).exists()
    assert not version.prices.filter(billing_interval=BillingInterval.ANNUAL).exists()

    # Subscription is set to ANNUAL, but starts in this period
    sub = Subscription.objects.get(organizer=org)
    sub.billing_interval = BillingInterval.ANNUAL
    sub.starts_at = make_aware(datetime(2026, 8, 15, 10, 0, 0))
    sub.save()

    # Since no ANNUAL price exists, subscription line should be skipped
    inv = generate_business_invoice_for_organizer(org, period_start, period_end)
    assert inv is None


@pytest.mark.django_db
def test_ecb_conversion_exceptions_handled(test_setup):
    org, tier, version, event = test_setup
    period_start = make_aware(datetime(2026, 8, 1, 0, 0, 0))
    period_end = make_aware(datetime(2026, 8, 31, 23, 59, 59))

    # Tier price is in USD
    price = version.prices.first()
    price.currency = "USD"
    price.save()

    gs = GlobalSettingsObject()
    # Malformed rate (e.g. division by zero in exchange rate)
    gs.settings.ecb_rates_dict = {"EUR": "1.0000", "USD": "0.0000"}

    # Should catch ArithmeticError / InvalidOperation, log, fallback to 0.00 and skip line
    inv = generate_business_invoice_for_organizer(
        org, period_start, period_end, currency="EUR"
    )
    assert inv is None


@pytest.mark.django_db
def test_annual_subscription_billed_only_on_renewal(test_setup):
    org, tier, version, event = test_setup
    TierPrice.objects.create(
        tier_version=version,
        billing_interval=BillingInterval.ANNUAL,
        amount=Decimal("490.00"),
        currency="EUR",
    )

    sub = Subscription.objects.get(organizer=org)
    sub.billing_interval = BillingInterval.ANNUAL
    sub.starts_at = make_aware(datetime(2026, 1, 15, 10, 0, 0))
    sub.save()

    jan_start = make_aware(datetime(2026, 1, 1, 0, 0, 0))
    jan_end = make_aware(datetime(2026, 1, 31, 23, 59, 59))
    feb_start = make_aware(datetime(2026, 2, 1, 0, 0, 0))
    feb_end = make_aware(datetime(2026, 2, 28, 23, 59, 59))
    next_jan_start = make_aware(datetime(2027, 1, 1, 0, 0, 0))
    next_jan_end = make_aware(datetime(2027, 1, 31, 23, 59, 59))

    # 1. Initial subscription month (Jan 2026) covers starts_at -> billed
    inv_jan = generate_business_invoice_for_organizer(org, jan_start, jan_end)
    assert inv_jan is not None
    assert inv_jan.lines.filter(line_type=InvoiceLineType.SUBSCRIPTION).exists()
    line_jan = inv_jan.lines.get(line_type=InvoiceLineType.SUBSCRIPTION)
    assert line_jan.amount == Decimal("490.00")
    assert line_jan.calculation_metadata["billing_interval"] == "annual"
    assert "2026-01-15" in line_jan.calculation_metadata["service_period_start"]
    assert "2027-01-15" in line_jan.calculation_metadata["service_period_end"]

    # 2. Next month (Feb 2026) has no renewal -> NOT billed (returns None)
    inv_feb = generate_business_invoice_for_organizer(org, feb_start, feb_end)
    assert inv_feb is None

    # 3. Subsequent renewal year (Jan 2027) -> billed
    inv_next_jan = generate_business_invoice_for_organizer(
        org, next_jan_start, next_jan_end
    )
    assert inv_next_jan is not None
    assert inv_next_jan.lines.filter(line_type=InvoiceLineType.SUBSCRIPTION).exists()
    line_next = inv_next_jan.lines.get(line_type=InvoiceLineType.SUBSCRIPTION)
    assert line_next.amount == Decimal("490.00")
    assert "2027-01-15" in line_next.calculation_metadata["service_period_start"]
    assert "2028-01-15" in line_next.calculation_metadata["service_period_end"]


@pytest.mark.django_db
def test_onetime_addon_billed_only_in_start_period(test_setup):
    org, tier, version, event = test_setup

    # Cancel sub to isolate addon
    sub = Subscription.objects.get(organizer=org)
    sub.status = SubscriptionStatus.CANCELED
    sub.save()

    addon_def = AddonDefinition.objects.create(
        slug="setup-fee",
        name="Setup Assistance",
        pricing_mode=AddonPricingMode.ONE_TIME,
        price=Decimal("150.00"),
        currency="EUR",
        capability="consulting.setup",
    )
    OrganizerAddon.objects.create(
        organizer=org,
        addon=addon_def,
        status=AddonStatus.ACTIVE,
        starts_at=make_aware(datetime(2026, 8, 10, 14, 0, 0)),
    )

    aug_start = make_aware(datetime(2026, 8, 1, 0, 0, 0))
    aug_end = make_aware(datetime(2026, 8, 31, 23, 59, 59))
    sep_start = make_aware(datetime(2026, 9, 1, 0, 0, 0))
    sep_end = make_aware(datetime(2026, 9, 30, 23, 59, 59))

    # 1. August invoice covers starts_at -> billed
    inv_aug = generate_business_invoice_for_organizer(org, aug_start, aug_end)
    assert inv_aug is not None
    assert inv_aug.total == Decimal("150.00")
    line = inv_aug.lines.first()
    assert line.line_type == InvoiceLineType.ADDON
    assert line.amount == Decimal("150.00")
    assert line.calculation_metadata["pricing_mode"] == "one_time"
    assert "2026-08-01" in line.calculation_metadata["billed_period"]

    # 2. September invoice does NOT cover starts_at -> NOT billed
    inv_sep = generate_business_invoice_for_organizer(org, sep_start, sep_end)
    assert inv_sep is None


@pytest.mark.django_db
def test_addon_and_subscription_currency_conversion(test_setup):
    org, tier, version, event = test_setup
    period_start = make_aware(datetime(2026, 8, 1, 0, 0, 0))
    period_end = make_aware(datetime(2026, 8, 31, 23, 59, 59))

    # Create USD prices for subscription and addon
    TierPrice.objects.filter(tier_version=version).delete()
    TierPrice.objects.create(
        tier_version=version,
        billing_interval="monthly",
        amount=Decimal("120.00"),
        currency="USD",
    )

    addon_def = AddonDefinition.objects.create(
        slug="usd-addon",
        name="USD Addon",
        pricing_mode=AddonPricingMode.RECURRING,
        price=Decimal("24.00"),
        currency="USD",
        capability="cloud_backup",
    )
    OrganizerAddon.objects.create(
        organizer=org,
        addon=addon_def,
        status=AddonStatus.ACTIVE,
        starts_at=make_aware(datetime(2026, 8, 1, 0, 0, 0)),
    )

    gs = GlobalSettingsObject()

    # Case A: Missing USD rate -> both skipped, no invoice produced
    gs.settings.ecb_rates_dict = {"EUR": "1.0000"}
    inv_missing = generate_business_invoice_for_organizer(
        org, period_start, period_end, currency="EUR"
    )
    assert inv_missing is None

    # Case B: Rates present: 1 EUR = 1.20 USD
    # Sub: 120 USD / 1.2 = 100 EUR
    # Addon: 24 USD / 1.2 = 20 EUR
    gs.settings.ecb_rates_dict = {"EUR": "1.0000", "USD": "1.2000"}
    inv = generate_business_invoice_for_organizer(
        org, period_start, period_end, currency="EUR"
    )
    assert inv is not None
    assert inv.currency == "EUR"
    assert inv.total == Decimal("120.00")  # 100 + 20
    sub_line = inv.lines.get(line_type=InvoiceLineType.SUBSCRIPTION)
    assert sub_line.amount == Decimal("100.00")
    assert sub_line.calculation_metadata["price_currency"] == "USD"

    addon_line = inv.lines.get(line_type=InvoiceLineType.ADDON)
    assert addon_line.amount == Decimal("20.00")
    assert addon_line.calculation_metadata["original_currency"] == "USD"


@pytest.mark.django_db
def test_historical_terminal_subscription_and_past_due_cutoff(test_setup):
    org, tier, version, event = test_setup
    aug_start = make_aware(datetime(2026, 8, 1, 0, 0, 0))
    aug_end = make_aware(datetime(2026, 8, 31, 23, 59, 59))
    sep_start = make_aware(datetime(2026, 9, 1, 0, 0, 0))
    sep_end = make_aware(datetime(2026, 9, 30, 23, 59, 59))

    # Canceled subscription that ended in August
    sub = Subscription.objects.get(organizer=org)
    sub.status = SubscriptionStatus.CANCELED
    sub.cancel_at = make_aware(datetime(2026, 8, 15, 0, 0, 0))
    sub.ends_at = make_aware(datetime(2026, 8, 15, 0, 0, 0))
    sub.save()

    # In August, it was active until the 15th -> included
    inv_aug = generate_business_invoice_for_organizer(org, aug_start, aug_end)
    assert inv_aug is not None
    assert inv_aug.lines.filter(line_type=InvoiceLineType.SUBSCRIPTION).exists()

    # In September, it was already ended -> excluded
    inv_sep = generate_business_invoice_for_organizer(org, sep_start, sep_end)
    assert inv_sep is None

    # Past-due subscription whose effective end expired in July should not be billed in August
    BusinessInvoice.objects.filter(organizer=org).delete()
    sub.status = SubscriptionStatus.PAST_DUE
    sub.ends_at = make_aware(datetime(2026, 7, 31, 23, 59, 59))
    sub.cancel_at = None
    sub.save()

    inv_aug_past = generate_business_invoice_for_organizer(org, aug_start, aug_end)
    assert inv_aug_past is None
