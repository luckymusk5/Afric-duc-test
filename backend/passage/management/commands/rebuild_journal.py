from datetime import date

from django.core.management.base import BaseCommand, CommandError

from passage.services import reconstruire_journal


class Command(BaseCommand):
    help = 'Reconstruit le journal de présence pour une date locale.'

    def add_arguments(self, parser):
        parser.add_argument('date_jour', help='Date locale au format AAAA-MM-JJ.')

    def handle(self, *args, **options):
        try:
            day = date.fromisoformat(options['date_jour'])
        except ValueError as error:
            raise CommandError('La date doit respecter le format AAAA-MM-JJ.') from error
        count = reconstruire_journal(day)
        self.stdout.write(self.style.SUCCESS(f'{count} ligne(s) de journal reconstruites.'))
