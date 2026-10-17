import pytest
import stripe
from datetime import datetime, timedelta
from decimal import Decimal
from django.core import mail
from django.core.exceptions import ValidationError
from django.test import override_settings
from django.utils.timezone import make_aware, now
from eventyay.base.models import Organizer
from eventyay.base.models.organizer import OrganizerBillingModel
from unittest.mock import patch

from eventyay_business.invoice_collection import (
    CollectionFailureCode,
    InvoiceCollectionError,
    collect_business_invoice,
    collect_ready_business_invoices,
    collection_retry_due,
    process_business_payment_intent,
    retry_outstanding_business_invoices_after_payment_method_update,
    update_invoice_from_payment_intent,
)
from eventyay_business.models import (
    BillingCollectionAuthorization,
    BusinessInvoice,
    BusinessInvoiceStatus,
    InvoicePaymentState,
    Subscription,
    SubscriptionStatus,
)
from eventyay_business.platform_fee_ledger import BILLING_CUTOVER_SETTING


@pytest.fixture
def collection_setup(db):
    organizer = Organizer.objects.create(name="Collection Org", slug="collection-org")
    organizer.settings.set(
        BILLING_CUTOVER_SETTING,
        (now() - timedelta(minutes=1)).isoformat(),
    )
    subscription = Subscription.objects.get(organizer=organizer)
    subscription.status = SubscriptionStatus.ACTIVE
    subscription.starts_at = now() - timedelta(days=60)
    subscription.save()
    billing = OrganizerBillingModel.objects.create(
        organizer=organizer,
        primary_contact_name="Collection Billing",
        primary_contact_email="billing@example.com",
        company_or_organization_name="Collection Org",
        address_line_1="Example Street 1",
        address_line_2="",
        city="Berlin",
        zip_code="10115",
        country="DE",
        preferred_language="en",
        tax_id="",
        stripe_customer_id="cus_collection",
        stripe_payment_method_id="pm_collection",
    )
    BillingCollectionAuthorization.objects.create(
        organizer=organizer,
        stripe_customer_id=billing.stripe_customer_id,
        authorized_at=now(),
    )
    invoice = BusinessInvoice.objects.create(
        organizer=organizer,
        invoice_number="INV-202610-0001-01",
        billing_period_start=make_aware(datetime(2026, 9, 1)),
        billing_period_end=make_aware(datetime(2026, 9, 30, 23, 59, 59, 999999)),
        currency="EUR",
        status=BusinessInvoiceStatus.OPEN,
        payment_state=InvoicePaymentState.NOT_STARTED,
        subtotal=Decimal("12.34"),
        total=Decimal("12.34"),
        finalized_at=now(),
        billing_address_snapshot={"contact_email": "billing@example.com"},
    )
    return organizer, subscription, billing, invoice


@pytest.mark.django_db
@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    DEFAULT_FROM_EMAIL="billing@eventyay.com",
)
def test_collects_finalized_invoice_once(
    collection_setup, django_capture_on_commit_callbacks
):
    organizer, subscription, billing, invoice = collection_setup
    payment_intent = {
        "id": "pi_invoice_1",
        "status": "succeeded",
        "latest_charge": "ch_invoice_1",
    }

    with patch(
        "eventyay_business.invoice_collection.get_stripe_secret_key",
        return_value="sk_test_collection",
    ), patch(
        "eventyay_business.invoice_collection.stripe.PaymentIntent.search",
        return_value={"data": []},
    ), patch(
        "eventyay_business.invoice_collection.stripe.PaymentIntent.create",
        return_value=payment_intent,
    ) as create:
        with django_capture_on_commit_callbacks(execute=True):
            result = collect_business_invoice(invoice)
            second_result = collect_business_invoice(invoice)

    assert result.pk == second_result.pk == invoice.pk
    create.assert_called_once_with(
        amount=1234,
        currency="eur",
        customer=billing.stripe_customer_id,
        payment_method=billing.stripe_payment_method_id,
        off_session=True,
        confirm=True,
        description=f"Eventyay invoice {invoice.invoice_number}",
        metadata={
            "type": "eventyay_business_invoice",
            "eventyay_business_invoice_id": str(invoice.pk),
            "eventyay_business_invoice_number": invoice.invoice_number,
            "organizer_id": str(organizer.pk),
        },
        idempotency_key=f"eventyay-business-invoice-{invoice.pk}-v1",
    )
    invoice.refresh_from_db()
    subscription.refresh_from_db()
    assert invoice.status == BusinessInvoiceStatus.PAID
    assert invoice.payment_state == InvoicePaymentState.SUCCEEDED
    assert invoice.collection_attempt_count == 1
    assert invoice.stripe_payment_intent_id == "pi_invoice_1"
    assert invoice.collection_metadata["stripe_charge_id"] == "ch_invoice_1"
    assert subscription.status == SubscriptionStatus.ACTIVE
    assert len(mail.outbox) == 1
    assert invoice.invoice_number in mail.outbox[0].subject


