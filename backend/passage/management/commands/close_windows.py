from django.core.management.base import BaseCommand
from passage.services import close_windows
class Command(BaseCommand):
    help='Détecte les passages manquants à la clôture des fenêtres.'
    def handle(self,*args,**kwargs): self.stdout.write(f"Anomalies créées : {close_windows()}")
