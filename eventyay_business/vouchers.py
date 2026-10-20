"""Platform fee vouchers redeemed against plans and add-ons."""

import logging
from dataclasses import dataclass
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils.timezone import now
from django.utils.translation import gettext_lazy as _
from django_scopes import scopes_disabled
from eventyay.base.models.choices import PriceModeChoices
from eventyay.base.models.vouchers import InvoiceVoucher

from .models import (
    BusinessInvoiceLine,
    EventAddon,
    InvoiceLineType,
    OrganizerAddon,
    Subscription,
    VoucherBusinessTargets,
    VoucherDiscountDuration,
    VoucherRedemption,
    VoucherRedemptionStatus,
)

logger = logging.getLogger(__name__)

CENT = Decimal("0.01")
ZERO = Decimal("0.00")

# Stripe Checkout sessions carrying a voucher expire after CHECKOUT_SESSION_EXPIRY. Their
# reservations stop counting a while later, in case the expiry webhook never arrives.
CHECKOUT_SESSION_EXPIRY = timedelta(hours=1)
CHECKOUT_RESERVATION_TTL = timedelta(hours=2)

VOUCHER_USED_UP = _("This voucher has expired or has already been used up.")


@dataclass(frozen=True)
class VoucherQuote:
    voucher: InvoiceVoucher
    targets: VoucherBusinessTargets
    amount: Decimal
    discount: Decimal

    @property
    def total(self) -> Decimal:
        return self.amount - self.discount


def voucher_discount(voucher: InvoiceVoucher, amount: Decimal) -> Decimal:
    """Return how much of ``amount`` the voucher takes off, capped by its remaining budget.

    Fixed credits are treated as being in the currency of ``amount``.
    """
    if amount <= ZERO or voucher.value is None:
        return ZERO
    if voucher.price_mode == PriceModeChoices.PERCENT:
        discount = (amount * voucher.value / Decimal("100")).quantize(
            CENT, ROUND_HALF_UP
        )
    elif voucher.price_mode == PriceModeChoices.SUBTRACT:
        discount = min(voucher.value, amount)
    elif voucher.price_mode == PriceModeChoices.SET:
        discount = max(amount - voucher.value, ZERO)
    else:
        return ZERO
    remaining = voucher.remaining_budget()
    if remaining is not None:
        discount = min(discount, remaining)
    return min(discount, amount)


def pending_reservations(voucher: InvoiceVoucher) -> int:
    return VoucherRedemption.objects.filter(
        voucher=voucher,
        status=VoucherRedemptionStatus.PENDING,
        created_at__gte=now() - CHECKOUT_RESERVATION_TTL,
    ).count()


def voucher_has_capacity(voucher: InvoiceVoucher) -> bool:
    """Return whether the voucher can be redeemed again, counting checkouts in progress."""
    return (
        voucher.is_active()
        and voucher.redeemed + pending_reservations(voucher) < voucher.max_usages
    )


def lock_redeemable_voucher(voucher_id: int) -> InvoiceVoucher:
    """Lock the voucher row and check it can be redeemed again. Call inside a transaction."""
    voucher = InvoiceVoucher.objects.select_for_update().get(pk=voucher_id)
    if not voucher_has_capacity(voucher):
        raise ValidationError(VOUCHER_USED_UP)
    return voucher


def voucher_allows_organizer(voucher: InvoiceVoucher, organizer, event=None) -> bool:
    """Check the voucher's organiser and event restrictions for a purchase."""
    with scopes_disabled():
        limit_events = voucher.limit_events.all()
        organizer_ids = set(voucher.limit_organizer.values_list("pk", flat=True))
        if not organizer_ids and not limit_events.exists():
            return True
        if organizer.pk in organizer_ids:
            return True
        if event is not None:
            return limit_events.filter(pk=event.pk).exists()
        return limit_events.filter(organizer=organizer).exists()