@pytest.mark.django_db
def test_zero_total_invoice_does_not_create_payment_intent(collection_setup):
    _, _, _, invoice = collection_setup
    BusinessInvoice.objects.filter(pk=invoice.pk).update(
        subtotal=Decimal("0.00"), total=Decimal("0.00")
    )

    with patch(
        "eventyay_business.invoice_collection.stripe.PaymentIntent.create"
    ) as create:
        result = collect_business_invoice(invoice.pk)

    create.assert_not_called()
    assert result.status == BusinessInvoiceStatus.PAID
    assert result.payment_state == InvoicePaymentState.SUCCEEDED
    assert result.stripe_payment_intent_id is None
    assert result.collection_attempt_count == 0


@pytest.mark.django_db
def test_collection_waits_for_organizer_cutover(collection_setup):
    _, _, _, invoice = collection_setup
    invoice.organizer.settings.set(BILLING_CUTOVER_SETTING, "")

    with patch(
        "eventyay_business.invoice_collection.stripe.PaymentIntent.create"
    ) as create:
        result = collect_business_invoice(invoice)

    create.assert_not_called()
    assert result.status == BusinessInvoiceStatus.OPEN
    assert result.payment_state == InvoicePaymentState.NOT_STARTED


@pytest.mark.django_db
@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    DEFAULT_FROM_EMAIL="billing@eventyay.com",
)
def test_revoked_authorization_marks_invoice_past_due_and_notifies_once(
    collection_setup, django_capture_on_commit_callbacks
):
    _, subscription, _, invoice = collection_setup
    BillingCollectionAuthorization.objects.filter(organizer=invoice.organizer).update(
        revoked_at=now()
    )

    with patch(
        "eventyay_business.invoice_collection.get_stripe_secret_key",
        return_value="sk_test_collection",
    ), patch(
        "eventyay_business.invoice_collection.stripe.PaymentIntent.create"
    ) as create, django_capture_on_commit_callbacks(
        execute=True
    ):
        first_result = collect_business_invoice(invoice)
        second_result = collect_business_invoice(invoice)

    create.assert_not_called()
    assert first_result.pk == second_result.pk
    invoice.refresh_from_db()
    subscription.refresh_from_db()
    assert invoice.status == BusinessInvoiceStatus.OPEN
    assert invoice.payment_state == InvoicePaymentState.FAILED
    assert invoice.last_collection_attempt_at is not None
    assert invoice.collection_metadata["last_error"] == {
        "code": CollectionFailureCode.AUTHORIZATION_MISSING
    }
    assert invoice.collection_metadata["retry_after_payment_method_update"] is True
    assert (
        collection_retry_due(invoice, current_time=now() + timedelta(days=30)) is False
    )
    assert subscription.status == SubscriptionStatus.PAST_DUE
    assert len(mail.outbox) == 1
    assert invoice.invoice_number in mail.outbox[0].subject


