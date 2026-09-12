from typing import Optional

from datetime import datetime, timedelta
from django.conf import settings
from django.db import models
from django.utils.timezone import now
from django.utils.translation import gettext_lazy as _


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
        on_delete=models.CASCADE,
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
