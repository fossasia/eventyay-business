import csv
import logging
import stripe
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Prefetch, Q, Sum
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.timezone import now
from django.utils.translation import gettext_lazy as _
from django.views.generic import (
    CreateView,
    DeleteView,
    DetailView,
    FormView,
    ListView,
    RedirectView,
    TemplateView,
    UpdateView,
    View,
)
from django_scopes import scope
from eventyay.base.i18n import language
from eventyay.base.models import Event, Organizer, User
from eventyay.base.models.organizer import OrganizerBillingModel
from eventyay.control.permissions import (
    AdministratorPermissionRequiredMixin,
    EventPermissionRequiredMixin,
    OrganizerPermissionRequiredMixin,
)
from eventyay.control.views.organizer_views.organizer_detail_view_mixin import (
    OrganizerDetailViewMixin,
)
from types import SimpleNamespace
from uuid import uuid4

from .billing_migration import (
    consolidated_billing_preflight,
    enable_consolidated_billing,
)
from .billing_overview import (
    build_organizer_billing_overview,
    parse_month,
    platform_fee_rows,
)
from .capabilities import CapabilityValueType, get_all_capabilities, get_capability
from .forms import (
    AddonDefinitionForm,
    BillingNotificationTemplateForm,
    BillingNotificationTestForm,
    CountryFeeSettingForm,
    EventAddonForm,
    EventAddonPurchaseForm,
    GlobalBusinessSettingsForm,
    InvoicePresentationSettingsForm,
    OrganizerAddonForm,
    OrganizerAddonPurchaseForm,
    SubscriptionAdminForm,
    TierEntitlementFormSet,
    TierForm,
    TierPriceFormSet,
    TierVersionForm,
)
from .invoice_document import render_invoice_pdf
from .invoice_presentation import current_invoice_presentation_snapshot
from .models import (
    AddonAssignmentScope,
    AddonDefinition,
    AddonStatus,
    BillingCollectionAuthorization,
    BillingInterval,
    BillingNotificationDelivery,
    BillingNotificationEventType,
    BillingNotificationRecipientSource,
    BillingNotificationStatus,
    BillingReconciliationResult,
    BillingReconciliationResultStatus,
    BillingReconciliationRun,
    BusinessInvoice,
    BusinessInvoiceStatus,
    CountryFeeSetting,
    EventAddon,
    InvoiceLineType,
    OrganizerAddon,
    PlatformFeeLedgerEntry,
    Subscription,
    SubscriptionStatus,
    Tier,
    TierPrice,
    TierStatus,
    TierVersion,
    UsageRecord,
)
from .notifications import (
    NOTIFICATION_PLACEHOLDERS,
    NOTIFICATION_SPECS,
    BillingNotificationRecipient,
    notification_template_sources,
    preview_notification_context,
    queue_billing_notification,
    render_notification_content,
    reset_notification_template,
    retry_billing_notification,
    save_notification_template,
)
from .platform_fee_ledger import (
    organizer_billing_cutover_at,
    organizer_consolidated_billing_enabled,
)
from .reconciliation import (
    accept_reconciliation_result,
    run_billing_reconciliation,
)
from .services import (
    apply_due_subscription_change,
    invalidate_entitlement_cache,
    log_addon_lifecycle_action,
    migrate_addon_assignments,
    migrate_tier_subscribers,
)
from .signals import (
    AUTOMATIC_COLLECTION_TERMS_VERSION,
    addon_canceled,
    addon_cancellation_scheduled,
    organizer_billing_payment_method_saved,
    subscription_change_canceled,
    subscription_change_scheduled,
    subscription_downgraded,
    subscription_purchased,
)
from .stripe_service import (
    create_addon_checkout_session,
    create_subscription_checkout_session,
    get_stripe_secret_key_safe,
    is_stripe_configured,
    sync_organizer_from_stripe,
    sync_tier_price_to_stripe,
)

logger = logging.getLogger(__name__)


class TierListView(AdministratorPermissionRequiredMixin, ListView):
    model = Tier
    template_name = "eventyay_business/tiers/list.html"
    context_object_name = "tiers"


class TierCreateView(AdministratorPermissionRequiredMixin, CreateView):
    model = Tier
    form_class = TierForm
    template_name = "eventyay_business/tiers/form.html"

    @transaction.atomic
    def form_valid(self, form):
        self.object = form.save()
        # Create initial draft TierVersion with standard capabilities seeded
        version = TierVersion.objects.create(
            tier=self.object, version=1, created_by=self.request.user
        )
        from .services import seed_standard_entitlements_for_version

        seed_standard_entitlements_for_version(version)
        messages.success(
            self.request,
            _("Tier created successfully with standard capabilities."),
        )
        return redirect(
            reverse(
                "plugins:eventyay_business:tiers.edit", kwargs={"pk": self.object.pk}
            )
        )


