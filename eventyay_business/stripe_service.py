from typing import Optional

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.timezone import now

from .invoicing_service import generate_invoice_number
from .models import (
    AddonDefinition,
    AddonPricingMode,
    AddonStatus,
    BillingInterval,
    BusinessInvoice,
    BusinessInvoiceLine,
    BusinessInvoiceStatus,
    EventAddon,
    InvoiceLineType,
    OrganizerAddon,
    Subscription,
    SubscriptionStatus,
    TierPrice,
    TierVersion,
)
from .services import invalidate_entitlement_cache, log_addon_lifecycle_action
from .signals import addon_purchased, subscription_purchased

logger = logging.getLogger(__name__)

try:
    from django_scopes import scopes_disabled
except ModuleNotFoundError as err:
    if err.name != "django_scopes":
        raise
    from contextlib import nullcontext as scopes_disabled

try:
    import stripe
except ImportError:
    stripe = None


def is_stripe_configured() -> bool:
    """Return True if Stripe secret key is available in Eventyay configuration."""
    if stripe is None:
        return False
    return bool(get_stripe_secret_key_safe())


def get_stripe_secret_key_safe() -> Optional[str]:
    """Return configured secret key, or None if not configured."""
    try:
        from eventyay.helpers.stripe_utils import get_stripe_secret_key

        return get_stripe_secret_key()
    except Exception:
        return None


def get_or_create_stripe_customer(organizer, user=None) -> Optional[str]:
    """
    Retrieve existing stripe_customer_id or create a new Stripe customer.
    """
    secret_key = get_stripe_secret_key_safe()
    if not secret_key or stripe is None:
        return None

    stripe.api_key = secret_key

    # 1. Check existing subscription
    sub = (
        organizer.subscriptions.filter(stripe_customer_id__isnull=False)
        .exclude(stripe_customer_id="")
        .first()
    )
    if sub and sub.stripe_customer_id:
        return sub.stripe_customer_id

    # 2. Check OrganizerBillingModel
    try:
        from eventyay.base.models.organizer import OrganizerBillingModel

        billing = OrganizerBillingModel.objects.filter(
            organizer_id=organizer.id, stripe_customer_id__isnull=False
        ).first()
        if billing and billing.stripe_customer_id:
            return billing.stripe_customer_id
    except Exception:
        pass

    # 3. Create customer in Stripe
    try:
        email = (
            user.email if user and user.email else f"billing@{organizer.slug}.eventyay"
        )
        customer = stripe.Customer.create(
            email=email,
            name=organizer.name,
            metadata={"organizer_slug": organizer.slug},
        )
        # Persist customer.id so subsequent calls do not create duplicate customers
        active_sub = (
            organizer.subscriptions.filter(status=SubscriptionStatus.ACTIVE).first()
            or organizer.subscriptions.first()
        )
        if active_sub:
            active_sub.stripe_customer_id = customer.id
            active_sub.save(update_fields=["stripe_customer_id"])
        else:
            try:
                from eventyay.base.models.organizer import OrganizerBillingModel

                billing, _ = OrganizerBillingModel.objects.get_or_create(
                    organizer=organizer,
                    defaults={
                        "primary_contact_name": organizer.name,
                        "primary_contact_email": email,
                    },
                )
                billing.stripe_customer_id = customer.id
                billing.save(update_fields=["stripe_customer_id"])
            except Exception:
                pass

        return customer.id
    except Exception as exc:
        logger.error("Failed to create Stripe customer for %s: %s", organizer.slug, exc)
        return None


def sync_tier_price_to_stripe(tier_price: TierPrice) -> Optional[str]:
    """
    Ensure the Tier and TierPrice exist in Stripe as Product and Price.
    Returns the stripe_price_id.
    """
    secret_key = get_stripe_secret_key_safe()
    if not secret_key or stripe is None or not tier_price:
        return getattr(tier_price, "stripe_price_id", None)

    stripe.api_key = secret_key
    tier_version = tier_price.tier_version
    tier = tier_version.tier

    # 1. Product
    product_id = getattr(tier, "stripe_product_id", None)
    if not product_id:
        try:
            prod_kwargs = {
                "name": f"{tier.name} (v{tier_version.version})",
                "metadata": {
                    "tier_slug": tier.slug,
                    "tier_version": str(tier_version.version),
                },
            }
            if tier.description:
                prod_kwargs["description"] = tier.description
            prod = stripe.Product.create(**prod_kwargs)
            product_id = prod.id
            if hasattr(tier, "stripe_product_id"):
                tier.stripe_product_id = product_id
                tier.save(update_fields=["stripe_product_id", "updated_at"])
        except Exception as exc:
            logger.warning(
                "Failed to create Stripe product for tier %s: %s", tier.slug, exc
            )
            return getattr(tier_price, "stripe_price_id", None)

    # 2. Price
    if getattr(tier_price, "stripe_price_id", None):
        return tier_price.stripe_price_id

    amount_val = getattr(tier_price, "amount", None)
    if amount_val is None:
        amount_val = getattr(tier_price, "price", Decimal("0.00"))
    unit_amount = int(Decimal(str(amount_val)) * 100)
    interval_val = getattr(tier_price, "billing_interval", None) or getattr(
        tier_price, "interval", BillingInterval.MONTHLY
    )
    interval = (
        "month"
        if interval_val in ("month", "monthly", BillingInterval.MONTHLY)
        else "year"
    )

    try:
        price = stripe.Price.create(
            product=product_id,
            unit_amount=unit_amount,
            currency=(tier_price.currency or "usd").lower(),
            recurring={"interval": interval},
            metadata={
                "tier_price_id": str(tier_price.id),
                "tier_slug": tier.slug,
            },
        )
        tier_price.stripe_price_id = price.id
        tier_price.save(update_fields=["stripe_price_id"])
        return price.id
    except Exception as exc:
        logger.warning(
            "Failed to create Stripe price for tier price %s: %s", tier_price.id, exc
        )
        return None


