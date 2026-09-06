from django.urls import path

from . import views

urlpatterns = [
    path("admin/global/business/tiers/", views.TierListView.as_view(), name="tiers.list"),
    path("admin/global/business/tiers/create/", views.TierCreateView.as_view(), name="tiers.create"),
    path("admin/global/business/tiers/<int:pk>/", views.TierDetailView.as_view(), name="tiers.detail"),
    path("admin/global/business/tiers/<int:pk>/edit/", views.TierUpdateView.as_view(), name="tiers.edit"),
    path("admin/global/business/tiers/<int:pk>/draft/", views.TierNewDraftView.as_view(), name="tiers.draft"),
    path("admin/global/business/tiers/<int:pk>/publish/", views.TierPublishView.as_view(), name="tiers.publish"),
    path("admin/global/business/tiers/<int:pk>/archive/", views.TierArchiveView.as_view(), name="tiers.archive"),
    
    path("admin/global/business/subscriptions/", views.SubscriptionListView.as_view(), name="subscriptions.list"),
    path("admin/global/business/subscriptions/create/", views.SubscriptionCreateView.as_view(), name="subscriptions.create"),
    path("admin/global/business/subscriptions/<int:pk>/edit/", views.SubscriptionUpdateView.as_view(), name="subscriptions.edit"),
]