class TierUpdateView(AdministratorPermissionRequiredMixin, UpdateView):
    model = Tier
    form_class = TierForm
    template_name = "eventyay_business/tiers/form.html"

    def dispatch(self, request, *args, **kwargs):
        self.object = self.get_object()
        self.latest_version = self.object.versions.first()

        if not self.latest_version:
            self.latest_version = TierVersion.objects.create(
                tier=self.object, version=1, created_by=request.user
            )

        # If the latest version is published, redirect to detail view or duplicate prompt
        if self.latest_version and self.latest_version.published_at:
            messages.info(
                request,
                _(
                    "This tier is published. To edit prices or entitlements, please create a new draft version."
                ),
            )
            return redirect(
                reverse(
                    "plugins:eventyay_business:tiers.detail",
                    kwargs={"pk": self.object.pk},
                )
            )

        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        self.latest_version = self.object.versions.first()
        if "populate_standard" in request.POST:
            with transaction.atomic():
                locked_version = (
                    TierVersion.objects.select_for_update()
                    .filter(tier=self.object)
                    .order_by("-version")
                    .first()
                )
                if not locked_version or locked_version.published_at:
                    messages.info(
                        request,
                        _(
                            "This tier is published. To edit prices or entitlements, please create a new draft version."
                        ),
                    )
                    return redirect(
                        reverse(
                            "plugins:eventyay_business:tiers.detail",
                            kwargs={"pk": self.object.pk},
                        )
                    )

                from .services import seed_standard_entitlements_for_version

                seed_standard_entitlements_for_version(locked_version)
                messages.success(
                    request,
                    _(
                        "Standard capabilities have been populated for this tier version."
                    ),
                )
                return redirect(
                    reverse(
                        "plugins:eventyay_business:tiers.edit",
                        kwargs={"pk": self.object.pk},
                    )
                )
        return super().post(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if self.request.POST:
            context["version_form"] = TierVersionForm(
                self.request.POST, instance=self.latest_version
            )
            context["price_formset"] = TierPriceFormSet(
                self.request.POST, instance=self.latest_version
            )
            context["entitlement_formset"] = TierEntitlementFormSet(
                self.request.POST, instance=self.latest_version
            )
        else:
            context["version_form"] = TierVersionForm(instance=self.latest_version)
            context["price_formset"] = TierPriceFormSet(instance=self.latest_version)
            context["entitlement_formset"] = TierEntitlementFormSet(
                instance=self.latest_version
            )

        from .capabilities import get_capabilities_dict

        context["capabilities_data"] = get_capabilities_dict()
        context["has_entitlements"] = (
            self.latest_version.entitlements.exists() if self.latest_version else False
        )
        return context

    @transaction.atomic
    def form_valid(self, form):
        context = self.get_context_data()
        version_form = context["version_form"]
        price_formset = context["price_formset"]
        entitlement_formset = context["entitlement_formset"]

        if (
            form.is_valid()
            and version_form.is_valid()
            and price_formset.is_valid()
            and entitlement_formset.is_valid()
        ):
            latest_version = (
                TierVersion.objects.select_for_update()
                .filter(pk=self.latest_version.pk)
                .first()
            )
            if latest_version and latest_version.published_at:
                messages.error(
                    self.request,
                    _("Cannot save edits: this version was published concurrently."),
                )
                return redirect(
                    reverse(
                        "plugins:eventyay_business:tiers.detail",
                        kwargs={"pk": self.object.pk},
                    )
                )

            self.object = form.save()
            version_form.save()
            price_formset.save()
            entitlement_formset.save()
            messages.success(self.request, _("Tier draft saved successfully."))
            return redirect(reverse("plugins:eventyay_business:tiers.list"))
        else:
            return self.render_to_response(self.get_context_data(form=form))


class TierDetailView(AdministratorPermissionRequiredMixin, DetailView):
    model = Tier
    template_name = "eventyay_business/tiers/detail.html"
    context_object_name = "tier"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        latest_version = self.object.versions.first()
        context["latest_version"] = latest_version
        if latest_version and not latest_version.published_at:
            prev_versions = self.object.versions.filter(published_at__isnull=False)
            has_prev = prev_versions.exists()
            context["has_previous_published_version"] = has_prev
            if has_prev:
                context["previous_subscribers_count"] = Subscription.objects.filter(
                    tier_version__in=prev_versions,
                    status__in=[SubscriptionStatus.ACTIVE, SubscriptionStatus.PENDING],
                ).count()
        return context


class TierVersionDetailView(AdministratorPermissionRequiredMixin, DetailView):
    """Show all details for a specific historical TierVersion."""

    model = TierVersion
    template_name = "eventyay_business/tiers/version_detail.html"
    context_object_name = "version"
    pk_url_kwarg = "version_pk"

    def get_object(self, queryset=None):
        tier = get_object_or_404(Tier, pk=self.kwargs["pk"])
        return get_object_or_404(TierVersion, pk=self.kwargs["version_pk"], tier=tier)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["tier"] = self.object.tier
        context["active_subscriber_count"] = Subscription.objects.filter(
            tier_version=self.object,
            status__in=[SubscriptionStatus.ACTIVE, SubscriptionStatus.PENDING],
        ).count()
        return context


class TierNewDraftView(AdministratorPermissionRequiredMixin, View):
    """Creates a new DRAFT TierVersion from the latest PUBLISHED version."""

    @transaction.atomic
    def post(self, request, *args, **kwargs):
        tier = get_object_or_404(Tier.objects.select_for_update(), pk=kwargs.get("pk"))
        latest_version = tier.versions.first()

        if not latest_version or not latest_version.published_at:
            messages.error(
                request,
                _("Cannot create a new draft: there is already an unpublished draft."),
            )
            return redirect(
                reverse("plugins:eventyay_business:tiers.edit", kwargs={"pk": tier.pk})
            )

        # Duplicate version
        new_version = TierVersion.objects.create(
            tier=tier,
            version=latest_version.version + 1,
            created_by=request.user,
            configuration_snapshot=dict(latest_version.configuration_snapshot or {}),
        )

        # Duplicate prices
        for price in latest_version.prices.all():
            price.pk = None
            price.tier_version = new_version
            price.save()

        # Duplicate entitlements
        for ent in latest_version.entitlements.all():
            ent.pk = None
            ent.tier_version = new_version
            ent.save()

        messages.success(
            request, _("New draft version created. You can now make changes.")
        )
        return redirect(
            reverse("plugins:eventyay_business:tiers.edit", kwargs={"pk": tier.pk})
        )


class TierPublishView(AdministratorPermissionRequiredMixin, View):
    @transaction.atomic
    def post(self, request, *args, **kwargs):
        tier = get_object_or_404(Tier, pk=kwargs.get("pk"))
        latest_version = tier.versions.first()

        if tier.status == TierStatus.ARCHIVED:
            messages.error(
                request,
                _("Cannot publish a draft for an archived tier. Unarchive it first."),
            )
            return redirect(reverse("plugins:eventyay_business:tiers.list"))

        if latest_version and not latest_version.published_at:
            latest_version.published_at = now()
            latest_version.save()

            if tier.status == TierStatus.DRAFT:
                tier.status = TierStatus.PUBLISHED
                tier.save()

            migrate_subs = request.POST.get("migrate_subscribers") in (
                "1",
                "true",
                "on",
            )
            if migrate_subs:
                count = migrate_tier_subscribers(tier, latest_version)
                messages.success(
                    request,
                    _(
                        "Tier published successfully. Migrated %(count)d subscriber(s) to v%(version)d."
                    )
                    % {"count": count, "version": latest_version.version},
                )
            else:
                messages.success(request, _("Tier published successfully."))
        else:
            messages.error(request, _("This tier has no unpublished draft."))

        return redirect(reverse("plugins:eventyay_business:tiers.list"))


class TierArchiveView(AdministratorPermissionRequiredMixin, View):
    @transaction.atomic
    def post(self, request, *args, **kwargs):
        tier = get_object_or_404(Tier, pk=kwargs.get("pk"))
        tier.status = TierStatus.ARCHIVED
        tier.save()
        messages.success(request, _("Tier archived successfully."))
        return redirect(reverse("plugins:eventyay_business:tiers.list"))


class SubscriptionListView(AdministratorPermissionRequiredMixin, ListView):
    model = Subscription
    template_name = "eventyay_business/subscriptions/list.html"
    context_object_name = "subscriptions"


class SubscriptionCreateView(AdministratorPermissionRequiredMixin, CreateView):
    model = Subscription
    form_class = SubscriptionAdminForm
    template_name = "eventyay_business/subscriptions/form.html"

    def get_success_url(self):
        messages.success(self.request, _("Subscription created successfully."))
        return reverse("plugins:eventyay_business:subscriptions.list")


class SubscriptionUpdateView(AdministratorPermissionRequiredMixin, UpdateView):
    model = Subscription
    form_class = SubscriptionAdminForm
    template_name = "eventyay_business/subscriptions/form.html"

    def get_success_url(self):
        messages.success(self.request, _("Subscription updated successfully."))
        return reverse("plugins:eventyay_business:subscriptions.list")


class OrganizerPlanView(
    OrganizerPermissionRequiredMixin, OrganizerDetailViewMixin, TemplateView
):
    permission = "can_change_organizer_settings"
    template_name = "eventyay_business/organizer/plan.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        organizer = self.request.organizer

        current_time = now()
        due_change_ids = Subscription.objects.filter(
            organizer=organizer,
            pending_tier_version__isnull=False,
            pending_change_at__lte=current_time,
        ).values_list("pk", flat=True)
        for subscription_id in due_change_ids:
            apply_due_subscription_change(subscription_id, reference=current_time)

        sub = (
            Subscription.objects.filter(
                organizer=organizer,
                status__in=[SubscriptionStatus.ACTIVE, SubscriptionStatus.PAST_DUE],
                starts_at__lte=current_time,
            )
            .filter(
                Q(ends_at__isnull=True)
                | Q(ends_at__gte=current_time)
                | Q(status=SubscriptionStatus.PAST_DUE)
            )
            .select_related("tier_version__tier", "pending_tier_version__tier")
            .first()
        )

        is_free_or_missing = (
            not sub
            or not sub.tier_version
            or getattr(sub.tier_version.tier, "slug", "") == "free"
        )
        if is_stripe_configured() and (
            is_free_or_missing or self.request.GET.get("sync")
        ):
            try:
                synced_sub = sync_organizer_from_stripe(organizer)
                if synced_sub:
                    sub = synced_sub
            except Exception as exc:
                logger.warning(
                    "Failed to sync organizer %s from Stripe: %s",
                    organizer.slug,
                    exc,
                )

        ctx["subscription"] = sub

        # Build effective entitlement values from the active subscription override
        override_dict = {}
        if sub and sub.tier_version:
            for ent in sub.tier_version.entitlements.all():
                override_dict[ent.capability] = ent.get_typed_value()

        organizer_entitlements = []
        developer_entitlements = []

        for cap in get_all_capabilities():
            val = override_dict.get(cap.name, cap.default_value)
            entry = {
                "capability": cap,
                "effective_value": val,
                "is_overridden": cap.name in override_dict,
            }
            audience = cap.metadata.get("audience", "organizer")
            if audience == "developer":
                developer_entitlements.append(entry)
            else:
                organizer_entitlements.append(entry)

        ctx["organizer_entitlements"] = sorted(
            organizer_entitlements, key=lambda x: x["capability"].category
        )
        ctx["developer_entitlements"] = sorted(
            developer_entitlements, key=lambda x: x["capability"].category
        )

        ctx["active_organizer_addons"] = (
            OrganizerAddon.objects.filter(
                organizer=organizer,
                addon__active=True,
                status=AddonStatus.ACTIVE,
                starts_at__lte=current_time,
            )
            .exclude(ends_at__lt=current_time)
            .exclude(cancel_at__lte=current_time)
            .select_related("addon")
            .order_by("addon__name")
        )

        ctx["active_event_addons"] = (
            EventAddon.objects.filter(
                event__organizer=organizer,
                addon__active=True,
                status=AddonStatus.ACTIVE,
                starts_at__lte=current_time,
            )
            .exclude(ends_at__lt=current_time)
            .exclude(cancel_at__lte=current_time)
            .select_related("addon", "event")
            .order_by("event__name", "addon__name")
        )

        available_addons = list(
            AddonDefinition.objects.filter(
                active=True,
                public=True,
            ).order_by("assignment_scope", "name")
        )

        active_org_addons = list(
            OrganizerAddon.objects.filter(
                organizer=organizer,
                status=AddonStatus.ACTIVE,
                starts_at__lte=current_time,
            )
            .exclude(ends_at__lt=current_time)
            .exclude(cancel_at__lte=current_time)
        )
        active_by_addon_id = {
            oa.addon_id: oa for oa in active_org_addons if oa.addon_id
        }
        active_by_capability = {
            oa.capability: oa for oa in active_org_addons if oa.capability
        }

        for addon in available_addons:
            active_assignment = active_by_addon_id.get(
                addon.id
            ) or active_by_capability.get(addon.capability)
            addon.active_assignment = active_assignment
            addon.is_active_for_organizer = active_assignment is not None

        ctx["available_addons"] = available_addons
        return ctx


class OrganizerPlanUpgradeView(
    OrganizerPermissionRequiredMixin, OrganizerDetailViewMixin, TemplateView
):
    permission = "can_change_organizer_settings"
    template_name = "eventyay_business/organizer/plan_upgrade.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        organizer = self.request.organizer
        current_time = now()
        active_sub = (
            Subscription.objects.filter(
                organizer=organizer,
                status=SubscriptionStatus.ACTIVE,
                starts_at__lte=current_time,
            )
            .exclude(ends_at__lt=current_time)
            .select_related("tier_version__tier")
            .first()
        )
        ctx["current_subscription"] = active_sub
        ctx["current_tier"] = (
            active_sub.tier_version.tier
            if active_sub and active_sub.tier_version
            else None
        )

        tiers = (
            Tier.objects.filter(status=TierStatus.PUBLISHED, is_public=True)
            .order_by("display_order", "id")
            .prefetch_related("versions__prices", "versions__entitlements")
        )
        tier_list = []
        for tier in tiers:
            latest_version = (
                tier.versions.filter(published_at__isnull=False)
                .order_by("-version")
                .first()
            )
            if not latest_version:
                continue
            prices = list(
                latest_version.prices.filter(active=True).order_by(
                    "billing_interval", "amount"
                )
            )
            entitlements = list(latest_version.entitlements.all())
            organizer_features = [
                ent for ent in entitlements if ent.capability_audience != "developer"
            ]
            developer_features = [
                ent for ent in entitlements if ent.capability_audience == "developer"
            ]
            is_current = (
                active_sub is not None
                and active_sub.tier_version is not None
                and active_sub.tier_version.tier_id == tier.id
            )
            tier_list.append(
                {
                    "tier": tier,
                    "version": latest_version,
                    "prices": prices,
                    "entitlements": entitlements,
                    "organizer_features": organizer_features,
                    "developer_features": developer_features,
                    "is_current": is_current,
                }
            )

        ctx["tier_list"] = tier_list
        return ctx

    def post(self, request, *args, **kwargs):
        tier_price_id = request.POST.get("tier_price_id")
        tier_version_id = request.POST.get("tier_version_id")
        if not tier_price_id and not tier_version_id:
            messages.error(request, _("Please select a plan."))
            return redirect(
                "plugins:eventyay_business:organizer.plan.upgrade",
                organizer=request.organizer.slug,
            )

        tier_price = None
        target_version = None
        target_interval = BillingInterval.MONTHLY
        amount = Decimal("0.00")

        if tier_price_id:
            tier_price = get_object_or_404(
                TierPrice.objects.select_related("tier_version__tier"),
                pk=tier_price_id,
                active=True,
                tier_version__tier__status=TierStatus.PUBLISHED,
            )
            target_version = tier_price.tier_version
            target_interval = tier_price.billing_interval
            amount = getattr(tier_price, "amount", None)
            if amount is None:
                amount = getattr(tier_price, "price", Decimal("0.00"))
        else:
            target_version = get_object_or_404(
                TierVersion.objects.select_related("tier"),
                pk=tier_version_id,
                tier__status=TierStatus.PUBLISHED,
                tier__is_public=True,
                published_at__isnull=False,
            )
            is_valid_free = (
                target_version.tier.slug.lower() == "free"
                or target_version.tier.name.strip().lower() == "free"
            ) and not target_version.prices.filter(active=True, amount__gt=0).exists()
            if not is_valid_free:
                messages.error(request, _("Invalid plan selected."))
                return redirect(
                    "plugins:eventyay_business:organizer.plan.upgrade",
                    organizer=request.organizer.slug,
                )
            target_interval = BillingInterval.MONTHLY
            amount = Decimal("0.00")

        organizer = request.organizer

        if amount > 0 and organizer_consolidated_billing_enabled(organizer):
            billing_overview = build_organizer_billing_overview(organizer)
            if not billing_overview["billing_profile_ready"]:
                messages.warning(
                    request,
                    _(
                        "Save a payment method in billing settings before changing "
                        "to a paid plan."
                    ),
                )
                return redirect(
                    "eventyay_common:organizer.billing",
                    organizer=organizer.slug,
                )

        active_sub = (
            Subscription.objects.filter(
                organizer=organizer, status=SubscriptionStatus.ACTIVE
            )
            .select_related("tier_version__tier")
            .first()
        )

        # Determine if this is a downgrade
        is_downgrade = False
        current_amount = Decimal("0.00")
        if active_sub and active_sub.tier_version:
            current_tier = active_sub.tier_version.tier
            target_tier = target_version.tier

            curr_price = (
                active_sub.tier_version.prices.filter(
                    billing_interval=active_sub.billing_interval, active=True
                ).first()
                or active_sub.tier_version.prices.filter(active=True).first()
            )
            if curr_price:
                current_amount = getattr(curr_price, "amount", None) or getattr(
                    curr_price, "price", Decimal("0.00")
                )

            if target_tier.display_order < current_tier.display_order:
                is_downgrade = True
            elif target_tier.display_order > current_tier.display_order:
                is_downgrade = False
            else:
                # Same tier display order (e.g. interval change or same tier level)
                if active_sub.billing_interval != target_interval:
                    norm_target = (
                        amount / Decimal("12")
                        if target_interval in ("annual", "year", BillingInterval.ANNUAL)
                        else amount
                    )
                    norm_current = (
                        current_amount / Decimal("12")
                        if active_sub.billing_interval
                        in ("annual", "year", BillingInterval.ANNUAL)
                        else current_amount
                    )
                    if norm_target < norm_current or (
                        amount == 0 and current_amount > 0
                    ):
                        is_downgrade = True
                    else:
                        is_downgrade = False
                else:
                    if amount < current_amount or (amount == 0 and current_amount > 0):
                        is_downgrade = True
                    else:
                        is_downgrade = False

        if is_downgrade and active_sub:
            # Check resource usage against new limits to warn without deleting data
            from .capabilities import default_registry

            current_time = now()
            for ent in target_version.entitlements.all():
                target_limit = ent.get_typed_value()
                if target_limit is None or not isinstance(target_limit, int):
                    continue

                current_usage = None
                cap_def = default_registry.get(ent.capability)
                cap_label = cap_def.name if cap_def else ent.capability

                if ent.capability == "organizer.full_admins":
                    current_usage = (
                        User.objects.filter(
                            teams__organizer=organizer,
                            teams__can_change_organizer_settings=True,
                        )
                        .distinct()
                        .count()
                    )
                elif ent.capability.endswith(".monthly"):
                    usage_agg = UsageRecord.objects.filter(
                        organizer=organizer,
                        capability=ent.capability,
                        occurred_at__year=current_time.year,
                        occurred_at__month=current_time.month,
                    ).aggregate(total=Sum("quantity"))
                    current_usage = usage_agg["total"] or 0

                if current_usage is not None and current_usage > target_limit:
                    messages.warning(
                        request,
                        _(
                            "Your organisation currently has %(current)d for %(feature)s, "
                            "which exceeds the limit of %(limit)d on %(tier)s. Existing "
                            "data will not be deleted, but you will not be able to "
                            "add or use additional capacity until usage is within limits."
                        )
                        % {
                            "current": current_usage,
                            "feature": cap_label,
                            "limit": target_limit,
                            "tier": target_version.tier.name,
                        },
                    )

            # If Stripe subscription is linked, reconcile with Stripe before committing local state
            if active_sub.stripe_subscription_id and is_stripe_configured():
                try:
                    secret_key = get_stripe_secret_key_safe()
                    if secret_key:
                        stripe.api_key = secret_key
                        if amount == 0:
                            # Paid-to-Free downgrade
                            stripe.Subscription.modify(
                                active_sub.stripe_subscription_id,
                                cancel_at_period_end=True,
                            )
                        else:
                            # Paid-to-Paid downgrade
                            stripe_sub = stripe.Subscription.retrieve(
                                active_sub.stripe_subscription_id
                            )
                            items = (stripe_sub.get("items") or {}).get("data", [])
                            target_price_id = (
                                (
                                    tier_price.stripe_price_id
                                    or sync_tier_price_to_stripe(tier_price)
                                )
                                if tier_price
                                else None
                            )
                            if items and target_price_id:
                                stripe.Subscription.modify(
                                    active_sub.stripe_subscription_id,
                                    cancel_at_period_end=False,
                                    proration_behavior="none",
                                    items=[
                                        {"id": items[0]["id"], "price": target_price_id}
                                    ],
                                )
                except Exception as exc:
                    logger.error(
                        "Failed to update Stripe subscription for downgrade: %s",
                        exc,
                    )
                    messages.error(
                        request,
                        _("Failed to update subscription with payment provider: %s")
                        % str(exc),
                    )
                    return redirect(
                        "plugins:eventyay_business:organizer.plan",
                        organizer=organizer.slug,
                    )

            # Apply downgrade: immediate if active subscription has no ends_at, else schedule for renewal
            with transaction.atomic():
                sub_locked = (
                    Subscription.objects.select_for_update()
                    .filter(pk=active_sub.pk)
                    .first()
                )
                if not sub_locked:
                    messages.error(request, _("Subscription not found."))
                    return redirect(
                        "plugins:eventyay_business:organizer.plan",
                        organizer=organizer.slug,
                    )

                if sub_locked.ends_at:
                    sub_locked.pending_tier_version = target_version
                    sub_locked.pending_billing_interval = target_interval
                    sub_locked.pending_change_at = sub_locked.ends_at
                    sub_locked.save(
                        update_fields=[
                            "pending_tier_version",
                            "pending_billing_interval",
                            "pending_change_at",
                            "updated_at",
                        ]
                    )
                    subscription_change_scheduled.send(
                        sender=Subscription,
                        instance=sub_locked,
                        cancellation=False,
                    )
                    is_immediate = False
                else:
                    sub_locked.tier_version = target_version
                    sub_locked.billing_interval = target_interval
                    sub_locked.pending_tier_version = None
                    sub_locked.pending_billing_interval = None
                    sub_locked.pending_change_at = None
                    sub_locked.save()
                    is_immediate = True
                    org_ref = organizer
                    inst_ref = sub_locked
                    transaction.on_commit(
                        lambda org=org_ref, inst=inst_ref: (
                            invalidate_entitlement_cache(organizer=org),
                            subscription_downgraded.send(
                                sender=Subscription, instance=inst
                            ),
                        )
                    )

            if is_immediate:
                messages.success(
                    request,
                    _("Your plan has been changed to %(tier)s.")
                    % {"tier": target_version.tier.name},
                )
            else:
                renewal_str = active_sub.ends_at.strftime("%b %d, %Y")
                messages.success(
                    request,
                    _(
                        "Your plan downgrade to %(tier)s has been scheduled for %(renewal)s. "
                        "Your current plan features will remain active until then."
                    )
                    % {
                        "tier": target_version.tier.name,
                        "renewal": renewal_str,
                    },
                )
            return redirect(
                "plugins:eventyay_business:organizer.plan",
                organizer=organizer.slug,
            )

        if (
            amount > 0
            and not organizer_consolidated_billing_enabled(organizer)
            and is_stripe_configured()
        ):
            success_url = request.build_absolute_uri(
                reverse(
                    "plugins:eventyay_business:checkout.success",
                    kwargs={"organizer": organizer.slug},
                )
            )
            cancel_url = request.build_absolute_uri(
                reverse(
                    "plugins:eventyay_business:checkout.cancel",
                    kwargs={"organizer": organizer.slug},
                )
            )
            try:
                checkout_url = create_subscription_checkout_session(
                    organizer=organizer,
                    tier_price=tier_price,
                    user=request.user,
                    success_url=success_url,
                    cancel_url=cancel_url,
                )
                if checkout_url:
                    return redirect(checkout_url)
                messages.error(
                    request,
                    _("Could not initiate payment session. Please try again later."),
                )
            except (ValidationError, stripe.error.StripeError):
                logger.exception(
                    "Failed to create Stripe checkout session for tier upgrade"
                )
                messages.error(
                    request,
                    _(
                        "We could not start the secure payment setup. Check your "
                        "billing details and try again."
                    ),
                )
            return redirect(
                "plugins:eventyay_business:organizer.plan.upgrade",
                organizer=organizer.slug,
            )

        # Free tier or offline switch
        target_currency = tier_price.currency if tier_price else None
        with transaction.atomic():
            Organizer.objects.select_for_update().get(pk=organizer.pk)
            active_sub = (
                Subscription.objects.select_for_update()
                .filter(organizer=organizer, status=SubscriptionStatus.ACTIVE)
                .first()
            )
            if active_sub:
                active_sub.tier_version = target_version
                active_sub.billing_interval = target_interval
                if target_currency:
                    active_sub.currency = target_currency
                active_sub.save(
                    update_fields=[
                        "tier_version",
                        "billing_interval",
                        "currency",
                        "updated_at",
                    ]
                )
                sub = active_sub
            else:
                sub = Subscription.objects.create(
                    organizer=organizer,
                    tier_version=target_version,
                    status=SubscriptionStatus.ACTIVE,
                    billing_interval=target_interval,
                    currency=target_currency or "EUR",
                    starts_at=now(),
                )
            invalidate_entitlement_cache(organizer=organizer)
            subscription_purchased.send(
                sender=Subscription, instance=sub, user=request.user
            )

        messages.success(
            request,
            _("Successfully updated your plan to %(name)s.")
            % {"name": target_version.tier.name},
        )
        return redirect(
            "plugins:eventyay_business:organizer.plan", organizer=organizer.slug
        )


class OrganizerPlanCancelDowngradeView(
    OrganizerPermissionRequiredMixin, OrganizerDetailViewMixin, View
):
    permission = "can_change_organizer_settings"

    def post(self, request, *args, **kwargs):
        organizer = request.organizer
        active_sub = Subscription.objects.filter(
            organizer=organizer, status=SubscriptionStatus.ACTIVE
        ).first()
        if not active_sub or not active_sub.pending_tier_version:
            messages.info(request, _("No scheduled downgrade found."))
            return redirect(
                "plugins:eventyay_business:organizer.plan",
                organizer=organizer.slug,
            )

        if active_sub.stripe_subscription_id and is_stripe_configured():
            try:
                secret_key = get_stripe_secret_key_safe()
                if secret_key:
                    stripe.api_key = secret_key
                    stripe.Subscription.modify(
                        active_sub.stripe_subscription_id,
                        cancel_at_period_end=False,
                    )
            except Exception as exc:
                logger.error("Failed to cancel Stripe scheduled downgrade: %s", exc)
                messages.error(
                    request,
                    _("Failed to cancel scheduled downgrade with payment provider: %s")
                    % str(exc),
                )
                return redirect(
                    "plugins:eventyay_business:organizer.plan",
                    organizer=organizer.slug,
                )

        with transaction.atomic():
            sub_locked = (
                Subscription.objects.select_for_update()
                .filter(pk=active_sub.pk)
                .first()
            )
            if not sub_locked:
                messages.error(request, _("Subscription not found."))
                return redirect(
                    "plugins:eventyay_business:organizer.plan",
                    organizer=organizer.slug,
                )
            canceled_marker = (
                sub_locked.pending_change_at.isoformat()
                if sub_locked.pending_change_at
                else sub_locked.updated_at.isoformat()
            )
            was_cancellation = bool(
                sub_locked.pending_tier_version_id
                and sub_locked.pending_tier_version.tier.slug == "free"
            )
            sub_locked.pending_tier_version = None
            sub_locked.pending_billing_interval = None
            sub_locked.pending_change_at = None
            sub_locked.save(
                update_fields=[
                    "pending_tier_version",
                    "pending_billing_interval",
                    "pending_change_at",
                    "updated_at",
                ]
            )
            subscription_change_canceled.send(
                sender=Subscription,
                instance=sub_locked,
                cancellation=was_cancellation,
                canceled_marker=canceled_marker,
            )

        messages.success(
            request,
            _(
                "Your scheduled plan downgrade has been canceled. Your current plan "
                "will continue renewing normally."
            ),
        )
        return redirect(
            "plugins:eventyay_business:organizer.plan",
            organizer=organizer.slug,
        )


class OrganizerPlanCancelView(
    OrganizerPermissionRequiredMixin, OrganizerDetailViewMixin, View
):
    permission = "can_change_organizer_settings"

    def post(self, request, *args, **kwargs):
        organizer = request.organizer
        active_sub = (
            Subscription.objects.filter(
                organizer=organizer, status=SubscriptionStatus.ACTIVE
            )
            .select_related("tier_version__tier")
            .first()
        )
        if not active_sub:
            messages.error(request, _("No active subscription found."))
            return redirect(
                "plugins:eventyay_business:organizer.plan",
                organizer=organizer.slug,
            )

        if (
            active_sub.tier_version.tier.slug == "free"
            or not active_sub.tier_version.prices.filter(amount__gt=0).exists()
        ):
            messages.info(
                request,
                _("You are already on the Free plan. No cancellation is required."),
            )
            return redirect(
                "plugins:eventyay_business:organizer.plan",
                organizer=organizer.slug,
            )

        if active_sub.has_scheduled_downgrade:
            messages.info(
                request,
                _("Your subscription already has a scheduled plan change in progress."),
            )
            return redirect(
                "plugins:eventyay_business:organizer.plan",
                organizer=organizer.slug,
            )

        free_tier = (
            Tier.objects.filter(slug="free", status=TierStatus.PUBLISHED).first()
            or Tier.objects.filter(
                name__iexact="Free", status=TierStatus.PUBLISHED
            ).first()
        )
        if not free_tier:
            messages.error(
                request,
                _("Free plan is currently not available. Please contact support."),
            )
            return redirect(
                "plugins:eventyay_business:organizer.plan",
                organizer=organizer.slug,
            )

        free_version = (
            free_tier.versions.filter(published_at__isnull=False)
            .order_by("-version")
            .first()
        )
        if not free_version:
            messages.error(
                request,
                _(
                    "Free plan version is currently not available. Please contact support."
                ),
            )
            return redirect(
                "plugins:eventyay_business:organizer.plan",
                organizer=organizer.slug,
            )

        stripe_period_end = None
        # Reconcile with Stripe: cancel at period end
        if active_sub.stripe_subscription_id and is_stripe_configured():
            try:
                secret_key = get_stripe_secret_key_safe()
                if secret_key:
                    stripe.api_key = secret_key
                    stripe_sub = stripe.Subscription.modify(
                        active_sub.stripe_subscription_id,
                        cancel_at_period_end=True,
                    )
                    period_end_ts = getattr(stripe_sub, "current_period_end", None)
                    if period_end_ts:
                        from datetime import datetime, timezone

                        stripe_period_end = datetime.fromtimestamp(
                            period_end_ts, tz=timezone.utc
                        )
            except Exception as exc:
                logger.error("Failed to schedule Stripe cancellation: %s", exc)
                messages.error(
                    request,
                    _("Failed to cancel subscription with payment provider: %s")
                    % str(exc),
                )
                return redirect(
                    "plugins:eventyay_business:organizer.plan",
                    organizer=organizer.slug,
                )

        with transaction.atomic():
            sub_locked = (
                Subscription.objects.select_for_update()
                .filter(pk=active_sub.pk)
                .first()
            )
            if not sub_locked:
                messages.error(request, _("Subscription not found."))
                return redirect(
                    "plugins:eventyay_business:organizer.plan",
                    organizer=organizer.slug,
                )

            effective_ends_at = sub_locked.ends_at or stripe_period_end
            if effective_ends_at:
                sub_locked.pending_tier_version = free_version
                sub_locked.pending_billing_interval = BillingInterval.MONTHLY
                sub_locked.pending_change_at = effective_ends_at
                if not sub_locked.ends_at:
                    sub_locked.ends_at = effective_ends_at
                sub_locked.save(
                    update_fields=[
                        "pending_tier_version",
                        "pending_billing_interval",
                        "pending_change_at",
                        "ends_at",
                        "updated_at",
                    ]
                )
                subscription_change_scheduled.send(
                    sender=Subscription,
                    instance=sub_locked,
                    cancellation=True,
                )
                is_immediate = False
            else:
                sub_locked.tier_version = free_version
                sub_locked.billing_interval = BillingInterval.MONTHLY
                sub_locked.pending_tier_version = None
                sub_locked.pending_billing_interval = None
                sub_locked.pending_change_at = None
                sub_locked.save()
                is_immediate = True
                org_ref = organizer
                inst_ref = sub_locked
                transaction.on_commit(
                    lambda org=org_ref, inst=inst_ref: (
                        invalidate_entitlement_cache(organizer=org),
                        subscription_downgraded.send(
                            sender=Subscription,
                            instance=inst,
                            cancellation=True,
                        ),
                    )
                )

        if is_immediate:
            messages.success(
                request,
                _(
                    "Your subscription has been canceled and your plan changed to %(tier)s."
                )
                % {"tier": free_tier.name},
            )
        else:
            renewal_str = effective_ends_at.strftime("%b %d, %Y")
            messages.success(
                request,
                _(
                    "Your subscription cancellation has been scheduled. Your current plan features "
                    "will remain active until %(renewal)s, after which your account will transition "
                    "to %(tier)s."
                )
                % {
                    "tier": free_tier.name,
                    "renewal": renewal_str,
                },
            )

        return redirect(
            "plugins:eventyay_business:organizer.plan",
            organizer=organizer.slug,
        )


class OrganizerAddonPurchaseView(
    OrganizerPermissionRequiredMixin, OrganizerDetailViewMixin, FormView
):
    permission = "can_change_organizer_settings"
    template_name = "eventyay_business/organizer/addon_purchase.html"
    form_class = OrganizerAddonPurchaseForm

    def get_addon(self):
        return get_object_or_404(
            AddonDefinition,
            pk=self.kwargs["pk"],
            active=True,
            public=True,
        )

    def dispatch(self, request, *args, **kwargs):
        self.addon = self.get_addon()
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["organizer"] = self.request.organizer
        kwargs["addon"] = self.addon
        return kwargs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["addon"] = self.addon
        ctx["capability"] = get_capability(self.addon.capability)
        return ctx

    def form_valid(self, form):
        cap = get_capability(self.addon.capability)

        with transaction.atomic():
            if self.addon.assignment_scope == AddonAssignmentScope.EVENT:
                event = form.cleaned_data["event"]
                Event.objects.select_for_update().get(pk=event.pk)
                if cap and cap.value_type == CapabilityValueType.BOOLEAN:
                    if EventAddon.objects.filter(
                        event=event,
                        capability=self.addon.capability,
                        status=AddonStatus.ACTIVE,
                    ).exists():
                        messages.warning(
                            self.request,
                            _("This add-on is already active for %(event)s.")
                            % {"event": event.name},
                        )
                        return redirect(
                            "plugins:eventyay_business:organizer.plan",
                            organizer=self.request.organizer.slug,
                        )
            else:
                Organizer.objects.select_for_update().get(pk=self.request.organizer.pk)
                if cap and cap.value_type == CapabilityValueType.BOOLEAN:
                    if OrganizerAddon.objects.filter(
                        organizer=self.request.organizer,
                        capability=self.addon.capability,
                        status=AddonStatus.ACTIVE,
                    ).exists():
                        messages.warning(
                            self.request,
                            _("This add-on is already active for your organisation."),
                        )
                        return redirect(
                            "plugins:eventyay_business:organizer.plan",
                            organizer=self.request.organizer.slug,
                        )

            is_paid_stripe = (
                self.addon.price
                and self.addon.price > 0
                and not organizer_consolidated_billing_enabled(self.request.organizer)
                and is_stripe_configured()
            )
            if is_paid_stripe:
                assignment = form.save(commit=False, status=AddonStatus.PENDING)
                assignment.status = AddonStatus.PENDING
                assignment.save()
            else:
                form.save()
                messages.success(
                    self.request,
                    _("The %(addon)s add-on has been added to your organisation.")
                    % {"addon": self.addon.name},
                )
                return redirect(
                    "plugins:eventyay_business:organizer.plan",
                    organizer=self.request.organizer.slug,
                )

        # Transaction committed, lock released. Create Stripe checkout session.
        success_url = self.request.build_absolute_uri(
            reverse(
                "plugins:eventyay_business:checkout.success",
                kwargs={"organizer": self.request.organizer.slug},
            )
        )
        cancel_url = self.request.build_absolute_uri(
            reverse(
                "plugins:eventyay_business:checkout.cancel",
                kwargs={"organizer": self.request.organizer.slug},
            )
        )
        cancel_url = f"{cancel_url}?assignment_id={assignment.pk}&scope={self.addon.assignment_scope}"
        try:
            checkout_url = create_addon_checkout_session(
                organizer=self.request.organizer,
                addon=self.addon,
                user=self.request.user,
                quantity=form.cleaned_data.get("quantity", 1),
                event=form.cleaned_data.get("event"),
                success_url=success_url,
                cancel_url=cancel_url,
                assignment=assignment,
            )
            if checkout_url:
                return redirect(checkout_url)
            messages.error(
                self.request,
                _("Could not initiate payment session. Please try again later."),
            )
        except Exception as exc:
            logger.exception(
                "Failed to create Stripe checkout session for organizer addon: %s",
                exc,
            )
            messages.error(
                self.request,
                _("Payment provider error: %(error)s") % {"error": str(exc)},
            )
        if assignment.pk and assignment.status == AddonStatus.PENDING:
            assignment.delete()
        return redirect(
            "plugins:eventyay_business:organizer.plan",
            organizer=self.request.organizer.slug,
        )


class OrganizerAddonCancelView(OrganizerPermissionRequiredMixin, TemplateView):
    permission = "can_change_organizer_settings"
    template_name = "eventyay_business/organizer/addon_cancel.html"

    def dispatch(self, request, *args, **kwargs):
        self.addon_assignment = get_object_or_404(
            OrganizerAddon.objects.select_related("addon"),
            pk=self.kwargs["pk"],
            organizer=self.request.organizer,
        )
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["assignment"] = self.addon_assignment
        ctx["organizer"] = self.request.organizer
        return ctx

    def post(self, request, *args, **kwargs):
        immediate = (
            request.POST.get("immediate") == "1" or not self.addon_assignment.ends_at
        )
        with transaction.atomic():
            assignment = OrganizerAddon.objects.select_for_update().get(
                pk=self.addon_assignment.pk
            )
            if assignment.status != AddonStatus.ACTIVE:
                messages.info(request, _("This add-on is already not active."))
                return redirect(
                    "plugins:eventyay_business:organizer.plan",
                    organizer=self.request.organizer.slug,
                )

            assignment.cancel(immediate=immediate)
            if immediate or assignment.status == AddonStatus.CANCELED:
                log_addon_lifecycle_action(
                    assignment,
                    "canceled",
                    user=request.user,
                    data={"immediate": True},
                )
                invalidate_entitlement_cache(organizer=self.request.organizer)
                addon_canceled.send(
                    sender=OrganizerAddon, instance=assignment, immediate=True
                )
            else:
                log_addon_lifecycle_action(
                    assignment,
                    "cancellation_scheduled",
                    user=request.user,
                    data={
                        "cancel_at": (
                            assignment.cancel_at.isoformat()
                            if assignment.cancel_at
                            else None
                        )
                    },
                )
                addon_cancellation_scheduled.send(
                    sender=OrganizerAddon,
                    instance=assignment,
                )

        if immediate or assignment.status == AddonStatus.CANCELED:
            messages.success(
                request,
                _("Add-on '%(name)s' has been canceled.")
                % {"name": assignment.addon.name},
            )
        else:
            messages.success(
                request,
                _(
                    "Add-on '%(name)s' is scheduled to cancel at the end of the billing period."
                )
                % {"name": assignment.addon.name},
            )
        return redirect(
            "plugins:eventyay_business:organizer.plan",
            organizer=self.request.organizer.slug,
        )


class EventDashboardAddonsView(EventPermissionRequiredMixin, TemplateView):
    permission = "can_change_event_settings"
    template_name = "eventyay_business/event/addons.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        event = self.request.event
        current_time = now()

        active_addons = list(
            EventAddon.objects.filter(
                event=event,
                status=AddonStatus.ACTIVE,
                starts_at__lte=current_time,
            )
            .exclude(ends_at__lt=current_time)
            .exclude(cancel_at__lte=current_time)
            .order_by("-starts_at", "addon__name")
        )
        ctx["active_addons"] = active_addons

        available_addons = list(
            AddonDefinition.objects.filter(
                active=True,
                public=True,
                assignment_scope=AddonAssignmentScope.EVENT,
            ).order_by("name")
        )

        active_by_addon_id = {ea.addon_id: ea for ea in active_addons if ea.addon_id}
        active_by_capability = {
            ea.capability: ea for ea in active_addons if ea.capability
        }

        for addon in available_addons:
            active_assignment = active_by_addon_id.get(
                addon.id
            ) or active_by_capability.get(addon.capability)
            addon.active_assignment = active_assignment
            addon.is_active_for_event = active_assignment is not None

        ctx["available_addons"] = available_addons
        return ctx


class EventDashboardAddonPurchaseView(EventPermissionRequiredMixin, FormView):
    permission = "can_change_event_settings"
    template_name = "eventyay_business/event/addon_purchase.html"
    form_class = EventAddonPurchaseForm

    def get_addon(self):
        return get_object_or_404(
            AddonDefinition,
            pk=self.kwargs["pk"],
            active=True,
            public=True,
            assignment_scope=AddonAssignmentScope.EVENT,
        )

    def dispatch(self, request, *args, **kwargs):
        self.addon = self.get_addon()
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["event"] = self.request.event
        kwargs["addon"] = self.addon
        return kwargs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["addon"] = self.addon
        ctx["capability"] = get_capability(self.addon.capability)
        return ctx

    def form_valid(self, form):
        cap = get_capability(self.addon.capability)
        is_boolean = (cap and cap.value_type == CapabilityValueType.BOOLEAN) or (
            self.addon.entitlement_value
            and str(self.addon.entitlement_value).lower() in ("true", "1")
        )

        with transaction.atomic():
            Event.objects.select_for_update().get(pk=self.request.event.pk)
            if is_boolean:
                if EventAddon.objects.filter(
                    event=self.request.event,
                    capability=self.addon.capability,
                    status=AddonStatus.ACTIVE,
                ).exists():
                    messages.warning(
                        self.request,
                        _("This add-on is already active for %(event)s.")
                        % {"event": self.request.event.name},
                    )
                    return redirect(
                        "plugins:eventyay_business:event.addons",
                        organizer=self.request.organizer.slug,
                        event=self.request.event.slug,
                    )

            is_paid_stripe = (
                self.addon.price
                and self.addon.price > 0
                and not organizer_consolidated_billing_enabled(self.request.organizer)
                and is_stripe_configured()
            )
            if is_paid_stripe:
                assignment = form.save(commit=False, status=AddonStatus.PENDING)
                assignment.status = AddonStatus.PENDING
                assignment.save()
            else:
                form.save()
                messages.success(
                    self.request,
                    _("The %(addon)s add-on has been added to %(event)s.")
                    % {"addon": self.addon.name, "event": self.request.event.name},
                )
                return redirect(
                    "plugins:eventyay_business:event.addons",
                    organizer=self.request.organizer.slug,
                    event=self.request.event.slug,
                )

        # Transaction committed, lock released. Create Stripe checkout session.
        success_url = self.request.build_absolute_uri(
            reverse(
                "plugins:eventyay_business:event.checkout.success",
                kwargs={
                    "organizer": self.request.organizer.slug,
                    "event": self.request.event.slug,
                },
            )
        )
        cancel_url = self.request.build_absolute_uri(
            reverse(
                "plugins:eventyay_business:event.checkout.cancel",
                kwargs={
                    "organizer": self.request.organizer.slug,
                    "event": self.request.event.slug,
                },
            )
        )
        cancel_url = f"{cancel_url}?assignment_id={assignment.pk}&scope=event"
        try:
            checkout_url = create_addon_checkout_session(
                organizer=self.request.organizer,
                addon=self.addon,
                user=self.request.user,
                quantity=form.cleaned_data.get("quantity", 1),
                event=self.request.event,
                success_url=success_url,
                cancel_url=cancel_url,
                assignment=assignment,
            )
            if checkout_url:
                return redirect(checkout_url)
            messages.error(
                self.request,
                _("Could not initiate payment session. Please try again later."),
            )
        except Exception as exc:
            logger.exception(
                "Failed to create Stripe checkout session for event addon: %s",
                exc,
            )
            messages.error(
                self.request,
                _("Payment provider error: %(error)s") % {"error": str(exc)},
            )
        if assignment.pk and assignment.status == AddonStatus.PENDING:
            assignment.delete()
        return redirect(
            "plugins:eventyay_business:event.addons",
            organizer=self.request.organizer.slug,
            event=self.request.event.slug,
        )


class EventAddonCancelView(EventPermissionRequiredMixin, TemplateView):
    permission = "can_change_event_settings"
    template_name = "eventyay_business/event/addon_cancel.html"

    def dispatch(self, request, *args, **kwargs):
        self.addon_assignment = get_object_or_404(
            EventAddon.objects.select_related("addon"),
            pk=self.kwargs["pk"],
            event=self.request.event,
        )
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["assignment"] = self.addon_assignment
        ctx["event"] = self.request.event
        return ctx

    def post(self, request, *args, **kwargs):
        immediate = (
            request.POST.get("immediate") == "1" or not self.addon_assignment.ends_at
        )
        with transaction.atomic():
            assignment = EventAddon.objects.select_for_update().get(
                pk=self.addon_assignment.pk
            )
            if assignment.status != AddonStatus.ACTIVE:
                messages.info(request, _("This add-on is already not active."))
                return redirect(
                    "plugins:eventyay_business:event.addons",
                    organizer=self.request.organizer.slug,
                    event=self.request.event.slug,
                )

            assignment.cancel(immediate=immediate)
            if immediate or assignment.status == AddonStatus.CANCELED:
                log_addon_lifecycle_action(
                    assignment,
                    "canceled",
                    user=request.user,
                    data={"immediate": True},
                )
                invalidate_entitlement_cache(
                    organizer=self.request.organizer, event=self.request.event
                )
                addon_canceled.send(
                    sender=EventAddon, instance=assignment, immediate=True
                )
            else:
                log_addon_lifecycle_action(
                    assignment,
                    "cancellation_scheduled",
                    user=request.user,
                    data={
                        "cancel_at": (
                            assignment.cancel_at.isoformat()
                            if assignment.cancel_at
                            else None
                        )
                    },
                )
                addon_cancellation_scheduled.send(
                    sender=EventAddon,
                    instance=assignment,
                )

        if immediate or assignment.status == AddonStatus.CANCELED:
            messages.success(
                request,
                _("Add-on '%(name)s' has been canceled for %(event)s.")
                % {
                    "name": assignment.addon.name,
                    "event": self.request.event.name,
                },
            )
        else:
            messages.success(
                request,
                _("Add-on '%(name)s' is scheduled to cancel at the end of the period.")
                % {"name": assignment.addon.name},
            )
        return redirect(
            "plugins:eventyay_business:event.addons",
            organizer=self.request.organizer.slug,
            event=self.request.event.slug,
        )


class AddonDefinitionListView(AdministratorPermissionRequiredMixin, ListView):
    model = AddonDefinition
    template_name = "eventyay_business/addons/list.html"
    context_object_name = "addons"


class AddonDefinitionCreateView(AdministratorPermissionRequiredMixin, CreateView):
    model = AddonDefinition
    form_class = AddonDefinitionForm
    template_name = "eventyay_business/addons/form.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from .capabilities import get_capabilities_dict

        context["capabilities_data"] = get_capabilities_dict()
        return context

    def form_valid(self, form):
        self.object = form.save()
        messages.success(self.request, _("Add-on created successfully."))
        return redirect("plugins:eventyay_business:addons.list")


class AddonDefinitionUpdateView(AdministratorPermissionRequiredMixin, UpdateView):
    model = AddonDefinition
    form_class = AddonDefinitionForm
    template_name = "eventyay_business/addons/form.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from .capabilities import get_capabilities_dict

        context["capabilities_data"] = get_capabilities_dict()
        return context

    def form_valid(self, form):
        self.object = form.save()
        if form.cleaned_data.get("update_existing_assignments"):
            count = migrate_addon_assignments(self.object)
            messages.success(
                self.request,
                _(
                    "Add-on updated successfully and %(count)d existing active assignment(s) updated."
                )
                % {"count": count},
            )
        else:
            messages.success(self.request, _("Add-on updated successfully."))
        return redirect("plugins:eventyay_business:addons.list")


class AddonDefinitionToggleActiveView(AdministratorPermissionRequiredMixin, View):
    def post(self, request, pk, *args, **kwargs):
        addon = get_object_or_404(AddonDefinition, pk=pk)
        addon.active = not addon.active
        addon.save(update_fields=["active"])
        status_text = _("activated") if addon.active else _("deactivated")
        messages.success(
            request,
            _("Add-on %(name)s was %(status)s.")
            % {"name": addon.name, "status": status_text},
        )
        return redirect("plugins:eventyay_business:addons.list")


class OrganizerAddonListView(AdministratorPermissionRequiredMixin, ListView):
    model = OrganizerAddon
    template_name = "eventyay_business/addons/assignments/organizer_list.html"
    context_object_name = "assignments"

    def get_queryset(self):
        return OrganizerAddon.objects.select_related("organizer", "addon").order_by(
            "-starts_at", "-id"
        )


class OrganizerAddonCreateView(AdministratorPermissionRequiredMixin, CreateView):
    model = OrganizerAddon
    form_class = OrganizerAddonForm
    template_name = "eventyay_business/addons/assignments/form.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["assignment_type"] = "organizer"
        return ctx

    def form_valid(self, form):
        self.object = form.save()
        messages.success(self.request, _("Organizer add-on assigned successfully."))
        return redirect("plugins:eventyay_business:addons.assignments.organizer.list")


class OrganizerAddonUpdateView(AdministratorPermissionRequiredMixin, UpdateView):
    model = OrganizerAddon
    form_class = OrganizerAddonForm
    template_name = "eventyay_business/addons/assignments/form.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["assignment_type"] = "organizer"
        return ctx

    def form_valid(self, form):
        self.object = form.save()
        messages.success(self.request, _("Organizer add-on assignment updated."))
        return redirect("plugins:eventyay_business:addons.assignments.organizer.list")


class EventAddonListView(AdministratorPermissionRequiredMixin, ListView):
    model = EventAddon
    template_name = "eventyay_business/addons/assignments/event_list.html"
    context_object_name = "assignments"

    def get_queryset(self):
        return EventAddon.objects.select_related(
            "event", "event__organizer", "addon"
        ).order_by("-starts_at", "-id")


class EventAddonCreateView(AdministratorPermissionRequiredMixin, CreateView):
    model = EventAddon
    form_class = EventAddonForm
    template_name = "eventyay_business/addons/assignments/form.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["assignment_type"] = "event"
        return ctx

    def form_valid(self, form):
        self.object = form.save()
        messages.success(self.request, _("Event add-on assigned successfully."))
        return redirect("plugins:eventyay_business:addons.assignments.event.list")


class EventAddonUpdateView(AdministratorPermissionRequiredMixin, UpdateView):
    model = EventAddon
    form_class = EventAddonForm
    template_name = "eventyay_business/addons/assignments/form.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["assignment_type"] = "event"
        return ctx

    def form_valid(self, form):
        self.object = form.save()
        messages.success(self.request, _("Event add-on assignment updated."))
        return redirect("plugins:eventyay_business:addons.assignments.event.list")


class OrganizerAddonAdminRevokeView(AdministratorPermissionRequiredMixin, View):
    def post(self, request, *args, **kwargs):
        with transaction.atomic():
            assignment = get_object_or_404(
                OrganizerAddon.objects.select_for_update().select_related(
                    "addon", "organizer"
                ),
                pk=self.kwargs["pk"],
            )
            if assignment.status != AddonStatus.ACTIVE:
                messages.info(request, _("This add-on assignment is already inactive."))
                return redirect(
                    "plugins:eventyay_business:addons.assignments.organizer.list"
                )

            assignment.cancel(immediate=True)
            log_addon_lifecycle_action(
                assignment, "revoked_by_admin", user=request.user
            )
            invalidate_entitlement_cache(organizer=assignment.organizer)
            addon_canceled.send(
                sender=OrganizerAddon, instance=assignment, immediate=True
            )

        messages.success(
            request, _("The organizer add-on assignment has been revoked.")
        )
        return redirect("plugins:eventyay_business:addons.assignments.organizer.list")


class EventAddonAdminRevokeView(AdministratorPermissionRequiredMixin, View):
    def post(self, request, *args, **kwargs):
        with transaction.atomic():
            assignment = get_object_or_404(
                EventAddon.objects.select_for_update().select_related(
                    "addon", "event", "event__organizer"
                ),
                pk=self.kwargs["pk"],
            )
            if assignment.status != AddonStatus.ACTIVE:
                messages.info(request, _("This add-on assignment is already inactive."))
                return redirect(
                    "plugins:eventyay_business:addons.assignments.event.list"
                )

            assignment.cancel(immediate=True)
            log_addon_lifecycle_action(
                assignment, "revoked_by_admin", user=request.user
            )
            invalidate_entitlement_cache(
                organizer=assignment.event.organizer, event=assignment.event
            )
            addon_canceled.send(sender=EventAddon, instance=assignment, immediate=True)

        messages.success(request, _("The event add-on assignment has been revoked."))
        return redirect("plugins:eventyay_business:addons.assignments.event.list")


class OrganizerInvoiceListView(
    OrganizerPermissionRequiredMixin, OrganizerDetailViewMixin, ListView
):
    model = BusinessInvoice
    template_name = "eventyay_business/invoices/organizer_list.html"
    context_object_name = "invoices"
    paginate_by = 25
    permission = "can_change_organizer_settings"

    def get_queryset(self):
        return (
            BusinessInvoice.objects.filter(organizer=self.request.organizer)
            .prefetch_related("lines")
            .order_by("-billing_period_start", "-id")
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["organizer"] = self.request.organizer
        return ctx


class OrganizerCurrentChargesView(
    OrganizerPermissionRequiredMixin, OrganizerDetailViewMixin, TemplateView
):
    template_name = "eventyay_business/billing/organizer_current.html"
    permission = "can_change_organizer_settings"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["organizer"] = self.request.organizer
        ctx.update(build_organizer_billing_overview(self.request.organizer))
        ctx["automatic_payment_save_supported"] = bool(
            organizer_billing_payment_method_saved
        )
        return ctx


class OrganizerBillingAuthorizationView(
    OrganizerPermissionRequiredMixin, OrganizerDetailViewMixin, View
):
    permission = "can_change_organizer_settings"

    def post(self, request, *args, **kwargs):
        with transaction.atomic():
            organizer = Organizer.objects.select_for_update().get(
                pk=request.organizer.pk
            )
            billing = OrganizerBillingModel.objects.filter(organizer=organizer).first()
        if not billing or not billing.stripe_customer_id:
            messages.error(
                request,
                _(
                    "Complete the organizer billing details before authorizing collection."
                ),
            )
        elif not billing.stripe_payment_method_id:
            messages.error(
                request,
                _("Save a payment method before authorizing automatic collection."),
            )
        elif request.POST.get("authorize") != "yes":
            messages.error(
                request, _("Confirm the automatic collection authorization.")
            )
        else:
            with transaction.atomic():
                organizer = Organizer.objects.select_for_update().get(
                    pk=request.organizer.pk
                )
                BillingCollectionAuthorization.objects.update_or_create(
                    organizer=organizer,
                    defaults={
                        "stripe_customer_id": billing.stripe_customer_id,
                        "authorized_at": now(),
                        "authorized_by": request.user,
                        "revoked_at": None,
                        "terms_version": AUTOMATIC_COLLECTION_TERMS_VERSION,
                    },
                )
            messages.success(request, _("Automatic invoice collection is authorized."))
        return redirect(
            "plugins:eventyay_business:organizer.current_charges",
            organizer=request.organizer.slug,
        )


class OrganizerBillingAuthorizationRevokeView(
    OrganizerPermissionRequiredMixin, OrganizerDetailViewMixin, View
):
    permission = "can_change_organizer_settings"

    def get(self, request, *args, **kwargs):
        authorization = BillingCollectionAuthorization.objects.filter(
            organizer=request.organizer, revoked_at__isnull=True
        ).first()
        if not authorization:
            messages.info(request, _("Automatic payments are already off."))
            return redirect(
                "plugins:eventyay_business:organizer.current_charges",
                organizer=request.organizer.slug,
            )

        subscription = (
            Subscription.objects.filter(
                organizer=request.organizer,
                status__in=(SubscriptionStatus.ACTIVE, SubscriptionStatus.PAST_DUE),
            )
            .select_related("tier_version__tier")
            .first()
        )
        organizer_addons = OrganizerAddon.objects.filter(
            organizer=request.organizer,
            status__in=(AddonStatus.ACTIVE, AddonStatus.PAST_DUE),
        ).select_related("addon")
        event_addons = []
        for event in request.organizer.events.all():
            with scope(event=event):
                event_addons.extend(
                    EventAddon.objects.filter(
                        event=event,
                        status__in=(AddonStatus.ACTIVE, AddonStatus.PAST_DUE),
                    ).select_related("addon", "event")
                )
        return render(
            request,
            "eventyay_business/billing/automatic_payment_cancel.html",
            {
                "organizer": request.organizer,
                "subscription": subscription,
                "organizer_addons": organizer_addons,
                "event_addons": event_addons,
            },
        )

    def post(self, request, *args, **kwargs):
        with transaction.atomic():
            organizer = Organizer.objects.select_for_update().get(
                pk=request.organizer.pk
            )
            BillingCollectionAuthorization.objects.filter(
                organizer=organizer, revoked_at__isnull=True
            ).update(revoked_at=now())
        messages.success(
            request,
            _(
                "Automatic payments are off. Your subscriptions remain active and invoices still become due."
            ),
        )
        return redirect(
            "plugins:eventyay_business:organizer.current_charges",
            organizer=request.organizer.slug,
        )


class OrganizerInvoiceDetailView(OrganizerPermissionRequiredMixin, DetailView):
    model = BusinessInvoice
    template_name = "eventyay_business/invoices/organizer_detail.html"
    context_object_name = "invoice"
    permission = "can_change_organizer_settings"

    def get_queryset(self):
        return BusinessInvoice.objects.filter(
            organizer=self.request.organizer
        ).prefetch_related("lines__event", "lines__tier_version__tier", "lines__addon")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["organizer"] = self.request.organizer
        ctx["lines"] = self.object.lines.all()
        return ctx


class OrganizerInvoiceDownloadView(
    OrganizerPermissionRequiredMixin, OrganizerDetailViewMixin, View
):
    permission = "can_change_organizer_settings"

    def get(self, request, *args, **kwargs):
        invoice = get_object_or_404(
            BusinessInvoice.objects.prefetch_related("lines"),
            pk=kwargs["pk"],
            organizer=request.organizer,
            finalized_at__isnull=False,
        )
        response = HttpResponse(
            render_invoice_pdf(invoice), content_type="application/pdf"
        )
        response["Content-Disposition"] = (
            f'attachment; filename="{invoice.invoice_number}.pdf"'
        )
        return response


class AdminBillingNotificationListView(AdministratorPermissionRequiredMixin, ListView):
    model = BillingNotificationDelivery
    template_name = "eventyay_business/notifications/admin_list.html"
    context_object_name = "deliveries"
    paginate_by = 50

    def get_queryset(self):
        queryset = BillingNotificationDelivery.objects.select_related(
            "organizer"
        ).order_by("-created_at", "-id")
        status = self.request.GET.get("status", "").strip()
        event_type = self.request.GET.get("event_type", "").strip()
        organizer = self.request.GET.get("organizer", "").strip()
        if status:
            queryset = queryset.filter(status=status)
        if event_type:
            queryset = queryset.filter(event_type=event_type)
        if organizer:
            queryset = queryset.filter(organizer__slug=organizer)
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        current_organizer = self.request.GET.get("organizer", "").strip()
        context.update(
            {
                "status_choices": BillingNotificationStatus.choices,
                "event_type_choices": BillingNotificationEventType.choices,
                "organizers": Organizer.objects.order_by("name", "slug"),
                "current_status": self.request.GET.get("status", ""),
                "current_event_type": self.request.GET.get("event_type", ""),
                "current_organizer": current_organizer,
            }
        )
        return context


class AdminBillingNotificationRetryView(AdministratorPermissionRequiredMixin, View):
    def post(self, request, *args, **kwargs):
        delivery = get_object_or_404(BillingNotificationDelivery, pk=kwargs["pk"])
        if delivery.status == BillingNotificationStatus.SENT:
            messages.info(request, _("This notification was already sent."))
        elif delivery.status == BillingNotificationStatus.SENDING:
            messages.info(request, _("This notification is already being delivered."))
        else:
            retry_billing_notification(delivery)
            messages.success(request, _("The notification retry was queued."))
        return redirect("plugins:eventyay_business:notifications.list")


class AdminBillingNotificationPreviewView(
    AdministratorPermissionRequiredMixin, FormView
):
    template_name = "eventyay_business/notifications/preview.html"
    form_class = BillingNotificationTestForm

    def get_initial(self):
        initial = super().get_initial()
        locale = self.request.GET.get("locale")
        if locale in {code for code, _label in settings.LANGUAGES}:
            initial["locale"] = locale
        return initial

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        locale = self.request.GET.get("locale") or "en"
        valid_locales = {code for code, _label in settings.LANGUAGES}
        if locale not in valid_locales:
            locale = settings.LANGUAGE_CODE
        previews = []
        for event_type, spec in NOTIFICATION_SPECS.items():
            preview_context = preview_notification_context(event_type)
            subject, custom_body, is_custom = render_notification_content(
                event_type, preview_context, locale
            )
            if custom_body is None:
                with language(locale):
                    body = render_to_string(spec.template_name, preview_context)
            else:
                body = custom_body
            previews.append(
                {
                    "event_type": event_type,
                    "label": BillingNotificationEventType(event_type).label,
                    "subject": subject,
                    "body": body,
                    "is_custom": is_custom,
                    "locale": locale,
                }
            )
        context["previews"] = previews
        context["preview_locale"] = locale
        context["language_choices"] = settings.LANGUAGES
        return context

    def form_valid(self, form):
        organizer = form.cleaned_data["organizer"]
        event_type = BillingNotificationEventType(form.cleaned_data["event_type"])
        recipient = BillingNotificationRecipient(
            email=form.cleaned_data["recipient"].strip().lower(),
            locale=form.cleaned_data["locale"],
            source=BillingNotificationRecipientSource.OVERRIDE,
        )
        queue_billing_notification(
            logical_event_key=f"admin-test:{uuid4()}",
            event_type=event_type,
            organizer=organizer,
            recipient=recipient,
            context=preview_notification_context(event_type),
            related_object_type="admin_preview",
        )
        messages.success(
            self.request,
            _("The test notification was queued without changing billing state."),
        )
        return redirect(
            f"{reverse('plugins:eventyay_business:notifications.preview')}"
            f"?locale={form.cleaned_data['locale']}"
        )


class AdminBillingNotificationTemplateView(
    AdministratorPermissionRequiredMixin, FormView
):
    template_name = "eventyay_business/notifications/template_form.html"
    form_class = BillingNotificationTemplateForm

    def get_event_type(self):
        try:
            return BillingNotificationEventType(self.kwargs["event_type"])
        except ValueError as exc:
            raise Http404 from exc

    def get_initial(self):
        initial = super().get_initial()
        subjects = {}
        messages_by_locale = {}
        active_locales = [settings.LANGUAGE_CODE]
        for locale, _label in settings.LANGUAGES:
            subject, body, is_custom = notification_template_sources(
                self.get_event_type(), locale
            )
            subjects[locale] = subject
            messages_by_locale[locale] = body
            if is_custom and locale not in active_locales:
                active_locales.append(locale)
        initial.update(
            {
                "active_locales": ",".join(active_locales),
                "subject": subjects,
                "message": messages_by_locale,
            }
        )
        return initial

    def post(self, request, *args, **kwargs):
        if request.POST.get("action") == "reset":
            event_type = self.get_event_type()
            for locale, _label in settings.LANGUAGES:
                reset_notification_template(event_type, locale)
            messages.success(
                request,
                _("The notification template was reset to its translated defaults."),
            )
            return redirect(
                reverse(
                    "plugins:eventyay_business:notifications.template",
                    kwargs={"event_type": event_type},
                )
            )
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        event_type = self.get_event_type()
        active_locales = form.cleaned_data["active_locales"]
        subjects = form.cleaned_data["subject"].data
        messages_by_locale = form.cleaned_data["message"].data
        for locale, _label in settings.LANGUAGES:
            if locale in active_locales:
                save_notification_template(
                    event_type,
                    locale,
                    subjects.get(locale, ""),
                    messages_by_locale.get(locale, ""),
                )
            else:
                reset_notification_template(event_type, locale)
        messages.success(self.request, _("The notification template was saved."))
        return redirect(
            reverse(
                "plugins:eventyay_business:notifications.template",
                kwargs={"event_type": event_type},
            )
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        event_type = self.get_event_type()
        is_custom = any(
            notification_template_sources(event_type, locale)[2]
            for locale, _label in settings.LANGUAGES
        )
        context.update(
            {
                "event_type": event_type,
                "event_label": event_type.label,
                "is_custom": is_custom,
                "platform_default_locale": settings.LANGUAGE_CODE,
                "placeholders": NOTIFICATION_PLACEHOLDERS.items(),
            }
        )
        return context


class AdminInvoiceListView(AdministratorPermissionRequiredMixin, ListView):
    model = BusinessInvoice
    template_name = "eventyay_business/invoices/admin_list.html"
    context_object_name = "invoices"
    paginate_by = 30

    def get_queryset(self):
        qs = BusinessInvoice.objects.select_related("organizer").order_by(
            "-created_at", "-id"
        )
        status = self.request.GET.get("status")
        if status:
            qs = qs.filter(status=status)
        search = self.request.GET.get("q")
        if search:
            qs = qs.filter(
                Q(invoice_number__icontains=search)
                | Q(organizer__name__icontains=search)
                | Q(organizer__slug__icontains=search)
            )
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["status_choices"] = BusinessInvoiceStatus.choices
        ctx["current_status"] = self.request.GET.get("status", "")
        ctx["search_query"] = self.request.GET.get("q", "")
        return ctx


class AdminInvoiceDetailView(AdministratorPermissionRequiredMixin, DetailView):
    model = BusinessInvoice
    template_name = "eventyay_business/invoices/admin_detail.html"
    context_object_name = "invoice"

    def get_queryset(self):
        return BusinessInvoice.objects.select_related("organizer").prefetch_related(
            "lines__event", "lines__tier_version__tier", "lines__addon"
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["lines"] = self.object.lines.all()
        return ctx


class AdminInvoiceDownloadView(AdministratorPermissionRequiredMixin, View):
    def get(self, request, *args, **kwargs):
        invoice = get_object_or_404(
            BusinessInvoice.objects.prefetch_related("lines"),
            pk=kwargs["pk"],
            finalized_at__isnull=False,
        )
        response = HttpResponse(
            render_invoice_pdf(invoice), content_type="application/pdf"
        )
        response["Content-Disposition"] = (
            f'attachment; filename="{invoice.invoice_number}.pdf"'
        )
        return response


class InvoicePresentationSettingsView(
    AdministratorPermissionRequiredMixin, RedirectView
):
    permanent = False

    def get_redirect_url(self, *args, **kwargs):
        return (
            f"{reverse('plugins:eventyay_business:settings')}#tab-invoice_presentation"
        )


class InvoicePresentationPreviewView(AdministratorPermissionRequiredMixin, View):
    def get(self, request, *args, **kwargs):
        today = date.today()
        period_end = today.replace(day=1) - timedelta(days=1)
        period_start = period_end.replace(day=1)
        invoice = BusinessInvoice(
            invoice_number="PREVIEW",
            billing_period_start=period_start,
            billing_period_end=period_end,
            issue_date=today,
            due_date=today + timedelta(days=14),
            currency="EUR",
            subtotal=Decimal("196.00"),
            tax=Decimal("37.24"),
            total=Decimal("233.24"),
            finalized_at=now(),
            billing_address_snapshot={
                "organization_name": "Sample Organizer",
                "contact_name": "Billing Contact",
                "address_line_1": "123 Event Street",
                "postal_code": "10115",
                "city": "Berlin",
                "country": "Germany",
                "tax_id": "DE123456789",
            },
            presentation_snapshot=current_invoice_presentation_snapshot(),
        )
        lines = [
            SimpleNamespace(
                line_type=line_type,
                description=description,
                service_period_start=period_start,
                service_period_end=period_end,
                quantity=quantity,
                unit_price=unit_price,
                amount=amount,
            )
            for line_type, description, quantity, unit_price, amount in (
                (
                    InvoiceLineType.SUBSCRIPTION,
                    "Professional monthly plan",
                    1,
                    Decimal("39.00"),
                    Decimal("39.00"),
                ),
                (
                    InvoiceLineType.ADDON,
                    "Livestream add-on",
                    1,
                    Decimal("12.00"),
                    Decimal("12.00"),
                ),
                (
                    InvoiceLineType.PLATFORM_FEE,
                    "Ticketing platform fees",
                    1,
                    Decimal("150.00"),
                    Decimal("150.00"),
                ),
                (
                    InvoiceLineType.MANUAL_ADJUSTMENT,
                    "Service credit",
                    1,
                    Decimal("-5.00"),
                    Decimal("-5.00"),
                ),
                (
                    InvoiceLineType.TAX,
                    "VAT (19%)",
                    1,
                    Decimal("37.24"),
                    Decimal("37.24"),
                ),
            )
        ]
        response = HttpResponse(
            render_invoice_pdf(invoice, lines=lines), content_type="application/pdf"
        )
        response["Content-Disposition"] = 'inline; filename="invoice-preview.pdf"'
        return response


class AdminCurrentChargesMixin:
    def get_filters(self):
        period_start, period_end, _issue_date = parse_month(
            self.request.GET.get("period")
        )
        organizer = None
        organizer_slug = self.request.GET.get("organizer", "").strip()
        if organizer_slug:
            organizer = Organizer.objects.filter(slug=organizer_slug).first()
        state = self.request.GET.get("state", "open")
        if state not in {"open", "invoiced", "all"}:
            state = "open"
        return {
            "organizer": organizer,
            "invalid_organizer": bool(organizer_slug and organizer is None),
            "period_start": period_start,
            "period_end": period_end,
            "state": state,
            "currency": self.request.GET.get("currency", "").strip().upper(),
            "event_query": self.request.GET.get("event", "").strip(),
        }

    def get_rows(self, filters=None):
        filters = filters or self.get_filters()
        query_filters = filters.copy()
        if query_filters.pop("invalid_organizer"):
            return []
        query_filters["include_carry_forward"] = query_filters["state"] == "open"
        return platform_fee_rows(**query_filters)


class AdminCurrentChargesView(
    AdministratorPermissionRequiredMixin, AdminCurrentChargesMixin, TemplateView
):
    template_name = "eventyay_business/billing/admin_current.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        filters = self.get_filters()
        rows = self.get_rows(filters.copy())
        totals = defaultdict(lambda: Decimal("0.00"))
        for row in rows:
            totals[row["currency"]] += row["net"]
        billing_customer_organizers = (
            OrganizerBillingModel.objects.exclude(stripe_customer_id__isnull=True)
            .exclude(stripe_customer_id="")
            .values_list("organizer_id", flat=True)
        )
        billing_attention_count = (
            Subscription.objects.filter(
                status__in=[
                    SubscriptionStatus.ACTIVE,
                    SubscriptionStatus.PAST_DUE,
                ]
            )
            .exclude(organizer_id__in=billing_customer_organizers)
            .values("organizer_id")
            .distinct()
            .count()
        )
        ctx.update(
            {
                "rows": rows,
                "row_count": len(rows),
                "totals": [
                    {"currency": currency, "amount": amount}
                    for currency, amount in sorted(totals.items())
                ],
                "organizers": Organizer.objects.order_by("name", "slug"),
                "billing_attention_count": billing_attention_count,
                "currencies": PlatformFeeLedgerEntry.objects.order_by("currency")
                .values_list("currency", flat=True)
                .distinct(),
                "current_organizer": self.request.GET.get("organizer", "").strip(),
                "current_period": filters["period_start"].strftime("%Y-%m"),
                "current_state": filters["state"],
                "current_currency": filters["currency"],
                "current_event": filters["event_query"],
                "export_query": self.request.GET.urlencode(),
            }
        )
        return ctx


class AdminCurrentChargesExportView(
    AdministratorPermissionRequiredMixin, AdminCurrentChargesMixin, View
):
    def get(self, request, *args, **kwargs):
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = (
            'attachment; filename="business-current-charges.csv"'
        )
        writer = csv.writer(response)
        writer.writerow(
            [
                "Organizer",
                "Event",
                "Order",
                "Payment",
                "Payment date",
                "Currency",
                "Eligible amount",
                "Rate",
                "Cap",
                "Calculated fee",
                "Adjustments",
                "Net amount",
                "State",
                "Policy source",
                "Collection path",
                "Cutover relation",
                "Source key",
            ]
        )
        for row in self.get_rows():
            writer.writerow(
                [
                    row["organizer"].slug,
                    row["event"].slug,
                    row["order_code"],
                    row["payment_reference"],
                    row["occurred_at"].isoformat(),
                    row["currency"],
                    row["fee_base"],
                    row["percentage"],
                    row["maximum_fee"],
                    row["calculated_fee"],
                    row["adjustment"],
                    row["net"],
                    row["state"],
                    row["policy_source"],
                    row["collection_path"],
                    row["cutover_relation"],
                    row["source_key"],
                ]
            )
        return response


class AdminBillingActivationView(AdministratorPermissionRequiredMixin, TemplateView):
    template_name = "eventyay_business/billing/admin_activation.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["organizer_preflights"] = [
            (organizer, consolidated_billing_preflight(organizer))
            for organizer in Organizer.objects.order_by("name", "slug")
        ]
        return ctx


class AdminBillingActivationBatchView(AdministratorPermissionRequiredMixin, View):
    def post(self, request, *args, **kwargs):
        organizer_slugs = {
            slug.strip() for slug in request.POST.getlist("organizers") if slug.strip()
        }
        activated = 0
        skipped = 0
        for organizer in Organizer.objects.filter(slug__in=organizer_slugs).order_by(
            "pk"
        ):
            try:
                enable_consolidated_billing(organizer)
            except ValidationError:
                skipped += 1
            else:
                activated += 1
        if activated:
            messages.success(
                request,
                _("Activated consolidated billing for %(count)s organizer(s).")
                % {"count": activated},
            )
        if skipped:
            messages.warning(
                request,
                _("Skipped %(count)s organizer(s) that failed the locked preflight.")
                % {"count": skipped},
            )
        return redirect("plugins:eventyay_business:billing_activation")


class AdminBillingRolloutMonitorView(
    AdministratorPermissionRequiredMixin, TemplateView
):
    template_name = "eventyay_business/billing/rollout_monitor.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        organizers = Organizer.objects.prefetch_related(
            Prefetch(
                "business_invoices",
                queryset=BusinessInvoice.objects.order_by("-created_at"),
                to_attr="monitoring_invoices",
            ),
            Prefetch(
                "subscriptions",
                queryset=Subscription.objects.order_by("-starts_at"),
                to_attr="monitoring_subscriptions",
            ),
        ).order_by("name", "slug")
        rows = []
        for organizer in organizers:
            cutover_at = organizer_billing_cutover_at(organizer)
            if not cutover_at:
                continue
            invoice = (
                organizer.monitoring_invoices[0]
                if organizer.monitoring_invoices
                else None
            )
            subscription = (
                organizer.monitoring_subscriptions[0]
                if organizer.monitoring_subscriptions
                else None
            )
            metadata = (
                invoice.collection_metadata
                if invoice and isinstance(invoice.collection_metadata, dict)
                else {}
            )
            rows.append(
                {
                    "organizer": organizer,
                    "cutover_at": cutover_at,
                    "invoice": invoice,
                    "subscription": subscription,
                    "reconciliation_required": bool(
                        metadata.get("reconciliation_required")
                    ),
                    "retry_exhausted": bool(metadata.get("retry_exhausted")),
                    "last_error": (metadata.get("last_error") or {}).get("code", ""),
                }
            )
        context["rollout_rows"] = rows
        return context


class AdminBillingReconciliationListView(
    AdministratorPermissionRequiredMixin, ListView
):
    model = BillingReconciliationResult
    template_name = "eventyay_business/billing/reconciliation_list.html"
    context_object_name = "results"
    paginate_by = 100

    def get_queryset(self):
        queryset = BillingReconciliationResult.objects.select_related(
            "run", "organizer", "accepted_by"
        )
        organizer = (self.request.GET.get("organizer") or "").strip()
        status = (self.request.GET.get("status") or "").strip()
        period = (self.request.GET.get("period") or "").strip()
        if organizer:
            queryset = queryset.filter(organizer__slug=organizer)
        if status in BillingReconciliationResultStatus.values:
            queryset = queryset.filter(status=status)
        if period:
            period_start, period_end, _ = parse_month(period)
            queryset = queryset.filter(
                run__period_start=period_start,
                run__period_end=period_end,
            )
        return queryset.order_by("-run__period_start", "organizer__name", "currency")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            {
                "organizers": Organizer.objects.order_by("name", "slug"),
                "statuses": BillingReconciliationResultStatus.choices,
                "current_organizer": (self.request.GET.get("organizer") or "").strip(),
                "current_status": (self.request.GET.get("status") or "").strip(),
                "current_period": (self.request.GET.get("period") or "").strip(),
                "latest_run": BillingReconciliationRun.objects.first(),
            }
        )
        return context


class AdminBillingReconciliationDetailView(
    AdministratorPermissionRequiredMixin, DetailView
):
    model = BillingReconciliationResult
    template_name = "eventyay_business/billing/reconciliation_detail.html"
    context_object_name = "result"

    def get_queryset(self):
        return super().get_queryset().select_related("run", "organizer", "accepted_by")


class AdminBillingReconciliationRunView(AdministratorPermissionRequiredMixin, View):
    def post(self, request, *args, **kwargs):
        period = (request.POST.get("period") or "").strip()
        if period:
            period_start, period_end, _ = parse_month(period)
            run = run_billing_reconciliation(period_start, period_end)
        else:
            run = run_billing_reconciliation()
        messages.success(
            request,
            _("Billing reconciliation completed for %(count)s result(s).")
            % {"count": run.summary.get("processed", 0)},
        )
        return redirect("plugins:eventyay_business:reconciliation.list")


class AdminBillingReconciliationAcceptView(AdministratorPermissionRequiredMixin, View):
    def post(self, request, *args, **kwargs):
        result = get_object_or_404(BillingReconciliationResult, pk=kwargs["pk"])
        try:
            accept_reconciliation_result(result.pk, request.user)
        except ValidationError:
            messages.error(
                request,
                _("Resolve every blocking finding before accepting this result."),
            )
        else:
            messages.success(request, _("Reconciliation result accepted."))
        return redirect(
            "plugins:eventyay_business:reconciliation.detail",
            pk=result.pk,
        )


class AdminBillingReconciliationExportView(AdministratorPermissionRequiredMixin, View):
    def get(self, request, *args, **kwargs):
        run = get_object_or_404(BillingReconciliationRun, pk=kwargs["pk"])
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = (
            f'attachment; filename="billing-reconciliation-{run.pk}.csv"'
        )
        writer = csv.writer(response)
        writer.writerow(
            [
                "Organizer",
                "Currency",
                "Result status",
                "Accepted",
                "Expected total",
                "Line type",
                "Description",
                "Service period start",
                "Service period end",
                "Amount",
                "Source reference",
                "Blocking findings",
            ]
        )
        for result in run.results.select_related("organizer").order_by(
            "organizer__slug", "currency"
        ):
            lines = result.snapshot.get("invoice", {}).get("lines", []) or [{}]
            finding_codes = ", ".join(
                finding["code"]
                for finding in result.findings
                if finding.get("severity") == "blocking"
            )
            for line in lines:
                writer.writerow(
                    [
                        result.organizer.slug,
                        result.currency,
                        result.status,
                        result.is_currently_accepted,
                        result.expected_total,
                        line.get("line_type", ""),
                        line.get("description", ""),
                        line.get("service_period_start", ""),
                        line.get("service_period_end", ""),
                        line.get("amount", ""),
                        line.get("usage_reference", ""),
                        finding_codes,
                    ]
                )
        return response


class AdminBillingActivationEnableView(AdministratorPermissionRequiredMixin, View):
    def post(self, request, *args, **kwargs):
        organizer = get_object_or_404(Organizer, slug=kwargs["organizer"])
        try:
            enable_consolidated_billing(organizer)
        except ValidationError:
            messages.error(request, _("This organizer is not ready for activation."))
        else:
            messages.success(request, _("Consolidated billing has been activated."))
        return redirect("plugins:eventyay_business:billing_activation")


class FeeSettingsView(AdministratorPermissionRequiredMixin, TemplateView):
    template_name = "eventyay_business/fees/list.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["country_settings"] = CountryFeeSetting.objects.all().order_by(
            "country", "currency"
        )
        return ctx


