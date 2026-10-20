import pytest
from datetime import datetime, timedelta
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.test import override_settings
from django.urls import reverse
from django.utils.timezone import make_aware, now
from eventyay.base.models import Event, Organizer
from eventyay.base.models.auth import StaffSession
from eventyay.base.models.vouchers import InvoiceVoucher
from unittest.mock import MagicMock, patch

from eventyay_business.invoicing_service import generate_business_invoice_for_organizer
from eventyay_business.models import (
    AddonAssignmentScope,
    AddonDefinition,
    AddonPricingMode,
    AddonStatus,
    BillingInterval,
    InvoiceLineType,
    OrganizerAddon,
    Subscription,
    Tier,
    TierPrice,
    TierStatus,
    TierVersion,
    VoucherBusinessTargets,
    VoucherDiscountDuration,
    VoucherRedemption,
)
from eventyay_business.stripe_service import (
    create_subscription_checkout_session,
    process_checkout_session_completed,
)
from eventyay_business.vouchers import (
    get_voucher_quote,
    redeem_addon_voucher,
    record_checkout_redemption,
    redeem_voucher,
    voucher_discount,
)


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
def plan_setup(db):
    organizer = Organizer.objects.create(name="Acme Corp", slug="acme")
    tier = Tier.objects.create(
        slug="pro", name="Pro Tier", status=TierStatus.PUBLISHED, is_public=True
    )
    version = TierVersion.objects.create(tier=tier, version=1, published_at=now())
    price = TierPrice.objects.create(
        tier_version=version,
        billing_interval=BillingInterval.MONTHLY,
        amount=Decimal("49.00"),
        currency="EUR",
    )
    addon = AddonDefinition.objects.create(
        name="Room Booster",
        slug="room-booster",
        capability="video.jitsi.concurrent_rooms",
        entitlement_value="5",
        assignment_scope=AddonAssignmentScope.ORGANIZER,
        price=Decimal("20.00"),
        currency="EUR",
        pricing_mode=AddonPricingMode.RECURRING,
        active=True,
        public=True,
    )
    return organizer, tier, version, price, addon


def make_voucher(code="PLAN25", *, tiers=(), addons=(), duration=VoucherDiscountDuration.ONCE, periods=None, **kwargs):
    defaults = {
        "price_mode": "percent",
        "value": Decimal("25.00"),
        "max_usages": 5,
        "valid_until": now() + timedelta(days=30),
        "applies_to_platform_fees": False,
    }
    defaults.update(kwargs)
    voucher = InvoiceVoucher.objects.create(code=code, **defaults)
    targets = VoucherBusinessTargets.objects.create(
        voucher=voucher, duration=duration, duration_periods=periods
    )
    targets.tiers.set(tiers)
    targets.addons.set(addons)
    return voucher


@pytest.mark.django_db
def test_voucher_discount_modes_and_budget():
    percent = InvoiceVoucher(price_mode="percent", value=Decimal("25.00"))
    assert voucher_discount(percent, Decimal("49.00")) == Decimal("12.25")

    credit = InvoiceVoucher(price_mode="subtract", value=Decimal("60.00"))
    assert voucher_discount(credit, Decimal("49.00")) == Decimal("49.00")

    capped = InvoiceVoucher(
        price_mode="percent",
        value=Decimal("100.00"),
        budget=Decimal("30.00"),
        budget_used=Decimal("20.00"),
    )
    assert voucher_discount(capped, Decimal("49.00")) == Decimal("10.00")