@pytest.mark.django_db
def test_missing_payment_method_blocks_retries_until_payment_method_update(
    collection_setup,
):
    _, subscription, billing, invoice = collection_setup
    billing.stripe_payment_method_id = ""
    billing.save(update_fields=["stripe_payment_method_id"])

    with patch(
        "eventyay_business.invoice_collection.get_stripe_secret_key",
        return_value="sk_test_collection",
    ), patch(
        "eventyay_business.invoice_collection.stripe.PaymentIntent.create"
    ) as create:
        result = collect_business_invoice(invoice)

    create.assert_not_called()
    subscription.refresh_from_db()
    assert result.payment_state == InvoicePaymentState.FAILED
    assert result.collection_metadata["last_error"] == {
        "code": CollectionFailureCode.PAYMENT_METHOD_MISSING
    }
    assert (
        collection_retry_due(result, current_time=now() + timedelta(days=30)) is False
    )
    assert subscription.status == SubscriptionStatus.PAST_DUE


@pytest.mark.django_db
def test_platform_configuration_failure_keeps_subscription_active(collection_setup):
    _, subscription, _, invoice = collection_setup

    with patch(
        "eventyay_business.invoice_collection.get_stripe_secret_key",
        side_effect=ValidationError("Stripe is not configured"),
    ):
        result = collect_business_invoice(invoice)

    subscription.refresh_from_db()
    assert result.payment_state == InvoicePaymentState.PROCESSING
    assert result.collection_metadata["reconciliation_required"] is True
    assert result.collection_metadata["last_error"] == {
        "code": CollectionFailureCode.STRIPE_NOT_CONFIGURED
    }
    assert subscription.status == SubscriptionStatus.ACTIVE


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("billing_change", "expected_code"),
    [
        ("delete", CollectionFailureCode.BILLING_RECORD_MISSING),
        ("clear_customer", CollectionFailureCode.BILLING_CUSTOMER_MISSING),
    ],
)
def test_missing_billing_configuration_is_organizer_actionable(
    collection_setup, billing_change, expected_code
):
    _, subscription, billing, invoice = collection_setup
    if billing_change == "delete":
        billing.delete()
    else:
        billing.stripe_customer_id = ""
        billing.save(update_fields=["stripe_customer_id"])

    with patch(
        "eventyay_business.invoice_collection.get_stripe_secret_key",
        return_value="sk_test_collection",
    ):
        result = collect_business_invoice(invoice)

    subscription.refresh_from_db()
    assert result.payment_state == InvoicePaymentState.FAILED
    assert result.collection_metadata["last_error"]["code"] == expected_code
    assert subscription.status == SubscriptionStatus.PAST_DUE


@pytest.mark.django_db
def test_authentication_required_is_recorded_for_organizer(collection_setup):
    _, subscription, _, invoice = collection_setup
    error = stripe.error.CardError(
        "Authentication required",
        "payment_method",
        "authentication_required",
        json_body={
            "error": {
                "code": "authentication_required",
                "payment_intent": {
                    "id": "pi_requires_action",
                    "status": "requires_payment_method",
                },
            }
        },
    )

    with patch(
        "eventyay_business.invoice_collection.get_stripe_secret_key",
        return_value="sk_test_collection",
    ), patch(
        "eventyay_business.invoice_collection.stripe.PaymentIntent.search",
        return_value={"data": []},
    ), patch(
        "eventyay_business.invoice_collection.stripe.PaymentIntent.create",
        side_effect=error,
    ):
        result = collect_business_invoice(invoice)

    subscription.refresh_from_db()
    assert result.payment_state == InvoicePaymentState.REQUIRES_ACTION
    assert result.stripe_payment_intent_id == "pi_requires_action"
    assert result.collection_metadata["last_error"]["code"] == (
        "authentication_required"
    )
    assert subscription.status == SubscriptionStatus.PAST_DUE


