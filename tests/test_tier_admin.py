import pytest
from django.urls import reverse
from django.utils.timezone import now
from eventyay.base.models import Organizer

from eventyay_business.models import (
    Subscription,
    SubscriptionStatus,
    Tier,
    TierStatus,
    TierVersion,
)
from eventyay_business.services import migrate_tier_subscribers


@pytest.fixture
def business_admin_client(admin_client, admin_user):
    from eventyay.base.models.auth import StaffSession

    session = admin_client.session
    session.save()
    StaffSession.objects.create(
        user=admin_user,
        session_key=session.session_key,
        comment="test",
    )
    # Ensure user has is_staff = True (pytest-django's admin_user might only have is_superuser)
    admin_user.is_staff = True
    admin_user.save()
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
    response = business_admin_client.post(
        url,
        {
            "name": "Enterprise",
            "slug": "enterprise",
            "description": "Top tier",
            "is_public": "on",
            "display_order": "1",
        },
    )
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

    url = reverse(
        "plugins:eventyay_business:tiers.draft", kwargs={"pk": sample_tier.pk}
    )
    response = business_admin_client.post(url)
    assert response.status_code == 302

    assert sample_tier.versions.count() == 2
    v2 = sample_tier.versions.order_by("-version").first()
    assert v2.version == 2
    assert v2.published_at is None


@pytest.mark.django_db
def test_migrate_tier_subscribers_service():
    tier = Tier.objects.create(
        name="Pro", slug="pro-migrate", status=TierStatus.PUBLISHED
    )
    v1 = TierVersion.objects.create(tier=tier, version=1, published_at=now())
    v2 = TierVersion.objects.create(tier=tier, version=2, published_at=now())

    org1 = Organizer.objects.create(name="Org 1", slug="org-1")
    Subscription.objects.filter(organizer=org1).delete()
    org2 = Organizer.objects.create(name="Org 2", slug="org-2")
    Subscription.objects.filter(organizer=org2).delete()

    sub_active = Subscription.objects.create(
        organizer=org1,
        tier_version=v1,
        status=SubscriptionStatus.ACTIVE,
        starts_at=now(),
    )
    sub_canceled = Subscription.objects.create(
        organizer=org2,
        tier_version=v1,
        status=SubscriptionStatus.CANCELED,
        starts_at=now(),
    )

    migrated_count = migrate_tier_subscribers(tier, v2)
    assert migrated_count == 1

    sub_active.refresh_from_db()
    sub_canceled.refresh_from_db()

    assert sub_active.tier_version == v2
    assert sub_canceled.tier_version == v1


@pytest.mark.django_db
def test_migrate_tier_subscribers_validation():
    tier1 = Tier.objects.create(
        name="Pro", slug="pro-val-1", status=TierStatus.PUBLISHED
    )
    TierVersion.objects.create(tier=tier1, version=1, published_at=now())
    v2_tier1 = TierVersion.objects.create(tier=tier1, version=2, published_at=now())

    tier2 = Tier.objects.create(
        name="Enterprise", slug="ent-val-2", status=TierStatus.PUBLISHED
    )
    v1_tier2 = TierVersion.objects.create(tier=tier2, version=1, published_at=now())

    with pytest.raises(
        ValueError, match="Target version does not belong to the specified tier."
    ):
        migrate_tier_subscribers(tier1, v1_tier2)

    with pytest.raises(
        ValueError, match="From version does not belong to the specified tier."
    ):
        migrate_tier_subscribers(tier1, v2_tier1, from_version=v1_tier2)


@pytest.mark.django_db
def test_migrate_tier_subscribers_with_from_version():
    tier = Tier.objects.create(
        name="Pro", slug="pro-scoped", status=TierStatus.PUBLISHED
    )
    v1 = TierVersion.objects.create(tier=tier, version=1, published_at=now())
    v2 = TierVersion.objects.create(tier=tier, version=2, published_at=now())
    v3 = TierVersion.objects.create(tier=tier, version=3, published_at=now())

    other_tier = Tier.objects.create(
        name="Basic", slug="basic-other", status=TierStatus.PUBLISHED
    )
    v1_other = TierVersion.objects.create(
        tier=other_tier, version=1, published_at=now()
    )

    org1 = Organizer.objects.create(name="Org 1", slug="org-sc-1")
    Subscription.objects.filter(organizer=org1).delete()
    org2 = Organizer.objects.create(name="Org 2", slug="org-sc-2")
    Subscription.objects.filter(organizer=org2).delete()
    org_other = Organizer.objects.create(name="Org Other", slug="org-sc-other")
    Subscription.objects.filter(organizer=org_other).delete()

    sub_v1 = Subscription.objects.create(
        organizer=org1,
        tier_version=v1,
        status=SubscriptionStatus.ACTIVE,
        starts_at=now(),
    )
    sub_v2 = Subscription.objects.create(
        organizer=org2,
        tier_version=v2,
        status=SubscriptionStatus.ACTIVE,
        starts_at=now(),
    )
    sub_other = Subscription.objects.create(
        organizer=org_other,
        tier_version=v1_other,
        status=SubscriptionStatus.ACTIVE,
        starts_at=now(),
    )

    migrated_count = migrate_tier_subscribers(tier, v3, from_version=v1)
    assert migrated_count == 1

    sub_v1.refresh_from_db()
    sub_v2.refresh_from_db()
    sub_other.refresh_from_db()

    assert sub_v1.tier_version == v3
    assert sub_v2.tier_version == v2
    assert sub_other.tier_version == v1_other


