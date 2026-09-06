from django import forms
from django.forms import inlineformset_factory
from django.utils.translation import gettext_lazy as _

from .capabilities import get_capability_choices
from .models import Tier, TierEntitlement, TierPrice, TierVersion


class TierForm(forms.ModelForm):
    class Meta:
        model = Tier
        fields = ["name", "slug", "description", "is_public", "display_order"]


class TierVersionForm(forms.ModelForm):
    class Meta:
        model = TierVersion
        fields = []  # No fields editable directly on version in this form


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
        choices = [("", "---------")] + get_capability_choices()
        if self.instance and self.instance.capability:
            existing_caps = [c[0] for c in choices]
            if self.instance.capability not in existing_caps:
                choices.append((self.instance.capability, self.instance.capability))
        self.fields["capability"].widget = forms.Select(choices=choices)

    def clean(self):
        cleaned_data = super().clean()
        capability_name = cleaned_data.get("capability")
        value = cleaned_data.get("value")

        if capability_name and value:
            from .capabilities import CapabilityValueType, get_capability

            cap = get_capability(capability_name)
            if cap:
                if cap.value_type == CapabilityValueType.INTEGER:
                    try:
                        int(value)
                    except ValueError:
                        self.add_error(
                            "value",
                            forms.ValidationError(
                                _("Value must be a valid whole number (integer).")
                            ),
                        )
                elif cap.value_type in (CapabilityValueType.DECIMAL, CapabilityValueType.MONEY):
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
                elif (
                    cap.value_type == CapabilityValueType.BOOLEAN
                    and value.lower() not in ("true", "false", "1", "0", "yes", "no")
                ):
                    self.add_error(
                        "value",
                        forms.ValidationError(
                            _("Value must be a boolean (e.g. 1, 0, true, false).")
                        ),
                    )
        return cleaned_data


TierPriceFormSet = inlineformset_factory(
    TierVersion,
    TierPrice,
    fields=["billing_interval", "currency", "amount", "stripe_price_id", "active"],
    extra=1,
    can_delete=True,
)


TierEntitlementFormSet = inlineformset_factory(
    TierVersion,
    TierEntitlement,
    form=TierEntitlementForm,
    fields=["capability", "value", "unit", "overage_allowed", "overage_price", "currency", "overage_block_size"],
    extra=1,
    can_delete=True,
)