def sync_addon_to_stripe(addon: AddonDefinition) -> Optional[str]:
    """
    Ensure the AddonDefinition exists in Stripe as Product and Price.
    Returns the stripe_price_id.
    """
    secret_key = get_stripe_secret_key_safe()
    if (
        not secret_key
        or stripe is None
        or not addon
        or not addon.price
        or addon.price <= 0
    ):
        return getattr(addon, "stripe_price_id", None)

    stripe.api_key = secret_key

    # 1. Product
    product_id = getattr(addon, "stripe_product_id", None)
    if not product_id:
        try:
            prod_kwargs = {
                "name": addon.name,
                "metadata": {
                    "addon_slug": addon.slug,
                    "capability": addon.capability,
                    "assignment_scope": addon.assignment_scope,
                },
            }
            if addon.description:
                prod_kwargs["description"] = addon.description
            prod = stripe.Product.create(**prod_kwargs)
            product_id = prod.id
            addon.stripe_product_id = product_id
            addon.save(update_fields=["stripe_product_id", "updated_at"])
        except Exception as exc:
            logger.warning(
                "Failed to create Stripe product for addon %s: %s", addon.slug, exc
            )
            return getattr(addon, "stripe_price_id", None)

    # 2. Price
    if getattr(addon, "stripe_price_id", None):
        return addon.stripe_price_id

    unit_amount = int(Decimal(str(addon.price)) * 100)
    price_kwargs = {
        "product": product_id,
        "unit_amount": unit_amount,
        "currency": (addon.currency or "usd").lower(),
        "metadata": {
            "addon_id": str(addon.id),
            "addon_slug": addon.slug,
        },
    }
    if addon.pricing_mode == AddonPricingMode.RECURRING:
        price_kwargs["recurring"] = {"interval": "month"}

    try:
        price = stripe.Price.create(**price_kwargs)
        addon.stripe_price_id = price.id
        addon.save(update_fields=["stripe_price_id", "updated_at"])
        return price.id
    except Exception as exc:
        logger.warning(
            "Failed to create Stripe price for addon %s: %s", addon.slug, exc
        )
        return None


def create_addon_checkout_session(
    organizer,
    addon: AddonDefinition,
    user,
    quantity: int = 1,
    event=None,
    success_url: str = "",
    cancel_url: str = "",
    assignment=None,
) -> Optional[str]:
    """
    Create a Stripe Checkout Session for an add-on purchase and return the session URL.
    """
    secret_key = get_stripe_secret_key_safe()
    if not secret_key or stripe is None:
        raise ValidationError("Stripe is not configured.")

    stripe.api_key = secret_key
    customer_id = get_or_create_stripe_customer(organizer, user=user)

    unit_amount = int(Decimal(str(addon.price)) * 100)
    currency = (addon.currency or "usd").lower()

    metadata = {
        "type": "addon_purchase",
        "scope": addon.assignment_scope,
        "organizer_slug": organizer.slug,
        "event_slug": event.slug if event else "",
        "addon_id": str(addon.pk),
        "quantity": str(quantity),
        "user_id": str(user.pk) if user else "",
        "assignment_id": str(assignment.pk) if assignment else "",
    }

    mode = (
        "subscription"
        if addon.pricing_mode == AddonPricingMode.RECURRING
        else "payment"
    )

    price_id = addon.stripe_price_id or sync_addon_to_stripe(addon)
    if price_id:
        line_items = [{"price": price_id, "quantity": quantity}]
    else:
        price_data = {
            "currency": currency,
            "unit_amount": unit_amount,
            "product_data": {"name": f"{addon.name} (Add-on)"},
        }
        if mode == "subscription":
            price_data["recurring"] = {"interval": "month"}
        line_items = [{"price_data": price_data, "quantity": quantity}]

    if success_url and "{CHECKOUT_SESSION_ID}" not in success_url:
        sep = "&" if "?" in success_url else "?"
        success_url = f"{success_url}{sep}session_id={{CHECKOUT_SESSION_ID}}"

    session_kwargs = {
        "payment_method_types": ["card"],
        "line_items": line_items,
        "mode": mode,
        "success_url": success_url,
        "cancel_url": cancel_url,
        "metadata": metadata,
    }
    if customer_id:
        session_kwargs["customer"] = customer_id

    if mode == "subscription":
        session_kwargs["subscription_data"] = {"metadata": metadata}

    session = stripe.checkout.Session.create(**session_kwargs)
    return session.url


def create_subscription_checkout_session(
    organizer,
    tier_price: TierPrice,
    user,
    success_url: str = "",
    cancel_url: str = "",
) -> Optional[str]:
    """
    Create a Stripe Checkout Session for a Tier subscription and return the session URL.
    """
    secret_key = get_stripe_secret_key_safe()
    if not secret_key or stripe is None:
        raise ValidationError("Stripe is not configured.")

    stripe.api_key = secret_key
    customer_id = get_or_create_stripe_customer(organizer, user=user)

    amount_val = getattr(tier_price, "amount", None)
    if amount_val is None:
        amount_val = getattr(tier_price, "price", Decimal("0.00"))
    unit_amount = int(Decimal(str(amount_val)) * 100)
    currency = (tier_price.currency or "usd").lower()
    interval_val = getattr(tier_price, "billing_interval", None) or getattr(
        tier_price, "interval", BillingInterval.MONTHLY
    )
    interval = (
        "month"
        if interval_val in ("month", "monthly", BillingInterval.MONTHLY)
        else "year"
    )

    metadata = {
        "type": "subscription",
        "scope": "organizer",
        "organizer_slug": organizer.slug,
        "tier_price_id": str(tier_price.pk),
        "tier_version_id": str(tier_price.tier_version.pk),
        "user_id": str(user.pk) if user else "",
    }

    price_id = tier_price.stripe_price_id or sync_tier_price_to_stripe(tier_price)
    if price_id:
        line_items = [{"price": price_id, "quantity": 1}]
    else:
        price_data = {
            "currency": currency,
            "unit_amount": unit_amount,
            "product_data": {"name": f"{tier_price.tier_version.tier.name} Plan"},
            "recurring": {"interval": interval},
        }
        line_items = [{"price_data": price_data, "quantity": 1}]

    if success_url and "{CHECKOUT_SESSION_ID}" not in success_url:
        sep = "&" if "?" in success_url else "?"
        success_url = f"{success_url}{sep}session_id={{CHECKOUT_SESSION_ID}}"

    session_kwargs = {
        "payment_method_types": ["card"],
        "line_items": line_items,
        "mode": "subscription",
        "success_url": success_url,
        "cancel_url": cancel_url,
        "metadata": metadata,
        "subscription_data": {"metadata": metadata},
    }
    if customer_id:
        session_kwargs["customer"] = customer_id

    session = stripe.checkout.Session.create(**session_kwargs)
    return session.url


