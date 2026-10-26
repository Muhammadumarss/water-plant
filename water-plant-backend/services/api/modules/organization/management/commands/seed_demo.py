from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from application.commands.setup import seed_demo


class Command(BaseCommand):
    help = "DEVELOPMENT ONLY: demo staff, products, prices, a released batch and a customer."

    def handle(self, *args, **opts):
        if not settings.DEBUG:
            raise CommandError("seed_demo only runs with DEBUG=True.")
        demo = seed_demo()
        self.stdout.write(self.style.SUCCESS(
            f"Demo ready. Users: owner / cashier / rider / operator (password: demo-password-123). "
            f"Till cash account: {demo.till_cash.id}"))
