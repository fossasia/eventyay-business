import pytest
from django.core.exceptions import ValidationError
from django.utils.timezone import now
from eventyay.base.models import Organizer
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
    assert organizer.settings.get(BILLING_CUTOVER_SETTING, as_type=str)
