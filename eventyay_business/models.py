from typing import Optional

from datetime import datetime, timedelta
from decimal import Decimal
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils.timezone import now
from django.utils.translation import gettext_lazy as _
from django_countries.fields import CountryField


class TierStatus(models.TextChoices):
    DRAFT = "draft", _("Draft")
    PUBLISHED = "published", _("Published")
    ARCHIVED = "archived", _("Archived")


class Tier(models.Model):
    slug = models.SlugField(max_length=50, unique=True, verbose_name=_("Slug"))
    name = models.CharField(max_length=200, verbose_name=_("Name"))
    description = models.TextField(blank=True, verbose_name=_("Description"))
    status = models.CharField(
        max_length=20,
        choices=TierStatus.choices,
        default=TierStatus.DRAFT,
        verbose_name=_("Status"),
    )
    is_public = models.BooleanField(default=False, verbose_name=_("Is public"))
    display_order = models.PositiveIntegerField(
        default=0, verbose_name=_("Display order")
    )
    stripe_product_id = models.CharField(
        max_length=255, blank=True, null=True, verbose_name=_("Stripe product ID")
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("Created at"))
    updated_at = models.DateTimeField(auto_now=True, verbose_name=_("Updated at"))

    class Meta:
        ordering = ["display_order", "id"]
        verbose_name = _("Tier")
        verbose_name_plural = _("Tiers")

    def __str__(self):
        return self.name


class TierVersion(models.Model):
    tier = models.ForeignKey(
        Tier, on_delete=models.CASCADE, related_name="versions", verbose_name=_("Tier")
    )
    version = models.PositiveIntegerField(verbose_name=_("Version"))
    effective_from = models.DateTimeField(
        null=True, blank=True, verbose_name=_("Effective from")
    )
    effective_until = models.DateTimeField(
        null=True, blank=True, verbose_name=_("Effective until")
    )
    published_at = models.DateTimeField(
        null=True, blank=True, verbose_name=_("Published at")
    )
    configuration_snapshot = models.JSONField(
        default=dict, blank=True, verbose_name=_("Configuration snapshot")
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        verbose_name=_("Created by"),
    )

    class Meta:
        ordering = ["-version"]
        verbose_name = _("Tier version")
        verbose_name_plural = _("Tier versions")
        unique_together = (("tier", "version"),)
        constraints = [
            models.CheckConstraint(
                name="tierversion_effective_dates",
                condition=models.Q(effective_until__isnull=True)
                | models.Q(effective_from__isnull=True)
                | models.Q(effective_until__gt=models.F("effective_from")),
            )
        ]

    def __str__(self):
        return f"{self.tier.name} v{self.version}"


class BillingInterval(models.TextChoices):
    MONTHLY = "monthly", _("Monthly")
    ANNUAL = "annual", _("Annual")


class TierPrice(models.Model):
    tier_version = models.ForeignKey(
        TierVersion,
        on_delete=models.CASCADE,
        related_name="prices",
        verbose_name=_("Tier version"),
    )
    billing_interval = models.CharField(
        max_length=20,
        choices=BillingInterval.choices,
        verbose_name=_("Billing interval"),
    )
    currency = models.CharField(max_length=3, verbose_name=_("Currency"))
    amount = models.DecimalField(
        max_digits=10, decimal_places=2, verbose_name=_("Amount")
    )
    stripe_price_id = models.CharField(
        max_length=255, blank=True, null=True, verbose_name=_("Stripe price ID")
    )
    active = models.BooleanField(default=True, verbose_name=_("Active"))

    class Meta:
        ordering = ["tier_version", "billing_interval", "currency"]
        verbose_name = _("Tier price")
        verbose_name_plural = _("Tier prices")
        unique_together = (("tier_version", "billing_interval", "currency"),)
        constraints = [
            models.CheckConstraint(
                name="tierprice_amount_nonnegative",
                condition=models.Q(amount__gte=0),
            )
        ]


class TierEntitlement(models.Model):
    tier_version = models.ForeignKey(
        TierVersion,
        on_delete=models.CASCADE,
        related_name="entitlements",
        verbose_name=_("Tier version"),
    )
    capability = models.CharField(max_length=100, verbose_name=_("Capability"))
    value = models.CharField(
        max_length=100, null=True, blank=True, verbose_name=_("Value")
    )
    unit = models.CharField(max_length=50, blank=True, verbose_name=_("Unit"))
    overage_allowed = models.BooleanField(
        default=False, verbose_name=_("Overage allowed")
    )
    overage_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name=_("Overage price"),
    )
    currency = models.CharField(
        max_length=3, blank=True, null=True, verbose_name=_("Overage currency")
    )
    overage_block_size = models.PositiveIntegerField(
        null=True, blank=True, verbose_name=_("Overage block size")
    )

    def get_typed_value(self):
        """
        Returns the typed Python representation of value based on capability metadata.
        """
        if self.value is None or self.value == "":
            return None
        from .capabilities import CapabilityValueType, get_capability

        cap = get_capability(self.capability)
        if not cap:
            return self.value
        if cap.value_type == CapabilityValueType.BOOLEAN:
            return str(self.value).lower() in ("true", "1", "yes")
        if cap.value_type == CapabilityValueType.INTEGER:
            return int(self.value)
        if cap.value_type in (CapabilityValueType.DECIMAL, CapabilityValueType.MONEY):
            from decimal import Decimal

            return Decimal(self.value)
        return self.value

    @property
    def capability_def(self):
        from .capabilities import get_capability

        return get_capability(self.capability)

    @property
    def capability_label(self):
        cap = self.capability_def
        if cap and cap.label:
            return cap.label
        return self.capability.replace("_", " ").replace(".", " → ").title()

    @property
    def capability_description(self):
        cap = self.capability_def
        return cap.description if cap else ""

    @property
    def capability_category(self):
        cap = self.capability_def
        return cap.category if cap else "General"

    @property
    def capability_audience(self):
        cap = self.capability_def
        return cap.metadata.get("audience", "organizer") if cap else "organizer"

    @property
    def is_boolean(self):
        from .capabilities import CapabilityValueType

        cap = self.capability_def
        if cap and cap.value_type == CapabilityValueType.BOOLEAN:
            return True
        return str(self.value).lower() in ("true", "false")

    @property
    def is_enabled(self):
        if self.is_boolean:
            return str(self.value).lower() in ("true", "1", "yes")
        return bool(self.value)

    @property
    def formatted_display_value(self):
        from decimal import Decimal
        from django.utils.translation import gettext as _

        cap = self.capability_def
        if self.is_boolean:
            return _("Included") if self.is_enabled else _("Not included")

        if not self.value and self.value != 0 and self.value != "0":
            return _("Included")

        val_str = str(self.value).strip()
        unit = (self.unit or (cap.unit if cap else "")).strip()

        if cap and cap.name == "commerce.platform_fee_percent":
            try:
                if Decimal(val_str) == Decimal("0.0"):
                    return _("0% (No platform fee)")
            except Exception:
                pass
            return f"{val_str}%"

        if cap and cap.name == "registration.free_overage_price":
            try:
                if Decimal(val_str) == Decimal("0.0"):
                    return _("Free (No overage fee)")
            except Exception:
                pass
            currency = self.currency or "EUR"
            return f"{val_str} {currency} / registration"

        if cap and cap.name == "registration.free_allowance_per_event":
            return f"{val_str} registrations / event"

        if cap and cap.name == "organizer.full_admins":
            return f"{val_str} admin seats"

        if cap and cap.name == "email.bulk.monthly":
            return f"{val_str} emails / month"

        if cap and cap.name == "video.jitsi.concurrent_rooms":
            return f"{val_str} rooms"

        if unit:
            return f"{val_str} {unit}"
        return val_str

    class Meta:
        ordering = ["tier_version", "capability"]
        verbose_name = _("Tier entitlement")
        verbose_name_plural = _("Tier entitlements")
        unique_together = (("tier_version", "capability"),)
        constraints = [
            models.CheckConstraint(
                name="tierentitlement_overage_price_nonnegative",
                condition=models.Q(overage_price__isnull=True)
                | models.Q(overage_price__gte=0),
            ),
            models.CheckConstraint(
                name="tierentitlement_overage_block_positive",
                condition=models.Q(overage_block_size__isnull=True)
                | models.Q(overage_block_size__gt=0),
            ),
        ]


