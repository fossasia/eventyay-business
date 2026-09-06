from django.conf import settings
from django.db import models
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
                condition=models.Q(effective_until__isnull=True) | models.Q(effective_from__isnull=True) | models.Q(effective_until__gt=models.F("effective_from")),
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
        max_length=20, choices=BillingInterval.choices, verbose_name=_("Billing interval")
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
    value = models.IntegerField(null=True, blank=True, verbose_name=_("Value"))
    unit = models.CharField(max_length=50, blank=True, verbose_name=_("Unit"))
    overage_allowed = models.BooleanField(
        default=False, verbose_name=_("Overage allowed")
    )
    overage_price = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True, verbose_name=_("Overage price")
    )
    currency = models.CharField(
        max_length=3, blank=True, null=True, verbose_name=_("Overage currency")
    )
    overage_block_size = models.PositiveIntegerField(
        null=True, blank=True, verbose_name=_("Overage block size")
    )

    class Meta:
        ordering = ["tier_version", "capability"]
        verbose_name = _("Tier entitlement")
        verbose_name_plural = _("Tier entitlements")
        unique_together = (("tier_version", "capability"),)
        constraints = [
            models.CheckConstraint(
                name="tierentitlement_overage_price_nonnegative",
                condition=models.Q(overage_price__isnull=True) | models.Q(overage_price__gte=0),
            ),
            models.CheckConstraint(
                name="tierentitlement_overage_block_positive",
                condition=models.Q(overage_block_size__isnull=True) | models.Q(overage_block_size__gt=0),
            )
        ]