@pytest.mark.django_db
def test_retry_confirms_existing_payment_intent(collection_setup):
    _, _, billing, invoice = collection_setup
    invoice.stripe_payment_intent_id = "pi_retry"
    invoice.payment_state = InvoicePaymentState.FAILED
    invoice.collection_attempt_count = 1
    invoice.last_collection_attempt_at = now() - timedelta(days=2)
    invoice.save()

    with patch(
        "eventyay_business.invoice_collection.get_stripe_secret_key",
        return_value="sk_test_collection",
    ), patch(
        "eventyay_business.invoice_collection.stripe.PaymentIntent.retrieve",
        return_value={"id": "pi_retry", "status": "requires_payment_method"},
    ) as retrieve, patch(
        "eventyay_business.invoice_collection.stripe.PaymentIntent.confirm",
        return_value={"id": "pi_retry", "status": "succeeded"},
    ) as confirm, patch(
        "eventyay_business.invoice_collection.stripe.PaymentIntent.create"
    ) as create:
        result = collect_business_invoice(invoice)

    retrieve.assert_called_once_with("pi_retry")
    confirm.assert_called_once_with(
        "pi_retry",
        payment_method=billing.stripe_payment_method_id,
        off_session=True,
        idempotency_key=f"eventyay-business-invoice-{invoice.pk}-retry-2",
    )
    create.assert_not_called()
    assert result.status == BusinessInvoiceStatus.PAID
    assert result.payment_state == InvoicePaymentState.SUCCEEDED
    assert result.collection_attempt_count == 2


@pytest.mark.django_db
def test_search_reuses_payment_intent_after_idempotency_window(collection_setup):
    _, _, _, invoice = collection_setup
    existing_intent = {
        "id": "pi_recovered",
        "status": "succeeded",
        "amount": 1234,
        "currency": "eur",
        "metadata": {
            "type": "eventyay_business_invoice",
            "eventyay_business_invoice_id": str(invoice.pk),
            "eventyay_business_invoice_number": invoice.invoice_number,
            "organizer_id": str(invoice.organizer_id),
        },
    }

    with patch(
        "eventyay_business.invoice_collection.get_stripe_secret_key",
        return_value="sk_test_collection",
    ), patch(
        "eventyay_business.invoice_collection.stripe.PaymentIntent.search",
        return_value={"data": [existing_intent]},
    ) as search, patch(
        "eventyay_business.invoice_collection.stripe.PaymentIntent.create"
    ) as create:
        result = collect_business_invoice(invoice)

    search.assert_called_once_with(
        query=(
            "metadata['eventyay_business_invoice_id']:"
            f"'{invoice.pk}' AND metadata['type']:'eventyay_business_invoice'"
        ),
        limit=100,
    )
    create.assert_not_called()
    assert result.stripe_payment_intent_id == "pi_recovered"
    assert result.payment_state == InvoicePaymentState.SUCCEEDED


@pytest.mark.django_db
def test_requires_action_intent_is_not_automatically_retried(collection_setup):
    _, _, _, invoice = collection_setup
    invoice.stripe_payment_intent_id = "pi_action"
    invoice.payment_state = InvoicePaymentState.REQUIRES_ACTION
    invoice.save()

    with patch(
        "eventyay_business.invoice_collection.get_stripe_secret_key",
        return_value="sk_test_collection",
    ), patch(
        "eventyay_business.invoice_collection.stripe.PaymentIntent.retrieve",
        return_value={"id": "pi_action", "status": "requires_action"},
    ), patch(
        "eventyay_business.invoice_collection.stripe.PaymentIntent.confirm"
    ) as confirm:
        result = collect_business_invoice(invoice)

    confirm.assert_not_called()
    assert result.stripe_payment_intent_id == "pi_action"
    assert result.payment_state == InvoicePaymentState.REQUIRES_ACTION