class SubscriptionStatus(models.TextChoices):
    PENDING = "pending", _("Pending")
    ACTIVE = "active", _("Active")
    PAST_DUE = "past_due", _("Past due")
    CANCELED = "canceled", _("Canceled")
    EXPIRED = "expired", _("Expired")


class Subscription(models.Model):
    organizer = models.ForeignKey(
        "base.Organizer",
        on_delete=models.CASCADE,
        related_name="subscriptions",
        verbose_name=_("Organizer"),
    )
    tier_version = models.ForeignKey(
        TierVersion,
        on_delete=models.RESTRICT,
        related_name="subscriptions",
        verbose_name=_("Tier version"),
    )
    status = models.CharField(
        max_length=20,
        choices=SubscriptionStatus.choices,
        default=SubscriptionStatus.PENDING,
        verbose_name=_("Status"),
    )
    billing_interval = models.CharField(
        max_length=20,
        choices=BillingInterval.choices,
        null=True,
        blank=True,
        verbose_name=_("Billing interval"),
    )
    currency = models.CharField(
        max_length=3, null=True, blank=True, verbose_name=_("Currency")
    )
    starts_at = models.DateTimeField(verbose_name=_("Starts at"))
    ends_at = models.DateTimeField(null=True, blank=True, verbose_name=_("Ends at"))
    cancel_at = models.DateTimeField(null=True, blank=True, verbose_name=_("Cancel at"))
    stripe_customer_id = models.CharField(
        max_length=255, blank=True, null=True, verbose_name=_("Stripe customer ID")
    )
    stripe_subscription_id = models.CharField(
        max_length=255, blank=True, null=True, verbose_name=_("Stripe subscription ID")
    )
    configuration_snapshot = models.JSONField(
        default=dict, blank=True, verbose_name=_("Configuration snapshot")
    )
    pending_tier_version = models.ForeignKey(
        TierVersion,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="pending_subscriptions",
        verbose_name=_("Pending tier version"),
    )
    pending_billing_interval = models.CharField(
        max_length=20,
        choices=BillingInterval.choices,
        null=True,
        blank=True,
        verbose_name=_("Pending billing interval"),
    )
    pending_change_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("Pending change at"),
    )
    past_due_since = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("Past due since"),
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("Created at"))
    updated_at = models.DateTimeField(auto_now=True, verbose_name=_("Updated at"))

    class Meta:
        ordering = ["-starts_at", "-id"]
        verbose_name = _("Subscription")
        verbose_name_plural = _("Subscriptions")
        constraints = [
            models.UniqueConstraint(
                fields=["organizer"],
                condition=models.Q(status__in=["active", "pending"]),
                name="unique_active_subscription_per_organizer",
            )
        ]

    def save(self, *args, **kwargs):
        if self.status == SubscriptionStatus.PAST_DUE and not self.past_due_since:
            self.past_due_since = now()
            if "update_fields" in kwargs and kwargs["update_fields"] is not None:
                kwargs["update_fields"] = set(kwargs["update_fields"]) | {
                    "past_due_since"
                }
        elif self.status == SubscriptionStatus.ACTIVE and self.past_due_since:
            self.past_due_since = None
            if "update_fields" in kwargs and kwargs["update_fields"] is not None:
                kwargs["update_fields"] = set(kwargs["update_fields"]) | {
                    "past_due_since"
                }
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Subscription for {self.organizer} ({self.status})"

    @property
    def has_scheduled_downgrade(self) -> bool:
        return (
            self.pending_tier_version is not None
            and self.status == SubscriptionStatus.ACTIVE
        )

    @property
    def grace_period_days(self) -> int:
        from .services import get_grace_period_days

        return get_grace_period_days(self)

    def is_in_grace_period(self, grace_days: Optional[int] = None) -> bool:
        if self.status != SubscriptionStatus.PAST_DUE:
            return False
        if not self.past_due_since:
            return False
        if grace_days is None:
            grace_days = self.grace_period_days
        return now() <= self.past_due_since + timedelta(days=grace_days)

    def grace_period_ends_at(
        self, grace_days: Optional[int] = None
    ) -> Optional[datetime]:
        if self.status != SubscriptionStatus.PAST_DUE or not self.past_due_since:
            return None
        if grace_days is None:
            grace_days = self.grace_period_days
        return self.past_due_since + timedelta(days=grace_days)


class UsageRecord(models.Model):
    organizer = models.ForeignKey(
        "base.Organizer",
        on_delete=models.CASCADE,
        related_name="usage_records",
        verbose_name=_("Organizer"),
    )
    event = models.ForeignKey(
        "base.Event",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="usage_records",
        verbose_name=_("Event"),
    )
    capability = models.CharField(max_length=100, verbose_name=_("Capability"))
    quantity = models.DecimalField(
        max_digits=10, decimal_places=2, default=1, verbose_name=_("Quantity")
    )
    unit = models.CharField(max_length=50, blank=True, verbose_name=_("Unit"))
    occurred_at = models.DateTimeField(default=now, verbose_name=_("Occurred at"))
    source_type = models.CharField(max_length=100, verbose_name=_("Source type"))
    source_id = models.CharField(max_length=255, verbose_name=_("Source ID"))
    idempotency_key = models.CharField(
        max_length=255, verbose_name=_("Idempotency key")
    )
    metadata = models.JSONField(default=dict, blank=True, verbose_name=_("Metadata"))
    invoice = models.ForeignKey(
        "BusinessInvoice",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="usage_records",
        verbose_name=_("Business invoice"),
    )

    class Meta:
        ordering = ["-occurred_at", "-id"]
        verbose_name = _("Usage record")
        verbose_name_plural = _("Usage records")
        constraints = [
            models.UniqueConstraint(
                fields=["organizer", "idempotency_key"],
                name="unique_usage_idempotency_per_organizer",
            )
        ]

    def __str__(self):
        return f"{self.quantity} {self.unit} of {self.capability} by {self.organizer}"


