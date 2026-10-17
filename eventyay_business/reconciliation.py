import hashlib
import json
import logging
from decimal import Decimal
from django.db import transaction
from django.db.models import Q
from django.utils.timezone import now
from eventyay.base.models import Organizer

from .billing_migration import consolidated_billing_preflight
from .invoicing_service import (
    InvoiceCalculationError,
    generate_business_invoice_for_organizer,
    get_previous_calendar_month_range,
)
from .models import (
    BillingReconciliationResult,
    BillingReconciliationResultStatus,
    BillingReconciliationRun,
    BillingReconciliationRunStatus,
    BusinessInvoice,
    PlatformFeeLedgerEntry,
    Subscription,
    UsageRecord,
)

logger = logging.getLogger(__name__)

try:
    from django_scopes import scopes_disabled
except ModuleNotFoundError as err:
    if err.name != "django_scopes":
        raise
    from contextlib import nullcontext as scopes_disabled


def _serialize_datetime(value):
    return value.isoformat() if value else None


def _serialize_invoice(invoice, *, existing_invoice: bool) -> dict:
    lines = []
    for line in invoice.lines.select_related("event", "tier_version", "addon"):
        lines.append(
            {
                "line_type": line.line_type,
                "description": line.description,
                "quantity": str(line.quantity),
                "unit_price": str(line.unit_price),
                "amount": str(line.amount),
                "service_period_start": _serialize_datetime(line.service_period_start),
                "service_period_end": _serialize_datetime(line.service_period_end),
                "event_id": line.event_id,
                "tier_version_id": line.tier_version_id,
                "addon_id": line.addon_id,
                "usage_reference": line.usage_reference,
                "calculation_metadata": line.calculation_metadata,
            }
        )
    return {
        "existing_invoice": existing_invoice,
        "existing_invoice_id": invoice.pk if existing_invoice else None,
        "invoice_number": (
            invoice.invoice_number if existing_invoice else "reporting-mode"
        ),
        "currency": invoice.currency,
        "subtotal": str(invoice.subtotal),
        "tax": str(invoice.tax),
        "total": str(invoice.total),
        "lines": lines,
    }


def _source_snapshot(organizer, period_start, period_end) -> dict:
    ledger_entries = (
        PlatformFeeLedgerEntry.objects.filter(
            organizer=organizer,
            occurred_at__lte=period_end,
        )
        .filter(Q(occurred_at__gte=period_start) | Q(invoice__isnull=True))
        .select_related("event", "invoice")
    )
    usage_records = UsageRecord.objects.filter(
        organizer=organizer,
        occurred_at__gte=period_start,
        occurred_at__lte=period_end,
    ).select_related("event", "invoice")
    return {
        "platform_fee_entries": [
            {
                "id": entry.pk,
                "source_key": entry.source_key,
                "entry_type": entry.entry_type,
                "amount": str(entry.amount),
                "currency": entry.currency,
                "occurred_at": _serialize_datetime(entry.occurred_at),
                "event_id": entry.event_id,
                "invoice_id": entry.invoice_id,
                "collection_path": entry.metadata.get("collection_path", ""),
                "cutover_relation": entry.metadata.get("cutover_relation", ""),
                "unreconciled_historical_amount": entry.metadata.get(
                    "unreconciled_historical_amount", "0.00"
                ),
            }
            for entry in ledger_entries
        ],
        "usage_records": [
            {
                "id": record.pk,
                "capability": record.capability,
                "quantity": str(record.quantity),
                "unit": record.unit,
                "occurred_at": _serialize_datetime(record.occurred_at),
                "source_type": record.source_type,
                "source_id": record.source_id,
                "event_id": record.event_id,
                "invoice_id": record.invoice_id,
            }
            for record in usage_records
        ],
    }