@pytest.mark.django_db
def test_quote_checks_targets_scope_and_validity(plan_setup):
    organizer, tier, _, price, addon = plan_setup
    other_tier = Tier.objects.create(slug="other", name="Other Tier")
    make_voucher(tiers=[tier])

    quote = get_voucher_quote("PLAN25", organizer, amount=price.amount, tier=tier)
    assert quote.discount == Decimal("12.25")
    assert quote.total == Decimal("36.75")

    with pytest.raises(ValidationError, match="not valid"):
        get_voucher_quote("NOPE00", organizer, amount=price.amount, tier=tier)
    with pytest.raises(ValidationError, match="cannot be used for Other Tier"):
        get_voucher_quote("PLAN25", organizer, amount=price.amount, tier=other_tier)
    with pytest.raises(ValidationError, match="cannot be used for Room Booster"):
        get_voucher_quote("PLAN25", organizer, amount=addon.price, addon=addon)

    other_organizer = Organizer.objects.create(name="Other", slug="other")
    limited = make_voucher("LIMITED1", tiers=[tier])
    limited.limit_organizer.set([other_organizer])
    with pytest.raises(ValidationError, match="not valid for your organisation"):
        get_voucher_quote("LIMITED1", organizer, amount=price.amount, tier=tier)

    make_voucher("EXPIRED1", tiers=[tier], valid_until=now() - timedelta(days=1))
    with pytest.raises(ValidationError, match="expired"):
        get_voucher_quote("EXPIRED1", organizer, amount=price.amount, tier=tier)


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_plan_upgrade_redeems_voucher(business_admin_client, plan_setup):
    organizer, tier, _, price, _ = plan_setup
    voucher = make_voucher(tiers=[tier])
    url = reverse(
        "plugins:eventyay_business:organizer.plan.upgrade",
        kwargs={"organizer": organizer.slug},
    )

    with patch("eventyay_business.views.is_stripe_configured", return_value=False):
        response = business_admin_client.post(
            url, {"tier_price_id": price.pk, "voucher_code": "PLAN25"}
        )

    assert response.status_code == 302
    subscription = Subscription.objects.get(organizer=organizer)
    assert subscription.tier_version.tier == tier
    redemption = VoucherRedemption.objects.get(voucher=voucher)
    assert redemption.subscription == subscription
    assert redemption.tier == tier
    voucher.refresh_from_db()
    assert voucher.redeemed == 1


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_plan_upgrade_rejects_invalid_voucher(business_admin_client, plan_setup):
    organizer, tier, _, price, _ = plan_setup
    previous_version = Subscription.objects.get(organizer=organizer).tier_version
    url = reverse(
        "plugins:eventyay_business:organizer.plan.upgrade",
        kwargs={"organizer": organizer.slug},
    )

    with patch("eventyay_business.views.is_stripe_configured", return_value=False):
        response = business_admin_client.post(
            url, {"tier_price_id": price.pk, "voucher_code": "UNKNOWN1"}, follow=True
        )

    assert "This voucher code is not valid." in response.content.decode()
    assert Subscription.objects.get(organizer=organizer).tier_version == previous_version
    assert not VoucherRedemption.objects.exists()


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_addon_purchase_redeems_voucher(business_admin_client, plan_setup):
    organizer, _, _, _, addon = plan_setup
    voucher = make_voucher("ADDON100", addons=[addon], value=Decimal("100.00"))
    url = reverse(
        "plugins:eventyay_business:organizer.addon.purchase",
        kwargs={"organizer": organizer.slug, "pk": addon.pk},
    )

    with patch("eventyay_business.views.is_stripe_configured", return_value=False):
        business_admin_client.post(url, {"quantity": 1, "voucher_code": "ADDON100"})

    assignment = OrganizerAddon.objects.get(organizer=organizer, addon=addon)
    assert VoucherRedemption.objects.get(voucher=voucher).organizer_addon == assignment


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_addon_purchase_shows_voucher_error(business_admin_client, plan_setup):
    organizer, tier, _, _, addon = plan_setup
    make_voucher(tiers=[tier])
    url = reverse(
        "plugins:eventyay_business:organizer.addon.purchase",
        kwargs={"organizer": organizer.slug, "pk": addon.pk},
    )

    with patch("eventyay_business.views.is_stripe_configured", return_value=False):
        response = business_admin_client.post(
            url, {"quantity": 1, "voucher_code": "PLAN25"}
        )

    assert response.status_code == 200
    assert "This voucher cannot be used for Room Booster." in response.content.decode()
    assert not OrganizerAddon.objects.filter(organizer=organizer, addon=addon).exists()