def get_voucher_quote(
    code: str, organizer, *, amount: Decimal, tier=None, addon=None, event=None
) -> VoucherQuote:
    """Validate a voucher code for buying ``tier`` or ``addon`` and price the discount.

    Raises ``ValidationError`` with a message for the organiser when the code cannot be used.
    """
    code = (code or "").strip()
    voucher = InvoiceVoucher.objects.filter(code=code).first() if code else None
    if voucher is None:
        raise ValidationError(_("This voucher code is not valid."))
    if not voucher_has_capacity(voucher):
        raise ValidationError(VOUCHER_USED_UP)
    if not voucher_allows_organizer(voucher, organizer, event=event):
        raise ValidationError(_("This voucher is not valid for your organisation."))

    targets = VoucherBusinessTargets.objects.filter(voucher=voucher).first()
    if tier is not None:
        name = tier.name
        applies = targets is not None and targets.tiers.filter(pk=tier.pk).exists()
    else:
        name = addon.name
        applies = targets is not None and targets.addons.filter(pk=addon.pk).exists()
    if not applies:
        raise ValidationError(
            _("This voucher cannot be used for %(name)s.") % {"name": name}
        )

    discount = voucher_discount(voucher, amount)
    if discount <= ZERO:
        raise ValidationError(
            _("This voucher gives no discount on %(name)s.") % {"name": name}
        )
    return VoucherQuote(
        voucher=voucher, targets=targets, amount=amount, discount=discount
    )


def redeem_voucher(
    quote: VoucherQuote,
    organizer,
    *,
    subscription: Subscription | None = None,
    tier=None,
    organizer_addon: OrganizerAddon | None = None,
    event_addon: EventAddon | None = None,
) -> VoucherRedemption:
    """Record a redemption billed on consolidated invoices.

    The discount itself is added to each invoice by ``build_voucher_credit_lines``. Call this
    inside the transaction that activates the purchase, so a voucher that was used up in the
    meantime rolls the purchase back.
    """
    with transaction.atomic():
        voucher = lock_redeemable_voucher(quote.voucher.pk)
        voucher.redeemed += 1
        voucher.save(update_fields=["redeemed", "updated_at"])
        return VoucherRedemption.objects.create(
            voucher=voucher,
            organizer=organizer,
            subscription=subscription,
            tier=tier,
            organizer_addon=organizer_addon,
            event_addon=event_addon,
            duration=quote.targets.duration,
            period_limit=quote.targets.period_limit,
        )


def reserve_checkout_voucher(quote: VoucherQuote, organizer) -> VoucherRedemption:
    """Reserve one use of the voucher for a Stripe Checkout session about to be created."""
    with transaction.atomic():
        voucher = lock_redeemable_voucher(quote.voucher.pk)
        return VoucherRedemption.objects.create(
            voucher=voucher,
            organizer=organizer,
            duration=quote.targets.duration,
            period_limit=quote.targets.period_limit,
            periods_applied=1,
            status=VoucherRedemptionStatus.PENDING,
        )


def attach_checkout_session(reservation: VoucherRedemption, session_id: str) -> None:
    VoucherRedemption.objects.filter(pk=reservation.pk).update(
        stripe_checkout_session_id=session_id
    )


def release_reservation(reservation_id) -> int:
    return VoucherRedemption.objects.filter(
        pk=reservation_id, status=VoucherRedemptionStatus.PENDING
    ).update(status=VoucherRedemptionStatus.RELEASED)


def release_checkout_reservation(session_data: dict) -> int:
    """Free the voucher reserved for a Stripe Checkout session that expired unpaid."""
    reservation_id = (session_data.get("metadata") or {}).get("voucher_reservation_id")
    if reservation_id:
        return release_reservation(reservation_id)
    session_id = session_data.get("id")
    if not session_id:
        return 0
    return VoucherRedemption.objects.filter(
        stripe_checkout_session_id=session_id, status=VoucherRedemptionStatus.PENDING
    ).update(status=VoucherRedemptionStatus.RELEASED)


