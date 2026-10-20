import pytest
from django.core.exceptions import ValidationError
from django.test import override_settings
from django.urls import reverse
from django.utils.timezone import now
from eventyay.base.models import Organizer
from eventyay.base.models.auth import StaffSession
from eventyay.base.models.organizer import OrganizerBillingModel

from eventyay_business.billing_migration import (
    consolidated_billing_preflight,
    enable_consolidated_billing,
)
from eventyay_business.models import (
    BillingCollectionAuthorization,
    Subscription,
    SubscriptionStatus,
)
from eventyay_business.platform_fee_ledger import BILLING_CUTOVER_SETTING
from eventyay_business.reconciliation import (
    accept_reconciliation_result,
    run_billing_reconciliation,
)


def accept_latest_reconciliation(organizer, user=None):
    run = run_billing_reconciliation(organizers=[organizer])
    return accept_reconciliation_result(run.results.get(organizer=organizer).pk, user)


def test_preflight_requires_billing_setup_authorization_and_no_stripe_subscription(db):
    organizer = Organizer.objects.create(name="Migration Org", slug="migration-org")

    result = consolidated_billing_preflight(organizer)
    assert result.ready is False
    assert result.billing_customer_ready is False
    assert result.payment_method_ready is False
    assert result.collection_authorized is False
    assert result.reconciliation_ready is False

    billing = OrganizerBillingModel.objects.create(
        organizer=organizer,
        stripe_customer_id="cus_migration",
        stripe_payment_method_id="pm_migration",
    )
    authorization = BillingCollectionAuthorization.objects.create(
        organizer=organizer,
        stripe_customer_id=billing.stripe_customer_id,
        authorized_at=now(),
    )

    result = consolidated_billing_preflight(organizer)
    assert result.ready is False
    accept_latest_reconciliation(organizer)
    result = consolidated_billing_preflight(organizer)
    assert result.reconciliation_ready is True
    assert result.ready is True

    authorization.stripe_customer_id = "cus_other"
    authorization.save()
    assert consolidated_billing_preflight(organizer).ready is False

    authorization.stripe_customer_id = billing.stripe_customer_id
    authorization.save()

    subscription = Subscription.objects.get(organizer=organizer)
    subscription.status = SubscriptionStatus.ACTIVE
    subscription.stripe_subscription_id = "sub_migration"
    subscription.save()

    result = consolidated_billing_preflight(organizer)
    assert result.ready is False
    assert result.active_stripe_subscription_ids == ("sub_migration",)

    subscription.status = SubscriptionStatus.PAST_DUE
    subscription.save()

    result = consolidated_billing_preflight(organizer)
    assert result.ready is False
    assert result.active_stripe_subscription_ids == ("sub_migration",)


def test_enable_consolidated_billing_requires_ready_preflight(db):
    organizer = Organizer.objects.create(name="Cutover Org", slug="cutover-org")

    with pytest.raises(ValidationError):
        enable_consolidated_billing(organizer)

    billing = OrganizerBillingModel.objects.create(
        organizer=organizer,
        stripe_customer_id="cus_cutover",
        stripe_payment_method_id="pm_cutover",
    )
    BillingCollectionAuthorization.objects.create(
        organizer=organizer,
        stripe_customer_id=billing.stripe_customer_id,
        authorized_at=now(),
    )
    accept_latest_reconciliation(organizer)

    activated_organizer = enable_consolidated_billing(organizer)
    first_cutover = activated_organizer.settings.get(
        BILLING_CUTOVER_SETTING, as_type=str
    )
    assert first_cutover

    activated_organizer = enable_consolidated_billing(organizer)
    assert (
        activated_organizer.settings.get(BILLING_CUTOVER_SETTING, as_type=str)
        == first_cutover
    )
    organizer = Organizer.objects.get(pk=organizer.pk)
    assert consolidated_billing_preflight(organizer).activated_at is not None


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_admin_activation_shows_preflight_and_preserves_cutover(
    admin_client, admin_user
):
    session = admin_client.session
    session.save()
    StaffSession.objects.create(
        user=admin_user,
        session_key=session.session_key,
        comment="test",
    )
    admin_user.is_staff = True
    admin_user.save()

    organizer = Organizer.objects.create(name="Activation Org", slug="activation-org")
    billing = OrganizerBillingModel.objects.create(
        organizer=organizer,
        stripe_customer_id="cus_activation",
        stripe_payment_method_id="pm_activation",
    )
    BillingCollectionAuthorization.objects.create(
        organizer=organizer,
        stripe_customer_id=billing.stripe_customer_id,
        authorized_at=now(),
    )
    accept_latest_reconciliation(organizer, admin_user)
    list_url = reverse("plugins:eventyay_business:billing_activation")
    enable_url = reverse(
        "plugins:eventyay_business:billing_activation.enable",
        kwargs={"organizer": organizer.slug},
    )

    response = admin_client.get(list_url)
    assert response.status_code == 200
    assert b"Activation Org" in response.content
    assert b"Before activation" in response.content
    assert b"Ready" in response.content
    assert b"Authorized" in response.content
    assert b"Accepted" in response.content
    assert b"Review required" not in response.content
    assert enable_url.encode() in response.content

    response = admin_client.post(enable_url)
    assert response.status_code == 302
    organizer = Organizer.objects.get(pk=organizer.pk)
    first_cutover = organizer.settings.get(BILLING_CUTOVER_SETTING, as_type=str)
    assert first_cutover

    response = admin_client.post(enable_url)
    assert response.status_code == 302
    organizer = Organizer.objects.get(pk=organizer.pk)
    assert organizer.settings.get(BILLING_CUTOVER_SETTING, as_type=str) == first_cutover

    response = admin_client.get(list_url)
    assert b"Active since" in response.content
    assert enable_url.encode() not in response.content

    monitor = admin_client.get(
        reverse("plugins:eventyay_business:billing_rollout.monitor")
    )
    assert monitor.status_code == 200
    assert b"Activation Org" in monitor.content
    assert b"Awaiting first invoice" in monitor.content