@pytest.mark.django_db
def test_canceled_intent_is_replaced_with_new_payment_intent(collection_setup):
    _, _, _, invoice = collection_setup
    invoice.stripe_payment_intent_id = "pi_canceled"
    invoice.collection_attempt_count = 0
    invoice.save()

    with patch(
        "eventyay_business.invoice_collection.get_stripe_secret_key",
        return_value="sk_test_collection",
    ), patch(
        "eventyay_business.invoice_collection.stripe.PaymentIntent.retrieve",
        return_value={"id": "pi_canceled", "status": "canceled"},
    ), patch(
        "eventyay_business.invoice_collection.stripe.PaymentIntent.create",
        return_value={"id": "pi_recreated", "status": "succeeded"},
    ) as create:
        result = collect_business_invoice(invoice)

    assert result.stripe_payment_intent_id == "pi_recreated"
    assert result.payment_state == InvoicePaymentState.SUCCEEDED
    assert create.call_args.kwargs["idempotency_key"] == (
        f"eventyay-business-invoice-{invoice.pk}-recreate-1"
    )


@pytest.mark.django_db
def test_connection_failure_remains_reconciliation_state(collection_setup):
    _, subscription, _, invoice = collection_setup
    connection_error = stripe.error.APIConnectionError("Stripe timed out")

    with patch(
        "eventyay_business.invoice_collection.get_stripe_secret_key",
        return_value="sk_test_collection",
    ), patch(
        "eventyay_business.invoice_collection.stripe.PaymentIntent.search",
        side_effect=connection_error,
    ):
        result = collect_business_invoice(invoice)

    subscription.refresh_from_db()
    assert result.payment_state == InvoicePaymentState.PROCESSING
    assert result.collection_metadata["reconciliation_required"] is True
    assert result.collection_metadata["last_error"]["code"] == (
        "stripe_connection_error"
    )
    assert subscription.status == SubscriptionStatus.ACTIVE


@pytest.mark.django_db
def test_stripe_library_failure_remains_reconciliation_state(collection_setup):
    _, subscription, _, invoice = collection_setup

    with patch("eventyay_business.invoice_collection.stripe", None):
        result = collect_business_invoice(invoice)

    subscription.refresh_from_db()
    assert result.payment_state == InvoicePaymentState.PROCESSING
    assert result.collection_metadata["last_error"]["code"] == (
        CollectionFailureCode.STRIPE_UNAVAILABLE
    )
    assert subscription.status == SubscriptionStatus.ACTIVE


@pytest.mark.django_db
def test_generic_stripe_failure_remains_reconciliation_state(collection_setup):
    _, subscription, _, invoice = collection_setup
    stripe_error = stripe.error.APIError("Stripe internal error")

    with patch(
        "eventyay_business.invoice_collection.get_stripe_secret_key",
        return_value="sk_test_collection",
    ), patch(
        "eventyay_business.invoice_collection.stripe.PaymentIntent.search",
        side_effect=stripe_error,
    ):
        result = collect_business_invoice(invoice)

    subscription.refresh_from_db()
    assert result.payment_state == InvoicePaymentState.PROCESSING
    assert result.collection_metadata["last_error"]["code"] == (
        CollectionFailureCode.STRIPE_ERROR
    )
    assert subscription.status == SubscriptionStatus.ACTIVE


@pytest.mark.django_db
def test_detached_payment_method_is_organizer_actionable(collection_setup):
    _, subscription, _, invoice = collection_setup
    detached_method_error = stripe.error.InvalidRequestError(
        "The payment method is detached.",
        "payment_method",
        code="payment_method_unexpected_state",
    )

    with patch(
        "eventyay_business.invoice_collection.get_stripe_secret_key",
        return_value="sk_test_collection",
    ), patch(
        "eventyay_business.invoice_collection.stripe.PaymentIntent.search",
        return_value={"data": []},
    ), patch(
        "eventyay_business.invoice_collection.stripe.PaymentIntent.create",
        side_effect=detached_method_error,
    ):
        result = collect_business_invoice(invoice)

    subscription.refresh_from_db()
    assert result.payment_state == InvoicePaymentState.FAILED
    assert (
        result.collection_metadata["last_error"]["code"]
        == CollectionFailureCode.PAYMENT_METHOD_MISSING
    )
    assert result.collection_metadata["retry_after_payment_method_update"] is True
    assert subscription.status == SubscriptionStatus.PAST_DUE