def process_webhook_event(event_type: str, data_object: dict):
    """
    Process incoming verified Stripe webhook event.
    """
    if event_type == "checkout.session.completed":
        return process_checkout_session_completed(data_object)
    elif event_type in (
        "customer.subscription.updated",
        "customer.subscription.deleted",
    ):
        return process_subscription_change(event_type, data_object)
    elif event_type == "invoice.payment_failed":
        return process_invoice_payment_failed(data_object)
    elif event_type == "invoice.paid":
        return process_invoice_paid(data_object)
    return None


def process_checkout_session_completed(session_data: dict):
    """
    Handle checkout.session.completed for add-on purchases and subscriptions.
    """
    metadata = session_data.get("metadata", {})
    item_type = metadata.get("type")

    if item_type == "addon_purchase":
        return process_addon_checkout_completed(session_data)
    elif item_type == "subscription":
        return process_subscription_checkout_completed(session_data)
    return None


def _record_checkout_invoice_for_subscription(
    organizer,
    subscription: Subscription,
    tier_version: TierVersion,
    tier_price: Optional[TierPrice],
    session_data: dict,
    user=None,
) -> Optional[BusinessInvoice]:
    """
    Creates a BusinessInvoice and BusinessInvoiceLine (status=PAID)
    for a completed subscription checkout session. Idempotent.
    """
    amount_total = session_data.get("amount_total")
    if amount_total is not None:
        try:
            total = (Decimal(str(amount_total)) / Decimal("100")).quantize(
                Decimal("0.01")
            )
        except Exception:
            total = getattr(tier_price, "amount", Decimal("0.00"))
    elif tier_price and tier_price.amount is not None:
        total = tier_price.amount
    else:
        total = Decimal("0.00")

    if total <= Decimal("0.00"):
        return None

    currency = (
        session_data.get("currency")
        or (tier_price.currency if tier_price else None)
        or subscription.currency
        or "USD"
    ).upper()

    stripe_sub_id = session_data.get("subscription")
    stripe_pi_id = session_data.get("payment_intent")
    stripe_invoice_id = session_data.get("invoice")

    if hasattr(stripe_invoice_id, "id"):
        stripe_invoice_id = stripe_invoice_id.id
    if hasattr(stripe_pi_id, "id"):
        stripe_pi_id = stripe_pi_id.id

    if not stripe_invoice_id and stripe_sub_id and stripe:
        try:
            secret_key = get_stripe_secret_key_safe()
            if secret_key:
                stripe.api_key = secret_key
                s_sub = stripe.Subscription.retrieve(stripe_sub_id)
                latest_inv = getattr(s_sub, "latest_invoice", None)
                if hasattr(latest_inv, "id"):
                    stripe_invoice_id = latest_inv.id
                elif isinstance(latest_inv, str):
                    stripe_invoice_id = latest_inv
        except Exception:
            pass

    if stripe_invoice_id:
        existing = BusinessInvoice.objects.filter(
            stripe_invoice_id=stripe_invoice_id
        ).first()
        if existing:
            return existing

    if stripe_pi_id:
        existing = BusinessInvoice.objects.filter(
            stripe_payment_intent_id=stripe_pi_id
        ).first()
        if existing:
            return existing

    start_date = subscription.starts_at or now()
    end_date = subscription.ends_at or (start_date + timedelta(days=30))

    existing_period = BusinessInvoice.objects.filter(
        organizer=organizer,
        billing_period_start=start_date,
        billing_period_end=end_date,
    ).first()
    if existing_period:
        if stripe_invoice_id and not existing_period.stripe_invoice_id:
            existing_period.stripe_invoice_id = stripe_invoice_id
            existing_period.save(update_fields=["stripe_invoice_id"])
        if stripe_pi_id and not existing_period.stripe_payment_intent_id:
            existing_period.stripe_payment_intent_id = stripe_pi_id
            existing_period.save(update_fields=["stripe_payment_intent_id"])
        return existing_period

    invoice_num = generate_invoice_number(organizer, start_date)
    invoice = BusinessInvoice.objects.create(
        organizer=organizer,
        invoice_number=invoice_num,
        billing_period_start=start_date,
        billing_period_end=end_date,
        currency=currency,
        status=BusinessInvoiceStatus.PAID,
        subtotal=total,
        tax=Decimal("0.00"),
        total=total,
        stripe_payment_intent_id=stripe_pi_id,
        stripe_invoice_id=stripe_invoice_id,
    )

    interval_disp = (
        subscription.get_billing_interval_display()
        if hasattr(subscription, "get_billing_interval_display")
        else subscription.billing_interval
    )
    desc = f"Plan Subscription: {tier_version.tier.name} ({interval_disp})"
    BusinessInvoiceLine.objects.create(
        invoice=invoice,
        line_type=InvoiceLineType.SUBSCRIPTION,
        description=desc,
        quantity=Decimal("1.00"),
        unit_price=total,
        amount=total,
        tier_version=tier_version,
        usage_reference=f"subscription_{subscription.pk}",
        calculation_metadata={
            "subscription_id": subscription.pk,
            "tier_slug": tier_version.tier.slug,
            "tier_name": tier_version.tier.name,
            "tier_version": tier_version.version,
            "billing_interval": str(subscription.billing_interval),
            "stripe_subscription_id": stripe_sub_id,
            "stripe_checkout_session_id": session_data.get("id"),
        },
    )
    logger.info(
        "Created BusinessInvoice %s (%s %s) for subscription %s",
        invoice_num,
        total,
        currency,
        subscription.pk,
    )
    return invoice