class AddonAssignmentScope(models.TextChoices):
    ORGANIZER = "organizer", _("Organizer")
    EVENT = "event", _("Event")


class AddonPricingMode(models.TextChoices):
    ONE_TIME = "one_time", _("One-time")
    RECURRING = "recurring", _("Recurring")


class AddonStatus(models.TextChoices):
    PENDING = "pending", _("Pending")
    ACTIVE = "active", _("Active")
    PAST_DUE = "past_due", _("Past Due")
    EXPIRED = "expired", _("Expired")
    CANCELED = "canceled", _("Canceled")


class AddonDefinition(models.Model):
    slug = models.SlugField(max_length=50, unique=True, verbose_name=_("Slug"))
    name = models.CharField(max_length=200, verbose_name=_("Name"))
    description = models.TextField(blank=True, verbose_name=_("Description"))
    assignment_scope = models.CharField(
        max_length=20,
        choices=AddonAssignmentScope.choices,
        default=AddonAssignmentScope.ORGANIZER,
        verbose_name=_("Assignment scope"),
    )
    pricing_mode = models.CharField(
        max_length=20,
        choices=AddonPricingMode.choices,
        default=AddonPricingMode.RECURRING,
        verbose_name=_("Pricing mode"),
    )
    currency = models.CharField(max_length=3, default="USD", verbose_name=_("Currency"))
    price = models.DecimalField(
        max_digits=10, decimal_places=2, default=0.00, verbose_name=_("Price")
    )
    capability = models.CharField(max_length=100, verbose_name=_("Capability"))
    entitlement_value = models.CharField(
        max_length=100, blank=True, default="true", verbose_name=_("Entitlement value")
    )
    quantity = models.PositiveIntegerField(
        default=1, verbose_name=_("Included quantity / allowance")
    )
    active = models.BooleanField(default=True, verbose_name=_("Active"))
    public = models.BooleanField(default=True, verbose_name=_("Public"))
    stripe_product_id = models.CharField(
        max_length=255, blank=True, null=True, verbose_name=_("Stripe product ID")
    )
    stripe_price_id = models.CharField(
        max_length=255, blank=True, null=True, verbose_name=_("Stripe price ID")
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("Created at"))
    updated_at = models.DateTimeField(auto_now=True, verbose_name=_("Updated at"))

    class Meta:
        ordering = ["name", "slug"]
        verbose_name = _("Add-on definition")
        verbose_name_plural = _("Add-on definitions")
        constraints = [
            models.CheckConstraint(
                name="addondefinition_price_nonnegative",
                condition=models.Q(price__gte=0),
            ),
            models.CheckConstraint(
                name="addondefinition_quantity_positive",
                condition=models.Q(quantity__gt=0),
            ),
        ]

    def __str__(self):
        return self.name

    def get_typed_value(self):
        if self.entitlement_value is None or self.entitlement_value == "":
            return None
        from .capabilities import CapabilityValueType, get_capability

        cap = get_capability(self.capability)
        if not cap:
            return self.entitlement_value
        if cap.value_type == CapabilityValueType.BOOLEAN:
            return str(self.entitlement_value).lower() in ("true", "1", "yes")
        if cap.value_type == CapabilityValueType.INTEGER:
            try:
                return int(self.entitlement_value)
            except (ValueError, TypeError):
                return 1
        if cap.value_type in (CapabilityValueType.DECIMAL, CapabilityValueType.MONEY):
            from decimal import Decimal

            try:
                return Decimal(self.entitlement_value)
            except Exception:
                return Decimal("0")
        return self.entitlement_value


class OrganizerAddon(models.Model):
    organizer = models.ForeignKey(
        "base.Organizer",
        on_delete=models.CASCADE,
        related_name="business_addons",
        verbose_name=_("Organizer"),
    )
    addon = models.ForeignKey(
        AddonDefinition,
        on_delete=models.PROTECT,
        related_name="organizer_assignments",
        verbose_name=_("Add-on"),
    )
    quantity = models.PositiveIntegerField(default=1, verbose_name=_("Quantity"))
    capability = models.CharField(
        max_length=100, blank=True, verbose_name=_("Capability snapshot")
    )
    entitlement_value = models.CharField(
        max_length=100, blank=True, verbose_name=_("Entitlement value snapshot")
    )
    price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name=_("Price snapshot"),
    )
    currency = models.CharField(
        max_length=3, blank=True, verbose_name=_("Currency snapshot")
    )
    starts_at = models.DateTimeField(default=now, verbose_name=_("Starts at"))
    ends_at = models.DateTimeField(null=True, blank=True, verbose_name=_("Ends at"))
    cancel_at = models.DateTimeField(null=True, blank=True, verbose_name=_("Cancel at"))
    canceled_at = models.DateTimeField(
        null=True, blank=True, verbose_name=_("Canceled at")
    )
    status = models.CharField(
        max_length=20,
        choices=AddonStatus.choices,
        default=AddonStatus.ACTIVE,
        verbose_name=_("Status"),
    )
    stripe_subscription_id = models.CharField(
        max_length=255, blank=True, null=True, verbose_name=_("Stripe subscription ID")
    )
    stripe_payment_intent_id = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        verbose_name=_("Stripe payment intent ID"),
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("Created at"))
    updated_at = models.DateTimeField(auto_now=True, verbose_name=_("Updated at"))

    class Meta:
        ordering = ["-starts_at", "-id"]
        verbose_name = _("Organizer add-on")
        verbose_name_plural = _("Organizer add-ons")

    def __str__(self):
        return f"{self.addon.name} for {self.organizer} ({self.status})"

    def save(self, *args, **kwargs):
        if self.addon_id:
            if not self.capability:
                self.capability = self.addon.capability
            if not self.entitlement_value:
                self.entitlement_value = self.addon.entitlement_value
            if self.price is None:
                self.price = self.addon.price
            if not self.currency:
                self.currency = self.addon.currency
        super().save(*args, **kwargs)

    def cancel(self, immediate: bool = False, cancel_at=None):
        current = now()
        if immediate or not (self.ends_at or cancel_at):
            self.status = AddonStatus.CANCELED
            self.cancel_at = current
            self.canceled_at = current
        else:
            effective_cancel = cancel_at or self.ends_at
            if effective_cancel <= current:
                self.status = AddonStatus.CANCELED
                self.canceled_at = current
            else:
                self.canceled_at = None
            self.cancel_at = effective_cancel
        self.save(update_fields=["status", "cancel_at", "canceled_at", "updated_at"])

    @property
    def is_active(self):
        current = now()
        if self.status != AddonStatus.ACTIVE:
            return False
        if self.starts_at and self.starts_at > current:
            return False
        if self.ends_at and self.ends_at < current:
            return False
        if self.cancel_at and self.cancel_at <= current:
            return False
        return True

    def get_typed_value(self):
        val = (
            self.entitlement_value
            if self.entitlement_value != ""
            else self.addon.entitlement_value
        )
        if val is None or val == "":
            return None
        from .capabilities import CapabilityValueType, get_capability

        cap_name = self.capability or self.addon.capability
        cap = get_capability(cap_name)
        if not cap:
            return val
        if cap.value_type == CapabilityValueType.BOOLEAN:
            return str(val).lower() in ("true", "1", "yes")
        if cap.value_type == CapabilityValueType.INTEGER:
            try:
                return int(val)
            except (ValueError, TypeError):
                return 1
        if cap.value_type in (CapabilityValueType.DECIMAL, CapabilityValueType.MONEY):
            from decimal import Decimal

            try:
                return Decimal(val)
            except Exception:
                return Decimal("0")
        return val