@pytest.mark.django_db
def test_unknown_payment_state_remains_reconciliation_state(collection_setup):
    _, subscription, _, invoice = collection_setup
    invoice.stripe_payment_intent_id = "pi_unknown"
    invoice.save(update_fields=["stripe_payment_intent_id", "updated_at"])

    with patch(
        "eventyay_business.invoice_collection.get_stripe_secret_key",
        return_value="sk_test_collection",
    ), patch(
        "eventyay_business.invoice_collection.stripe.PaymentIntent.retrieve",
        return_value={"id": "pi_unknown", "status": "unexpected_future_state"},
    ):
        result = collect_business_invoice(invoice)

    subscription.refresh_from_db()
    assert result.payment_state == InvoicePaymentState.PROCESSING
    assert (
        result.collection_metadata["last_error"]["code"]
        == CollectionFailureCode.UNKNOWN_PAYMENT_STATE
    )
    assert subscription.status == SubscriptionStatus.ACTIVE


@pytest.mark.django_db
def test_ready_collection_continues_after_one_invoice_failure(collection_setup):
    organizer, _, _, invoice = collection_setup
    second_invoice = BusinessInvoice.objects.create(
        organizer=organizer,
        invoice_number="INV-202611-0001-01",
        billing_period_start=make_aware(datetime(2026, 10, 1)),
        billing_period_end=make_aware(datetime(2026, 10, 31, 23, 59, 59, 999999)),
        currency="EUR",
        status=BusinessInvoiceStatus.OPEN,
        payment_state=InvoicePaymentState.NOT_STARTED,
        subtotal=Decimal("5.00"),
        total=Decimal("5.00"),
        finalized_at=now(),
    )

    with patch(
        "eventyay_business.invoice_collection.collect_business_invoice",
        side_effect=[InvoiceCollectionError("temporary failure"), second_invoice],
    ) as collect:
        result = collect_ready_business_invoices(
            invoice_ids=[invoice.pk, second_invoice.pk]
        )

    assert collect.call_count == 2
    assert result["processed"] == 2
    assert result["failed"] == 1


@pytest.mark.django_db
def test_verified_payment_intent_event_updates_only_matching_invoice(
    collection_setup,
):
    _, _, _, invoice = collection_setup
    metadata = {
        "type": "eventyay_business_invoice",
        "eventyay_business_invoice_id": str(invoice.pk),
        "eventyay_business_invoice_number": invoice.invoice_number,
        "organizer_id": str(invoice.organizer_id),
    }
    result = process_business_payment_intent(
        {
            "id": "pi_webhook",
            "status": "succeeded",
            "amount": 1234,
            "currency": "eur",
            "metadata": metadata,
        }
    )
    assert result.status == BusinessInvoiceStatus.PAID
    assert result.stripe_payment_intent_id == "pi_webhook"

    stale_result = process_business_payment_intent(
        {
            "id": "pi_webhook",
            "status": "requires_payment_method",
            "amount": 1234,
            "currency": "eur",
            "metadata": metadata,
        }
    )
    assert stale_result.payment_state == InvoicePaymentState.SUCCEEDED

    assert (
        process_business_payment_intent(
            {
                "id": "pi_other",
                "status": "succeeded",
                "amount": 1234,
                "currency": "eur",
                "metadata": {"type": "ticket_payment"},
            }
        )
        is None
    )


@pytest.mark.django_db
def test_ready_collection_respects_retry_delay(collection_setup):
    _, _, _, invoice = collection_setup
    invoice.payment_state = InvoicePaymentState.FAILED
    invoice.last_collection_attempt_at = now()
    invoice.save()

    with patch(
        "eventyay_business.invoice_collection.collect_business_invoice"
    ) as collect:
        result = collect_ready_business_invoices()
        collect.assert_not_called()
        assert result["processed"] == 0

        invoice.last_collection_attempt_at = now() - timedelta(days=2)
        invoice.save(update_fields=["last_collection_attempt_at", "updated_at"])
        collect.return_value = invoice
        result = collect_ready_business_invoices()

    collect.assert_called_once_with(invoice.pk)
    assert result["processed"] == 1
    assert result["failed"] == 1


