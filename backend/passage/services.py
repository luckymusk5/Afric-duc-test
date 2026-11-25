import hashlib,hmac,json,os,re,time,urllib.request
from datetime import timedelta, datetime
from django.conf import settings
from django.db import transaction,IntegrityError
from django.utils import timezone
from .models import Badge, BadgeUsage, Scan, Anomaly, RouteWindow, Student, AISummary, AIUsageLog, DeviceClock, Checkpoint
from .badge_security import BadgeRejected, validate_rotating_token, record_device_attempt
from .notifications import notification_gateway

def create_anomaly(student,kind,when,scan=None,details=''):
    anomaly,created=Anomaly.objects.get_or_create(student=student,kind=kind,occurred_at=when,scan=scan,defaults={'details':details})
    if not created:return anomaly
    for parent in student.parents.all():notification_gateway.queue(parent,anomaly)
    return anomaly
def _scan_datetime(value):
    if isinstance(value, str):
        from django.utils.dateparse import parse_datetime
        value = parse_datetime(value)
    if value is None:
        raise BadgeRejected
    if timezone.is_naive(value):
        value = timezone.make_aware(value, timezone.get_current_timezone())
    return value

def accept_scan(data, now=None, count_attempt=True):
    now = now or timezone.now()
    token = data.get('token')
    event_id = data.get('eventId', data.get('event_id'))
    local_time = _scan_datetime(data.get('localTime', data.get('local_time')))
    estimated_time = _scan_datetime(data.get('estimatedTime', data.get('estimated_time')))
    device_id = str(data.get('deviceId', data.get('device_id', '')))[:100]
    checkpoint_id = data.get('checkpointId', data.get('checkpoint_id'))
    if not event_id or not device_id or not checkpoint_id or not token:
        raise BadgeRejected
    device_clock = DeviceClock.objects.filter(device_id=device_id).first()
    if device_clock is None:
        raise BadgeRejected
    if count_attempt and not record_device_attempt(device_id, now=now):
        raise BadgeRejected
    badge, counter = validate_rotating_token(token, estimated_time, now=now)
    submitted_offset = float(data.get('clockOffsetSeconds', data.get('clock_offset_seconds', device_clock.offset_seconds)))
    stale_anchor = (now - device_clock.synchronized_at).total_seconds() > 24 * 60 * 60
    offset_changed = abs(submitted_offset - device_clock.offset_seconds) > 300
    clock_suspect = (
        abs((local_time - estimated_time).total_seconds() + device_clock.offset_seconds) > 300
        or stale_anchor
        or offset_changed
    )
    try:
        with transaction.atomic():
            existing = Scan.objects.filter(event_id=event_id).first()
            if existing:
                return {'id': existing.id, 'duplicate': True, 'clockSuspect': existing.clock_suspect}
            checkpoint = Checkpoint.objects.get(pk=checkpoint_id)
            scan = Scan.objects.create(
                event_id=event_id,
                badge=badge,
                checkpoint=checkpoint,
                local_time=local_time,
                estimated_time=estimated_time,
                device_id=device_id,
                clock_offset_seconds=device_clock.offset_seconds,
                clock_suspect=clock_suspect,
            )
            BadgeUsage.objects.create(badge=badge, counter=counter, scan=scan)
    except IntegrityError:
        existing = Scan.objects.filter(event_id=event_id).first()
        if existing:
            return {'id': existing.id, 'duplicate': True, 'clockSuspect': existing.clock_suspect}
        raise BadgeRejected from None
    except Checkpoint.DoesNotExist:
        raise BadgeRejected from None
    if clock_suspect:
        create_anomaly(badge.student, 'horloge_appareil', estimated_time, scan, 'Décalage de l’horloge de l’appareil supérieur à cinq minutes.')
    window = RouteWindow.objects.filter(student=badge.student, checkpoint=checkpoint).first()
    if not window:
        create_anomaly(badge.student, 'point_inattendu', estimated_time, scan, 'Point absent de l’itinéraire')
    elif not (window.start_time <= timezone.localtime(estimated_time).time().replace(tzinfo=None) <= window.end_time):
        create_anomaly(badge.student, 'hors_fenetre', estimated_time, scan, 'Validation hors fenêtre')
    if Scan.objects.filter(
        badge=badge,
        estimated_time__range=(estimated_time - timedelta(seconds=60), estimated_time + timedelta(seconds=60)),
    ).exclude(pk=scan.pk).exists():
        create_anomaly(badge.student, 'double_scan', estimated_time, scan, 'Scans rapprochés de moins de 60 secondes')
    return {'id': scan.id, 'duplicate': False, 'clockSuspect': clock_suspect}
