import hashlib,hmac,json,os,re,time,urllib.request
from datetime import timedelta, datetime
from django.conf import settings
from django.db import transaction,IntegrityError
from django.utils import timezone
from .models import Badge,Scan,Anomaly,RouteWindow,Student,AISummary,AIUsageLog
from .notifications import notification_gateway

def badge_token(badge, now=None):
    slot=int((now or time.time())//60); payload=f'{badge.pk}.{slot}'
    key=os.getenv('BADGE_SIGNING_KEY',settings.SECRET_KEY).encode()
    return payload+'.'+hmac.new(key,payload.encode(),hashlib.sha256).hexdigest()
def verify_token(token, now=None, allow_expired=False):
    try:
        badge_id,slot,sig=token.split('.'); slot=int(slot)
        if not allow_expired and abs(int((now or time.time())//60)-slot)>1:return None
        payload=f'{badge_id}.{slot}'; key=os.getenv('BADGE_SIGNING_KEY',settings.SECRET_KEY).encode()
        if not hmac.compare_digest(sig,hmac.new(key,payload.encode(),hashlib.sha256).hexdigest()):return None
        return Badge.objects.select_related('student').get(pk=int(badge_id),state='active',student__active=True)
    except Exception:return None
def create_anomaly(student,kind,when,scan=None,details=''):
    anomaly,created=Anomaly.objects.get_or_create(student=student,kind=kind,occurred_at=when,scan=scan,defaults={'details':details})
    if not created:return anomaly
    for parent in student.parents.all():notification_gateway.queue(parent,anomaly)
    return anomaly
def accept_scan(data, prevalidated=False):
    badge=verify_token(data['token'],allow_expired=prevalidated)
    if not badge: raise ValueError('Badge invalide ou expiré')
    now=timezone.now(); local=data['localTime']
    if isinstance(local,str):
        from django.utils.dateparse import parse_datetime
        local=parse_datetime(local)
        if local is None:raise ValueError('Heure locale invalide')
        if timezone.is_naive(local):local=timezone.make_aware(local,timezone.get_current_timezone())
    skew=abs((now-local).total_seconds())>300
    with transaction.atomic():
        old=Scan.objects.filter(event_id=data['eventId']).first()
        if old:return {'id':old.id,'duplicate':True,'clockSkew':False}
        if Scan.objects.filter(token=data['token']).exists():
            create_anomaly(badge.student,'badge_replay',now,None,'Code tournant déjà utilisé')
            return {'rejected':'Code déjà utilisé'}
        checkpoint_id=data['checkpointId']
        try: window=RouteWindow.objects.get(student=badge.student,checkpoint_id=checkpoint_id)
        except RouteWindow.DoesNotExist: window=None
        scan=Scan.objects.create(event_id=data['eventId'],badge=badge,checkpoint_id=checkpoint_id,local_time=local,device_id=data['deviceId'],clock_skew=skew,token=data['token'])
        if skew:create_anomaly(badge.student,'horloge_appareil',now,scan,'Heure locale écartée; heure serveur utilisée')
        if not window:create_anomaly(badge.student,'point_inattendu',now,scan,'Point absent de l’itinéraire')
        elif not(window.start_time<=timezone.localtime(now).time().replace(tzinfo=None)<=window.end_time):create_anomaly(badge.student,'hors_fenetre',now,scan,'Validation hors fenêtre')
        if Scan.objects.filter(badge=badge,received_at__gte=now-timedelta(seconds=60)).exclude(pk=scan.pk).exists():create_anomaly(badge.student,'double_scan',now,scan,'Scans rapprochés de moins de 60 secondes')
        return {'id':scan.id,'duplicate':False,'clockSkew':skew}
def close_windows(now=None):
    now=now or timezone.now(); local=timezone.localtime(now); count=0
    windows=RouteWindow.objects.filter(days__contains=[local.isoweekday()],end_time__lt=local.time())
    for w in windows.select_related('student','checkpoint'):
        badge=Badge.objects.filter(student=w.student,state='active').first()
        if badge and not Scan.objects.filter(badge=badge,checkpoint=w.checkpoint,received_at__date=local.date()).exists():
            occurrence=timezone.make_aware(datetime.combine(local.date(),w.end_time),timezone.get_current_timezone())
            before=Anomaly.objects.filter(student=w.student,kind='passage_manquant',occurred_at=occurrence,details=str(w.pk)).exists()
            create_anomaly(w.student,'passage_manquant',occurrence,None,str(w.pk))
            if not before: count+=1
    return count
def summary_stats(class_name,day):
    qs=Student.objects.filter(class_name=class_name,active=True); total=qs.count()
    scans=Scan.objects.filter(badge__student__in=qs,received_at__date=day,checkpoint_id=1)
    arrived=scans.values('badge__student_id').distinct().count(); late=scans.filter(received_at__time__gt='07:45:00').values('badge__student_id').distinct().count()
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
