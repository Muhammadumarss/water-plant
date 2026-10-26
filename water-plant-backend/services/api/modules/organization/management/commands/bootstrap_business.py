import os

from django.core.management.base import BaseCommand, CommandError

from application.commands.setup import bootstrap_business


class Command(BaseCommand):
    help = "Provision the single v1 business, its main branch, owner account and base stock locations."

    def add_arguments(self, parser):
        parser.add_argument("--name", required=True)
        parser.add_argument("--owner-username", required=True)

    def handle(self, *args, **opts):
        password = os.environ.get("OWNER_INITIAL_PASSWORD")
        if not password:
            raise CommandError("Set OWNER_INITIAL_PASSWORD in the environment (never pass passwords as arguments).")
        try:
            business = bootstrap_business(name=opts["name"], owner_username=opts["owner_username"],
                                          owner_password=password)
        except RuntimeError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f"Business {business.name} provisioned ({business.id})."))