class EventAddon(models.Model):
    event = models.ForeignKey(
        "base.Event",
        on_delete=models.CASCADE,
        related_name="business_addons",
        verbose_name=_("Event"),
    )
    addon = models.ForeignKey(
        AddonDefinition,
        on_delete=models.PROTECT,
        related_name="event_assignments",
        verbose_name=_("Add-on"),
    )
    quantity = models.PositiveIntegerField(default=1, verbose_name=_("Quantity"))
    capability = models.CharField(
        max_length=100, blank=True, verbose_name=_("Capability snapshot")
    )
    entitlement_value = models.CharField(
        max_length=100, blank=True, verbose_name=_("Entitlement value snapshot")
    )
    price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name=_("Price snapshot"),
    )
    currency = models.CharField(
        max_length=3, blank=True, verbose_name=_("Currency snapshot")
    )
    starts_at = models.DateTimeField(default=now, verbose_name=_("Starts at"))
    ends_at = models.DateTimeField(null=True, blank=True, verbose_name=_("Ends at"))
    cancel_at = models.DateTimeField(null=True, blank=True, verbose_name=_("Cancel at"))
    canceled_at = models.DateTimeField(
        null=True, blank=True, verbose_name=_("Canceled at")
    )
    status = models.CharField(
        max_length=20,
        choices=AddonStatus.choices,
        default=AddonStatus.ACTIVE,
        verbose_name=_("Status"),
    )
    stripe_subscription_id = models.CharField(
        max_length=255, blank=True, null=True, verbose_name=_("Stripe subscription ID")
    )
    stripe_payment_intent_id = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        verbose_name=_("Stripe payment intent ID"),
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("Created at"))
    updated_at = models.DateTimeField(auto_now=True, verbose_name=_("Updated at"))

    class Meta:
        ordering = ["-starts_at", "-id"]
        verbose_name = _("Event add-on")
        verbose_name_plural = _("Event add-ons")

    def __str__(self):
        return f"{self.addon.name} for {self.event} ({self.status})"

    def save(self, *args, **kwargs):
        if self.addon_id:
            if not self.capability:
                self.capability = self.addon.capability
            if not self.entitlement_value:
                self.entitlement_value = self.addon.entitlement_value
            if self.price is None:
                self.price = self.addon.price
            if not self.currency:
                self.currency = self.addon.currency
        super().save(*args, **kwargs)

    def cancel(self, immediate: bool = False, cancel_at=None):
        current = now()
        if immediate or not (self.ends_at or cancel_at):
            self.status = AddonStatus.CANCELED
            self.cancel_at = current
            self.canceled_at = current
        else:
            effective_cancel = cancel_at or self.ends_at
            if effective_cancel <= current:
                self.status = AddonStatus.CANCELED
                self.canceled_at = current
            else:
                self.canceled_at = None
            self.cancel_at = effective_cancel
        self.save(update_fields=["status", "cancel_at", "canceled_at", "updated_at"])

    @property
    def is_active(self):
        current = now()
        if self.status != AddonStatus.ACTIVE:
            return False
        if self.starts_at and self.starts_at > current:
            return False
        if self.ends_at and self.ends_at < current:
            return False
        if self.cancel_at and self.cancel_at <= current:
            return False
        return True

    def get_typed_value(self):
        val = (
            self.entitlement_value
            if self.entitlement_value != ""
            else self.addon.entitlement_value
        )
        if val is None or val == "":
            return None
        from .capabilities import CapabilityValueType, get_capability

        cap_name = self.capability or self.addon.capability
        cap = get_capability(cap_name)
        if not cap:
            return val
        if cap.value_type == CapabilityValueType.BOOLEAN:
            return str(val).lower() in ("true", "1", "yes")
        if cap.value_type == CapabilityValueType.INTEGER:
            try:
                return int(val)
            except (ValueError, TypeError):
                return 1
        if cap.value_type in (CapabilityValueType.DECIMAL, CapabilityValueType.MONEY):
            from decimal import Decimal

            try:
                return Decimal(val)
            except Exception:
                return Decimal("0")
        return val


class BusinessInvoiceStatus(models.TextChoices):
    DRAFT = "draft", _("Draft")
    OPEN = "open", _("Open")
    PAID = "paid", _("Paid")
    CANCELED = "canceled", _("Canceled")
    UNCOLLECTIBLE = "uncollectible", _("Uncollectible")


class InvoicePaymentState(models.TextChoices):
    NOT_STARTED = "not_started", _("Not started")
    PROCESSING = "processing", _("Processing")
    REQUIRES_ACTION = "requires_action", _("Action required")
    FAILED = "failed", _("Failed")
    SUCCEEDED = "succeeded", _("Succeeded")