def _record_checkout_invoice_for_addon(
    organizer,
    assignment,
    addon: AddonDefinition,
    quantity: int,
    session_data: dict,
    event=None,
    user=None,
) -> Optional[BusinessInvoice]:
    """
    Creates a BusinessInvoice and BusinessInvoiceLine (status=PAID)
    for a completed add-on checkout session. Idempotent.
    """
    amount_total = session_data.get("amount_total")
    if amount_total is not None:
        try:
            total = (Decimal(str(amount_total)) / Decimal("100")).quantize(
                Decimal("0.01")
            )
        except Exception:
            total = (Decimal(str(addon.price)) * Decimal(quantity)).quantize(
                Decimal("0.01")
            )
    elif assignment.price is not None:
        total = (Decimal(str(assignment.price)) * Decimal(quantity)).quantize(
            Decimal("0.01")
        )
    elif addon.price is not None:
        total = (Decimal(str(addon.price)) * Decimal(quantity)).quantize(
            Decimal("0.01")
        )
    else:
        total = Decimal("0.00")

    if total <= Decimal("0.00"):
        return None

    currency = (
        session_data.get("currency") or assignment.currency or addon.currency or "USD"
    ).upper()

    stripe_pi_id = session_data.get("payment_intent")
    stripe_sub_id = session_data.get("subscription")
    stripe_invoice_id = session_data.get("invoice")

    if hasattr(stripe_invoice_id, "id"):
        stripe_invoice_id = stripe_invoice_id.id
    if hasattr(stripe_pi_id, "id"):
        stripe_pi_id = stripe_pi_id.id

    if stripe_invoice_id:
        existing = BusinessInvoice.objects.filter(
            stripe_invoice_id=stripe_invoice_id
        ).first()
        if existing:
            return existing

    if stripe_pi_id:
        existing = BusinessInvoice.objects.filter(
            stripe_payment_intent_id=stripe_pi_id
        ).first()
        if existing:
            return existing

    start_date = assignment.starts_at or now()
    end_date = assignment.ends_at or (start_date + timedelta(days=30))

    existing_period = BusinessInvoice.objects.filter(
        organizer=organizer,
        billing_period_start=start_date,
        billing_period_end=end_date,
    ).first()
    if existing_period:
        if stripe_invoice_id and not existing_period.stripe_invoice_id:
            existing_period.stripe_invoice_id = stripe_invoice_id
            existing_period.save(update_fields=["stripe_invoice_id"])
        if stripe_pi_id and not existing_period.stripe_payment_intent_id:
            existing_period.stripe_payment_intent_id = stripe_pi_id
            existing_period.save(update_fields=["stripe_payment_intent_id"])
        return existing_period

    invoice_num = generate_invoice_number(organizer, start_date)
    invoice = BusinessInvoice.objects.create(
        organizer=organizer,
        invoice_number=invoice_num,
        billing_period_start=start_date,
        billing_period_end=end_date,
        currency=currency,
        status=BusinessInvoiceStatus.PAID,
        subtotal=total,
        tax=Decimal("0.00"),
        total=total,
        stripe_payment_intent_id=stripe_pi_id,
        stripe_invoice_id=stripe_invoice_id,
    )

    unit_price = (total / Decimal(quantity)).quantize(Decimal("0.01"))
    BusinessInvoiceLine.objects.create(
        invoice=invoice,
        event=event,
        line_type=InvoiceLineType.ADDON,
        description=f"Add-on: {addon.name} (x{quantity})",
        quantity=Decimal(str(quantity)),
        unit_price=unit_price,
        amount=total,
        addon=addon,
        usage_reference=f"addon_assignment_{assignment.pk}",
        calculation_metadata={
            "assignment_id": assignment.pk,
            "addon_id": addon.pk,
            "addon_slug": addon.slug,
            "addon_name": addon.name,
            "quantity": int(quantity),
            "stripe_subscription_id": stripe_sub_id,
            "stripe_payment_intent_id": stripe_pi_id,
            "stripe_checkout_session_id": session_data.get("id"),
        },
    )
    logger.info(
        "Created BusinessInvoice %s (%s %s) for add-on %s",
        invoice_num,
        total,
        currency,
        addon.slug,
    )
    return invoice