def redeemed_at(redemption, moment):
    VoucherRedemption.objects.filter(pk=redemption.pk).update(created_at=make_aware(moment))


def generate_august_invoice(organizer):
    return generate_business_invoice_for_organizer(
        organizer,
        make_aware(datetime(2026, 8, 1)),
        make_aware(datetime(2026, 8, 31, 23, 59, 59)),
    )


def subscribe(organizer, version):
    subscription = Subscription.objects.get(organizer=organizer)
    subscription.tier_version = version
    subscription.currency = "EUR"
    subscription.billing_interval = BillingInterval.MONTHLY
    subscription.starts_at = make_aware(datetime(2026, 1, 1))
    subscription.save()
    return subscription


@pytest.mark.django_db
def test_invoice_credits_redeemed_plan_voucher_once(plan_setup):
    organizer, tier, version, price, _ = plan_setup
    subscription = subscribe(organizer, version)
    voucher = make_voucher(tiers=[tier])
    quote = get_voucher_quote("PLAN25", organizer, amount=price.amount, tier=tier)
    redemption = redeem_voucher(quote, organizer, subscription=subscription, tier=tier)
    redeemed_at(redemption, datetime(2026, 7, 15))

    invoice = generate_august_invoice(organizer)

    # The invoice bills August and September in advance; only the first period is discounted.
    assert invoice.lines.filter(line_type=InvoiceLineType.SUBSCRIPTION).count() == 2
    credits = list(invoice.lines.filter(line_type=InvoiceLineType.CREDIT))
    assert [line.amount for line in credits] == [Decimal("-12.25")]
    assert credits[0].service_period_start == make_aware(datetime(2026, 8, 1))
    assert invoice.total == Decimal("85.75")
    redemption.refresh_from_db()
    voucher.refresh_from_db()
    assert redemption.periods_applied == 1
    assert redemption.has_periods_left is False
    assert voucher.budget_used == Decimal("12.25")

    september = generate_business_invoice_for_organizer(
        organizer,
        make_aware(datetime(2026, 9, 1)),
        make_aware(datetime(2026, 9, 30, 23, 59, 59)),
    )
    assert not september.lines.filter(line_type=InvoiceLineType.CREDIT).exists()


@pytest.mark.django_db
def test_invoice_credit_respects_budget_and_plan_changes(plan_setup):
    organizer, tier, version, price, _ = plan_setup
    subscription = subscribe(organizer, version)
    voucher = make_voucher(
        tiers=[tier],
        duration=VoucherDiscountDuration.FOREVER,
        value=Decimal("100.00"),
        budget=Decimal("60.00"),
    )
    quote = get_voucher_quote("PLAN25", organizer, amount=price.amount, tier=tier)
    redemption = redeem_voucher(quote, organizer, subscription=subscription, tier=tier)
    redeemed_at(redemption, datetime(2026, 7, 15))

    invoice = generate_august_invoice(organizer)

    credits = sorted(
        line.amount for line in invoice.lines.filter(line_type=InvoiceLineType.CREDIT)
    )
    assert credits == [Decimal("-49.00"), Decimal("-11.00")]
    voucher.refresh_from_db()
    assert voucher.budget_used == Decimal("60.00")

    other_tier = Tier.objects.create(slug="business", name="Business Tier")
    other_version = TierVersion.objects.create(tier=other_tier, version=1, published_at=now())
    TierPrice.objects.create(
        tier_version=other_version,
        billing_interval=BillingInterval.MONTHLY,
        amount=Decimal("99.00"),
        currency="EUR",
    )
    voucher.budget = None
    voucher.save()
    subscribe(organizer, other_version)
    october = generate_business_invoice_for_organizer(
        organizer,
        make_aware(datetime(2026, 10, 1)),
        make_aware(datetime(2026, 10, 31, 23, 59, 59)),
    )
    assert not october.lines.filter(line_type=InvoiceLineType.CREDIT).exists()