def close_windows(now=None):
    now=now or timezone.now(); local=timezone.localtime(now); count=0
    windows=RouteWindow.objects.filter(days__contains=[local.isoweekday()],end_time__lt=local.time())
    for w in windows.select_related('student','checkpoint'):
        badge=Badge.objects.filter(student=w.student,state='active').first()
        if badge and not Scan.objects.filter(badge=badge,checkpoint=w.checkpoint,estimated_time__date=local.date()).exists():
            occurrence=timezone.make_aware(datetime.combine(local.date(),w.end_time),timezone.get_current_timezone())
            before=Anomaly.objects.filter(student=w.student,kind='passage_manquant',occurred_at=occurrence,details=str(w.pk)).exists()
            create_anomaly(w.student,'passage_manquant',occurrence,None,str(w.pk))
            if not before: count+=1
    return count
def summary_stats(class_name,day):
    qs=Student.objects.filter(class_name=class_name,active=True); total=qs.count()
    scans=Scan.objects.filter(badge__student__in=qs,estimated_time__date=day,checkpoint_id=1)
    arrived=scans.values('badge__student_id').distinct().count(); late=scans.filter(estimated_time__time__gt='07:45:00').values('badge__student_id').distinct().count()
    anomalies=Anomaly.objects.filter(student__in=qs,occurred_at__date=day,kind__in=['hors_fenetre','point_inattendu']).count()
    return {'class':class_name,'date':str(day),'total':total,'arrived':arrived,'late':late,'anomalies':anomalies}
def summarize(stats):
    key=hashlib.sha256(json.dumps(stats,sort_keys=True).encode()).hexdigest(); cached=AISummary.objects.filter(cache_key=key).first()
    if cached:return {'text':cached.text,'origin':cached.origin,'reason':cached.reason or None,'stats':stats,'cached':True}
    fallback=f"Sur {stats['total']} élèves de {stats['class']}, {stats['arrived']} passages à l’entrée ont été enregistrés, dont {stats['late']} après la fenêtre prévue. {stats['anomalies']} anomalie(s) hors plage ou point inattendu."
    model=os.getenv('AI_MODEL','gpt-4o-mini'); cap=float(os.getenv('AI_MONTHLY_BUDGET','2')); spent=sum(float(x.estimated_cost) for x in AIUsageLog.objects.filter(created_at__year=timezone.localdate().year,created_at__month=timezone.localdate().month))
    key_api=os.getenv('AI_API_KEY'); url=os.getenv('AI_API_URL'); origin='template'; text=fallback; cost=0
    if key_api and url and spent+.0002<=cap:
        body={'model':model,'messages':[{'role':'system','content':'Rédige une phrase en français à partir des faits JSON. Ajoute aucun chiffre ni fait. Réponds en JSON strict: {"text":"..."}.'},{'role':'user','content':json.dumps(stats,ensure_ascii=False)}],'response_format':{'type':'json_object'},'max_tokens':100}
        for attempt in range(2):
            input_size=len(json.dumps(body))//4
            try:
                req=urllib.request.Request(url,data=json.dumps(body).encode(),headers={'Authorization':'Bearer '+key_api,'Content-Type':'application/json'})
                with urllib.request.urlopen(req,timeout=8) as response: parsed=json.loads(response.read())
                candidate=json.loads(parsed['choices'][0]['message']['content'])['text']
                if not isinstance(candidate,str) or len(candidate)>500:raise ValueError()
                allowed=set(re.findall(r'\d+',json.dumps(stats))); nums=re.findall(r'\d+',candidate)
                if any(n not in allowed for n in nums):raise ValueError()
                text=candidate; origin='model'; cost+=.0001
                AIUsageLog.objects.create(cache_key=key,model=model,input_tokens=input_size,output_tokens=max(1,len(candidate)//4),estimated_cost=.0001,outcome='success')
                break
            except Exception:
                cost+=.0001
                AIUsageLog.objects.create(cache_key=key,model=model,input_tokens=input_size,output_tokens=0,estimated_cost=.0001,outcome='retry' if attempt==0 else 'fallback')
    reason=None
    if origin=='template':
        if not key_api or not url:reason='Service IA non configuré; gabarit local utilisé.'
        elif spent+.0002>cap:reason='Plafond mensuel IA atteint; gabarit exact utilisé.'
        else:reason='Service IA indisponible ou réponse invalide; gabarit exact utilisé.'
    AISummary.objects.create(cache_key=key,model=model,text=text,stats=stats,origin=origin,reason=reason or '',input_tokens=len(json.dumps(stats))//4,output_tokens=len(text)//4,estimated_cost=cost)
    return {'text':text,'origin':origin,'reason':reason,'stats':stats,'cached':False}