def process_addon_checkout_completed(session_data: dict):
    from eventyay.base.models import Event, Organizer, User

    metadata = session_data.get("metadata", {})
    organizer_slug = metadata.get("organizer_slug")
    event_slug = metadata.get("event_slug")
    addon_id = metadata.get("addon_id")
    assignment_id = metadata.get("assignment_id")
    quantity = int(metadata.get("quantity", 1))
    user_id = metadata.get("user_id")

    stripe_sub_id = session_data.get("subscription")
    stripe_pi_id = session_data.get("payment_intent")

    with scopes_disabled():
        try:
            organizer = Organizer.objects.get(slug=organizer_slug)
        except Organizer.DoesNotExist:
            logger.error("Organizer %s not found for checkout session", organizer_slug)
            return None

        event = None
        if event_slug:
            try:
                event = Event.objects.get(organizer=organizer, slug=event_slug)
            except Event.DoesNotExist:
                logger.error("Event %s not found for checkout session", event_slug)
                return None

        try:
            addon = AddonDefinition.objects.get(pk=addon_id)
        except AddonDefinition.DoesNotExist:
            logger.error("Addon %s not found for checkout session", addon_id)
            return None

        user = None
        if user_id:
            user = User.objects.filter(pk=user_id).first()

        current_time = now()
        ends_at = (
            current_time + timedelta(days=30)
            if addon.pricing_mode == AddonPricingMode.RECURRING
            else None
        )

        with transaction.atomic():
            if assignment_id:
                # Update existing pending assignment
                model_cls = EventAddon if event else OrganizerAddon
                assignment = (
                    model_cls.objects.select_for_update()
                    .filter(pk=assignment_id)
                    .first()
                )
                if assignment:
                    if assignment.status == AddonStatus.ACTIVE:
                        # Idempotent: already activated
                        _record_checkout_invoice_for_addon(
                            organizer=organizer,
                            assignment=assignment,
                            addon=addon,
                            quantity=quantity,
                            session_data=session_data,
                            event=event,
                            user=user,
                        )
                        return assignment
                    assignment.status = AddonStatus.ACTIVE
                    assignment.starts_at = current_time
                    assignment.ends_at = ends_at
                    assignment.stripe_subscription_id = stripe_sub_id
                    assignment.stripe_payment_intent_id = stripe_pi_id
                    assignment.save()
                    log_addon_lifecycle_action(
                        assignment,
                        "purchased_via_stripe",
                        user=user,
                        data=metadata,
                    )
                    invalidate_entitlement_cache(organizer=organizer, event=event)
                    addon_purchased.send(sender=model_cls, instance=assignment)
                    _record_checkout_invoice_for_addon(
                        organizer=organizer,
                        assignment=assignment,
                        addon=addon,
                        quantity=quantity,
                        session_data=session_data,
                        event=event,
                        user=user,
                    )
                    return assignment

            # Idempotency check by stripe IDs
            if event:
                existing = EventAddon.objects.filter(
                    event=event,
                    addon=addon,
                    status=AddonStatus.ACTIVE,
                )
                if stripe_sub_id:
                    existing = existing.filter(stripe_subscription_id=stripe_sub_id)
                elif stripe_pi_id:
                    existing = existing.filter(stripe_payment_intent_id=stripe_pi_id)
                if existing.exists():
                    assignment = existing.first()
                    _record_checkout_invoice_for_addon(
                        organizer=organizer,
                        assignment=assignment,
                        addon=addon,
                        quantity=quantity,
                        session_data=session_data,
                        event=event,
                        user=user,
                    )
                    return assignment

                assignment = EventAddon.objects.create(
                    event=event,
                    addon=addon,
                    quantity=quantity,
                    capability=addon.capability,
                    entitlement_value=addon.entitlement_value,
                    price=addon.price,
                    currency=addon.currency,
                    starts_at=current_time,
                    ends_at=ends_at,
                    status=AddonStatus.ACTIVE,
                    stripe_subscription_id=stripe_sub_id,
                    stripe_payment_intent_id=stripe_pi_id,
                )
                log_addon_lifecycle_action(
                    assignment,
                    "purchased_via_stripe",
                    user=user,
                    data=metadata,
                )
                invalidate_entitlement_cache(organizer=organizer, event=event)
                addon_purchased.send(sender=EventAddon, instance=assignment)
                _record_checkout_invoice_for_addon(
                    organizer=organizer,
                    assignment=assignment,
                    addon=addon,
                    quantity=quantity,
                    session_data=session_data,
                    event=event,
                    user=user,
                )
                return assignment
            else:
                existing = OrganizerAddon.objects.filter(
                    organizer=organizer,
                    addon=addon,
                    status=AddonStatus.ACTIVE,
                )
                if stripe_sub_id:
                    existing = existing.filter(stripe_subscription_id=stripe_sub_id)
                elif stripe_pi_id:
                    existing = existing.filter(stripe_payment_intent_id=stripe_pi_id)
                if existing.exists():
                    assignment = existing.first()
                    _record_checkout_invoice_for_addon(
                        organizer=organizer,
                        assignment=assignment,
                        addon=addon,
                        quantity=quantity,
                        session_data=session_data,
                        event=event,
                        user=user,
                    )
                    return assignment

                assignment = OrganizerAddon.objects.create(
                    organizer=organizer,
                    addon=addon,
                    quantity=quantity,
                    capability=addon.capability,
                    entitlement_value=addon.entitlement_value,
                    price=addon.price,
                    currency=addon.currency,
                    starts_at=current_time,
                    ends_at=ends_at,
                    status=AddonStatus.ACTIVE,
                    stripe_subscription_id=stripe_sub_id,
                    stripe_payment_intent_id=stripe_pi_id,
                )
                log_addon_lifecycle_action(
                    assignment,
                    "purchased_via_stripe",
                    user=user,
                    data=metadata,
                )
                invalidate_entitlement_cache(organizer=organizer)
                addon_purchased.send(sender=OrganizerAddon, instance=assignment)
                _record_checkout_invoice_for_addon(
                    organizer=organizer,
                    assignment=assignment,
                    addon=addon,
                    quantity=quantity,
                    session_data=session_data,
                    event=event,
                    user=user,
                )
                return assignment