@pytest.mark.django_db
def test_batch_activation_rechecks_and_skips_blocked_organizers(
    admin_client, admin_user
):
    session = admin_client.session
    session.save()
    StaffSession.objects.create(
        user=admin_user,
        session_key=session.session_key,
        comment="test",
    )
    admin_user.is_staff = True
    admin_user.save(update_fields=["is_staff"])

    ready = Organizer.objects.create(name="Ready Org", slug="ready-org")
    billing = OrganizerBillingModel.objects.create(
        organizer=ready,
        stripe_customer_id="cus_ready",
        stripe_payment_method_id="pm_ready",
    )
    BillingCollectionAuthorization.objects.create(
        organizer=ready,
        stripe_customer_id=billing.stripe_customer_id,
        authorized_at=now(),
    )
    accept_latest_reconciliation(ready, admin_user)
    blocked = Organizer.objects.create(name="Blocked Org", slug="blocked-org")

    response = admin_client.post(
        reverse("plugins:eventyay_business:billing_activation.batch"),
        {"organizers": [ready.slug, blocked.slug]},
    )

    assert response.status_code == 302
    ready = Organizer.objects.get(pk=ready.pk)
    blocked = Organizer.objects.get(pk=blocked.pk)
    assert ready.settings.get(BILLING_CUTOVER_SETTING, as_type=str)
    assert not blocked.settings.get(BILLING_CUTOVER_SETTING, as_type=str)


@pytest.mark.django_db
def test_auto_unblock_activation_unblocks_all_organizers(admin_client, admin_user):
    from eventyay_business.models import Tier, TierVersion

    session = admin_client.session
    session.save()
    StaffSession.objects.create(
        user=admin_user,
        session_key=session.session_key,
        comment="test",
    )
    admin_user.is_staff = True
    admin_user.save(update_fields=["is_staff"])

    org = Organizer.objects.create(name="Blocked Org", slug="blocked-org")
    tier = Tier.objects.create(name="Pro Tier", slug="pro-tier")
    tier_version = TierVersion.objects.create(tier=tier, version=1)
    sub = Subscription.objects.get(organizer=org)
    sub.tier_version = tier_version
    sub.status = SubscriptionStatus.ACTIVE
    sub.stripe_subscription_id = "sub_legacy123"
    sub.configuration_snapshot = {
        "uncanceled_stripe_subscription_ids": ["sub_legacy123"]
    }
    sub.save()

    preflight = consolidated_billing_preflight(org)
    assert not preflight.ready
    assert preflight.active_stripe_subscription_ids == ("sub_legacy123",)

    response = admin_client.post(
        reverse("plugins:eventyay_business:billing_activation.auto_unblock"),
    )
    assert response.status_code == 302

    org = Organizer.objects.get(pk=org.pk)
    assert org.settings.get(BILLING_CUTOVER_SETTING, as_type=str)

    sub.refresh_from_db()
    assert sub.status == SubscriptionStatus.ACTIVE
    assert sub.tier_version == tier_version
    assert sub.stripe_subscription_id == ""
    assert (
        sub.configuration_snapshot.get("legacy_stripe_subscription_id")
        == "sub_legacy123"
    )
    assert "uncanceled_stripe_subscription_ids" not in sub.configuration_snapshot

    preflight = consolidated_billing_preflight(org)
    assert preflight.ready
    assert preflight.activated_at is not None
