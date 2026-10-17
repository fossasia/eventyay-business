import pytest
from datetime import datetime
from decimal import Decimal
from django.utils.timezone import make_aware, now
from eventyay.base.models import Organizer
from eventyay.base.models.organizer import OrganizerBillingModel

from eventyay_business.models import (
    BillingCollectionAuthorization,
    BillingInterval,
    BillingNotificationDelivery,
    BillingReconciliationResult,
    BillingReconciliationResultStatus,
    BillingReconciliationRun,
    BusinessInvoice,
    BusinessInvoiceLine,
    Subscription,
    Tier,
    TierPrice,
    TierVersion,
)
from eventyay_business.reconciliation import run_billing_reconciliation


@pytest.fixture
def reconciliation_setup(db):
    organizer = Organizer.objects.create(
        name="Reconciliation Organizer", slug="reconciliation-organizer"
    )
    tier = Tier.objects.create(slug="reconciliation-pro", name="Pro")
    version = TierVersion.objects.create(
        tier=tier,
        version=1,
        published_at=now(),
    )
    price = TierPrice.objects.create(
        tier_version=version,
        billing_interval=BillingInterval.MONTHLY,
        amount=Decimal("49.00"),
        currency="EUR",
    )
    subscription = Subscription.objects.get(organizer=organizer)
    subscription.tier_version = version
    subscription.billing_interval = BillingInterval.MONTHLY
    subscription.currency = "EUR"
    subscription.starts_at = make_aware(datetime(2026, 1, 1))
    subscription.save()
    billing = OrganizerBillingModel.objects.create(
        organizer=organizer,
        primary_contact_name="Billing Contact",
        primary_contact_email="billing@example.com",
        company_or_organization_name=organizer.name,
        address_line_1="Example Street 1",
        address_line_2="",
        city="Berlin",
        zip_code="10115",
        country="DE",
        preferred_language="en",
        tax_id="",
        stripe_customer_id="cus_reconciliation",
        stripe_payment_method_id="pm_reconciliation",
    )
    BillingCollectionAuthorization.objects.create(
        organizer=organizer,
        stripe_customer_id=billing.stripe_customer_id,
        authorized_at=now(),
    )
    return organizer, subscription, billing, price


@pytest.mark.django_db
def test_reporting_mode_records_reproducible_result_without_billing_side_effects(
    reconciliation_setup,
):
    organizer, _, _, _ = reconciliation_setup
    period_start = make_aware(datetime(2026, 9, 1))
    period_end = make_aware(datetime(2026, 9, 30, 23, 59, 59, 999999))
    state_before = {
        "invoices": BusinessInvoice.objects.count(),
        "lines": BusinessInvoiceLine.objects.count(),
        "notifications": BillingNotificationDelivery.objects.count(),
    }

    run = run_billing_reconciliation(
        period_start,
        period_end,
        organizers=[organizer],
    )

    result = run.results.get()
    assert result.status == BillingReconciliationResultStatus.READY
    assert result.expected_total == Decimal("98.00")
    assert result.line_count == 2
    assert result.blocking_finding_count == 0
    assert (
        sum(Decimal(line["amount"]) for line in result.snapshot["invoice"]["lines"])
        == result.expected_total
    )
    assert result.snapshot["invoice"]["existing_invoice"] is False
    assert BusinessInvoice.objects.count() == state_before["invoices"]
    assert BusinessInvoiceLine.objects.count() == state_before["lines"]
    assert BillingNotificationDelivery.objects.count() == state_before["notifications"]


@pytest.mark.django_db
def test_reporting_mode_is_idempotent_for_period_and_result(
    reconciliation_setup,
):
    organizer, _, _, _ = reconciliation_setup
    period_start = make_aware(datetime(2026, 9, 1))
    period_end = make_aware(datetime(2026, 9, 30, 23, 59, 59, 999999))

    first = run_billing_reconciliation(
        period_start,
        period_end,
        organizers=[organizer],
    )
    first_fingerprint = first.results.get().fingerprint
    second = run_billing_reconciliation(
        period_start,
        period_end,
        organizers=[organizer],
    )

    assert first.pk == second.pk
    assert BillingReconciliationRun.objects.count() == 1
    assert BillingReconciliationResult.objects.count() == 1
    assert second.results.get().fingerprint == first_fingerprint


@pytest.mark.django_db
def test_reporting_mode_records_collection_readiness_blockers(
    reconciliation_setup,
):
    organizer, subscription, billing, _ = reconciliation_setup
    billing.stripe_payment_method_id = ""
    billing.save(update_fields=["stripe_payment_method_id"])
    subscription.stripe_subscription_id = "sub_legacy"
    subscription.save(update_fields=["stripe_subscription_id", "updated_at"])

    run = run_billing_reconciliation(
        make_aware(datetime(2026, 9, 1)),
        make_aware(datetime(2026, 9, 30, 23, 59, 59, 999999)),
        organizers=[organizer],
    )

    result = run.results.get()
    codes = {finding["code"] for finding in result.findings}
    assert result.status == BillingReconciliationResultStatus.BLOCKED
    assert "payment_method_missing" in codes
    assert "legacy_collection_active" in codes
    assert result.snapshot["readiness"]["active_stripe_subscription_ids"] == [
        "sub_legacy"
    ]


@pytest.mark.django_db
def test_reporting_mode_records_calculation_error_without_partial_invoice(
    reconciliation_setup,
):
    organizer, subscription, _, price = reconciliation_setup
    price.currency = "USD"
    price.save(update_fields=["currency"])
    subscription.currency = "EUR"
    subscription.save(update_fields=["currency", "updated_at"])

    run = run_billing_reconciliation(
        make_aware(datetime(2026, 9, 1)),
        make_aware(datetime(2026, 9, 30, 23, 59, 59, 999999)),
        organizers=[organizer],
    )

    result = run.results.get()
    assert result.status == BillingReconciliationResultStatus.ERROR
    assert result.findings == [
        {
            "code": "calculation_error",
            "severity": "blocking",
            "message": "The invoice calculation could not be completed.",
        }
    ]
    assert result.snapshot["error_type"] == "InvoiceCalculationError"
    assert not BusinessInvoice.objects.exists()
