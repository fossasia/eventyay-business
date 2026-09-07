import logging
from django.db import IntegrityError, transaction
from django.utils.timezone import now

logger = logging.getLogger(__name__)


def record_usage(
    organizer,
    capability,
    quantity,
    unit,
    source_type,
    source_id,
    idempotency_key,
    event=None,
    metadata=None,
):
    """
    Atomically records a usage event.
    Relies on database unique constraints for idempotency.
    Returns the created UsageRecord or None if it was already processed.
    """
    from .models import UsageRecord

    try:
        with transaction.atomic():
            record = UsageRecord.objects.create(
                organizer=organizer,
                event=event,
                capability=capability,
                quantity=quantity,
                unit=unit,
                source_type=source_type,
                source_id=source_id,
                idempotency_key=idempotency_key,
                metadata=metadata or {},
                occurred_at=now(),
            )
            return record
    except IntegrityError as exc:
        cause = getattr(exc, "__cause__", None)
        diag = getattr(cause, "diag", None) if cause else None
        constraint_name = getattr(diag, "constraint_name", None) if diag else None

        if constraint_name == "unique_usage_idempotency_per_organizer":
            logger.info(
                f"Usage record with idempotency key {idempotency_key} already exists for organizer {organizer.slug}."
            )
            return None

        # Re-raise all other integrity errors
        raise
