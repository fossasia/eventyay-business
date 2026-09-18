from django import forms
from django.forms import inlineformset_factory
from django.utils.translation import gettext_lazy as _

from .capabilities import get_grouped_capability_choices
from .models import (
    AddonDefinition,
    CountryFeeSetting,
    EventAddon,
    OrganizerAddon,
    Subscription,
    SubscriptionStatus,
    Tier,
    TierEntitlement,
    TierPrice,
    TierVersion,
)

try:
    from eventyay.base.forms.widgets import SplitDateTimePickerWidget
    from eventyay.control.forms import SplitDateTimeField
except ImportError:
    from django.forms import (
        SplitDateTimeField,
        SplitDateTimeWidget as SplitDateTimePickerWidget,
    )


class TierForm(forms.ModelForm):
    class Meta:
        model = Tier
        fields = ["name", "slug", "description", "is_public", "display_order"]


class TierVersionForm(forms.ModelForm):
    grace_period_days = forms.IntegerField(
        label=_("Grace period (days)"),
        required=False,
        min_value=0,
        help_text=_(
            "Override the grace period duration in days for subscriptions on this tier version. "
            "Leave empty to use global default."
        ),
    )

    class Meta:
        model = TierVersion
        fields = []  # No model fields editable directly on version in this form

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk and self.instance.configuration_snapshot:
            val = self.instance.configuration_snapshot.get("grace_period_days")
            if val is not None:
                self.fields["grace_period_days"].initial = val

    def save(self, commit=True):
        instance = super().save(commit=False)
        snapshot = dict(instance.configuration_snapshot or {})
        grace_days = self.cleaned_data.get("grace_period_days")
        if grace_days is not None:
            snapshot["grace_period_days"] = grace_days
        else:
            snapshot.pop("grace_period_days", None)
        instance.configuration_snapshot = snapshot
        if commit:
            instance.save()
            self.save_m2m()
        return instance


