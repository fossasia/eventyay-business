import logging
from decimal import Decimal
from django.contrib import messages
from django.db import transaction
from django.db.models import Q, Sum

logger = logging.getLogger(__name__)
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.timezone import now
from django.utils.translation import gettext_lazy as _
from django.views.generic import (
    CreateView,
    DetailView,
    FormView,
    ListView,
    TemplateView,
    UpdateView,
    View,
)
from eventyay.base.models import Event, Organizer, User
from eventyay.control.permissions import (
    AdministratorPermissionRequiredMixin,
    EventPermissionRequiredMixin,
    OrganizerPermissionRequiredMixin,
)
from eventyay.control.views.organizer_views.organizer_detail_view_mixin import (
    OrganizerDetailViewMixin,
)

from .capabilities import CapabilityValueType, get_all_capabilities, get_capability
from .forms import (
    AddonDefinitionForm,
    EventAddonForm,
    EventAddonPurchaseForm,
    OrganizerAddonForm,
    OrganizerAddonPurchaseForm,
    SubscriptionAdminForm,
    TierEntitlementFormSet,
    TierForm,
    TierPriceFormSet,
)
from .models import (
    AddonAssignmentScope,
    AddonDefinition,
    AddonStatus,
    BillingInterval,
    EventAddon,
    OrganizerAddon,
    Subscription,
    SubscriptionStatus,
    Tier,
    TierPrice,
    TierStatus,
    TierVersion,
    UsageRecord,
)
from .services import (
    invalidate_entitlement_cache,
    log_addon_lifecycle_action,
    migrate_addon_assignments,
    migrate_tier_subscribers,
)
from .signals import (
    addon_canceled,
    subscription_downgraded,
    subscription_purchased,
)
from .stripe_service import (
    create_addon_checkout_session,
    create_subscription_checkout_session,
    get_stripe_secret_key_safe,
    is_stripe_configured,
    sync_tier_price_to_stripe,
)


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
        # Create the initial draft TierVersion
        TierVersion.objects.create(
            tier=self.object, version=1, created_by=self.request.user
        )
        messages.success(
            self.request,
            _("Tier created successfully. You can now add prices and entitlements."),
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

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if self.request.POST:
            context["price_formset"] = TierPriceFormSet(
                self.request.POST, instance=self.latest_version
            )
            context["entitlement_formset"] = TierEntitlementFormSet(
                self.request.POST, instance=self.latest_version
            )
        else:
            context["price_formset"] = TierPriceFormSet(instance=self.latest_version)
            context["entitlement_formset"] = TierEntitlementFormSet(
                instance=self.latest_version
            )
        return context

    @transaction.atomic
    def form_valid(self, form):
        context = self.get_context_data()
        price_formset = context["price_formset"]
        entitlement_formset = context["entitlement_formset"]

        if (
            form.is_valid()
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
            tier=tier, version=latest_version.version + 1, created_by=request.user
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
                    "is_current": is_current,
                }
            )

        ctx["tier_list"] = tier_list
        return ctx

    def post(self, request, *args, **kwargs):
        tier_price_id = request.POST.get("tier_price_id")
        if not tier_price_id:
            messages.error(request, _("Please select a plan."))
            return redirect(
                "plugins:eventyay_business:organizer.plan.upgrade",
                organizer=request.organizer.slug,
            )

        tier_price = get_object_or_404(
            TierPrice.objects.select_related("tier_version__tier"),
            pk=tier_price_id,
            active=True,
            tier_version__tier__status=TierStatus.PUBLISHED,
        )

        organizer = request.organizer
        amount = getattr(tier_price, "amount", None)
        if amount is None:
            amount = getattr(tier_price, "price", Decimal("0.00"))

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
            target_tier = tier_price.tier_version.tier

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
                if active_sub.billing_interval != tier_price.billing_interval:
                    norm_target = (
                        amount / Decimal("12")
                        if tier_price.billing_interval
                        in ("annual", "year", BillingInterval.ANNUAL)
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
            for ent in tier_price.tier_version.entitlements.all():
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
                            "tier": tier_price.tier_version.tier.name,
                        },
                    )

            # If Stripe subscription is linked, reconcile with Stripe before committing local state
            if active_sub.stripe_subscription_id and is_stripe_configured():
                try:
                    import stripe

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
                                tier_price.stripe_price_id
                                or sync_tier_price_to_stripe(tier_price)
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
                    sub_locked.pending_tier_version = tier_price.tier_version
                    sub_locked.pending_billing_interval = tier_price.billing_interval
                    sub_locked.pending_change_at = sub_locked.ends_at
                    sub_locked.save(
                        update_fields=[
                            "pending_tier_version",
                            "pending_billing_interval",
                            "pending_change_at",
                            "updated_at",
                        ]
                    )
                    is_immediate = False
                else:
                    sub_locked.tier_version = tier_price.tier_version
                    sub_locked.billing_interval = tier_price.billing_interval
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
                    % {"tier": tier_price.tier_version.tier.name},
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
                        "tier": tier_price.tier_version.tier.name,
                        "renewal": renewal_str,
                    },
                )
            return redirect(
                "plugins:eventyay_business:organizer.plan",
                organizer=organizer.slug,
            )

        if amount > 0 and is_stripe_configured():
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
            except Exception as exc:
                logger.exception(
                    "Failed to create Stripe checkout session for tier upgrade: %s",
                    exc,
                )
                messages.error(
                    request,
                    _("Payment provider error: %(error)s") % {"error": str(exc)},
                )
            return redirect(
                "plugins:eventyay_business:organizer.plan.upgrade",
                organizer=organizer.slug,
            )

        # Free tier or offline switch
        with transaction.atomic():
            Organizer.objects.select_for_update().get(pk=organizer.pk)
            active_sub = (
                Subscription.objects.select_for_update()
                .filter(organizer=organizer, status=SubscriptionStatus.ACTIVE)
                .first()
            )
            if active_sub:
                active_sub.tier_version = tier_price.tier_version
                active_sub.billing_interval = tier_price.billing_interval
                active_sub.currency = tier_price.currency
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
                    tier_version=tier_price.tier_version,
                    status=SubscriptionStatus.ACTIVE,
                    billing_interval=tier_price.billing_interval,
                    currency=tier_price.currency,
                    starts_at=now(),
                )
            invalidate_entitlement_cache(organizer=organizer)
            subscription_purchased.send(
                sender=Subscription, instance=sub, user=request.user
            )

        messages.success(
            request,
            _("Successfully updated your plan to %(name)s.")
            % {"name": tier_price.tier_version.tier.name},
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
                import stripe

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
                self.addon.price and self.addon.price > 0 and is_stripe_configured()
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
                self.addon.price and self.addon.price > 0 and is_stripe_configured()
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

    def form_valid(self, form):
        self.object = form.save()
        messages.success(self.request, _("Add-on created successfully."))
        return redirect("plugins:eventyay_business:addons.list")


class AddonDefinitionUpdateView(AdministratorPermissionRequiredMixin, UpdateView):
    model = AddonDefinition
    form_class = AddonDefinitionForm
    template_name = "eventyay_business/addons/form.html"

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
