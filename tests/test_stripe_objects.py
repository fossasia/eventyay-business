import pytest
import stripe
from unittest.mock import patch

from eventyay_business.invoice_collection import payment_intent_metadata
from eventyay_business.stripe_objects import stripe_object_to_dict
from eventyay_business.stripe_service import process_webhook_event

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
