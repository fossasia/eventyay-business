from django.urls import path

from . import views

urlpatterns = [
    path(
        "admin/global/business/tiers/", views.TierListView.as_view(), name="tiers.list"
    ),
]