class BillingNotificationEventType(models.TextChoices):
    INVOICE_ISSUED = "invoice_issued", _("Invoice issued")
    COLLECTION_PROBLEM = "collection_problem", _("Collection problem")
    COLLECTION_SUCCEEDED = "collection_succeeded", _("Collection succeeded")
    PAYMENT_METHOD_MISSING = "payment_method_missing", _("Payment method missing")
    AUTHORIZATION_MISSING = "authorization_missing", _("Authorization missing")
    PAYMENT_DECLINED = "payment_declined", _("Payment declined")
    AUTHENTICATION_REQUIRED = "authentication_required", _("Authentication required")
    RETRY_SCHEDULED = "retry_scheduled", _("Payment retry scheduled")
    GRACE_PERIOD_STARTED = "grace_period_started", _("Grace period started")
    GRACE_PERIOD_REMINDER = "grace_period_reminder", _("Grace period reminder")
    ACCESS_EXPIRED = "access_expired", _("Access expired for nonpayment")
    PAYMENT_METHOD_UPDATED = "payment_method_updated", _("Payment method updated")
    ACCESS_RESTORED = "access_restored", _("Access restored")
    SUPPORT_REQUIRED = "support_required", _("Support intervention required")
    PLATFORM_ALERT = "platform_alert", _("Platform billing alert")
    PLAN_ACTIVATED = "plan_activated", _("Plan activated")
    PLAN_CHANGE_SCHEDULED = "plan_change_scheduled", _("Plan change scheduled")
    PLAN_CHANGE_CANCELED = "plan_change_canceled", _("Plan change canceled")
    PLAN_CHANGED = "plan_changed", _("Plan changed")
    PLAN_CANCELLATION_SCHEDULED = "plan_cancellation_scheduled", _(
        "Plan cancellation scheduled"
    )
    PLAN_CANCELLATION_CANCELED = "plan_cancellation_canceled", _(
        "Plan cancellation canceled"
    )
    PLAN_CANCELED = "plan_canceled", _("Plan canceled")
    ADDON_ACTIVATED = "addon_activated", _("Add-on activated")
    ADDON_CANCELLATION_SCHEDULED = "addon_cancellation_scheduled", _(
        "Add-on cancellation scheduled"
    )
    ADDON_CANCELED = "addon_canceled", _("Add-on canceled")
    ADDON_EXPIRED = "addon_expired", _("Add-on expired")
    RENEWAL_APPROACHING = "renewal_approaching", _("Renewal approaching")
    CREDIT_ISSUED = "credit_issued", _("Credit or adjustment issued")
    ADMIN_TEST = "admin_test", _("Administrator test")


class BillingNotificationStatus(models.TextChoices):
    PENDING = "pending", _("Pending")
    SENDING = "sending", _("Sending")
    SENT = "sent", _("Sent")
    FAILED = "failed", _("Failed")


class BillingNotificationRecipientSource(models.TextChoices):
    INVOICE_SNAPSHOT = "invoice_snapshot", _("Invoice billing snapshot")
    BILLING_CONTACT = "billing_contact", _("Organizer billing contact")
    ORGANIZER_ADMIN = "organizer_admin", _("Organizer administrator")
    PLATFORM_ADMIN = "platform_admin", _("Platform administrator")
    OVERRIDE = "override", _("Explicit test recipient")


class BillingReconciliationRunStatus(models.TextChoices):
    RUNNING = "running", _("Running")
    COMPLETED = "completed", _("Completed")
    FAILED = "failed", _("Failed")


class BillingReconciliationResultStatus(models.TextChoices):
    READY = "ready", _("Ready for review")
    BLOCKED = "blocked", _("Blocked")
    ERROR = "error", _("Calculation error")


class InvoiceLineType(models.TextChoices):
    SUBSCRIPTION = "subscription", _("Subscription plan")
    ADDON = "addon", _("Add-on package")
    PLATFORM_FEE = "platform_fee", _("Platform ticket transaction fee")
    REGISTRATION_OVERAGE = (
        "registration_overage",
        _("Free ticket registration overage"),
    )
    MANUAL_ADJUSTMENT = "manual_adjustment", _("Manual adjustment")
    CREDIT = "credit", _("Credit")
    TAX = "tax", _("Tax")
    OTHER = "other", _("Other")


class PlatformFeeLedgerEntryType(models.TextChoices):
    CHARGE = "charge", _("Charge")
    REFUND = "refund", _("Refund")
    CANCELLATION = "cancellation", _("Cancellation")
    CORRECTION = "correction", _("Correction")


class BusinessInvoice(models.Model):
    IMMUTABLE_AFTER_FINALIZATION = (
        "organizer_id",
        "invoice_number",
        "billing_period_start",
        "billing_period_end",
        "currency",
        "subtotal",
        "tax",
        "total",
        "issue_date",
        "due_date",
        "finalized_at",
        "billing_address_snapshot",
        "presentation_snapshot",
        "corrects_invoice_id",
    )
    organizer = models.ForeignKey(
        "base.Organizer",
        on_delete=models.CASCADE,
        related_name="business_invoices",
        verbose_name=_("Organizer"),
    )
    invoice_number = models.CharField(
        max_length=64, unique=True, verbose_name=_("Invoice number")
    )
    billing_period_start = models.DateTimeField(verbose_name=_("Billing period start"))
    billing_period_end = models.DateTimeField(verbose_name=_("Billing period end"))
    currency = models.CharField(max_length=3, default="EUR", verbose_name=_("Currency"))
    status = models.CharField(
        max_length=20,
        choices=BusinessInvoiceStatus.choices,
        default=BusinessInvoiceStatus.OPEN,
        verbose_name=_("Status"),
    )
    payment_state = models.CharField(
        max_length=20,
        choices=InvoicePaymentState.choices,
        default=InvoicePaymentState.NOT_STARTED,
        verbose_name=_("Payment state"),
    )
    issue_date = models.DateField(null=True, blank=True, verbose_name=_("Issue date"))
    due_date = models.DateField(null=True, blank=True, verbose_name=_("Due date"))
    finalized_at = models.DateTimeField(
        null=True, blank=True, verbose_name=_("Finalized at")
    )
    finalized_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="finalized_business_invoices",
        verbose_name=_("Finalized by"),
    )
    paid_at = models.DateTimeField(null=True, blank=True, verbose_name=_("Paid at"))
    billing_address_snapshot = models.JSONField(
        default=dict, blank=True, verbose_name=_("Billing address snapshot")
    )
    presentation_snapshot = models.JSONField(
        default=dict, blank=True, verbose_name=_("Invoice presentation snapshot")
    )
    collection_attempt_count = models.PositiveIntegerField(
        default=0, verbose_name=_("Collection attempt count")
    )
    last_collection_attempt_at = models.DateTimeField(
        null=True, blank=True, verbose_name=_("Last collection attempt at")
    )
    collection_metadata = models.JSONField(
        default=dict, blank=True, verbose_name=_("Collection metadata")
    )
    corrects_invoice = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="correction_invoices",
        verbose_name=_("Corrects invoice"),
    )
    subtotal = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        verbose_name=_("Subtotal"),
    )
    tax = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        verbose_name=_("Tax"),
    )
    total = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        verbose_name=_("Total"),
    )
    stripe_payment_intent_id = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        verbose_name=_("Stripe payment intent ID"),
    )
    stripe_invoice_id = models.CharField(
        max_length=255, blank=True, null=True, verbose_name=_("Stripe invoice ID")
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("Created at"))
    updated_at = models.DateTimeField(auto_now=True, verbose_name=_("Updated at"))

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = _("Business invoice")
        verbose_name_plural = _("Business invoices")
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "organizer",
                    "billing_period_start",
                    "billing_period_end",
                    "currency",
                ],
                name="unique_business_invoice_period_currency",
            )
        ]

    def __str__(self):
        return (
            f"{self.invoice_number} - {self.organizer} ({self.total} {self.currency})"
        )

    def save(self, *args, **kwargs):
        if self.pk:
            previous = type(self).objects.filter(pk=self.pk).first()
            if previous and previous.finalized_at:
                changed = [
                    field
                    for field in self.IMMUTABLE_AFTER_FINALIZATION
                    if getattr(previous, field) != getattr(self, field)
                ]
                if changed:
                    raise ValidationError(
                        _(
                            "Finalized invoice financial and customer data cannot be changed."
                        )
                    )
        return super().save(*args, **kwargs)


