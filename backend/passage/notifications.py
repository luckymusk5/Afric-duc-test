import os
from typing import Protocol
from django.db import transaction
from django.utils import timezone
from .models import OutboxMessage, Student
class NotificationGateway(Protocol):
    def queue(self,parent,anomaly): ...
class OutboxNotificationGateway:
    """Provider local; un fournisseur SMS pourra implémenter la même interface."""
    def queue(self,parent,anomaly):
        cap=int(os.getenv('MAX_DAILY_MESSAGES','5'))
        with transaction.atomic():
            Student.objects.select_for_update().get(pk=anomaly.student_id)
            existing = OutboxMessage.objects.filter(parent=parent, anomaly=anomaly).first()
            if existing:
                return existing
            count=OutboxMessage.objects.filter(
                anomaly__student=anomaly.student,
                created_at__date=timezone.localdate(),
            ).count()
            state='queued' if count<cap else 'suppressed'
            reason='' if state=='queued' else 'Plafond quotidien atteint'
            return OutboxMessage.objects.create(
                parent=parent,
                anomaly=anomaly,
                message='Un événement concernant votre enfant nécessite une vérification dans SchoolLogs.',
                state=state,
                reason=reason,
            )
notification_gateway:NotificationGateway=OutboxNotificationGateway()