def process_subscription_checkout_completed(session_data: dict):
    from eventyay.base.models import Organizer, User

    metadata = session_data.get("metadata", {})
    organizer_slug = metadata.get("organizer_slug")
    tier_price_id = metadata.get("tier_price_id")
    tier_version_id = metadata.get("tier_version_id")
    user_id = metadata.get("user_id")

    stripe_customer_id = session_data.get("customer")
    stripe_sub_id = session_data.get("subscription")

    with scopes_disabled():
        try:
            organizer = Organizer.objects.get(slug=organizer_slug)
        except Organizer.DoesNotExist:
            logger.error(
                "Organizer %s not found for subscription checkout",
                organizer_slug,
            )
            return None

        tier_price = TierPrice.objects.filter(pk=tier_price_id).first()
        tier_version = (
            TierVersion.objects.filter(pk=tier_version_id).first()
            if tier_version_id
            else (tier_price.tier_version if tier_price else None)
        )
        if not tier_version:
            logger.error("TierVersion not found for checkout session")
            return None

        user = None
        if user_id:
            user = User.objects.filter(pk=user_id).first()

        current_time = now()
        interval_val = (
            getattr(tier_price, "billing_interval", None)
            or getattr(tier_price, "interval", None)
            or BillingInterval.MONTHLY
        )
        ends_at = current_time + (
            timedelta(days=365)
            if interval_val in ("year", "annual", BillingInterval.ANNUAL)
            else timedelta(days=30)
        )

        with transaction.atomic():
            # Check existing subscription
            sub = (
                Subscription.objects.select_for_update()
                .filter(
                    organizer=organizer,
                    status__in=[
                        SubscriptionStatus.ACTIVE,
                        SubscriptionStatus.PENDING,
                    ],
                )
                .first()
            )

            if sub:
                if (
                    sub.stripe_subscription_id == stripe_sub_id
                    and sub.status == SubscriptionStatus.ACTIVE
                ):
                    # Idempotent: subscription already active, ensure invoice is recorded
                    _record_checkout_invoice_for_subscription(
                        organizer=organizer,
                        subscription=sub,
                        tier_version=tier_version,
                        tier_price=tier_price,
                        session_data=session_data,
                        user=user,
                    )
                    return sub

                old_stripe_sub_id = sub.stripe_subscription_id
                if (
                    old_stripe_sub_id
                    and stripe_sub_id
                    and old_stripe_sub_id != stripe_sub_id
                ):
                    try:
                        import stripe

                        secret_key = get_stripe_secret_key_safe()
                        if secret_key:
                            stripe.api_key = secret_key
                            stripe.Subscription.cancel(old_stripe_sub_id)
                    except Exception as exc:
                        logger.warning(
                            "Failed to cancel old Stripe subscription %s: %s",
                            old_stripe_sub_id,
                            exc,
                        )

                sub.tier_version = tier_version
                sub.status = SubscriptionStatus.ACTIVE
                sub.billing_interval = interval_val
                sub.currency = tier_price.currency if tier_price else "USD"
                sub.starts_at = current_time
                sub.ends_at = ends_at
                sub.stripe_customer_id = stripe_customer_id or sub.stripe_customer_id
                sub.stripe_subscription_id = stripe_sub_id or sub.stripe_subscription_id
                sub.pending_tier_version = None
                sub.pending_billing_interval = None
                sub.pending_change_at = None
                sub.past_due_since = None
                sub.save()
            else:
                sub = Subscription.objects.create(
                    organizer=organizer,
                    tier_version=tier_version,
                    status=SubscriptionStatus.ACTIVE,
                    billing_interval=interval_val,
                    currency=tier_price.currency if tier_price else "USD",
                    starts_at=current_time,
                    ends_at=ends_at,
                    stripe_customer_id=stripe_customer_id,
                    stripe_subscription_id=stripe_sub_id,
                )

            invalidate_entitlement_cache(organizer=organizer)
            subscription_purchased.send(sender=Subscription, instance=sub, user=user)
            _record_checkout_invoice_for_subscription(
                organizer=organizer,
                subscription=sub,
                tier_version=tier_version,
                tier_price=tier_price,
                session_data=session_data,
                user=user,
            )
            return sub


def process_subscription_change(event_type: str, sub_data: dict):
    """
    Handle customer.subscription.updated and customer.subscription.deleted.
    """
    stripe_sub_id = sub_data.get("id")
    if not stripe_sub_id:
        return

    stripe_status = sub_data.get("status")  # active, past_due, canceled, unpaid
    period_end_ts = sub_data.get("current_period_end")
    if not period_end_ts:
        items_data = (sub_data.get("items") or {}).get("data", [])
        if items_data and isinstance(items_data, list):
            period_end_ts = items_data[0].get("current_period_end")
    period_end = (
        datetime.fromtimestamp(period_end_ts, tz=timezone.utc)
        if period_end_ts
        else None
    )

    with scopes_disabled():
        with transaction.atomic():
            # 1. Check subscriptions
            sub = (
                Subscription.objects.select_for_update()
                .filter(stripe_subscription_id=stripe_sub_id)
                .first()
            )
            if sub:
                is_downgrading = False
                if (
                    event_type == "customer.subscription.deleted"
                    or stripe_status == "canceled"
                ):
                    sub.status = SubscriptionStatus.CANCELED
                    sub.cancel_at = now()
                elif stripe_status in ("past_due", "unpaid"):
                    sub.status = SubscriptionStatus.PAST_DUE
                    if not sub.past_due_since:
                        sub.past_due_since = now()
                elif stripe_status == "active":
                    sub.status = SubscriptionStatus.ACTIVE
                    sub.past_due_since = None
                    if period_end:
                        sub.ends_at = period_end
                    if sub.pending_tier_version and (
                        not sub.pending_change_at or sub.pending_change_at <= now()
                    ):
                        sub.tier_version = sub.pending_tier_version
                        if sub.pending_billing_interval:
                            sub.billing_interval = sub.pending_billing_interval
                        sub.pending_tier_version = None
                        sub.pending_billing_interval = None
                        sub.pending_change_at = None
                        is_downgrading = True
                sub.save()
                if is_downgrading:
                    from .signals import subscription_downgraded

                    subscription_downgraded.send(sender=Subscription, instance=sub)
                invalidate_entitlement_cache(organizer=sub.organizer)

            # 2. Check OrganizerAddon
            org_addon = (
                OrganizerAddon.objects.select_for_update()
                .filter(stripe_subscription_id=stripe_sub_id)
                .first()
            )
            if org_addon:
                if (
                    event_type == "customer.subscription.deleted"
                    or stripe_status == "canceled"
                ):
                    org_addon.status = AddonStatus.CANCELED
                    org_addon.cancel_at = now()
                    org_addon.canceled_at = now()
                elif stripe_status == "active":
                    org_addon.status = AddonStatus.ACTIVE
                    if period_end:
                        org_addon.ends_at = period_end
                org_addon.save()
                invalidate_entitlement_cache(organizer=org_addon.organizer)

            # 3. Check EventAddon
            event_addon = (
                EventAddon.objects.select_for_update()
                .filter(stripe_subscription_id=stripe_sub_id)
                .first()
            )
            if event_addon:
                if (
                    event_type == "customer.subscription.deleted"
                    or stripe_status == "canceled"
                ):
                    event_addon.status = AddonStatus.CANCELED
                    event_addon.cancel_at = now()
                    event_addon.canceled_at = now()
                elif stripe_status == "active":
                    event_addon.status = AddonStatus.ACTIVE
                    if period_end:
                        event_addon.ends_at = period_end
                event_addon.save()
                invalidate_entitlement_cache(
                    organizer=event_addon.event.organizer,
                    event=event_addon.event,
                )