class BillingNotificationDelivery(models.Model):
    logical_event_key = models.CharField(
        max_length=255, verbose_name=_("Logical event key")
    )
    event_type = models.CharField(
        max_length=64,
        choices=BillingNotificationEventType.choices,
        verbose_name=_("Event type"),
    )
    organizer = models.ForeignKey(
        "base.Organizer",
        on_delete=models.CASCADE,
        related_name="business_notification_deliveries",
        verbose_name=_("Organizer"),
    )
    related_object_type = models.CharField(
        max_length=64, blank=True, verbose_name=_("Related object type")
    )
    related_object_id = models.CharField(
        max_length=64, blank=True, verbose_name=_("Related object ID")
    )
    recipient = models.EmailField(verbose_name=_("Recipient"))
    recipient_source = models.CharField(
        max_length=32,
        choices=BillingNotificationRecipientSource.choices,
        verbose_name=_("Recipient source"),
    )
    locale = models.CharField(max_length=16, default="en", verbose_name=_("Locale"))
    template_name = models.CharField(max_length=255, verbose_name=_("Template name"))
    subject = models.CharField(max_length=255, blank=True, verbose_name=_("Subject"))
    context = models.JSONField(default=dict, blank=True, verbose_name=_("Context"))
    status = models.CharField(
        max_length=20,
        choices=BillingNotificationStatus.choices,
        default=BillingNotificationStatus.PENDING,
        verbose_name=_("Status"),
    )
    attempt_count = models.PositiveIntegerField(
        default=0, verbose_name=_("Attempt count")
    )
    last_attempt_at = models.DateTimeField(
        null=True, blank=True, verbose_name=_("Last attempt at")
    )
    sent_at = models.DateTimeField(null=True, blank=True, verbose_name=_("Sent at"))
    last_error = models.JSONField(
        default=dict, blank=True, verbose_name=_("Last error")
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("Created at"))
    updated_at = models.DateTimeField(auto_now=True, verbose_name=_("Updated at"))

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = _("Billing notification delivery")
        verbose_name_plural = _("Billing notification deliveries")
        constraints = [
            models.UniqueConstraint(
                fields=["logical_event_key", "recipient"],
                name="unique_billing_notification_event_recipient",
            )
        ]
        indexes = [
            models.Index(
                fields=["status", "created_at"],
                name="billing_notify_status_created",
            ),
            models.Index(
                fields=["organizer", "event_type", "created_at"],
                name="billing_notify_org_event",
            ),
        ]

    def __str__(self):
        return f"{self.get_event_type_display()} to {self.recipient}"


class BillingReconciliationRun(models.Model):
    period_start = models.DateTimeField(verbose_name=_("Billing period start"))
    period_end = models.DateTimeField(verbose_name=_("Billing period end"))
    status = models.CharField(
        max_length=20,
        choices=BillingReconciliationRunStatus.choices,
        default=BillingReconciliationRunStatus.RUNNING,
        verbose_name=_("Status"),
    )
    summary = models.JSONField(default=dict, blank=True, verbose_name=_("Summary"))
    started_at = models.DateTimeField(default=now, verbose_name=_("Started at"))
    completed_at = models.DateTimeField(
        null=True, blank=True, verbose_name=_("Completed at")
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("Created at"))
    updated_at = models.DateTimeField(auto_now=True, verbose_name=_("Updated at"))

    class Meta:
        ordering = ["-period_start", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["period_start", "period_end"],
                name="unique_billing_reconciliation_period",
            )
        ]

    def __str__(self):
        return f"Billing reconciliation {self.period_start:%Y-%m-%d}"


class BillingReconciliationResult(models.Model):
    run = models.ForeignKey(
        BillingReconciliationRun,
        on_delete=models.CASCADE,
        related_name="results",
        verbose_name=_("Reconciliation run"),
    )
    organizer = models.ForeignKey(
        "base.Organizer",
        on_delete=models.CASCADE,
        related_name="billing_reconciliation_results",
        verbose_name=_("Organizer"),
    )
    currency = models.CharField(max_length=3, verbose_name=_("Currency"))
    status = models.CharField(
        max_length=20,
        choices=BillingReconciliationResultStatus.choices,
        default=BillingReconciliationResultStatus.BLOCKED,
        verbose_name=_("Status"),
    )
    expected_total = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        verbose_name=_("Expected total"),
    )
    line_count = models.PositiveIntegerField(default=0, verbose_name=_("Line count"))
    blocking_finding_count = models.PositiveIntegerField(
        default=0, verbose_name=_("Blocking finding count")
    )
    findings = models.JSONField(default=list, blank=True, verbose_name=_("Findings"))
    snapshot = models.JSONField(default=dict, blank=True, verbose_name=_("Snapshot"))
    fingerprint = models.CharField(max_length=64, verbose_name=_("Fingerprint"))
    accepted_fingerprint = models.CharField(
        max_length=64, blank=True, verbose_name=_("Accepted fingerprint")
    )
    accepted_at = models.DateTimeField(
        null=True, blank=True, verbose_name=_("Accepted at")
    )
    accepted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="accepted_billing_reconciliations",
        verbose_name=_("Accepted by"),
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("Created at"))
    updated_at = models.DateTimeField(auto_now=True, verbose_name=_("Updated at"))

    class Meta:
        ordering = ["organizer__name", "currency"]
        constraints = [
            models.UniqueConstraint(
                fields=["run", "organizer", "currency"],
                name="unique_billing_reconciliation_result",
            )
        ]
        indexes = [
            models.Index(
                fields=["status", "updated_at"],
                name="billing_recon_status_updated",
            )
        ]

    def __str__(self):
        return f"{self.organizer} {self.currency} reconciliation"

    @property
    def is_currently_accepted(self) -> bool:
        return bool(
            self.accepted_at
            and self.accepted_fingerprint
            and self.accepted_fingerprint == self.fingerprint
            and self.status == BillingReconciliationResultStatus.READY
            and self.blocking_finding_count == 0
        )