class TierEntitlementForm(forms.ModelForm):
    class Meta:
        model = TierEntitlement
        fields = [
            "capability",
            "value",
            "unit",
            "overage_allowed",
            "overage_price",
            "currency",
            "overage_block_size",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        grouped_choices = get_grouped_capability_choices()
        choices = [("", "---------")] + grouped_choices
        if self.instance and self.instance.capability:
            all_caps = []
            for item in choices:
                if isinstance(item[1], (list, tuple)):
                    all_caps.extend(c[0] for c in item[1])
                else:
                    all_caps.append(item[0])
            if self.instance.capability not in all_caps:
                choices.append(
                    (
                        _("Custom / Other"),
                        [(self.instance.capability, self.instance.capability)],
                    )
                )
        self.fields["capability"].widget = forms.Select(
            choices=choices,
            attrs={"class": "form-control entitlement-capability-select"},
        )
        self.fields["value"].widget.attrs.update(
            {
                "class": "form-control entitlement-value-input",
                "placeholder": _("Value / Allowance"),
            }
        )
        self.fields["unit"].widget.attrs.update(
            {
                "class": "form-control entitlement-unit-input",
                "placeholder": _("Unit (e.g. rooms, %)"),
            }
        )
        self.fields["overage_allowed"].widget.attrs.update(
            {"class": "entitlement-overage-toggle"}
        )
        self.fields["currency"].widget.attrs.update(
            {
                "class": "form-control text-uppercase",
                "placeholder": "USD",
                "maxlength": "3",
                "list": "tier-common-currencies",
            }
        )
        self.fields["overage_price"].widget.attrs.update(
            {"class": "form-control", "placeholder": "0.00", "step": "0.01"}
        )
        self.fields["overage_block_size"].widget.attrs.update(
            {"class": "form-control", "placeholder": "1"}
        )

    def clean(self):
        cleaned_data = super().clean()
        capability_name = cleaned_data.get("capability")
        value = cleaned_data.get("value")

        if capability_name:
            from .capabilities import CapabilityValueType, get_capability

            cap = get_capability(capability_name)
            if cap:
                if cap.value_type == CapabilityValueType.INTEGER:
                    if value not in (None, ""):
                        try:
                            int(value)
                        except ValueError:
                            self.add_error(
                                "value",
                                forms.ValidationError(
                                    _("Value must be a valid whole number (integer).")
                                ),
                            )
                    # Auto-fill standard unit if not provided
                    if not cleaned_data.get("unit") and cap.unit:
                        cleaned_data["unit"] = cap.unit

                elif cap.value_type in (
                    CapabilityValueType.DECIMAL,
                    CapabilityValueType.MONEY,
                ):
                    if value not in (None, ""):
                        from decimal import Decimal, InvalidOperation

                        try:
                            val = Decimal(value)
                            if not val.is_finite():
                                raise InvalidOperation
                        except (InvalidOperation, TypeError):
                            self.add_error(
                                "value",
                                forms.ValidationError(
                                    _("Value must be a valid number or decimal.")
                                ),
                            )
                    if not cleaned_data.get("unit") and cap.unit:
                        cleaned_data["unit"] = cap.unit

                elif cap.value_type == CapabilityValueType.BOOLEAN:
                    if value not in (None, ""):
                        val_str = str(value).strip().lower()
                        if val_str in ("true", "1", "yes", "included", "enabled"):
                            cleaned_data["value"] = "true"
                        elif val_str in (
                            "false",
                            "0",
                            "no",
                            "not included",
                            "disabled",
                        ):
                            cleaned_data["value"] = "false"
                        else:
                            self.add_error(
                                "value",
                                forms.ValidationError(
                                    _(
                                        "Value must be a boolean (e.g. true, false, included, disabled)."
                                    )
                                ),
                            )
                    cleaned_data["unit"] = ""
                    cleaned_data["overage_allowed"] = False
                    cleaned_data["overage_price"] = None
                    cleaned_data["currency"] = ""
                    cleaned_data["overage_block_size"] = None

        if not cleaned_data.get("overage_allowed"):
            cleaned_data["overage_price"] = None
            cleaned_data["currency"] = ""
            cleaned_data["overage_block_size"] = None

        return cleaned_data

    def clean_currency(self):
        currency = self.cleaned_data.get("currency")
        if currency:
            currency = currency.strip().upper()
            if not currency.isalpha() or len(currency) != 3:
                raise forms.ValidationError(
                    _("Please enter a valid 3-letter currency code (e.g. USD, EUR).")
                )
        return currency


class TierPriceForm(forms.ModelForm):
    class Meta:
        model = TierPrice
        fields = ["billing_interval", "currency", "amount", "stripe_price_id", "active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["billing_interval"].widget.attrs.update({"class": "form-control"})
        self.fields["currency"].widget.attrs.update(
            {
                "class": "form-control text-uppercase",
                "placeholder": "USD",
                "maxlength": "3",
                "list": "tier-common-currencies",
            }
        )
        self.fields["amount"].widget.attrs.update(
            {"class": "form-control", "placeholder": "0.00", "step": "0.01"}
        )
        self.fields["stripe_price_id"].widget.attrs.update(
            {"class": "form-control", "placeholder": "price_1..."}
        )
        if not self.instance.pk and not self.initial.get("currency"):
            from django.conf import settings

            self.fields["currency"].initial = getattr(
                settings, "DEFAULT_CURRENCY", "USD"
            )

    def clean_currency(self):
        currency = (self.cleaned_data.get("currency") or "").strip().upper()
        if not currency.isalpha() or len(currency) != 3:
            raise forms.ValidationError(
                _("Please enter a valid 3-letter currency code (e.g. USD, EUR).")
            )
        return currency


TierPriceFormSet = inlineformset_factory(
    TierVersion,
    TierPrice,
    form=TierPriceForm,
    fields=["billing_interval", "currency", "amount", "stripe_price_id", "active"],
    extra=1,
    can_delete=True,
)


TierEntitlementFormSet = inlineformset_factory(
    TierVersion,
    TierEntitlement,
    form=TierEntitlementForm,
    fields=[
        "capability",
        "value",
        "unit",
        "overage_allowed",
        "overage_price",
        "currency",
        "overage_block_size",
    ],
    extra=1,
    can_delete=True,
)


class SubscriptionAdminForm(forms.ModelForm):
    grace_period_days = forms.IntegerField(
        label=_("Grace period (days)"),
        required=False,
        min_value=0,
        help_text=_(
            "Override the grace period duration in days for this subscription. "
            "Leave empty to use tier version or global default."
        ),
    )

    class Meta:
        model = Subscription
        fields = [
            "organizer",
            "tier_version",
            "status",
            "billing_interval",
            "currency",
            "starts_at",
            "ends_at",
            "cancel_at",
            "stripe_customer_id",
            "stripe_subscription_id",
        ]
        field_classes = {
            "starts_at": SplitDateTimeField,
            "ends_at": SplitDateTimeField,
            "cancel_at": SplitDateTimeField,
        }
        widgets = {
            "starts_at": SplitDateTimePickerWidget(),
            "ends_at": SplitDateTimePickerWidget(),
            "cancel_at": SplitDateTimePickerWidget(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk and self.instance.configuration_snapshot:
            val = self.instance.configuration_snapshot.get("grace_period_days")
            if val is not None:
                self.fields["grace_period_days"].initial = val

    def clean(self):
        cleaned_data = super().clean()
        organizer = cleaned_data.get("organizer")
        status = cleaned_data.get("status")

        if organizer and status in [
            SubscriptionStatus.ACTIVE,
            SubscriptionStatus.PENDING,
        ]:
            qs = Subscription.objects.filter(
                organizer=organizer,
                status__in=[SubscriptionStatus.ACTIVE, SubscriptionStatus.PENDING],
            )
            if self.instance and self.instance.pk:
                qs = qs.exclude(pk=self.instance.pk)

            if qs.exists():
                raise forms.ValidationError(
                    _("This organizer already has an active or pending subscription.")
                )
        return cleaned_data

    def save(self, commit=True):
        instance = super().save(commit=False)
        snapshot = dict(instance.configuration_snapshot or {})
        grace_days = self.cleaned_data.get("grace_period_days")
        if grace_days is not None:
            snapshot["grace_period_days"] = grace_days
        else:
            snapshot.pop("grace_period_days", None)
        instance.configuration_snapshot = snapshot
        if commit:
            instance.save()
            self.save_m2m()
        return instance


class AddonDefinitionForm(forms.ModelForm):
    class Meta:
        model = AddonDefinition
        fields = [
            "name",
            "slug",
            "description",
            "assignment_scope",
            "pricing_mode",
            "currency",
            "price",
            "capability",
            "entitlement_value",
            "quantity",
            "active",
            "public",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        grouped_choices = get_grouped_capability_choices()
        choices = [("", "---------")] + grouped_choices
        if self.instance and self.instance.capability:
            all_caps = []
            for item in choices:
                if isinstance(item[1], (list, tuple)):
                    all_caps.extend(c[0] for c in item[1])
                else:
                    all_caps.append(item[0])
            if self.instance.capability not in all_caps:
                choices.append(
                    (
                        _("Custom / Other"),
                        [(self.instance.capability, self.instance.capability)],
                    )
                )
        self.fields["capability"].widget = forms.Select(
            choices=choices,
            attrs={"class": "form-control addon-capability-select"},
        )
        self.fields["entitlement_value"].widget.attrs.update(
            {
                "class": "form-control addon-value-input",
                "placeholder": _("Value / Allowance"),
            }
        )
        self.fields["currency"].widget.attrs.update(
            {
                "class": "form-control text-uppercase",
                "placeholder": "USD",
                "maxlength": "3",
                "list": "tier-common-currencies",
            }
        )
        if not self.instance.pk and not self.initial.get("currency"):
            from django.conf import settings

            self.fields["currency"].initial = getattr(
                settings, "DEFAULT_CURRENCY", "USD"
            )

        if self.instance and self.instance.pk:
            from .models import AddonStatus, EventAddon, OrganizerAddon

            active_org_count = OrganizerAddon.objects.filter(
                addon=self.instance, status=AddonStatus.ACTIVE
            ).count()
            active_event_count = EventAddon.objects.filter(
                addon=self.instance, status=AddonStatus.ACTIVE
            ).count()
            total_active = active_org_count + active_event_count

            self.fields["update_existing_assignments"] = forms.BooleanField(
                required=False,
                label=_("Update all existing active assignments"),
                help_text=_(
                    "If checked, %(count)d active assignment(s) will be updated to match the new "
                    "capability, allowance, and price immediately. If unchecked, existing assignments "
                    "remain grandfathered."
                )
                % {"count": total_active},
            )

    def clean_currency(self):
        currency = self.cleaned_data.get("currency")
        if currency:
            currency = currency.strip().upper()
            if not currency.isalpha() or len(currency) != 3:
                raise forms.ValidationError(
                    _("Please enter a valid 3-letter currency code (e.g. USD, EUR).")
                )
        return currency

    def clean(self):
        cleaned_data = super().clean()
        capability_name = cleaned_data.get("capability")
        value = cleaned_data.get("entitlement_value")

        if capability_name:
            from .capabilities import CapabilityValueType, get_capability

            cap = get_capability(capability_name)
            is_existing_unchanged = (
                self.instance
                and self.instance.pk
                and self.instance.capability == capability_name
            )
            if not cap and not is_existing_unchanged:
                self.add_error(
                    "capability",
                    forms.ValidationError(
                        _("Unknown capability: %(name)s"),
                        params={"name": capability_name},
                    ),
                )
            elif cap and value:
                if cap.value_type == CapabilityValueType.INTEGER:
                    try:
                        int(value)
                    except ValueError:
                        self.add_error(
                            "entitlement_value",
                            forms.ValidationError(
                                _("Value must be a valid whole number (integer).")
                            ),
                        )
                elif cap.value_type in (
                    CapabilityValueType.DECIMAL,
                    CapabilityValueType.MONEY,
                ):
                    from decimal import Decimal, InvalidOperation

                    try:
                        val = Decimal(value)
                        if not val.is_finite():
                            raise InvalidOperation
                    except (InvalidOperation, TypeError):
                        self.add_error(
                            "entitlement_value",
                            forms.ValidationError(
                                _("Value must be a valid number or decimal.")
                            ),
                        )
                elif cap.value_type == CapabilityValueType.BOOLEAN:
                    if value not in (None, ""):
                        val_str = str(value).strip().lower()
                        if val_str in ("true", "1", "yes", "included", "enabled"):
                            cleaned_data["entitlement_value"] = "true"
                        elif val_str in (
                            "false",
                            "0",
                            "no",
                            "not included",
                            "disabled",
                        ):
                            cleaned_data["entitlement_value"] = "false"
                        else:
                            self.add_error(
                                "entitlement_value",
                                forms.ValidationError(
                                    _(
                                        "Value must be a boolean (e.g. true, false, included, disabled)."
                                    )
                                ),
                            )
        return cleaned_data


class OrganizerAddonForm(forms.ModelForm):
    class Meta:
        model = OrganizerAddon
        fields = ["organizer", "addon", "quantity", "starts_at", "ends_at", "status"]
        field_classes = {
            "starts_at": SplitDateTimeField,
            "ends_at": SplitDateTimeField,
        }
        widgets = {
            "starts_at": SplitDateTimePickerWidget(),
            "ends_at": SplitDateTimePickerWidget(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from .models import AddonAssignmentScope, AddonDefinition

        qs = AddonDefinition.objects.filter(
            assignment_scope=AddonAssignmentScope.ORGANIZER
        )
        if self.instance and self.instance.pk and self.instance.addon_id:
            qs = qs | AddonDefinition.objects.filter(pk=self.instance.addon_id)
        self.fields["addon"].queryset = qs.distinct()


class EventAddonForm(forms.ModelForm):
    class Meta:
        model = EventAddon
        fields = ["event", "addon", "quantity", "starts_at", "ends_at", "status"]
        field_classes = {
            "starts_at": SplitDateTimeField,
            "ends_at": SplitDateTimeField,
        }
        widgets = {
            "starts_at": SplitDateTimePickerWidget(),
            "ends_at": SplitDateTimePickerWidget(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from .models import AddonAssignmentScope, AddonDefinition

        qs = AddonDefinition.objects.filter(assignment_scope=AddonAssignmentScope.EVENT)
        if self.instance and self.instance.pk and self.instance.addon_id:
            qs = qs | AddonDefinition.objects.filter(pk=self.instance.addon_id)
        self.fields["addon"].queryset = qs.distinct()


class OrganizerAddonPurchaseForm(forms.Form):
    quantity = forms.IntegerField(
        min_value=1,
        initial=1,
        label=_("Quantity"),
        help_text=_("Number of units to activate."),
    )
    event = forms.ModelChoiceField(
        queryset=None,
        required=False,
        label=_("Target Event"),
        help_text=_("Select which event this add-on applies to."),
    )

    def __init__(self, *args, organizer=None, addon=None, **kwargs):
        self.organizer = organizer
        self.addon = addon
        super().__init__(*args, **kwargs)

        if self.addon:
            if self.addon.quantity:
                self.fields["quantity"].initial = self.addon.quantity

            from .capabilities import CapabilityValueType, get_capability
            from .models import AddonAssignmentScope

            cap = get_capability(self.addon.capability)
            if cap and cap.value_type == CapabilityValueType.BOOLEAN:
                self.fields["quantity"].initial = 1
                self.fields["quantity"].widget = forms.HiddenInput()

            if self.addon.assignment_scope == AddonAssignmentScope.EVENT:
                self.fields["event"].required = True
                if self.organizer:
                    self.fields["event"].queryset = self.organizer.events.filter(
                        live=True
                    ).order_by("name")
                else:
                    from eventyay.base.models import Event

                    self.fields["event"].queryset = Event.objects.none()
            else:
                self.fields["event"].widget = forms.HiddenInput()
                self.fields["event"].required = False

    def clean(self):
        cleaned_data = super().clean()
        from .capabilities import CapabilityValueType, get_capability
        from .models import (
            AddonAssignmentScope,
            AddonStatus,
            EventAddon,
            OrganizerAddon,
        )

        if not self.addon or not self.addon.active or not self.addon.public:
            raise forms.ValidationError(
                _("This add-on is currently unavailable for purchase.")
            )

        cap = get_capability(self.addon.capability)

        if self.addon.assignment_scope == AddonAssignmentScope.EVENT:
            event = cleaned_data.get("event")
            if not event:
                raise forms.ValidationError(
                    _("Please select an event for this add-on.")
                )
            if self.organizer and event.organizer_id != self.organizer.id:
                raise forms.ValidationError(_("Invalid event selected."))

            # Prevent duplicate active boolean add-on for the same event
            is_boolean = (cap and cap.value_type == CapabilityValueType.BOOLEAN) or (
                self.addon.entitlement_value
                and str(self.addon.entitlement_value).lower() in ("true", "1")
            )
            if is_boolean:
                if EventAddon.objects.filter(
                    event=event,
                    capability=self.addon.capability,
                    status=AddonStatus.ACTIVE,
                ).exists():
                    raise forms.ValidationError(
                        _("This add-on capability is already active for %(event)s.")
                        % {"event": event.name}
                    )
        else:
            is_boolean = (cap and cap.value_type == CapabilityValueType.BOOLEAN) or (
                self.addon.entitlement_value
                and str(self.addon.entitlement_value).lower() in ("true", "1")
            )
            if is_boolean:
                if OrganizerAddon.objects.filter(
                    organizer=self.organizer,
                    capability=self.addon.capability,
                    status=AddonStatus.ACTIVE,
                ).exists():
                    raise forms.ValidationError(
                        _(
                            "This add-on capability is already active for your organisation."
                        )
                    )

        return cleaned_data

    def save(self, commit=True, status=None):
        from django.utils.timezone import now

        from .models import (
            AddonAssignmentScope,
            AddonStatus,
            EventAddon,
            OrganizerAddon,
        )

        if status is None:
            status = AddonStatus.ACTIVE
        qty = self.cleaned_data.get("quantity") or 1

        if self.addon.assignment_scope == AddonAssignmentScope.EVENT:
            event = self.cleaned_data["event"]
            assignment = EventAddon(
                event=event,
                addon=self.addon,
                quantity=qty,
                status=status,
                starts_at=now(),
            )
        else:
            assignment = OrganizerAddon(
                organizer=self.organizer,
                addon=self.addon,
                quantity=qty,
                status=status,
                starts_at=now(),
            )
        if commit:
            assignment.save()
        return assignment


class EventAddonPurchaseForm(forms.Form):
    quantity = forms.IntegerField(
        min_value=1,
        initial=1,
        label=_("Quantity"),
        widget=forms.NumberInput(attrs={"class": "form-control", "min": 1}),
    )

    def __init__(self, *args, event=None, addon=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.event = event
        self.addon = addon

        if self.addon:
            if self.addon.quantity:
                self.fields["quantity"].initial = self.addon.quantity

            from .capabilities import CapabilityValueType, get_capability

            cap = get_capability(self.addon.capability)
            if cap and cap.value_type == CapabilityValueType.BOOLEAN:
                self.fields["quantity"].initial = 1
                self.fields["quantity"].widget = forms.HiddenInput()

    def clean(self):
        cleaned_data = super().clean()
        from .capabilities import CapabilityValueType, get_capability
        from .models import AddonAssignmentScope, AddonStatus, EventAddon

        if (
            not self.addon
            or not self.addon.active
            or not self.addon.public
            or self.addon.assignment_scope != AddonAssignmentScope.EVENT
        ):
            raise forms.ValidationError(
                _("This add-on is currently unavailable for this event.")
            )

        cap = get_capability(self.addon.capability)
        is_boolean = (cap and cap.value_type == CapabilityValueType.BOOLEAN) or (
            self.addon.entitlement_value
            and str(self.addon.entitlement_value).lower() in ("true", "1")
        )
        if is_boolean:
            if EventAddon.objects.filter(
                event=self.event,
                capability=self.addon.capability,
                status=AddonStatus.ACTIVE,
            ).exists():
                raise forms.ValidationError(
                    _("This add-on capability is already active for %(event)s.")
                    % {"event": self.event.name}
                )

        return cleaned_data

    def save(self, commit=True, status=None):
        from django.utils.timezone import now

        from .models import AddonStatus, EventAddon

        if status is None:
            status = AddonStatus.ACTIVE
        qty = self.cleaned_data.get("quantity") or 1
        assignment = EventAddon(
            event=self.event,
            addon=self.addon,
            quantity=qty,
            status=status,
            starts_at=now(),
        )
        if commit:
            assignment.save()
        return assignment


class CountryFeeSettingForm(forms.ModelForm):
    class Meta:
        model = CountryFeeSetting
        fields = ["country", "currency", "service_fee_percent", "maximum_fee"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from django.conf import settings

        self.fields["country"].widget.attrs.update({"class": "form-control"})
        if hasattr(settings, "CURRENCIES") and settings.CURRENCIES:
            currency_choices = [("", "---------")] + [
                (c.alpha_3, f"{c.alpha_3} - {c.name}") for c in settings.CURRENCIES
            ]
            self.fields["currency"].widget = forms.Select(
                choices=currency_choices, attrs={"class": "form-control"}
            )
        else:
            self.fields["currency"].widget = forms.TextInput(
                attrs={"class": "form-control", "maxlength": "3"}
            )

        self.fields["service_fee_percent"].widget.attrs.update(
            {"class": "form-control", "step": "0.01", "min": "0", "max": "100"}
        )
        self.fields["maximum_fee"].widget.attrs.update(
            {"class": "form-control", "step": "0.01", "min": "0"}
        )

    def clean_currency(self):
        currency = (self.cleaned_data.get("currency") or "").strip().upper()
        if not currency.isalpha() or len(currency) != 3:
            raise forms.ValidationError(
                _("Please enter a valid 3-letter currency code (e.g. USD, EUR).")
            )
        return currency
