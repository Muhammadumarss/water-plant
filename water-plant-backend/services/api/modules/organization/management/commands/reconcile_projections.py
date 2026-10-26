import json

from django.core.management.base import BaseCommand

from application.commands.scheduled import reconcile_projections


class Command(BaseCommand):
    help = "Compare stock/invoice/receipt projections with their ledgers. Reports first; --repair fixes stock."

    def add_arguments(self, parser):
        parser.add_argument("--repair", action="store_true")

    def handle(self, *args, **opts):
        report = reconcile_projections(repair=opts["repair"])
        self.stdout.write(json.dumps(report, indent=2))
        if any(report.values()):
            self.stderr.write(self.style.WARNING("Differences found."))
        else:
            self.stdout.write(self.style.SUCCESS("All projections match their ledgers."))
