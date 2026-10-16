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


def test_preflight_requires_billing_setup_authorization_and_no_stripe_subscription(db):
    organizer = Organizer.objects.create(name="Migration Org", slug="migration-org")

    result = consolidated_billing_preflight(organizer)
    assert result.ready is False
    assert result.billing_customer_ready is False
    assert result.payment_method_ready is False
    assert result.collection_authorized is False

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

    enable_consolidated_billing(organizer)
    first_cutover = organizer.settings.get(BILLING_CUTOVER_SETTING, as_type=str)
    assert first_cutover

    enable_consolidated_billing(organizer)
    assert organizer.settings.get(BILLING_CUTOVER_SETTING, as_type=str) == first_cutover
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
    assert enable_url.encode() in response.content

    response = admin_client.post(enable_url)
    assert response.status_code == 302
    organizer.refresh_from_db()
    first_cutover = organizer.settings.get(BILLING_CUTOVER_SETTING, as_type=str)
    assert first_cutover

    response = admin_client.post(enable_url)
    assert response.status_code == 302
    organizer.refresh_from_db()
    assert organizer.settings.get(BILLING_CUTOVER_SETTING, as_type=str) == first_cutover

    response = admin_client.get(list_url)
    assert b"Active since" in response.content
    assert enable_url.encode() not in response.content