@pytest.mark.django_db
def test_collection_retry_schedule_is_bounded(collection_setup):
    _, _, _, invoice = collection_setup
    invoice.payment_state = InvoicePaymentState.FAILED
    invoice.collection_attempt_count = 1
    invoice.last_collection_attempt_at = now()
    invoice.collection_metadata = {
        "last_error": {"code": "card_declined"},
        "organizer_actionable": True,
    }

    assert (
        collection_retry_due(
            invoice,
            current_time=invoice.last_collection_attempt_at + timedelta(hours=23),
        )
        is False
    )
    assert (
        collection_retry_due(
            invoice, current_time=invoice.last_collection_attempt_at + timedelta(days=1)
        )
        is True
    )

    invoice.collection_attempt_count = 2
    invoice.last_collection_attempt_at = now()
    assert (
        collection_retry_due(
            invoice,
            current_time=invoice.last_collection_attempt_at + timedelta(days=1),
        )
        is False
    )
    assert (
        collection_retry_due(
            invoice,
            current_time=invoice.last_collection_attempt_at + timedelta(days=2),
        )
        is True
    )

    invoice.collection_attempt_count = 4
    invoice.collection_metadata["retry_exhausted"] = True
    assert (
        collection_retry_due(
            invoice,
            current_time=invoice.last_collection_attempt_at + timedelta(days=30),
        )
        is False
    )


@pytest.mark.django_db
def test_exhausted_platform_reconciliation_uses_final_retry_interval(
    collection_setup,
):
    _, _, _, invoice = collection_setup
    invoice.payment_state = InvoicePaymentState.PROCESSING
    invoice.last_collection_attempt_at = now()
    invoice.collection_metadata = {
        "last_error": {"code": CollectionFailureCode.STRIPE_CONNECTION_ERROR},
        "reconciliation_required": True,
        "reconciliation_attempt_count": 4,
        "retry_exhausted": True,
    }

    assert not collection_retry_due(
        invoice,
        current_time=invoice.last_collection_attempt_at + timedelta(days=1),
    )
    assert collection_retry_due(
        invoice,
        current_time=invoice.last_collection_attempt_at + timedelta(days=2),
    )


@pytest.mark.django_db
@override_settings(EVENTYAY_BUSINESS_COLLECTION_RETRY_DAYS=(2, 6))
def test_collection_retry_schedule_uses_configured_days(collection_setup):
    _, _, _, invoice = collection_setup
    invoice.payment_state = InvoicePaymentState.FAILED
    invoice.collection_attempt_count = 1
    invoice.last_collection_attempt_at = now()
    invoice.collection_metadata = {
        "last_error": {"code": "card_declined"},
        "organizer_actionable": True,
    }

    assert not collection_retry_due(
        invoice,
        current_time=invoice.last_collection_attempt_at + timedelta(days=1),
    )
    assert collection_retry_due(
        invoice,
        current_time=invoice.last_collection_attempt_at + timedelta(days=2),
    )


@pytest.mark.django_db
def test_failed_invoice_becomes_uncollectible_after_retry_budget(collection_setup):
    _, subscription, _, invoice = collection_setup
    invoice.collection_attempt_count = 4

    result = update_invoice_from_payment_intent(
        invoice,
        {
            "id": "pi_exhausted",
            "status": "requires_payment_method",
            "last_payment_error": {"code": "card_declined"},
        },
    )

    subscription.refresh_from_db()
    assert result.status == BusinessInvoiceStatus.UNCOLLECTIBLE
    assert result.payment_state == InvoicePaymentState.FAILED
    assert result.collection_metadata["retry_exhausted"] is True
    assert subscription.status == SubscriptionStatus.PAST_DUE