def record_checkout_redemption(
    session_data: dict, purchase
) -> VoucherRedemption | None:
    """Turn the voucher reserved for a Stripe Checkout session into a redemption once paid.

    Stripe applies the discount through a coupon, including on renewals, so the redemption
    only tracks usage. Safe to call more than once for the same session.
    """
    metadata = session_data.get("metadata") or {}
    voucher_id = metadata.get("invoice_voucher_id")
    reservation_id = metadata.get("voucher_reservation_id")
    session_id = session_data.get("id") or ""
    if not voucher_id or not session_id or purchase is None:
        return None

    if isinstance(purchase, Subscription):
        purchase_fields = {
            "organizer": purchase.organizer,
            "subscription": purchase,
            "tier": purchase.tier_version.tier,
        }
    elif isinstance(purchase, OrganizerAddon):
        purchase_fields = {"organizer": purchase.organizer, "organizer_addon": purchase}
    elif isinstance(purchase, EventAddon):
        purchase_fields = {
            "organizer": purchase.event.organizer,
            "event_addon": purchase,
        }
    else:
        return None

    discount_cents = (session_data.get("total_details") or {}).get(
        "amount_discount"
    ) or 0
    discount = (Decimal(discount_cents) / Decimal("100")).quantize(CENT)
    try:
        with transaction.atomic():
            voucher = (
                InvoiceVoucher.objects.select_for_update().filter(pk=voucher_id).first()
            )
            if voucher is None:
                logger.warning(
                    "Voucher %s from checkout session %s no longer exists",
                    voucher_id,
                    session_id,
                )
                return None
            redemptions = VoucherRedemption.objects.select_for_update()
            redemption = (
                redemptions.filter(pk=reservation_id, voucher=voucher).first()
                if reservation_id
                else None
            ) or redemptions.filter(stripe_checkout_session_id=session_id).first()
            if redemption and redemption.status == VoucherRedemptionStatus.REDEEMED:
                return redemption

            voucher.redeemed += 1
            voucher.budget_used += discount
            voucher.save(update_fields=["redeemed", "budget_used", "updated_at"])
            if voucher.redeemed > voucher.max_usages:
                logger.warning(
                    "Voucher %s is now redeemed %s times, over its limit of %s, "
                    "after checkout session %s",
                    voucher.code,
                    voucher.redeemed,
                    voucher.max_usages,
                    session_id,
                )

            if redemption is None:
                # A session created before reservations existed.
                targets = VoucherBusinessTargets.objects.filter(voucher=voucher).first()
                return VoucherRedemption.objects.create(
                    voucher=voucher,
                    duration=(
                        targets.duration if targets else VoucherDiscountDuration.ONCE
                    ),
                    period_limit=targets.period_limit if targets else 1,
                    periods_applied=1,
                    stripe_checkout_session_id=session_id,
                    **purchase_fields,
                )
            for field, value in purchase_fields.items():
                setattr(redemption, field, value)
            redemption.stripe_checkout_session_id = session_id
            redemption.status = VoucherRedemptionStatus.REDEEMED
            redemption.save()
            return redemption
    except IntegrityError:
        return VoucherRedemption.objects.filter(
            stripe_checkout_session_id=session_id
        ).first()


def redemption_key(line: dict) -> tuple[str, int] | None:
    metadata = line.get("calculation_metadata") or {}
    if line["line_type"] == InvoiceLineType.SUBSCRIPTION and metadata.get(
        "subscription_id"
    ):
        return ("subscription", metadata["subscription_id"])
    if line["line_type"] == InvoiceLineType.ADDON:
        if metadata.get("organizer_addon_id"):
            return ("organizer_addon", metadata["organizer_addon_id"])
        if metadata.get("event_addon_id"):
            return ("event_addon", metadata["event_addon_id"])
    return None


