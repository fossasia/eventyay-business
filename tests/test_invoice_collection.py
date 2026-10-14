import pytest
import stripe
from datetime import datetime, timedelta
from decimal import Decimal
from django.core import mail
from django.test import override_settings
from django.utils.timezone import make_aware, now
from eventyay.base.models import Organizer
from eventyay.base.models.organizer import OrganizerBillingModel
from unittest.mock import patch

from eventyay_business.invoice_collection import (
    collect_business_invoice,
    collect_ready_business_invoices,
    process_business_payment_intent,
)
from eventyay_business.models import (
    BillingCollectionAuthorization,
    BusinessInvoice,
    BusinessInvoiceStatus,
    InvoicePaymentState,
    Subscription,
    SubscriptionStatus,
)


@pytest.fixture
def collection_setup(db):
    organizer = Organizer.objects.create(name="Collection Org", slug="collection-org")
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
@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    DEFAULT_FROM_EMAIL="billing@eventyay.com",
)
def test_missing_authorization_marks_invoice_past_due_and_notifies_once(
    collection_setup, django_capture_on_commit_callbacks
):
    _, subscription, _, invoice = collection_setup
    BillingCollectionAuthorization.objects.filter(organizer=invoice.organizer).delete()

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
    assert invoice.collection_metadata["last_error"] == {"code": "billing_not_ready"}
    assert subscription.status == SubscriptionStatus.PAST_DUE
    assert len(mail.outbox) == 1
    assert invoice.invoice_number in mail.outbox[0].subject


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
