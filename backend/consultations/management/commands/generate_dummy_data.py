from django.core.management.base import BaseCommand

from consultations.dummy_data import (
    DUMMY_CONSULTATIONS,
    NUMBER_RESPONDENTS,
    create_dummy_consultation,
)
from consultations.models import Consultation


class Command(BaseCommand):
    help = "Generate dummy consultations at each pipeline stage."

    def add_arguments(self, parser):
        parser.add_argument(
            "--skip-if-exists",
            action="store_true",
            help="Do nothing if any consultation already exists (makes local setup re-runnable).",
        )

    def handle(self, *args, **options):
        if options["skip_if_exists"] and Consultation.objects.exists():
            self.stdout.write("Consultations already exist, skipping dummy data.")
            return

        self.stdout.write("Generating dummy data...")
        for config in DUMMY_CONSULTATIONS:
            self.stdout.write(f"  Creating consultation: {config['CONSULTATION_NAME']}...")
            create_dummy_consultation(number_respondents=NUMBER_RESPONDENTS, config=config)
        self.stdout.write("Done.")
