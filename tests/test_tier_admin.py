import pytest
from django.urls import reverse
from eventyay_business.models import Tier, TierVersion, TierStatus

@pytest.fixture
def business_admin_client(admin_client):
    # Depending on Eventyay setup, admin_client might just work.
    return admin_client

@pytest.fixture
def sample_tier():
    tier = Tier.objects.create(name="Pro", slug="pro", status=TierStatus.DRAFT)
    TierVersion.objects.create(tier=tier, version=1)
    return tier

@pytest.mark.django_db
def test_tier_list_view(business_admin_client, sample_tier):
    url = reverse("plugins:eventyay_business:tiers.list")
    response = business_admin_client.get(url)
    assert response.status_code == 200
    assert "Pro" in response.content.decode()

@pytest.mark.django_db
def test_tier_create_view(business_admin_client):
    url = reverse("plugins:eventyay_business:tiers.create")
    response = business_admin_client.post(url, {
        "name": "Enterprise",
        "slug": "enterprise",
        "description": "Top tier",
        "is_public": "on",
        "display_order": "1"
    })
    assert response.status_code == 302
    
    tier = Tier.objects.get(slug="enterprise")
    assert tier.versions.count() == 1
    version = tier.versions.first()
    assert version.version == 1
    assert version.published_at is None

@pytest.mark.django_db
def test_tier_new_draft_view(business_admin_client, sample_tier):
    # Publish first version
    v1 = sample_tier.versions.first()
    v1.published_at = "2024-01-01T00:00:00Z"
    v1.save()
    sample_tier.status = TierStatus.PUBLISHED
    sample_tier.save()

    url = reverse("plugins:eventyay_business:tiers.draft", kwargs={"pk": sample_tier.pk})
    response = business_admin_client.post(url)
    assert response.status_code == 302
    
    assert sample_tier.versions.count() == 2
    v2 = sample_tier.versions.order_by("-version").first()
    assert v2.version == 2
    assert v2.published_at is None