def process_invoice_payment_failed(invoice_data: dict):
    stripe_sub_id = invoice_data.get("subscription")
    if not stripe_sub_id:
        parent = invoice_data.get("parent") or {}
        if parent.get("type") == "subscription_details":
            stripe_sub_id = parent.get("subscription_details", {}).get("subscription")
    if not stripe_sub_id:
        return
    with scopes_disabled():
        with transaction.atomic():
            sub = (
                Subscription.objects.select_for_update()
                .filter(stripe_subscription_id=stripe_sub_id)
                .first()
            )
            if sub:
                sub.status = SubscriptionStatus.PAST_DUE
                if not sub.past_due_since:
                    sub.past_due_since = now()
                sub.save(update_fields=["status", "past_due_since", "updated_at"])
                invalidate_entitlement_cache(organizer=sub.organizer)

            # Also mark linked recurring add-ons past due
            org_addons = OrganizerAddon.objects.select_for_update().filter(
                stripe_subscription_id=stripe_sub_id,
                status=AddonStatus.ACTIVE,
            )
            for oa in org_addons:
                oa.status = AddonStatus.PAST_DUE
                oa.save(update_fields=["status", "updated_at"])
                invalidate_entitlement_cache(organizer=oa.organizer)

            event_addons = EventAddon.objects.select_for_update().filter(
                stripe_subscription_id=stripe_sub_id,
                status=AddonStatus.ACTIVE,
            )
            for ea in event_addons:
                ea.status = AddonStatus.PAST_DUE
                ea.save(update_fields=["status", "updated_at"])
                invalidate_entitlement_cache(
                    organizer=ea.event.organizer, event=ea.event
                )


def process_invoice_paid(invoice_data: dict):
    stripe_sub_id = invoice_data.get("subscription")
    if not stripe_sub_id:
        parent = invoice_data.get("parent") or {}
        if parent.get("type") == "subscription_details":
            stripe_sub_id = parent.get("subscription_details", {}).get("subscription")
    if not stripe_sub_id:
        return
    with scopes_disabled():
        with transaction.atomic():
            sub = (
                Subscription.objects.select_for_update()
                .filter(stripe_subscription_id=stripe_sub_id)
                .first()
            )
            if sub and sub.status == SubscriptionStatus.PAST_DUE:
                sub.status = SubscriptionStatus.ACTIVE
                sub.past_due_since = None
                sub.save(update_fields=["status", "past_due_since", "updated_at"])
                invalidate_entitlement_cache(organizer=sub.organizer)

            if sub:
                stripe_inv_id = invoice_data.get("id")
                if (
                    stripe_inv_id
                    and not BusinessInvoice.objects.filter(
                        stripe_invoice_id=stripe_inv_id
                    ).exists()
                ):
                    amount_paid = invoice_data.get("amount_paid")
                    if amount_paid is not None:
                        try:
                            total = (
                                Decimal(str(amount_paid)) / Decimal("100")
                            ).quantize(Decimal("0.01"))
                        except Exception:
                            total = Decimal("0.00")
                    else:
                        total = Decimal("0.00")

                    if total > Decimal("0.00"):
                        p_start_ts = invoice_data.get("period_start")
                        p_end_ts = invoice_data.get("period_end")
                        p_start = (
                            datetime.fromtimestamp(p_start_ts, tz=timezone.utc)
                            if p_start_ts
                            else (sub.starts_at or now())
                        )
                        p_end = (
                            datetime.fromtimestamp(p_end_ts, tz=timezone.utc)
                            if p_end_ts
                            else (sub.ends_at or (p_start + timedelta(days=30)))
                        )
                        inv_currency = (
                            invoice_data.get("currency") or sub.currency or "USD"
                        ).upper()

                        if not BusinessInvoice.objects.filter(
                            organizer=sub.organizer,
                            billing_period_start=p_start,
                            billing_period_end=p_end,
                        ).exists():
                            inv_num = generate_invoice_number(sub.organizer, p_start)
                            rec_inv = BusinessInvoice.objects.create(
                                organizer=sub.organizer,
                                invoice_number=inv_num,
                                billing_period_start=p_start,
                                billing_period_end=p_end,
                                currency=inv_currency,
                                status=BusinessInvoiceStatus.PAID,
                                subtotal=total,
                                tax=Decimal("0.00"),
                                total=total,
                                stripe_payment_intent_id=invoice_data.get(
                                    "payment_intent"
                                ),
                                stripe_invoice_id=stripe_inv_id,
                            )
                            tier_name = (
                                sub.tier_version.tier.name
                                if sub.tier_version
                                else "Subscription"
                            )
                            interval_disp = (
                                sub.get_billing_interval_display()
                                if hasattr(sub, "get_billing_interval_display")
                                else sub.billing_interval
                            )
                            BusinessInvoiceLine.objects.create(
                                invoice=rec_inv,
                                line_type=InvoiceLineType.SUBSCRIPTION,
                                description=f"Plan Subscription: {tier_name} ({interval_disp})",
                                quantity=Decimal("1.00"),
                                unit_price=total,
                                amount=total,
                                tier_version=sub.tier_version,
                                usage_reference=f"subscription_{sub.pk}",
                                calculation_metadata={
                                    "subscription_id": sub.pk,
                                    "stripe_subscription_id": stripe_sub_id,
                                    "stripe_invoice_id": stripe_inv_id,
                                    "billing_reason": invoice_data.get(
                                        "billing_reason", ""
                                    ),
                                },
                            )

            # Also restore linked recurring add-ons back to ACTIVE
            org_addons = OrganizerAddon.objects.select_for_update().filter(
                stripe_subscription_id=stripe_sub_id,
                status=AddonStatus.PAST_DUE,
            )
            for oa in org_addons:
                oa.status = AddonStatus.ACTIVE
                oa.save(update_fields=["status", "updated_at"])
                invalidate_entitlement_cache(organizer=oa.organizer)

            event_addons = EventAddon.objects.select_for_update().filter(
                stripe_subscription_id=stripe_sub_id,
                status=AddonStatus.PAST_DUE,
            )
            for ea in event_addons:
                ea.status = AddonStatus.ACTIVE
                ea.save(update_fields=["status", "updated_at"])
                invalidate_entitlement_cache(
                    organizer=ea.event.organizer, event=ea.event
                )