def _calculation_currencies(organizer, period_start, period_end) -> tuple[str, ...]:
    existing = tuple(
        BusinessInvoice.objects.filter(
            organizer=organizer,
            billing_period_start=period_start,
            billing_period_end=period_end,
        )
        .order_by("currency")
        .values_list("currency", flat=True)
        .distinct()
    )
    if existing:
        return existing
    subscription_currency = (
        Subscription.objects.filter(organizer=organizer)
        .exclude(currency__isnull=True)
        .exclude(currency="")
        .order_by("-starts_at")
        .values_list("currency", flat=True)
        .first()
    )
    return ((subscription_currency or "EUR").upper(),)


def _readiness_snapshot(organizer) -> tuple[dict, list[dict]]:
    preflight = consolidated_billing_preflight(organizer)
    snapshot = {
        "billing_customer_ready": preflight.billing_customer_ready,
        "payment_method_ready": preflight.payment_method_ready,
        "collection_authorized": preflight.collection_authorized,
        "active_stripe_subscription_ids": list(
            preflight.active_stripe_subscription_ids
        ),
        "activated_at": _serialize_datetime(preflight.activated_at),
    }
    findings = []
    for ready, code, message in (
        (
            preflight.billing_customer_ready,
            "billing_customer_missing",
            "The organizer has no canonical Stripe billing customer.",
        ),
        (
            preflight.payment_method_ready,
            "payment_method_missing",
            "The organizer has no saved off-session payment method.",
        ),
        (
            preflight.collection_authorized,
            "authorization_missing",
            "Automatic collection is not authorized for the billing customer.",
        ),
    ):
        if not ready:
            findings.append({"code": code, "severity": "blocking", "message": message})
    if preflight.active_stripe_subscription_ids:
        findings.append(
            {
                "code": "legacy_collection_active",
                "severity": "blocking",
                "message": "A legacy Stripe subscription could still collect independently.",
                "references": list(preflight.active_stripe_subscription_ids),
            }
        )
    return snapshot, findings


def _source_findings(source_snapshot: dict) -> list[dict]:
    findings = []
    for entry in source_snapshot["platform_fee_entries"]:
        collection_path = entry["collection_path"]
        amount = Decimal(entry["amount"])
        if collection_path in {
            "historical_immediate",
            "legacy_usage_record",
            "historical_pre_cutover",
        } and amount != Decimal("0.00"):
            findings.append(
                {
                    "code": "historical_fee_rebilling_risk",
                    "severity": "blocking",
                    "message": "A historical fee exclusion has a non-zero billable amount.",
                    "source_key": entry["source_key"],
                }
            )
        if collection_path == "migration_not_enabled" and Decimal(
            entry["unreconciled_historical_amount"] or "0.00"
        ) != Decimal("0.00"):
            findings.append(
                {
                    "code": "pre_cutover_unreconciled_amount",
                    "severity": "warning",
                    "message": "A pre-cutover amount is recorded for investigation only.",
                    "source_key": entry["source_key"],
                }
            )
    return findings


def calculate_reconciliation_snapshot(
    organizer, period_start, period_end, currency
) -> tuple[dict, list[dict]]:
    existing_invoice = BusinessInvoice.objects.filter(
        organizer=organizer,
        billing_period_start=period_start,
        billing_period_end=period_end,
        currency=currency,
    ).exists()
    with transaction.atomic():
        invoice = generate_business_invoice_for_organizer(
            organizer=organizer,
            start_date=period_start,
            end_date=period_end,
            currency=currency,
            allow_empty=True,
        )
        invoice_snapshot = _serialize_invoice(
            invoice,
            existing_invoice=existing_invoice,
        )
        transaction.set_rollback(True)

    readiness, findings = _readiness_snapshot(organizer)
    sources = _source_snapshot(organizer, period_start, period_end)
    findings.extend(_source_findings(sources))
    line_total = sum(
        (Decimal(line["amount"]) for line in invoice_snapshot["lines"]),
        Decimal("0.00"),
    )
    if line_total != Decimal(invoice_snapshot["total"]):
        findings.append(
            {
                "code": "total_not_reproducible",
                "severity": "blocking",
                "message": "The expected total does not equal the serialized line total.",
            }
        )
    snapshot = {
        "schema_version": 1,
        "organizer": {"id": organizer.pk, "slug": organizer.slug},
        "period_start": _serialize_datetime(period_start),
        "period_end": _serialize_datetime(period_end),
        "currency": currency,
        "invoice": invoice_snapshot,
        "readiness": readiness,
        "sources": sources,
    }
    return snapshot, findings


