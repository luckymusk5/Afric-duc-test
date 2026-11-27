import time

from apscheduler.schedulers.background import BackgroundScheduler
from django.core.management.base import BaseCommand

from passage.services import close_windows


class Command(BaseCommand):
    help = 'Planifie la clôture idempotente des fenêtres de passage.'

    def handle(self, *args, **options):
        scheduler = BackgroundScheduler(timezone='Africa/Douala')
        scheduler.add_job(
            close_windows,
            trigger='interval',
            minutes=1,
            id='close_passage_windows',
            coalesce=True,
            max_instances=1,
        )
        scheduler.start()
        self.stdout.write(self.style.SUCCESS('Ordonnanceur des passages démarré.'))
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            scheduler.shutdown()
