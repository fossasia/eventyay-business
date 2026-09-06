from django.core.management.base import BaseCommand
from django.utils.timezone import now
from eventyay.base.models import Organizer

from eventyay_business.models import Subscription, SubscriptionStatus, Tier, TierVersion

class Command(BaseCommand):
    help = "Assigns the Free tier to all organizers that currently lack a subscription."

    def handle(self, *args, **options):
        # Fetch or create free tier
        free_tier = Tier.objects.filter(slug="free").first()
        if not free_tier:
            free_tier = Tier.objects.create(
                slug="free",
                name="Free",
                description="Default free tier",
                is_public=True,
            )
            self.stdout.write(self.style.SUCCESS('Created default "Free" tier.'))

        latest_version = free_tier.versions.order_by("-version").first()
        if not latest_version:
            latest_version = TierVersion.objects.create(
                tier=free_tier,
                version=1,
                published_at=now()
            )
            self.stdout.write(self.style.SUCCESS('Created default TierVersion for "Free".'))

        organizers_without_subs = Organizer.objects.filter(subscriptions__isnull=True)
        count = organizers_without_subs.count()

        if count == 0:
            self.stdout.write(self.style.SUCCESS("All organizers already have a subscription."))
            return

        for org in organizers_without_subs:
            Subscription.objects.create(
                organizer=org,
                tier_version=latest_version,
                status=SubscriptionStatus.ACTIVE,
                starts_at=now(),
            )
            self.stdout.write(f"Assigned Free tier to Organizer: {org.name}")

        self.stdout.write(self.style.SUCCESS(f"Successfully assigned Free tier to {count} organizer(s)."))