def _snapshot_fingerprint(snapshot: dict, findings: list[dict]) -> str:
    payload = json.dumps(
        {"snapshot": snapshot, "findings": findings},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def run_billing_reconciliation(
    period_start=None,
    period_end=None,
    organizers=None,
) -> BillingReconciliationRun:
    if period_start is None or period_end is None:
        period_start, period_end = get_previous_calendar_month_range()
    run, _ = BillingReconciliationRun.objects.get_or_create(
        period_start=period_start,
        period_end=period_end,
    )
    run.status = BillingReconciliationRunStatus.RUNNING
    run.started_at = now()
    run.completed_at = None
    run.summary = {}
    run.save(
        update_fields=["status", "started_at", "completed_at", "summary", "updated_at"]
    )

    organizer_query = organizers if organizers is not None else Organizer.objects.all()
    processed = ready = blocked = errors = 0
    try:
        with scopes_disabled():
            for organizer in organizer_query:
                for currency in _calculation_currencies(
                    organizer, period_start, period_end
                ):
                    try:
                        snapshot, findings = calculate_reconciliation_snapshot(
                            organizer,
                            period_start,
                            period_end,
                            currency,
                        )
                    except InvoiceCalculationError as exc:
                        snapshot = {
                            "schema_version": 1,
                            "organizer": {
                                "id": organizer.pk,
                                "slug": organizer.slug,
                            },
                            "period_start": _serialize_datetime(period_start),
                            "period_end": _serialize_datetime(period_end),
                            "currency": currency,
                            "error_type": type(exc).__name__,
                        }
                        findings = [
                            {
                                "code": "calculation_error",
                                "severity": "blocking",
                                "message": "The invoice calculation could not be completed.",
                            }
                        ]
                        result_status = BillingReconciliationResultStatus.ERROR
                        errors += 1
                    else:
                        result_status = (
                            BillingReconciliationResultStatus.BLOCKED
                            if any(
                                finding["severity"] == "blocking"
                                for finding in findings
                            )
                            else BillingReconciliationResultStatus.READY
                        )
                        if result_status == BillingReconciliationResultStatus.BLOCKED:
                            blocked += 1
                        else:
                            ready += 1
                    invoice_snapshot = snapshot.get("invoice", {})
                    BillingReconciliationResult.objects.update_or_create(
                        run=run,
                        organizer=organizer,
                        currency=currency,
                        defaults={
                            "status": result_status,
                            "expected_total": Decimal(
                                invoice_snapshot.get("total", "0.00")
                            ),
                            "line_count": len(invoice_snapshot.get("lines", [])),
                            "blocking_finding_count": sum(
                                finding["severity"] == "blocking"
                                for finding in findings
                            ),
                            "findings": findings,
                            "snapshot": snapshot,
                            "fingerprint": _snapshot_fingerprint(snapshot, findings),
                        },
                    )
                    processed += 1
    except Exception:
        run.status = BillingReconciliationRunStatus.FAILED
        run.completed_at = now()
        run.summary = {"processed": processed, "failed": True}
        run.save(update_fields=["status", "completed_at", "summary", "updated_at"])
        raise

    run.status = BillingReconciliationRunStatus.COMPLETED
    run.completed_at = now()
    run.summary = {
        "processed": processed,
        "ready": ready,
        "blocked": blocked,
        "errors": errors,
    }
    run.save(update_fields=["status", "completed_at", "summary", "updated_at"])
    return run
