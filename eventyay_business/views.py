from django.contrib import messages
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.timezone import now
from django.utils.translation import gettext_lazy as _
from django.views.generic import (
    CreateView,
    DetailView,
    ListView,
    UpdateView,
    View,
)
from eventyay.control.permissions import AdministratorPermissionRequiredMixin

from .forms import TierEntitlementFormSet, TierForm, TierPriceFormSet
from .models import Tier, TierStatus, TierVersion


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
            tier=self.object,
            version=1,
            created_by=self.request.user
        )
        messages.success(self.request, _("Tier created successfully. You can now add prices and entitlements."))
        return redirect(reverse("plugins:eventyay_business:tiers.edit", kwargs={"pk": self.object.pk}))


class TierUpdateView(AdministratorPermissionRequiredMixin, UpdateView):
    model = Tier
    form_class = TierForm
    template_name = "eventyay_business/tiers/form.html"

    def dispatch(self, request, *args, **kwargs):
        self.object = self.get_object()
        self.latest_version = self.object.versions.first()
        
        if not self.latest_version:
            self.latest_version = TierVersion.objects.create(
                tier=self.object,
                version=1,
                created_by=request.user
            )
            
        # If the latest version is published, redirect to detail view or duplicate prompt
        if self.latest_version and self.latest_version.published_at:
            messages.info(
                request, 
                _("This tier is published. To edit prices or entitlements, please create a new draft version.")
            )
            return redirect(reverse("plugins:eventyay_business:tiers.detail", kwargs={"pk": self.object.pk}))
            
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if self.request.POST:
            context["price_formset"] = TierPriceFormSet(self.request.POST, instance=self.latest_version)
            context["entitlement_formset"] = TierEntitlementFormSet(self.request.POST, instance=self.latest_version)
        else:
            context["price_formset"] = TierPriceFormSet(instance=self.latest_version)
            context["entitlement_formset"] = TierEntitlementFormSet(instance=self.latest_version)
        return context

    @transaction.atomic
    def form_valid(self, form):
        context = self.get_context_data()
        price_formset = context["price_formset"]
        entitlement_formset = context["entitlement_formset"]

        if form.is_valid() and price_formset.is_valid() and entitlement_formset.is_valid():
            latest_version = TierVersion.objects.select_for_update().filter(pk=self.latest_version.pk).first()
            if latest_version and latest_version.published_at:
                messages.error(self.request, _("Cannot save edits: this version was published concurrently."))
                return redirect(reverse("plugins:eventyay_business:tiers.detail", kwargs={"pk": self.object.pk}))

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
        context["latest_version"] = self.object.versions.first()
        return context


class TierNewDraftView(AdministratorPermissionRequiredMixin, View):
    """Creates a new DRAFT TierVersion from the latest PUBLISHED version."""
    
    @transaction.atomic
    def post(self, request, *args, **kwargs):
        tier = get_object_or_404(Tier.objects.select_for_update(), pk=kwargs.get("pk"))
        latest_version = tier.versions.first()
        
        if not latest_version or not latest_version.published_at:
            messages.error(request, _("Cannot create a new draft: there is already an unpublished draft."))
            return redirect(reverse("plugins:eventyay_business:tiers.edit", kwargs={"pk": tier.pk}))
            
        # Duplicate version
        new_version = TierVersion.objects.create(
            tier=tier,
            version=latest_version.version + 1,
            created_by=request.user
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
            
        messages.success(request, _("New draft version created. You can now make changes."))
        return redirect(reverse("plugins:eventyay_business:tiers.edit", kwargs={"pk": tier.pk}))


class TierPublishView(AdministratorPermissionRequiredMixin, View):
    @transaction.atomic
    def post(self, request, *args, **kwargs):
        tier = get_object_or_404(Tier, pk=kwargs.get("pk"))
        latest_version = tier.versions.first()
        
        if tier.status == TierStatus.ARCHIVED:
            messages.error(request, _("Cannot publish a draft for an archived tier. Unarchive it first."))
            return redirect(reverse("plugins:eventyay_business:tiers.list"))

        if latest_version and not latest_version.published_at:
            latest_version.published_at = now()
            latest_version.save()
            
            if tier.status == TierStatus.DRAFT:
                tier.status = TierStatus.PUBLISHED
                tier.save()
                
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


from .models import Subscription
from .forms import SubscriptionAdminForm

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

from eventyay.control.permissions import OrganizerPermissionRequiredMixin
from eventyay.control.views.organizer_views.organizer_detail_view_mixin import OrganizerDetailViewMixin
from django.views.generic import TemplateView
from .capabilities import get_all_capabilities

class OrganizerPlanView(OrganizerPermissionRequiredMixin, OrganizerDetailViewMixin, TemplateView):
    permission = "can_change_organizer_settings"
    template_name = "eventyay_business/organizer/plan.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        organizer = self.request.organizer
        from .models import Subscription
        
        from django.utils.timezone import now
        current_time = now()
        sub = Subscription.objects.filter(
            organizer=organizer,
            status="active",
            starts_at__lte=current_time,
        ).exclude(
            ends_at__lt=current_time
        ).select_related('tier_version__tier').first()
        
        ctx['subscription'] = sub
        
        # Build a complete picture of effective entitlements
        effective_entitlements = []
        all_caps = get_all_capabilities()
        
        override_dict = {}
        if sub and sub.tier_version:
            for ent in sub.tier_version.entitlements.all():
                override_dict[ent.capability] = ent.get_typed_value()
                
        for cap in all_caps:
            val = override_dict.get(cap.name, cap.default_value)
            effective_entitlements.append({
                'capability': cap,
                'effective_value': val,
                'is_overridden': cap.name in override_dict
            })
            
        ctx['effective_entitlements'] = sorted(effective_entitlements, key=lambda x: x['capability'].category)
        return ctx