@pytest.mark.django_db
def test_subscription_checkout_attaches_voucher_coupon(plan_setup):
    organizer, tier, _, price, _ = plan_setup
    voucher = make_voucher(
        tiers=[tier], duration=VoucherDiscountDuration.REPEATING, periods=3
    )
    quote = get_voucher_quote("PLAN25", organizer, amount=price.amount, tier=tier)

    with (
        patch(
            "eventyay_business.stripe_service.get_stripe_secret_key_safe",
            return_value="sk_test_123",
        ),
        patch(
            "eventyay_business.stripe_service.get_or_create_stripe_customer",
            return_value="cus_123",
        ),
        patch(
            "eventyay_business.stripe_service.sync_tier_price_to_stripe",
            return_value=None,
        ),
        patch("stripe.Coupon.create", return_value=MagicMock(id="coupon_123")) as coupon,
        patch("stripe.checkout.Session.create") as session,
    ):
        session.return_value = MagicMock(url="https://checkout.stripe.com/c/pay/cs_1")
        create_subscription_checkout_session(
            organizer=organizer,
            tier_price=price,
            user=None,
            success_url="https://example.com/success",
            cancel_url="https://example.com/cancel",
            voucher_quote=quote,
        )

    coupon_kwargs = coupon.call_args.kwargs
    assert coupon_kwargs["percent_off"] == 25.0
    assert coupon_kwargs["duration"] == "repeating"
    assert coupon_kwargs["duration_in_months"] == 3
    session_kwargs = session.call_args.kwargs
    assert session_kwargs["discounts"] == [{"coupon": "coupon_123"}]
    assert session_kwargs["metadata"]["invoice_voucher_id"] == str(voucher.pk)


@pytest.mark.django_db
def test_checkout_completion_records_redemption_once(plan_setup):
    organizer, tier, version, _, _ = plan_setup
    subscription = subscribe(organizer, version)
    voucher = make_voucher(tiers=[tier])
    session_data = {
        "id": "cs_test_voucher",
        "metadata": {"type": "subscription", "invoice_voucher_id": str(voucher.pk)},
        "total_details": {"amount_discount": 1225},
    }

    first = record_checkout_redemption(session_data, subscription)
    second = record_checkout_redemption(session_data, subscription)

    assert first == second
    voucher.refresh_from_db()
    assert voucher.redeemed == 1
    assert voucher.budget_used == Decimal("12.25")
    assert first.tier == tier

    with patch(
        "eventyay_business.stripe_service.process_subscription_checkout_completed",
        return_value=subscription,
    ):
        process_checkout_session_completed(session_data)
    assert VoucherRedemption.objects.count() == 1


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_voucher_quote_endpoint(business_admin_client, plan_setup):
    organizer, tier, _, price, _ = plan_setup
    make_voucher(tiers=[tier], duration=VoucherDiscountDuration.REPEATING, periods=2)
    url = reverse(
        "plugins:eventyay_business:organizer.voucher.quote",
        kwargs={"organizer": organizer.slug},
    )

    data = business_admin_client.get(url, {"code": "PLAN25", "tier_price": price.pk}).json()

    assert data["valid"] is True
    item = data["items"][f"tier_price-{price.pk}"]
    assert item["total"] == "36.75"
    assert item["discount"] == "12.25"
    assert "first 2 billing periods" in item["message"]