def build_voucher_credit_lines(organizer, lines_data: list[dict]) -> list[dict]:
    """Return credit lines for the plan and add-on lines that a redeemed voucher discounts.

    Each discounted line counts as one billing period. Lines for service periods that ended
    before the day the voucher was redeemed are not discounted. Must run inside the invoice
    generation transaction.
    """
    redemptions = list(
        VoucherRedemption.objects.filter(
            organizer=organizer,
            status=VoucherRedemptionStatus.REDEEMED,
            stripe_checkout_session_id="",
        ).select_related("voucher")
    )
    latest = {}
    for redemption in redemptions:
        if redemption.subscription_id:
            latest.setdefault(("subscription", redemption.subscription_id), redemption)
        if redemption.organizer_addon_id:
            latest.setdefault(
                ("organizer_addon", redemption.organizer_addon_id), redemption
            )
        if redemption.event_addon_id:
            latest.setdefault(("event_addon", redemption.event_addon_id), redemption)
    if not latest:
        return []

    vouchers = {}
    applied = {}
    credits = []
    ordered_lines = sorted(
        lines_data,
        key=lambda line: line.get("service_period_start") or redemptions[0].created_at,
    )
    for line in ordered_lines:
        key = redemption_key(line)
        redemption = latest.get(key) if key else None
        if redemption is None:
            continue
        if (
            redemption.period_limit is not None
            and redemption.periods_applied + applied.get(redemption.pk, 0)
            >= redemption.period_limit
        ):
            continue
        # Compare by day: a one-time add-on's line ends the moment it was bought, just
        # before its redemption is saved.
        redeemed_on = redemption.created_at.replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        service_period_end = line.get("service_period_end")
        if service_period_end and service_period_end < redeemed_on:
            continue
        tier_version = line.get("tier_version")
        if key[0] == "subscription" and (
            tier_version is None or tier_version.tier_id != redemption.tier_id
        ):
            # The organiser has since moved to a plan the voucher was not redeemed for.
            continue
        source_key = f"voucher:{redemption.pk}:{line['usage_reference']}"
        if BusinessInvoiceLine.objects.filter(usage_reference=source_key).exists():
            continue
        voucher = vouchers.get(redemption.voucher_id)
        if voucher is None:
            voucher = InvoiceVoucher.objects.select_for_update().get(
                pk=redemption.voucher_id
            )
            vouchers[voucher.pk] = voucher
        if not voucher.is_usable():
            continue
        discount = voucher_discount(voucher, line["amount"])
        if discount <= ZERO:
            continue

        voucher.budget_used += discount
        applied[redemption.pk] = applied.get(redemption.pk, 0) + 1
        credits.append(
            {
                "line_type": InvoiceLineType.CREDIT,
                "event": line["event"],
                "description": f"Voucher {voucher.code}: {line['description']}"[:255],
                "quantity": Decimal("1.00"),
                "unit_price": -discount,
                "amount": -discount,
                "tier_version": tier_version,
                "addon": line.get("addon"),
                "usage_reference": source_key,
                "calculation_metadata": {
                    "voucher_id": voucher.pk,
                    "voucher_code": voucher.code,
                    "redemption_id": redemption.pk,
                    "discounted_line": line["usage_reference"],
                    "discount": str(discount),
                },
                "service_period_start": line.get("service_period_start"),
                "service_period_end": line.get("service_period_end"),
            }
        )

    for voucher in vouchers.values():
        voucher.save(update_fields=["budget_used", "updated_at"])
    for redemption in redemptions:
        if redemption.pk in applied:
            redemption.periods_applied += applied[redemption.pk]
            redemption.save(update_fields=["periods_applied"])
    return credits


def redeem_addon_voucher(
    quote: VoucherQuote, assignment: OrganizerAddon | EventAddon
) -> VoucherRedemption:
    if isinstance(assignment, EventAddon):
        return redeem_voucher(quote, assignment.event.organizer, event_addon=assignment)
    return redeem_voucher(quote, assignment.organizer, organizer_addon=assignment)
