from django.core.management.base import BaseCommand
from django.utils.timezone import now
from eventyay.base.models import Organizer

from eventyay_business.models import Subscription, SubscriptionStatus, Tier, TierVersion

class Command(BaseCommand):
    help = "Assigns the Free tier to all organizers that currently lack a subscription."

    def handle(self, *args, **options):
        # Fetch or create free tier
        free_tier, created = Tier.objects.get_or_create(
            slug="free",
            defaults={
                "name": "Free",
                "description": "Default free tier",
                "is_public": True,
            }
        )
        if created:
            self.stdout.write(self.style.SUCCESS('Created default "Free" tier.'))

        latest_version = free_tier.versions.filter(published_at__isnull=False).order_by("-version").first()
        if not latest_version:
            latest_version, v_created = TierVersion.objects.get_or_create(
                tier=free_tier,
                version=1,
                defaults={"published_at": now()}
            )
            if v_created:
                self.stdout.write(self.style.SUCCESS('Created default published TierVersion for "Free".'))

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