@pytest.mark.django_db
def test_admin_voucher_form_saves_targets(business_admin_client, plan_setup):
    _, tier, _, _, addon = plan_setup
    valid_until = now() + timedelta(days=30)
    response = business_admin_client.post(
        reverse("eventyay_admin:admin.vouchers.add"),
        {
            "code": "ADMINPLAN1",
            "status": InvoiceVoucher.STATUS_ACTIVE,
            "valid_until_0": f"{valid_until:%Y-%m-%d}",
            "valid_until_1": "12:00:00",
            "waiver_type": "percent",
            "value": "50.00",
            "max_usages": "10",
            "scope_type": "platform_wide",
            "business-tiers": [tier.pk],
            "business-addons": [addon.pk],
            "business-duration": VoucherDiscountDuration.REPEATING,
            "business-duration_periods": "6",
        },
    )

    assert response.status_code == 302
    targets = VoucherBusinessTargets.objects.get(voucher__code="ADMINPLAN1")
    assert list(targets.tiers.all()) == [tier]
    assert list(targets.addons.all()) == [addon]
    assert targets.period_limit == 6

    detail = business_admin_client.get(
        reverse("eventyay_admin:admin.voucher.detail", kwargs={"voucher": targets.voucher.pk})
    )
    content = detail.content.decode()
    assert "Plans: Pro Tier" in content
    assert "Add-ons: Room Booster" in content


@pytest.mark.django_db
def test_admin_voucher_form_requires_periods_for_repeating(business_admin_client, plan_setup):
    _, tier, _, _, _ = plan_setup
    valid_until = now() + timedelta(days=30)
    response = business_admin_client.post(
        reverse("eventyay_admin:admin.vouchers.add"),
        {
            "code": "ADMINPLAN2",
            "status": InvoiceVoucher.STATUS_ACTIVE,
            "valid_until_0": f"{valid_until:%Y-%m-%d}",
            "valid_until_1": "12:00:00",
            "waiver_type": "percent",
            "value": "50.00",
            "max_usages": "10",
            "scope_type": "platform_wide",
            "business-tiers": [tier.pk],
            "business-duration": VoucherDiscountDuration.REPEATING,
        },
    )

    assert response.status_code == 200
    assert "Enter how many billing periods the discount lasts." in response.content.decode()
    assert not InvoiceVoucher.objects.filter(code="ADMINPLAN2").exists()


@pytest.mark.django_db
def test_invoice_credit_skips_periods_that_ended_before_redemption(plan_setup):
    organizer, tier, version, price, _ = plan_setup
    subscription = subscribe(organizer, version)
    make_voucher(tiers=[tier], value=Decimal("100.00"))
    quote = get_voucher_quote("PLAN25", organizer, amount=price.amount, tier=tier)
    redemption = redeem_voucher(quote, organizer, subscription=subscription, tier=tier)
    redeemed_at(redemption, datetime(2026, 9, 10))

    invoice = generate_august_invoice(organizer)

    credits = list(invoice.lines.filter(line_type=InvoiceLineType.CREDIT))
    assert [line.amount for line in credits] == [Decimal("-49.00")]
    assert credits[0].service_period_start == make_aware(datetime(2026, 9, 1))


@pytest.mark.django_db
def test_invoice_credits_one_time_addon_bought_with_voucher(plan_setup):
    organizer, _, version, _, _ = plan_setup
    subscribe(organizer, version)
    onboarding = AddonDefinition.objects.create(
        name="VIP Onboarding",
        slug="vip-onboarding",
        capability="support.onboarding",
        entitlement_value="true",
        assignment_scope=AddonAssignmentScope.ORGANIZER,
        price=Decimal("250.00"),
        currency="EUR",
        pricing_mode=AddonPricingMode.ONE_TIME,
        active=True,
        public=True,
    )
    make_voucher("ONBOARD20", addons=[onboarding], price_mode="subtract", value=Decimal("20.00"))
    bought_at = make_aware(datetime(2026, 8, 10, 12, 0))
    assignment = OrganizerAddon.objects.create(
        organizer=organizer, addon=onboarding, quantity=1, status=AddonStatus.ACTIVE, starts_at=bought_at
    )
    quote = get_voucher_quote("ONBOARD20", organizer, amount=onboarding.price, addon=onboarding)
    redemption = redeem_addon_voucher(quote, assignment)
    # The redemption is saved just after the add-on line's zero-length service period.
    redeemed_at(redemption, datetime(2026, 8, 10, 12, 0, 1))

    invoice = generate_august_invoice(organizer)

    credits = [line.amount for line in invoice.lines.filter(line_type=InvoiceLineType.CREDIT)]
    assert credits == [Decimal("-20.00")]