def fulfill_checkout_session_by_id(session_id: str):
    """
    Retrieve checkout session directly from Stripe and fulfill it synchronously.
    Handles subscription plan upgrades, add-on purchases, and invoice generation.
    Safe and idempotent.
    """
    secret_key = get_stripe_secret_key_safe()
    if not secret_key or stripe is None or not session_id:
        return None

    stripe.api_key = secret_key
    try:
        session = stripe.checkout.Session.retrieve(session_id)
        if not session:
            return None

        payment_status = getattr(session, "payment_status", None)
        if payment_status not in ("paid", "no_payment_required"):
            logger.info(
                "Checkout session %s payment_status is %s; skipping synchronous fulfillment",
                session_id,
                payment_status,
            )
            return None

        session_dict = (
            session.to_dict() if hasattr(session, "to_dict") else dict(session)
        )
        return process_checkout_session_completed(session_dict)
    except Exception as exc:
        logger.exception(
            "Error fulfilling Stripe checkout session %s: %s", session_id, exc
        )
        return None


def sync_organizer_from_stripe(organizer) -> Optional[Subscription]:
    """
    Query Stripe for any active subscriptions belonging to this organizer
    and synchronize Eventyay state. Acts as a self-healing fallback if
    webhooks were delayed or missed.
    """
    secret_key = get_stripe_secret_key_safe()
    if not secret_key or stripe is None or not organizer:
        return None

    stripe.api_key = secret_key

    # 1. Find stripe_customer_id
    customer_id = None
    sub = organizer.subscriptions.first()
    if sub and sub.stripe_customer_id:
        customer_id = sub.stripe_customer_id

    if not customer_id:
        try:
            from eventyay.base.models.organizer import OrganizerBillingModel

            billing = OrganizerBillingModel.objects.filter(
                organizer_id=organizer.id
            ).first()
            if billing and billing.stripe_customer_id:
                customer_id = billing.stripe_customer_id
        except Exception:
            pass

    if not customer_id:
        try:
            customers = stripe.Customer.search(
                query=f"metadata['organizer_slug']:'{organizer.slug}'"
            )
            if customers and getattr(customers, "data", None):
                customer_id = customers.data[0].id
        except Exception:
            pass

    if not customer_id:
        return None

    # 2. List active subscriptions in Stripe
    try:
        stripe_subs = stripe.Subscription.list(
            customer=customer_id, status="active", limit=5
        )
        for s_sub in getattr(stripe_subs, "data", []):
            s_dict = s_sub.to_dict() if hasattr(s_sub, "to_dict") else dict(s_sub)
            meta = s_dict.get("metadata", {})
            tier_version_id = meta.get("tier_version_id")
            tier_price_id = meta.get("tier_price_id")

            items_data = (s_dict.get("items") or {}).get("data", [])
            if not tier_version_id and items_data:
                first_item = items_data[0]
                price_obj = first_item.get("price") or {}
                price_meta = price_obj.get("metadata", {})
                tier_price_id = tier_price_id or price_meta.get("tier_price_id")

                prod_id = price_obj.get("product")
                if prod_id and not tier_version_id:
                    try:
                        prod = stripe.Product.retrieve(prod_id)
                        prod_meta = getattr(prod, "metadata", {})
                        tier_slug = prod_meta.get("tier_slug")
                        t_ver_str = prod_meta.get("tier_version")
                        if tier_slug and t_ver_str:
                            t_ver = TierVersion.objects.filter(
                                tier__slug=tier_slug, version=int(t_ver_str)
                            ).first()
                            if t_ver:
                                tier_version_id = str(t_ver.pk)
                    except Exception:
                        pass

            if tier_version_id or tier_price_id:
                tier_price = (
                    TierPrice.objects.filter(pk=tier_price_id).first()
                    if tier_price_id
                    else None
                )
                tier_version = (
                    TierVersion.objects.filter(pk=tier_version_id).first()
                    if tier_version_id
                    else (tier_price.tier_version if tier_price else None)
                )
                if tier_version:
                    session_mock = {
                        "metadata": {
                            "type": "subscription",
                            "organizer_slug": organizer.slug,
                            "tier_price_id": str(tier_price.pk) if tier_price else "",
                            "tier_version_id": str(tier_version.pk),
                        },
                        "customer": customer_id,
                        "subscription": s_sub.id,
                        "invoice": getattr(s_sub, "latest_invoice", None),
                    }
                    return process_subscription_checkout_completed(session_mock)
    except Exception as exc:
        logger.exception(
            "Error syncing subscriptions from Stripe for %s: %s",
            organizer.slug,
            exc,
        )

    return None