class BusinessInvoiceLine(models.Model):
    invoice = models.ForeignKey(
        BusinessInvoice,
        on_delete=models.CASCADE,
        related_name="lines",
        verbose_name=_("Invoice"),
    )
    event = models.ForeignKey(
        "base.Event",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="business_invoice_lines",
        verbose_name=_("Event"),
    )
    line_type = models.CharField(
        max_length=30, choices=InvoiceLineType.choices, verbose_name=_("Line type")
    )
    description = models.CharField(max_length=255, verbose_name=_("Description"))
    quantity = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("1.00"),
        verbose_name=_("Quantity"),
    )
    unit_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        verbose_name=_("Unit price"),
    )
    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        verbose_name=_("Amount"),
    )
    service_period_start = models.DateTimeField(
        null=True, blank=True, verbose_name=_("Service period start")
    )
    service_period_end = models.DateTimeField(
        null=True, blank=True, verbose_name=_("Service period end")
    )
    tier_version = models.ForeignKey(
        TierVersion,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name=_("Tier version"),
    )
    addon = models.ForeignKey(
        AddonDefinition,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name=_("Add-on"),
    )
    usage_reference = models.CharField(
        max_length=255, blank=True, null=True, verbose_name=_("Usage reference")
    )
    calculation_metadata = models.JSONField(
        default=dict, blank=True, verbose_name=_("Calculation metadata")
    )

    class Meta:
        ordering = ["id"]
        verbose_name = _("Business invoice line")
        verbose_name_plural = _("Business invoice lines")

    def __str__(self):
        return f"{self.description}: {self.amount}"

    def save(self, *args, **kwargs):
        if self.invoice.finalized_at:
            raise ValidationError(_("Finalized invoice lines cannot be changed."))
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.invoice.finalized_at:
            raise ValidationError(_("Finalized invoice lines cannot be deleted."))
        return super().delete(*args, **kwargs)


class BillingCollectionAuthorization(models.Model):
    organizer = models.OneToOneField(
        "base.Organizer",
        on_delete=models.CASCADE,
        related_name="business_billing_authorization",
        verbose_name=_("Organizer"),
    )
    stripe_customer_id = models.CharField(
        max_length=255, verbose_name=_("Authorized Stripe customer ID")
    )
    authorized_at = models.DateTimeField(verbose_name=_("Authorized at"))
    authorized_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="business_billing_authorizations",
        verbose_name=_("Authorized by"),
    )
    revoked_at = models.DateTimeField(
        null=True, blank=True, verbose_name=_("Revoked at")
    )
    terms_version = models.CharField(
        max_length=20, default="1", verbose_name=_("Terms version")
    )
    updated_at = models.DateTimeField(auto_now=True, verbose_name=_("Updated at"))

    class Meta:
        verbose_name = _("Billing collection authorization")
        verbose_name_plural = _("Billing collection authorizations")

    @property
    def is_active(self):
        return self.revoked_at is None


class PlatformFeeLedgerEntry(models.Model):
    IMMUTABLE_FIELDS = (
        "organizer_id",
        "event_id",
        "order_id",
        "payment_id",
        "refund_id",
        "related_entry_id",
        "entry_type",
        "source_key",
        "occurred_at",
        "billing_period",
        "currency",
        "fee_base",
        "percentage",
        "maximum_fee",
        "calculated_fee",
        "collected_at_checkout",
        "amount",
        "policy_source",
        "is_override",
        "metadata",
    )

    organizer = models.ForeignKey(
        "base.Organizer",
        on_delete=models.PROTECT,
        related_name="platform_fee_ledger_entries",
        verbose_name=_("Organizer"),
    )
    event = models.ForeignKey(
        "base.Event",
        on_delete=models.PROTECT,
        related_name="platform_fee_ledger_entries",
        verbose_name=_("Event"),
    )
    order = models.ForeignKey(
        "base.Order",
        on_delete=models.PROTECT,
        related_name="platform_fee_ledger_entries",
        verbose_name=_("Order"),
    )
    payment = models.ForeignKey(
        "base.OrderPayment",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="platform_fee_ledger_entries",
        verbose_name=_("Payment"),
    )
    refund = models.ForeignKey(
        "base.OrderRefund",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="platform_fee_ledger_entries",
        verbose_name=_("Refund"),
    )
    related_entry = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="adjustments",
        verbose_name=_("Related ledger entry"),
    )
    invoice = models.ForeignKey(
        BusinessInvoice,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="platform_fee_ledger_entries",
        verbose_name=_("Business invoice"),
    )
    entry_type = models.CharField(
        max_length=20,
        choices=PlatformFeeLedgerEntryType.choices,
        verbose_name=_("Entry type"),
    )
    source_key = models.CharField(max_length=255, verbose_name=_("Source key"))
    occurred_at = models.DateTimeField(verbose_name=_("Occurred at"))
    billing_period = models.DateField(verbose_name=_("Billing period"))
    currency = models.CharField(max_length=3, verbose_name=_("Currency"))
    fee_base = models.DecimalField(
        max_digits=12, decimal_places=2, verbose_name=_("Eligible fee base")
    )
    percentage = models.DecimalField(
        max_digits=7, decimal_places=4, verbose_name=_("Fee percentage")
    )
    maximum_fee = models.DecimalField(
        max_digits=12, decimal_places=2, verbose_name=_("Order fee maximum")
    )
    calculated_fee = models.DecimalField(
        max_digits=12, decimal_places=2, verbose_name=_("Calculated fee")
    )
    collected_at_checkout = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        verbose_name=_("Collected at checkout"),
    )
    amount = models.DecimalField(
        max_digits=12, decimal_places=2, verbose_name=_("Accrued amount")
    )
    policy_source = models.CharField(max_length=100, verbose_name=_("Policy source"))
    is_override = models.BooleanField(default=False, verbose_name=_("Is override"))
    metadata = models.JSONField(default=dict, blank=True, verbose_name=_("Metadata"))
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("Created at"))

    class Meta:
        ordering = ["occurred_at", "id"]
        verbose_name = _("Platform fee ledger entry")
        verbose_name_plural = _("Platform fee ledger entries")
        constraints = [
            models.UniqueConstraint(
                fields=["organizer", "source_key"],
                name="unique_platform_fee_source_per_organizer",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        entry_type=PlatformFeeLedgerEntryType.CHARGE,
                        amount__gte=0,
                        payment__isnull=False,
                        refund__isnull=True,
                        related_entry__isnull=True,
                    )
                    | models.Q(
                        entry_type=PlatformFeeLedgerEntryType.REFUND,
                        amount__lt=0,
                        refund__isnull=False,
                        related_entry__isnull=False,
                    )
                    | models.Q(
                        entry_type=PlatformFeeLedgerEntryType.CANCELLATION,
                        amount__lt=0,
                        refund__isnull=True,
                        related_entry__isnull=False,
                    )
                    | models.Q(entry_type=PlatformFeeLedgerEntryType.CORRECTION)
                ),
                name="platform_fee_ledger_entry_shape",
            ),
        ]

    def __str__(self):
        return f"{self.entry_type}: {self.amount} {self.currency} ({self.source_key})"

    def save(self, *args, **kwargs):
        if self.pk and not self._state.adding:
            previous = type(self).objects.only(*self.IMMUTABLE_FIELDS).get(pk=self.pk)
            changed_fields = [
                field
                for field in self.IMMUTABLE_FIELDS
                if getattr(previous, field) != getattr(self, field)
            ]
            if changed_fields:
                raise ValidationError(
                    _("Platform fee ledger financial fields cannot be changed.")
                )
        return super().save(*args, **kwargs)


