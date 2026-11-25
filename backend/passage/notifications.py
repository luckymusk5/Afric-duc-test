import os
from typing import Protocol
from django.utils import timezone
from .models import OutboxMessage
class NotificationGateway(Protocol):
    def queue(self,parent,anomaly): ...
class OutboxNotificationGateway:
    """Provider local; un fournisseur SMS pourra implémenter la même interface."""
    def queue(self,parent,anomaly):
        cap=int(os.getenv('MAX_DAILY_MESSAGES','5'))
        count=OutboxMessage.objects.filter(parent=parent,created_at__date=timezone.localdate()).count()
        state='queued' if count<cap else 'suppressed'
        reason='' if state=='queued' else 'Plafond quotidien atteint'
        OutboxMessage.objects.get_or_create(parent=parent,anomaly=anomaly,defaults={'message':'Un événement concernant votre enfant nécessite une vérification dans SchoolLogs.','state':state,'reason':reason})
notification_gateway:NotificationGateway=OutboxNotificationGateway()