@pytest.mark.django_db
def test_tier_detail_view_shows_migrate_checkbox(business_admin_client, sample_tier):
    v1 = sample_tier.versions.first()
    v1.published_at = now()
    v1.save()
    sample_tier.status = TierStatus.PUBLISHED
    sample_tier.save()

    # Create draft v2
    TierVersion.objects.create(tier=sample_tier, version=2)

    org = Organizer.objects.create(name="Org Subscriber", slug="org-sub")
    Subscription.objects.filter(organizer=org).delete()
    Subscription.objects.create(
        organizer=org,
        tier_version=v1,
        status=SubscriptionStatus.ACTIVE,
        starts_at=now(),
    )

    url = reverse(
        "plugins:eventyay_business:tiers.detail", kwargs={"pk": sample_tier.pk}
    )
    response = business_admin_client.get(url)
    assert response.status_code == 200
    content = response.content.decode()
    assert "migrate_subscribers" in content
    assert "Migrate 1 existing subscriber to v2" in content


@pytest.mark.django_db
def test_tier_publish_view_with_migrate_subscribers(business_admin_client, sample_tier):
    v1 = sample_tier.versions.first()
    v1.published_at = now()
    v1.save()
    sample_tier.status = TierStatus.PUBLISHED
    sample_tier.save()

    v2 = TierVersion.objects.create(tier=sample_tier, version=2)

    org = Organizer.objects.create(name="Org To Migrate", slug="org-migrate")
    Subscription.objects.filter(organizer=org).delete()
    sub = Subscription.objects.create(
        organizer=org,
        tier_version=v1,
        status=SubscriptionStatus.ACTIVE,
        starts_at=now(),
    )

    url = reverse(
        "plugins:eventyay_business:tiers.publish", kwargs={"pk": sample_tier.pk}
    )
    response = business_admin_client.post(
        url, {"migrate_subscribers": "1"}, follow=True
    )
    assert response.status_code == 200

    v2.refresh_from_db()
    assert v2.published_at is not None

    sub.refresh_from_db()
    assert sub.tier_version == v2


@pytest.mark.django_db
def test_tier_publish_view_without_migrate_subscribers(
    business_admin_client, sample_tier
):
    v1 = sample_tier.versions.first()
    v1.published_at = now()
    v1.save()
    sample_tier.status = TierStatus.PUBLISHED
    sample_tier.save()

    v2 = TierVersion.objects.create(tier=sample_tier, version=2)

    org = Organizer.objects.create(name="Org Keep V1", slug="org-keep-v1")
    Subscription.objects.filter(organizer=org).delete()
    sub = Subscription.objects.create(
        organizer=org,
        tier_version=v1,
        status=SubscriptionStatus.ACTIVE,
        starts_at=now(),
    )

    url = reverse(
        "plugins:eventyay_business:tiers.publish", kwargs={"pk": sample_tier.pk}
    )
    response = business_admin_client.post(url, {}, follow=True)
    assert response.status_code == 200

    v2.refresh_from_db()
    assert v2.published_at is not None

    sub.refresh_from_db()
    assert sub.tier_version == v1


@pytest.mark.django_db
def test_tier_version_detail_view(business_admin_client, sample_tier):
    version = sample_tier.versions.first()
    url = reverse(
        "plugins:eventyay_business:tiers.version_detail",
        kwargs={"pk": sample_tier.pk, "version_pk": version.pk},
    )
    response = business_admin_client.get(url)
    assert response.status_code == 200
    content = response.content.decode()
    assert f"v{version.version}" in content
    assert sample_tier.name in content


@pytest.mark.django_db
def test_tier_version_detail_view_wrong_tier(business_admin_client, sample_tier):
    other_tier = Tier.objects.create(
        name="Other", slug="other-vd", status=TierStatus.DRAFT
    )
    version = sample_tier.versions.first()
    url = reverse(
        "plugins:eventyay_business:tiers.version_detail",
        kwargs={"pk": other_tier.pk, "version_pk": version.pk},
    )
    response = business_admin_client.get(url)
    assert response.status_code == 404


@pytest.mark.django_db
def test_tier_detail_version_history_links(business_admin_client, sample_tier):
    """Version history items in detail page should link to tiers.version_detail."""
    version = sample_tier.versions.first()
    expected_url = reverse(
        "plugins:eventyay_business:tiers.version_detail",
        kwargs={"pk": sample_tier.pk, "version_pk": version.pk},
    )
    url = reverse(
        "plugins:eventyay_business:tiers.detail", kwargs={"pk": sample_tier.pk}
    )
    response = business_admin_client.get(url)
    assert response.status_code == 200
    assert expected_url in response.content.decode()


def test_organizer_plan_view_audience_split():
    """organizer_entitlements and developer_entitlements are split correctly by audience."""
    from eventyay_business.capabilities import get_all_capabilities

    # Replicate the splitting logic from OrganizerPlanView.get_context_data
    organizer_entitlements = []
    developer_entitlements = []
    for cap in get_all_capabilities():
        entry = {
            "capability": cap,
            "effective_value": cap.default_value,
            "is_overridden": False,
        }
        audience = cap.metadata.get("audience", "organizer")
        if audience == "developer":
            developer_entitlements.append(entry)
        else:
            organizer_entitlements.append(entry)

    organizer_names = {e["capability"].name for e in organizer_entitlements}
    developer_names = {e["capability"].name for e in developer_entitlements}

    # API caps must be in developer bucket only
    assert "api.read" in developer_names
    assert "api.write" in developer_names
    assert "api.webhooks" in developer_names
    assert "api.read" not in organizer_names
    assert "api.write" not in organizer_names

    # Organiser caps must not bleed into developer bucket
    assert "video.youtube" in organizer_names
    assert "email.bulk.monthly" in organizer_names
    assert "video.youtube" not in developer_names
    assert "email.bulk.monthly" not in developer_names