class CountryFeeSetting(models.Model):
    country = CountryField(verbose_name=_("Country"))
    currency = models.CharField(max_length=3, verbose_name=_("Currency"))
    service_fee_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[
            MinValueValidator(Decimal("0.00")),
            MaxValueValidator(Decimal("100.00")),
        ],
        verbose_name=_("Service fee percentage"),
        help_text=_("Percentage fee applied to tickets for this country and currency."),
    )
    maximum_fee = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name=_("Maximum fee"),
        help_text=_(
            "Maximum fee limit in the specified currency. 0 indicates no maximum fee limit (unlimited)."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("Created at"))
    updated_at = models.DateTimeField(auto_now=True, verbose_name=_("Updated at"))

    class Meta:
        ordering = ["country", "currency"]
        verbose_name = _("Country fee setting")
        verbose_name_plural = _("Country fee settings")
        unique_together = (("country", "currency"),)

    def clean(self):
        super().clean()
        if self.service_fee_percent is not None:
            if self.service_fee_percent < Decimal(
                "0.00"
            ) or self.service_fee_percent > Decimal("100.00"):
                raise ValidationError(
                    {
                        "service_fee_percent": _(
                            "Service fee percentage must be between 0 and 100."
                        )
                    }
                )
        if self.maximum_fee is not None and self.maximum_fee < Decimal("0.00"):
            raise ValidationError({"maximum_fee": _("Maximum fee cannot be negative.")})

    def save(self, *args, **kwargs):
        if self.currency:
            self.currency = self.currency.strip().upper()
        if self.country:
            self.country = str(self.country).strip().upper()
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.country} ({self.currency}): {self.service_fee_percent}% (max: {self.maximum_fee})"


class VoucherDiscountDuration(models.TextChoices):
    ONCE = "once", _("First billing period only")
    REPEATING = "repeating", _("A set number of billing periods")
    FOREVER = "forever", _("Every billing period while the voucher is valid")


class VoucherBusinessTargets(models.Model):
    """The plans and add-ons a platform fee voucher discounts, and for how long."""

    voucher = models.OneToOneField(
        "base.InvoiceVoucher",
        on_delete=models.CASCADE,
        related_name="business_targets",
        verbose_name=_("Voucher"),
    )
    tiers = models.ManyToManyField(
        Tier,
        blank=True,
        related_name="voucher_targets",
        verbose_name=_("Plans"),
    )
    addons = models.ManyToManyField(
        AddonDefinition,
        blank=True,
        related_name="voucher_targets",
        verbose_name=_("Add-ons"),
    )
    duration = models.CharField(
        max_length=20,
        choices=VoucherDiscountDuration.choices,
        default=VoucherDiscountDuration.ONCE,
        verbose_name=_("Discount duration"),
    )
    duration_periods = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        verbose_name=_("Number of billing periods"),
    )

    class Meta:
        verbose_name = _("Voucher plan and add-on targets")
        verbose_name_plural = _("Voucher plan and add-on targets")

    def __str__(self):
        return f"{self.voucher}"

    @property
    def period_limit(self) -> int | None:
        if self.duration == VoucherDiscountDuration.ONCE:
            return 1
        if self.duration == VoucherDiscountDuration.REPEATING:
            return self.duration_periods or 1
        return None


class VoucherRedemptionStatus(models.TextChoices):
    PENDING = "pending", _("Reserved for checkout")
    REDEEMED = "redeemed", _("Redeemed")
    RELEASED = "released", _("Released")


class VoucherRedemption(models.Model):
    """A voucher redeemed by an organizer for one plan or add-on purchase.

    Stripe Checkout purchases reserve the voucher with a pending redemption while the
    organiser pays, so concurrent checkouts cannot use it more often than allowed.
    """

    voucher = models.ForeignKey(
        "base.InvoiceVoucher",
        on_delete=models.PROTECT,
        related_name="business_redemptions",
        verbose_name=_("Voucher"),
    )
    organizer = models.ForeignKey(
        "base.Organizer",
        on_delete=models.CASCADE,
        related_name="voucher_redemptions",
        verbose_name=_("Organizer"),
    )
    subscription = models.ForeignKey(
        Subscription,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="voucher_redemptions",
        verbose_name=_("Subscription"),
    )
    tier = models.ForeignKey(
        Tier,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="voucher_redemptions",
        verbose_name=_("Plan"),
    )
    organizer_addon = models.ForeignKey(
        OrganizerAddon,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="voucher_redemptions",
        verbose_name=_("Organizer add-on"),
    )
    event_addon = models.ForeignKey(
        EventAddon,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="voucher_redemptions",
        verbose_name=_("Event add-on"),
    )
    duration = models.CharField(
        max_length=20,
        choices=VoucherDiscountDuration.choices,
        verbose_name=_("Discount duration"),
    )
    period_limit = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        verbose_name=_("Billing periods discounted"),
        help_text=_("Empty when every billing period is discounted."),
    )
    periods_applied = models.PositiveIntegerField(
        default=0, verbose_name=_("Billing periods already discounted")
    )
    stripe_checkout_session_id = models.CharField(
        max_length=255,
        blank=True,
        default="",
        verbose_name=_("Stripe checkout session"),
    )
    status = models.CharField(
        max_length=20,
        choices=VoucherRedemptionStatus.choices,
        default=VoucherRedemptionStatus.REDEEMED,
        db_index=True,
        verbose_name=_("Status"),
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("Created at"))

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = _("Voucher redemption")
        verbose_name_plural = _("Voucher redemptions")
        constraints = [
            models.UniqueConstraint(
                fields=["stripe_checkout_session_id"],
                condition=~models.Q(stripe_checkout_session_id=""),
                name="unique_voucher_redemption_checkout_session",
            ),
        ]

    def __str__(self):
        return f"{self.voucher} ({self.organizer})"

    @property
    def has_periods_left(self) -> bool:
        return self.period_limit is None or self.periods_applied < self.period_limit
