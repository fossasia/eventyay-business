from django.views.generic import TemplateView
from eventyay.control.permissions import AdministratorPermissionRequiredMixin


class TierListView(AdministratorPermissionRequiredMixin, TemplateView):
    template_name = "eventyay_business/tiers/list.html"
