from django import forms
from django.forms import inlineformset_factory

from .models import Tier, TierEntitlement, TierPrice, TierVersion


class TierForm(forms.ModelForm):
    class Meta:
        model = Tier
        fields = ["name", "slug", "description", "is_public", "display_order"]


class TierVersionForm(forms.ModelForm):
    class Meta:
        model = TierVersion
        fields = []  # No fields editable directly on version in this form


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
    fields=["capability", "value", "unit", "overage_allowed", "overage_price", "currency", "overage_block_size"],
    extra=1,
    can_delete=True,
)