@pytest.mark.django_db
def test_payment_method_update_retries_and_recovers_uncollectible_invoice(
    collection_setup,
):
    _, subscription, billing, invoice = collection_setup
    invoice.status = BusinessInvoiceStatus.UNCOLLECTIBLE
    invoice.payment_state = InvoicePaymentState.FAILED
    invoice.collection_attempt_count = 4
    invoice.last_collection_attempt_at = now()
    invoice.stripe_payment_intent_id = "pi_recover"
    invoice.collection_metadata = {
        "last_error": {"code": CollectionFailureCode.PAYMENT_METHOD_MISSING},
        "organizer_actionable": True,
        "retry_exhausted": True,
    }
    invoice.save()
    scheduled_end = now() + timedelta(days=10)
    subscription.status = SubscriptionStatus.EXPIRED
    subscription.past_due_since = now() - timedelta(days=2)
    subscription.ends_at = scheduled_end
    subscription.save()

    with patch(
        "eventyay_business.invoice_collection.get_stripe_secret_key",
        return_value="sk_test_collection",
    ), patch(
        "eventyay_business.invoice_collection.stripe.PaymentIntent.retrieve",
        return_value={"id": "pi_recover", "status": "requires_payment_method"},
    ), patch(
        "eventyay_business.invoice_collection.stripe.PaymentIntent.confirm",
        return_value={"id": "pi_recover", "status": "succeeded"},
    ) as confirm:
        result = retry_outstanding_business_invoices_after_payment_method_update(
            invoice.organizer_id
        )
        duplicate_result = (
            retry_outstanding_business_invoices_after_payment_method_update(
                invoice.organizer_id
            )
        )

    confirm.assert_called_once_with(
        "pi_recover",
        payment_method=billing.stripe_payment_method_id,
        off_session=True,
        idempotency_key=f"eventyay-business-invoice-{invoice.pk}-retry-5",
    )
    invoice.refresh_from_db()
    subscription.refresh_from_db()
    assert result["succeeded"] == 1
    assert invoice.status == BusinessInvoiceStatus.PAID
    assert invoice.payment_state == InvoicePaymentState.SUCCEEDED
    assert invoice.collection_metadata["previous_error"] == {
        "code": CollectionFailureCode.PAYMENT_METHOD_MISSING
    }
    assert subscription.status == SubscriptionStatus.ACTIVE
    assert subscription.ends_at == scheduled_end
    assert duplicate_result["processed"] == 0


@pytest.mark.django_db
def test_payment_method_update_unblocks_all_invoice_currencies(collection_setup):
    organizer, _, _, invoice = collection_setup
    second_invoice = BusinessInvoice.objects.create(
        organizer=organizer,
        invoice_number="INV-202610-0001-USD",
        billing_period_start=invoice.billing_period_start,
        billing_period_end=invoice.billing_period_end,
        currency="USD",
        status=BusinessInvoiceStatus.OPEN,
        payment_state=InvoicePaymentState.FAILED,
        subtotal=Decimal("5.00"),
        total=Decimal("5.00"),
        finalized_at=now(),
        collection_metadata={
            "last_error": {"code": CollectionFailureCode.PAYMENT_METHOD_MISSING},
            "organizer_actionable": True,
            "retry_after_payment_method_update": True,
        },
    )
    invoice.payment_state = InvoicePaymentState.FAILED
    invoice.collection_metadata = {
        "last_error": {"code": CollectionFailureCode.PAYMENT_METHOD_MISSING},
        "organizer_actionable": True,
        "retry_after_payment_method_update": True,
    }
    invoice.save()

    with patch(
        "eventyay_business.invoice_collection.collect_ready_business_invoices",
        return_value={"processed": 2},
    ) as collect:
        result = retry_outstanding_business_invoices_after_payment_method_update(
            organizer.pk
        )

    assert result == {"processed": 2}
    assert set(collect.call_args.kwargs["invoice_ids"]) == {
        invoice.pk,
        second_invoice.pk,
    }