class CountryFeeSettingCreateView(AdministratorPermissionRequiredMixin, CreateView):
    model = CountryFeeSetting
    form_class = CountryFeeSettingForm
    template_name = "eventyay_business/fees/form.html"

    def get_success_url(self):
        return reverse("plugins:eventyay_business:fees.list")

    def form_valid(self, form):
        messages.success(self.request, _("Country fee setting created successfully."))
        return super().form_valid(form)


class CountryFeeSettingUpdateView(AdministratorPermissionRequiredMixin, UpdateView):
    model = CountryFeeSetting
    form_class = CountryFeeSettingForm
    template_name = "eventyay_business/fees/form.html"

    def get_success_url(self):
        return reverse("plugins:eventyay_business:fees.list")

    def form_valid(self, form):
        messages.success(self.request, _("Country fee setting updated successfully."))
        return super().form_valid(form)


class CountryFeeSettingDeleteView(AdministratorPermissionRequiredMixin, DeleteView):
    model = CountryFeeSetting
    template_name = "eventyay_business/fees/delete.html"

    def get_success_url(self):
        return reverse("plugins:eventyay_business:fees.list")

    def form_valid(self, form):
        messages.success(self.request, _("Country fee setting deleted successfully."))
        return super().form_valid(form)


class GlobalBusinessSettingsView(AdministratorPermissionRequiredMixin, FormView):
    template_name = "eventyay_business/admin/business_settings.html"
    form_class = GlobalBusinessSettingsForm

    def get_invoice_form(self):
        if self.request.method == "POST":
            return InvoicePresentationSettingsForm(
                data=self.request.POST, files=self.request.FILES
            )
        return InvoicePresentationSettingsForm()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if "invoice_form" not in context:
            context["invoice_form"] = self.get_invoice_form()
        return context

    def post(self, request, *args, **kwargs):
        form = self.get_form()
        invoice_form = self.get_invoice_form()
        if form.is_valid() and invoice_form.is_valid():
            return self.forms_valid(form, invoice_form)
        return self.forms_invalid(form, invoice_form)

    def forms_valid(self, form, invoice_form):
        form.save()
        invoice_form.save()
        messages.success(self.request, _("Your changes have been saved."))
        return redirect(self.get_success_url())

    def forms_invalid(self, form, invoice_form):
        messages.error(
            self.request, _("Your changes have not been saved, see below for errors.")
        )
        return self.render_to_response(
            self.get_context_data(form=form, invoice_form=invoice_form)
        )

    def get_success_url(self):
        return reverse("plugins:eventyay_business:settings")
