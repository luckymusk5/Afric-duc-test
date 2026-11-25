from celery import shared_task,Task
from django.db import OperationalError,DatabaseError
from .models import SyncReceipt
from .services import accept_scan
class ScanTask(Task):
    autoretry_for=(OperationalError,DatabaseError)
    retry_backoff=True
    retry_jitter=True
    max_retries=8
    acks_late=True
    reject_on_worker_lost=True
    def on_failure(self,exc,task_id,args,kwargs,einfo):
        if args:
            SyncReceipt.objects.filter(event_id=args[0],state__in=['queued','processing']).update(state='failed',error='Échec après plusieurs tentatives.')
        super().on_failure(exc,task_id,args,kwargs,einfo)
@shared_task(bind=True,base=ScanTask,name='passage.process_scan')
def process_scan(self,event_id):
    receipt=SyncReceipt.objects.filter(event_id=event_id).first()
    if receipt is None or receipt.state=='processed':return
    receipt.state='processing';receipt.error='';receipt.save(update_fields=['state','error','updated_at'])
    try:
        result=accept_scan(receipt.payload,prevalidated=True)
        if result.get('rejected'):
            receipt.state='rejected';receipt.error=result['rejected']
        else:
            receipt.state='processed';receipt.payload={}
        receipt.save(update_fields=['state','error','payload','updated_at'])
        return result
    except ValueError as exc:
        receipt.state='rejected';receipt.error=str(exc)[:250]
        receipt.save(update_fields=['state','error','updated_at'])
        return {'rejected':receipt.error}
    except Exception as exc:
        receipt.state='queued';receipt.error='Nouvelle tentative de synchronisation.'
        receipt.save(update_fields=['state','error','updated_at'])
        raise self.retry(exc=exc, countdown=min(2**(self.request.retries+1),60))
