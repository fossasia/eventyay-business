"""Stripe SDK object compatibility.

Stripe Python SDK 15 stopped making ``StripeObject`` a ``dict`` subclass. These
tests feed real SDK objects (not plain dicts) through the code paths that read
Stripe payloads, so they fail if dict-only access sneaks back in.
"""

import pytest
import stripe
from decimal import Decimal
from django.test import RequestFactory, override_settings
from django.urls import reverse
from django.utils.timezone import now
from unittest.mock import patch

from eventyay_business.invoice_collection import payment_intent_metadata
from eventyay_business.models import (
    BillingInterval,
    Subscription,
    Tier,
    TierPrice,
    TierStatus,
    TierVersion,
)
from eventyay_business.stripe_objects import stripe_to_dict
from eventyay_business.stripe_service import fulfill_checkout_session_by_id
from eventyay_business.views_stripe import stripe_business_webhook_view

from .test_subscription_lifecycle import lifecycle_data  # noqa: F401


def stripe_object(cls, payload):
    return cls.construct_from(payload, "sk_test_123")


def test_stripe_to_dict_converts_sdk_objects_with_nested_fields():
    session = stripe_object(
        stripe.checkout.Session,
        {
            "id": "cs_test_1",
            "object": "checkout.session",
            "metadata": {"voucher_reservation_id": "42"},
            "total_details": {"amount_discount": 500},
        },
    )

    data = stripe_to_dict(session)

    assert isinstance(data, dict)
    assert data.get("id") == "cs_test_1"
    assert data.get("metadata").get("voucher_reservation_id") == "42"
    assert (data.get("total_details") or {}).get("amount_discount") == 500


def test_stripe_to_dict_passes_through_plain_values():
    payload = {"id": "cs_plain"}

    assert stripe_to_dict(payload) is payload
    assert stripe_to_dict(None) == {}


def test_payment_intent_metadata_reads_sdk_object():
    intent = stripe_object(
        stripe.PaymentIntent,
        {
            "id": "pi_test_1",
            "object": "payment_intent",
            "metadata": {"eventyay_business_invoice_id": "7"},
        },
    )

    assert payment_intent_metadata(intent) == {"eventyay_business_invoice_id": "7"}
    assert payment_intent_metadata({"id": "pi_no_metadata"}) == {}


def test_webhook_passes_dict_payload_from_real_stripe_event():
    event = stripe_object(
        stripe.Event,
        {
            "id": "evt_test_1",
            "object": "event",
            "type": "checkout.session.expired",
            "data": {
                "object": {
                    "id": "cs_test_1",
                    "object": "checkout.session",
                    "metadata": {"voucher_reservation_id": "42"},
                }
            },
        },
    )
    request = RequestFactory().post(
        "/control/business/stripe/webhook/",
        data=b'{"id": "evt_test_1"}',
        content_type="application/json",
        HTTP_STRIPE_SIGNATURE="sig_valid_123",
    )

    with (
        patch(
            "eventyay.helpers.stripe_utils.get_stripe_webhook_secret_key",
            return_value="whsec_test_secret",
        ),
        patch("stripe.Webhook.construct_event", return_value=event),
        patch("eventyay_business.views_stripe.process_webhook_event") as mock_process,
    ):
        response = stripe_business_webhook_view(request)

    assert response.status_code == 200
    event_type, data_object = mock_process.call_args.args
    assert event_type == "checkout.session.expired"
    assert isinstance(data_object, dict)
    assert data_object.get("metadata").get("voucher_reservation_id") == "42"


def test_fulfill_checkout_session_by_id_converts_retrieved_session():
    session = stripe_object(
        stripe.checkout.Session,
        {
            "id": "cs_test_1",
            "object": "checkout.session",
            "payment_status": "paid",
            "metadata": {"type": "subscription"},
        },
    )

    with (
        patch(
            "eventyay_business.stripe_service.get_stripe_secret_key_safe",
            return_value="sk_test_123",
        ),
        patch(
            "eventyay_business.stripe_service.stripe.checkout.Session.retrieve",
            return_value=session,
        ),
        patch(
            "eventyay_business.stripe_service.process_checkout_session_completed",
            return_value="fulfilled",
        ) as mock_complete,
    ):
        assert fulfill_checkout_session_by_id("cs_test_1") == "fulfilled"

    session_data = mock_complete.call_args.args[0]
    assert isinstance(session_data, dict)
    assert session_data.get("metadata").get("type") == "subscription"


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_paid_to_paid_downgrade_reads_items_from_sdk_subscription(
    client, lifecycle_data  # noqa: F811
):
    organizer, _, _, _, _, pro_version, pro_price, user = lifecycle_data
    pro_price.stripe_price_id = "price_pro_monthly"
    pro_price.save(update_fields=["stripe_price_id"])

    plus_tier = Tier.objects.create(
        name="Plus Tier",
        slug="plus-tier",
        status=TierStatus.PUBLISHED,
        is_public=True,
        display_order=2,
    )
    plus_version = TierVersion.objects.create(
        tier=plus_tier, version=1, published_at=now()
    )
    TierPrice.objects.create(
        tier_version=plus_version,
        billing_interval=BillingInterval.MONTHLY,
        amount=Decimal("100.00"),
        currency="USD",
        active=True,
    )

    sub = Subscription.objects.get(organizer=organizer)
    sub.tier_version = plus_version
    sub.billing_interval = BillingInterval.MONTHLY
    sub.stripe_subscription_id = "sub_stripe_paid_downgrade"
    sub.save()

    stripe_sub = stripe_object(
        stripe.Subscription,
        {
            "id": "sub_stripe_paid_downgrade",
            "object": "subscription",
            "items": {
                "object": "list",
                "data": [{"id": "si_test_1", "object": "subscription_item"}],
            },
        },
    )

    client.force_login(user)
    with (
        patch("eventyay_business.views.is_stripe_configured", return_value=True),
        patch(
            "eventyay_business.views.get_stripe_secret_key_safe",
            return_value="sk_test_123",
        ),
        patch(
            "eventyay_business.views.stripe.Subscription.retrieve",
            return_value=stripe_sub,
        ),
        patch("eventyay_business.views.stripe.Subscription.modify") as mock_modify,
    ):
        response = client.post(
            reverse(
                "plugins:eventyay_business:organizer.plan.upgrade",
                kwargs={"organizer": organizer.slug},
            ),
            {"tier_price_id": pro_price.id},
            follow=True,
        )

    assert response.status_code == 200
    mock_modify.assert_called_once_with(
        "sub_stripe_paid_downgrade",
        cancel_at_period_end=False,
        proration_behavior="none",
        items=[{"id": "si_test_1", "price": "price_pro_monthly"}],
    )
    sub.refresh_from_db()
    assert sub.pending_tier_version == pro_version
