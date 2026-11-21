import hashlib
import hmac
import json
import pytest
import stripe
import time
from django.test import RequestFactory
from unittest.mock import patch

from eventyay_business.invoice_collection import payment_intent_metadata
from eventyay_business.stripe_objects import stripe_object_to_dict
from eventyay_business.stripe_service import process_webhook_event
from eventyay_business.views_stripe import stripe_business_webhook_view

from .stripe_sdk import ObjectWithoutDictMethods


def test_stripe_object_to_dict_supports_dict_methods_on_api_objects():
    subscription = stripe_object_to_dict(
        stripe.StripeObject.construct_from(
            {"id": "sub_1", "metadata": {"tier_price_id": "7"}}, "sk_test_dummy"
        )
    )

    assert subscription.get("id") == "sub_1"
    assert subscription.get("metadata", {}).get("tier_price_id") == "7"
    assert subscription.get("missing") is None


def test_stripe_object_to_dict_converts_objects_without_dict_methods():
    subscription = stripe_object_to_dict(
        ObjectWithoutDictMethods({"items": {"data": [{"id": "si_1"}]}})
    )

    assert subscription.get("items", {}).get("data") == [{"id": "si_1"}]


def test_stripe_object_to_dict_keeps_plain_dicts_and_handles_none():
    data = {"id": "sub_1"}

    assert stripe_object_to_dict(data) is data
    assert stripe_object_to_dict(None) == {}


@pytest.mark.parametrize(
    "event_type,handler",
    [
        ("checkout.session.completed", "process_checkout_session_completed"),
        ("checkout.session.expired", "release_checkout_reservation"),
        ("payment_intent.succeeded", "process_business_payment_intent"),
        ("invoice.paid", "process_invoice_paid"),
        ("invoice.payment_failed", "process_invoice_payment_failed"),
    ],
)
def test_webhook_handlers_receive_dicts(event_type, handler):
    data_object = ObjectWithoutDictMethods({"id": "obj_1", "metadata": {"a": "b"}})

    with patch(f"eventyay_business.stripe_service.{handler}") as mock_handler:
        process_webhook_event(event_type, data_object)

    mock_handler.assert_called_once_with({"id": "obj_1", "metadata": {"a": "b"}})


def test_subscription_webhooks_receive_dicts():
    data_object = ObjectWithoutDictMethods({"id": "sub_1", "status": "active"})

    with patch(
        "eventyay_business.stripe_service.process_subscription_change"
    ) as mock_handler:
        process_webhook_event("customer.subscription.updated", data_object)

    mock_handler.assert_called_once_with(
        "customer.subscription.updated", {"id": "sub_1", "status": "active"}
    )


def test_payment_intent_metadata_reads_objects_without_dict_methods():
    intent = ObjectWithoutDictMethods(
        {"id": "pi_1", "metadata": {"eventyay_business_invoice_id": "42"}}
    )

    assert payment_intent_metadata(intent) == {"eventyay_business_invoice_id": "42"}


def test_webhook_view_passes_a_dict_built_from_a_real_stripe_event():
    secret = "whsec_test_secret"
    session = {
        "id": "cs_test_1",
        "object": "checkout.session",
        "metadata": {"type": "subscription", "organizer_slug": "org"},
    }
    payload = json.dumps(
        {
            "id": "evt_test_1",
            "object": "event",
            "type": "checkout.session.completed",
            "data": {"object": session},
        }
    )
    timestamp = int(time.time())
    signature = hmac.new(
        secret.encode(), f"{timestamp}.{payload}".encode(), hashlib.sha256
    ).hexdigest()
    request = RequestFactory().post(
        "/webhook/",
        data=payload,
        content_type="application/json",
        HTTP_STRIPE_SIGNATURE=f"t={timestamp},v1={signature}",
    )

    with (
        patch(
            "eventyay.helpers.stripe_utils.get_stripe_webhook_secret_key",
            return_value=secret,
        ),
        patch(
            "eventyay_business.stripe_service.process_checkout_session_completed"
        ) as mock_handler,
    ):
        response = stripe_business_webhook_view(request)

    assert response.status_code == 200
    (received,) = mock_handler.call_args.args
    assert received.get("metadata", {}).get("organizer_slug") == "org"
