import pytest
from datetime import timedelta
from decimal import Decimal
from django.contrib.messages import get_messages
from django.test import override_settings
from django.urls import reverse
from django.utils.timezone import now
from eventyay.base.entitlements import check_entitlement
from eventyay.base.models import Organizer, Team, User
from eventyay.base.models.auth import StaffSession
from unittest.mock import patch

from eventyay_business.models import (
    BillingInterval,
    Subscription,
    SubscriptionStatus,
    Tier,
    TierEntitlement,
    TierPrice,
    TierStatus,
    TierVersion,
)
from eventyay_business.signals import (
    subscription_downgraded,
    subscription_expired,
)
from eventyay_business.tasks import (
    manage_subscription_lifecycles,
    manage_subscription_lifecycles_task,
    periodic_manage_subscription_lifecycles,
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
def lifecycle_data():
    organizer = Organizer.objects.create(name="Lifecycle Org", slug="lifecycle-org")

    # Free tier
    free_tier, _ = Tier.objects.get_or_create(
        slug="free",
        defaults={
            "name": "Free Tier",
            "status": TierStatus.PUBLISHED,
            "is_public": True,
        },
    )
    free_version = free_tier.versions.first()
    if not free_version:
        free_version = TierVersion.objects.create(
            tier=free_tier, version=1, published_at=now()
        )
    TierEntitlement.objects.get_or_create(
        tier_version=free_version,
        capability="organizer.full_admins",
        defaults={"value": "1"},
    )

    free_price = TierPrice.objects.create(
        tier_version=free_version,
        billing_interval=BillingInterval.MONTHLY,
        amount=Decimal("0.00"),
        currency="USD",
        active=True,
    )

    # Pro tier
    pro_tier = Tier.objects.create(
        name="Pro Tier",
        slug="pro-tier",
        status=TierStatus.PUBLISHED,
        is_public=True,
    )
    pro_version = TierVersion.objects.create(
        tier=pro_tier, version=1, published_at=now()
    )
    pro_price = TierPrice.objects.create(
        tier_version=pro_version,
        billing_interval=BillingInterval.MONTHLY,
        amount=Decimal("50.00"),
        currency="USD",
        active=True,
    )
    TierEntitlement.objects.create(
        tier_version=pro_version,
        capability="organizer.full_admins",
        value="10",
    )

    user = User.objects.create_user(
        email="orgowner@lifecycle-org.com", password="secretpassword"
    )
    team = Team.objects.create(
        organizer=organizer,
        name="Admins",
        can_change_organizer_settings=True,
        can_create_events=True,
        can_change_teams=True,
    )
    team.members.add(user)

    return (
        organizer,
        free_tier,
        free_version,
        free_price,
        pro_tier,
        pro_version,
        pro_price,
        user,
    )


@pytest.mark.django_db
def test_immediate_upgrade_unlocks_entitlements_and_clears_pending(
    lifecycle_data,
):
    (
        organizer,
        free_tier,
        free_version,
        free_price,
        pro_tier,
        pro_version,
        pro_price,
        user,
    ) = lifecycle_data

    # Initially has free tier subscription with limit of 1 admin
    sub = Subscription.objects.get(organizer=organizer)
    assert sub.tier_version == free_version

    # Set up pending downgrade to test that an upgrade clears it
    sub.pending_tier_version = free_version
    sub.pending_change_at = now() + timedelta(days=10)
    sub.save()

    # Simulate upgrade via checkout completion or direct upgrade
    from eventyay_business.stripe_service import (
        process_checkout_session_completed,
    )

    pro_price = pro_version.prices.first()
    session_data = {
        "metadata": {
            "type": "subscription",
            "organizer_slug": organizer.slug,
            "tier_price_id": str(pro_price.id),
        },
        "customer": "cus_test_upg",
        "subscription": "sub_test_upg",
    }

    with patch(
        "eventyay_business.stripe_service.stripe.Subscription.retrieve"
    ) as mock_retrieve:
        mock_retrieve.return_value = {
            "id": "sub_test_upg",
            "current_period_end": int((now() + timedelta(days=30)).timestamp()),
        }
        process_checkout_session_completed(session_data)

    sub.refresh_from_db()
    assert sub.tier_version == pro_version
    assert sub.status == SubscriptionStatus.ACTIVE
    assert sub.pending_tier_version is None
    assert sub.pending_change_at is None

    # Entitlement decision is now 10
    decision = check_entitlement(
        organizer, "organizer.full_admins", quantity=5, user=user
    )
    assert decision is not None
    assert decision.allowed is True


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_scheduled_downgrade_preserves_entitlements_until_renewal(
    client, lifecycle_data
):
    (
        organizer,
        free_tier,
        free_version,
        free_price,
        pro_tier,
        pro_version,
        pro_price,
        user,
    ) = lifecycle_data
    client.force_login(user)

    # Start with active Pro subscription ending in 15 days
    sub = Subscription.objects.get(organizer=organizer)
    sub.tier_version = pro_version
    sub.billing_interval = BillingInterval.MONTHLY
    sub.starts_at = now() - timedelta(days=15)
    sub.ends_at = now() + timedelta(days=15)
    sub.stripe_subscription_id = "sub_stripe_downgrade"
    sub.save()

    # Pro entitlements active
    decision = check_entitlement(
        organizer, "organizer.full_admins", quantity=5, user=user
    )
    assert decision.allowed is True

    # Post downgrade request to Free tier
    url = reverse(
        "plugins:eventyay_business:organizer.plan.upgrade",
        kwargs={"organizer": organizer.slug},
    )
    with (
        patch("eventyay_business.views.is_stripe_configured", return_value=True),
        patch(
            "eventyay_business.views.get_stripe_secret_key_safe",
            return_value="sk_test_123",
        ),
        patch(
            "eventyay_business.stripe_service.stripe.Subscription.modify"
        ) as mock_modify,
    ):
        response = client.post(
            url,
            {"tier_price_id": free_price.id},
            follow=True,
        )
        assert response.status_code == 200
        mock_modify.assert_called_once_with(
            "sub_stripe_downgrade", cancel_at_period_end=True
        )

    sub.refresh_from_db()
    assert sub.has_scheduled_downgrade is True
    assert sub.pending_tier_version == free_version
    assert sub.pending_change_at == sub.ends_at
    assert sub.tier_version == pro_version  # still Pro!

    # Entitlements should STILL be Pro entitlements until pending_change_at
    decision_during_downgrade = check_entitlement(
        organizer, "organizer.full_admins", quantity=5, user=user
    )
    assert decision_during_downgrade.allowed is True


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_scheduled_downgrade_excess_usage_warning_non_destructive(
    client, lifecycle_data
):
    (
        organizer,
        free_tier,
        free_version,
        free_price,
        pro_tier,
        pro_version,
        pro_price,
        user,
    ) = lifecycle_data
    client.force_login(user)

    # Setup active Pro subscription
    sub = Subscription.objects.get(organizer=organizer)
    sub.tier_version = pro_version
    sub.billing_interval = BillingInterval.MONTHLY
    sub.ends_at = now() + timedelta(days=15)
    sub.save()

    # Add 2 more admins so usage = 3 (exceeding free limit of 1)
    admin_team = organizer.teams.first()
    u2 = User.objects.create_user(email="u2@org.com", password="pw")
    u3 = User.objects.create_user(email="u3@org.com", password="pw")
    admin_team.members.add(u2, u3)

    admins_count = (
        User.objects.filter(
            teams__organizer=organizer, teams__can_change_organizer_settings=True
        )
        .distinct()
        .count()
    )
    assert admins_count == 3

    # Post downgrade to Free
    url = reverse(
        "plugins:eventyay_business:organizer.plan.upgrade",
        kwargs={"organizer": organizer.slug},
    )
    response = client.post(
        url,
        {"tier_price_id": free_price.id},
        follow=True,
    )
    assert response.status_code == 200

    # Verify warning message was displayed
    messages = list(get_messages(response.wsgi_request))
    warning_msgs = [str(m) for m in messages if "which exceeds the limit of" in str(m)]
    assert len(warning_msgs) > 0

    # Verify non-destructive: no admins or data were deleted
    admins_after = (
        User.objects.filter(
            teams__organizer=organizer, teams__can_change_organizer_settings=True
        )
        .distinct()
        .count()
    )
    assert admins_after == 3
    sub.refresh_from_db()
    assert sub.pending_tier_version == free_version


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_cancel_scheduled_downgrade(client, lifecycle_data):
    (
        organizer,
        free_tier,
        free_version,
        free_price,
        pro_tier,
        pro_version,
        pro_price,
        user,
    ) = lifecycle_data
    client.force_login(user)

    sub = Subscription.objects.get(organizer=organizer)
    sub.tier_version = pro_version
    sub.pending_tier_version = free_version
    sub.pending_change_at = now() + timedelta(days=10)
    sub.stripe_subscription_id = "sub_stripe_cancel"
    sub.save()

    assert sub.has_scheduled_downgrade is True

    url = reverse(
        "plugins:eventyay_business:organizer.plan.cancel_downgrade",
        kwargs={"organizer": organizer.slug},
    )
    with (
        patch("eventyay_business.views.is_stripe_configured", return_value=True),
        patch(
            "eventyay_business.views.get_stripe_secret_key_safe",
            return_value="sk_test_123",
        ),
        patch("stripe.Subscription.modify") as mock_modify,
    ):
        response = client.post(url, follow=True)
        assert response.status_code == 200
        mock_modify.assert_called_once_with(
            "sub_stripe_cancel", cancel_at_period_end=False
        )

    sub.refresh_from_db()
    assert sub.has_scheduled_downgrade is False
    assert sub.pending_tier_version is None
    assert sub.pending_change_at is None
    assert sub.tier_version == pro_version


@pytest.mark.django_db
def test_past_due_grace_period_and_expiration(lifecycle_data):
    (
        organizer,
        free_tier,
        free_version,
        free_price,
        pro_tier,
        pro_version,
        pro_price,
        user,
    ) = lifecycle_data

    sub = Subscription.objects.get(organizer=organizer)
    sub.tier_version = pro_version
    sub.status = SubscriptionStatus.PAST_DUE
    sub.starts_at = now() - timedelta(days=35)
    sub.ends_at = now() - timedelta(days=5)  # period ended
    sub.past_due_since = now() - timedelta(days=3)  # 3 days past due
    sub.save()

    # Within 7-day grace period
    assert sub.is_in_grace_period() is True
    assert sub.grace_period_ends_at() is not None

    decision = check_entitlement(
        organizer, "organizer.full_admins", quantity=5, user=user
    )
    assert decision is not None
    assert decision.allowed is True

    # Beyond 7-day grace period
    sub.past_due_since = now() - timedelta(days=8)
    sub.save()

    assert sub.is_in_grace_period() is False
    decision_expired = check_entitlement(
        organizer, "organizer.full_admins", quantity=5, user=user
    )
    # Beyond grace period without platform admin, should not allow 5 admins
    assert decision_expired.allowed is False

    # When past_due_since is missing, grace period is False
    sub.past_due_since = None
    assert sub.is_in_grace_period() is False
    assert sub.grace_period_ends_at() is None


@pytest.mark.django_db
def test_platform_administrator_bypass(lifecycle_data):
    (
        organizer,
        free_tier,
        free_version,
        free_price,
        pro_tier,
        pro_version,
        pro_price,
        user,
    ) = lifecycle_data

    sub = Subscription.objects.get(organizer=organizer)
    sub.status = SubscriptionStatus.EXPIRED
    sub.save()

    # Non-staff user should be denied
    non_staff_decision = check_entitlement(
        organizer, "organizer.full_admins", quantity=100, user=user
    )
    assert non_staff_decision is not None
    assert non_staff_decision.allowed is False

    staff_user = User.objects.create_user(
        email="admin@platform.com", password="pw", is_staff=True
    )
    decision = check_entitlement(
        organizer, "organizer.full_admins", quantity=100, user=staff_user
    )
    assert decision is not None
    assert decision.allowed is True


@pytest.mark.django_db
def test_manage_subscription_lifecycles_task(
    lifecycle_data, django_capture_on_commit_callbacks
):
    (
        organizer,
        free_tier,
        free_version,
        free_price,
        pro_tier,
        pro_version,
        pro_price,
        user,
    ) = lifecycle_data

    # 1. Overdue scheduled downgrade
    sub1 = Subscription.objects.get(organizer=organizer)
    sub1.tier_version = pro_version
    sub1.pending_tier_version = free_version
    sub1.pending_change_at = now() - timedelta(minutes=5)
    sub1.save()

    # 2. Overdue past_due grace period
    org2 = Organizer.objects.create(name="Past Due Org", slug="past-due-org")
    sub2 = Subscription.objects.get(organizer=org2)
    sub2.tier_version = pro_version
    sub2.status = SubscriptionStatus.PAST_DUE
    sub2.past_due_since = now() - timedelta(days=8)
    sub2.save()

    downgraded_signals = []
    expired_signals = []

    def on_downgraded(sender, instance, **kwargs):
        downgraded_signals.append(instance)

    def on_expired(sender, instance, **kwargs):
        expired_signals.append(instance)

    subscription_downgraded.connect(on_downgraded)
    subscription_expired.connect(on_expired)

    try:
        with django_capture_on_commit_callbacks(execute=True):
            result = manage_subscription_lifecycles()
        assert result["downgraded"] == 1
        assert result["expired"] == 1

        sub1.refresh_from_db()
        assert sub1.tier_version == free_version
        assert sub1.pending_tier_version is None
        assert sub1.pending_change_at is None
        assert len(downgraded_signals) == 1

        sub2.refresh_from_db()
        assert sub2.status == SubscriptionStatus.EXPIRED
        assert len(expired_signals) == 1

        # Check celery task wrapper and periodic receiver
        assert manage_subscription_lifecycles_task() is not None
        if periodic_manage_subscription_lifecycles:
            periodic_res = periodic_manage_subscription_lifecycles(sender=None)
            assert periodic_res is not None
    finally:
        subscription_downgraded.disconnect(on_downgraded)
        subscription_expired.disconnect(on_expired)


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_plan_view_banners(client, lifecycle_data):
    (
        organizer,
        free_tier,
        free_version,
        free_price,
        pro_tier,
        pro_version,
        pro_price,
        user,
    ) = lifecycle_data
    client.force_login(user)

    # Test scheduled downgrade banner
    sub = Subscription.objects.get(organizer=organizer)
    sub.tier_version = pro_version
    sub.pending_tier_version = free_version
    sub.pending_change_at = now() + timedelta(days=7)
    sub.save()

    url = reverse(
        "plugins:eventyay_business:organizer.plan",
        kwargs={"organizer": organizer.slug},
    )
    res = client.get(url)
    assert res.status_code == 200
    content = res.content.decode("utf-8")
    assert "Scheduled Plan Change:" in content
    assert "Keep Current Plan" in content

    # Test past-due grace period banner
    sub.pending_tier_version = None
    sub.pending_change_at = None
    sub.status = SubscriptionStatus.PAST_DUE
    sub.ends_at = now() - timedelta(days=2)
    sub.past_due_since = now() - timedelta(days=2)
    sub.save()

    res_pd = client.get(url)
    assert res_pd.status_code == 200
    content_pd = res_pd.content.decode("utf-8")
    assert "Payment Past Due:" in content_pd
    assert "7-day grace period" in content_pd


@pytest.mark.django_db
@override_settings(EVENTYAY_BUSINESS_GRACE_PERIOD_DAYS=14)
def test_configurable_grace_period_via_django_settings(lifecycle_data):
    (
        organizer,
        free_tier,
        free_version,
        free_price,
        pro_tier,
        pro_version,
        pro_price,
        user,
    ) = lifecycle_data

    sub = Subscription.objects.get(organizer=organizer)
    sub.status = SubscriptionStatus.PAST_DUE
    sub.past_due_since = now() - timedelta(days=10)
    sub.save()

    assert sub.grace_period_days == 14
    assert sub.is_in_grace_period() is True

    sub.past_due_since = now() - timedelta(days=15)
    sub.save()
    assert sub.is_in_grace_period() is False


@pytest.mark.django_db
def test_configurable_grace_period_via_subscription_snapshot(lifecycle_data):
    (
        organizer,
        free_tier,
        free_version,
        free_price,
        pro_tier,
        pro_version,
        pro_price,
        user,
    ) = lifecycle_data

    sub = Subscription.objects.get(organizer=organizer)
    sub.status = SubscriptionStatus.PAST_DUE
    sub.configuration_snapshot = {"grace_period_days": 3}
    sub.past_due_since = now() - timedelta(days=2)
    sub.save()

    assert sub.grace_period_days == 3
    assert sub.is_in_grace_period() is True

    sub.past_due_since = now() - timedelta(days=4)
    sub.save()
    assert sub.is_in_grace_period() is False


@pytest.mark.django_db
def test_configurable_grace_period_via_tier_version_snapshot(lifecycle_data):
    (
        organizer,
        free_tier,
        free_version,
        free_price,
        pro_tier,
        pro_version,
        pro_price,
        user,
    ) = lifecycle_data

    pro_version.configuration_snapshot = {"grace_period_days": 5}
    pro_version.save()

    sub = Subscription.objects.get(organizer=organizer)
    sub.tier_version = pro_version
    sub.status = SubscriptionStatus.PAST_DUE
    sub.past_due_since = now() - timedelta(days=4)
    sub.save()

    assert sub.grace_period_days == 5
    assert sub.is_in_grace_period() is True

    sub.past_due_since = now() - timedelta(days=6)
    sub.save()
    assert sub.is_in_grace_period() is False


@pytest.mark.django_db
@override_settings(
    SITE_URL="https://testserver", EVENTYAY_BUSINESS_GRACE_PERIOD_DAYS=14
)
def test_plan_view_renders_configured_grace_period(client, lifecycle_data):
    (
        organizer,
        free_tier,
        free_version,
        free_price,
        pro_tier,
        pro_version,
        pro_price,
        user,
    ) = lifecycle_data
    client.force_login(user)

    sub = Subscription.objects.get(organizer=organizer)
    sub.tier_version = pro_version
    sub.status = SubscriptionStatus.PAST_DUE
    sub.past_due_since = now() - timedelta(days=5)
    sub.save()

    url = reverse(
        "plugins:eventyay_business:organizer.plan",
        kwargs={"organizer": organizer.slug},
    )
    res = client.get(url)
    assert res.status_code == 200
    content = res.content.decode("utf-8")
    assert "14-day grace period" in content
