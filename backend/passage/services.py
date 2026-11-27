import hashlib,hmac,json,os,re,time,urllib.request
from datetime import timedelta, datetime
from django.conf import settings
from django.db import transaction,IntegrityError,models
from django.db.models import F,Value
from django.db.models.functions import Coalesce,Greatest,Least
from django.utils import timezone
from .models import Badge, BadgeUsage, Scan, Anomaly, RouteWindow, Student, AISummary, AIUsageLog, DeviceClock, Checkpoint, Device, DeviceAssignment, PresenceJournal
from .badge_security import BadgeRejected, parse_rotating_token, validate_rotating_token, record_device_attempt
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
    now = now or timezone.now()
    local = timezone.localtime(now)
    grace = int(os.getenv('PASSAGE_GRACE_MINUTES', '15'))
    created = 0
    for window_day in (local.date() - timedelta(days=1), local.date()):
        windows = RouteWindow.objects.filter(
            active=True,
            days__contains=[window_day.isoweekday()],
        ).select_related('student', 'checkpoint')
        for window in windows:
            times = _window_times(window, window_day)
            if times is None:
                continue
            start_time, end_time = times
            start = timezone.make_aware(datetime.combine(window_day, start_time))
            end = timezone.make_aware(datetime.combine(window_day, end_time))
            deadline = end + timedelta(minutes=grace)
            if deadline > now:
                continue
            completed = Scan.objects.filter(
                badge__student=window.student,
                checkpoint=window.checkpoint,
                direction=window.direction,
                result=Scan.Result.ACCEPTED,
                estimated_time__gte=start,
                estimated_time__lte=end,
            ).exists()
            if completed:
                continue
            _, was_created = Anomaly.objects.get_or_create(
                student=window.student,
                kind='passage_manquant',
                date_jour=window_day,
                route_window=window,
                defaults={
                    'occurred_at': end,
                    'details': str(window.pk),
                    'state': Anomaly.State.PROVISIONAL,
                },
            )
            if was_created:
                created += 1
    return created


def _assignment_at(device, estimated_time):
    return DeviceAssignment.objects.filter(
        device=device,
        starts_at__lte=estimated_time,
    ).filter(
        models.Q(ends_at__isnull=True) | models.Q(ends_at__gt=estimated_time),
    ).select_related('checkpoint').order_by('-starts_at').first()


def _window_times(window, day):
    exception = window.exceptions.filter(date_jour=day).first()
    if exception is not None and not exception.active:
        return None
    return (
        exception.start_time if exception and exception.start_time else window.start_time,
        exception.end_time if exception and exception.end_time else window.end_time,
    )


def _update_presence_journal(scan):
    if scan.badge_id is None or scan.estimated_time is None:
        return
    day = timezone.localtime(scan.estimated_time).date()
    # PostgreSQL traduit ignore_conflicts en INSERT ... ON CONFLICT DO NOTHING.
    # Deux appareils peuvent créer la même journée sans perdre l’un des passages.
    PresenceJournal.objects.bulk_create(
        [PresenceJournal(student_id=scan.badge.student_id, date_jour=day)],
        ignore_conflicts=True,
    )
    journal = PresenceJournal.objects.filter(
        student_id=scan.badge.student_id,
        date_jour=day,
    )
    if scan.direction == RouteWindow.Direction.EXIT:
        journal.update(
            heure_sortie=Greatest(
                Coalesce(F('heure_sortie'), Value(scan.estimated_time)),
                Value(scan.estimated_time),
            ),
        )
    else:
        journal.update(
            heure_entree=Least(
                Coalesce(F('heure_entree'), Value(scan.estimated_time)),
                Value(scan.estimated_time),
            ),
        )


def _scan_direction(student, checkpoint, estimated_time):
    local = timezone.localtime(estimated_time)
    day_windows = list(RouteWindow.objects.filter(
        student=student,
        active=True,
        days__contains=[local.isoweekday()],
    ).order_by('sequence'))
    timed_windows = [
        (window, _window_times(window, local.date()))
        for window in day_windows
    ]
    timed_windows = [(window, times) for window, times in timed_windows if times is not None]
    point_windows = [
        (window, times)
        for window, times in timed_windows
        if window.checkpoint_id == checkpoint.id
    ]
    current_time = local.time().replace(tzinfo=None)
    for window, (start_time, end_time) in point_windows:
        if start_time <= current_time <= end_time:
            return window.direction, window, False
    if point_windows:
        window, _ = point_windows[0]
        return window.direction, window, True
    if timed_windows:
        nearest = min(
            timed_windows,
            key=lambda window: min(
                abs((datetime.combine(local.date(), window[1][0]) - datetime.combine(local.date(), current_time)).total_seconds()),
                abs((datetime.combine(local.date(), window[1][1]) - datetime.combine(local.date(), current_time)).total_seconds()),
            ),
        )
        return nearest[0].direction, None, False
    return RouteWindow.Direction.ENTRY, None, False


def _student_is_inside_at(student, estimated_time):
    """Return whether the latest accepted passage earlier that school day was an entry."""
    day = timezone.localtime(estimated_time).date()
    start = timezone.make_aware(datetime.combine(day, datetime.min.time()), timezone.get_current_timezone())
    previous = Scan.objects.filter(
        badge__student=student,
        result=Scan.Result.ACCEPTED,
        estimated_time__gte=start,
        estimated_time__lt=estimated_time,
    ).order_by('-estimated_time', '-pk').first()
    return bool(previous and previous.direction == RouteWindow.Direction.ENTRY)


def sync_passage_events(device, events, now=None):
    """Crée ou retrouve tout un lot avant que la vue ne renvoie ses accusés."""
    server_time = now or timezone.now()
    acknowledgements = []
    with transaction.atomic():
        device = Device.objects.select_for_update().get(pk=device.pk, active=True)
        device_id = str(device.device_id)
        for event in sorted(events, key=lambda item: item['est_time']):
            event_id = event['event_id']
            local_time = event['local_time']
            estimated_time = event['est_time']
            clock_offset = (server_time - local_time).total_seconds()

            existing = Scan.objects.filter(device_id=device_id, event_id=event_id).first()
            if existing:
                acknowledgements.append({
                    'event_id': str(event_id),
                    'status': 'duplicate',
                    'resultat': existing.result,
                })
                continue

            if estimated_time > server_time + timedelta(minutes=5) or estimated_time.year < 2000:
                assignment = _assignment_at(device, server_time)
                checkpoint = assignment.checkpoint if assignment else None
                Scan.objects.create(
                    event_id=event_id, badge=None, checkpoint=checkpoint,
                    local_time=local_time, estimated_time=estimated_time,
                    device_id=device_id, device_record=device, server_time=server_time,
                    clock_offset_seconds=clock_offset,
                    result=Scan.Result.REFUSED,
                )
                acknowledgements.append({
                    'event_id': str(event_id), 'status': 'created', 'resultat': 'refuse',
                })
                continue

            assignment = _assignment_at(device, estimated_time)
            checkpoint = assignment.checkpoint if assignment else None
            badge = None
            inactive_badge = None
            counter = None
            try:
                badge_id, counter, _ = parse_rotating_token(event['token'])
                badge = Badge.objects.select_for_update().select_related('student').filter(
                    badge_id=badge_id,
                ).first()
                if badge is None:
                    raise BadgeRejected
                badge, counter = validate_rotating_token(
                    event['token'], estimated_time, now=server_time,
                    check_active=False,
                )
                if badge.state != Badge.State.ACTIVE or not badge.student.active:
                    inactive_badge = badge
                else:
                    if (
                        estimated_time < badge.issued_at
                        or BadgeUsage.objects.filter(badge=badge, counter=counter).exists()
                    ):
                        raise BadgeRejected
            except BadgeRejected:
                badge = None
                counter = None

            result = Scan.Result.REFUSED
            direction = RouteWindow.Direction.ENTRY
            scan = None
            if inactive_badge is None and badge is not None and counter is not None and checkpoint is not None:
                direction, window, outside_window = _scan_direction(
                    badge.student, checkpoint, estimated_time,
                )
                if (
                    direction == RouteWindow.Direction.EXIT
                    and not _student_is_inside_at(badge.student, estimated_time)
                ):
                    refused_scan = Scan.objects.create(
                        event_id=event_id, badge=badge, checkpoint=checkpoint,
                        local_time=local_time, estimated_time=estimated_time,
                        device_id=device_id, device_record=device, server_time=server_time,
                        clock_offset_seconds=clock_offset,
                        clock_suspect=abs((local_time - estimated_time).total_seconds()) > 300,
                        direction=direction, result=Scan.Result.REFUSED,
                    )
                    BadgeUsage.objects.create(badge=badge, counter=counter, scan=refused_scan)
                    acknowledgements.append({
                        'event_id': str(event_id), 'status': 'created',
                        'resultat': Scan.Result.REFUSED,
                    })
                    continue
                result = Scan.Result.ACCEPTED
                scan = Scan.objects.create(
                    event_id=event_id, badge=badge, checkpoint=checkpoint,
                    local_time=local_time, estimated_time=estimated_time,
                    device_id=device_id, device_record=device, server_time=server_time,
                    clock_offset_seconds=clock_offset,
                    clock_suspect=abs((local_time - estimated_time).total_seconds()) > 300,
                    direction=direction, result=result,
                )
                BadgeUsage.objects.create(badge=badge, counter=counter, scan=scan)
                if scan.clock_suspect:
                    create_anomaly(
                        badge.student, 'horloge_appareil', estimated_time, scan,
                        'Écart d’horloge supérieur à cinq minutes.',
                    )
                day = timezone.localtime(estimated_time).isoweekday()
                has_route = RouteWindow.objects.filter(
                    student=badge.student, active=True, days__contains=[day],
                ).exists()
                if not has_route or window is None:
                    create_anomaly(
                        badge.student, 'point_inattendu', estimated_time, scan,
                        'Point absent de l’itinéraire.',
                    )
                elif outside_window:
                    create_anomaly(
                        badge.student, 'hors_fenetre', estimated_time, scan,
                        'Passage hors de la fenêtre prévue.',
                    )
                if Scan.objects.filter(
                    badge=badge,
                    result=Scan.Result.ACCEPTED,
                    estimated_time__range=(
                        estimated_time - timedelta(seconds=60),
                        estimated_time + timedelta(seconds=60),
                    ),
                ).exclude(pk=scan.pk).exists():
                    create_anomaly(
                        badge.student, 'double_scan', estimated_time, scan,
                        'Deux passages distincts à moins de 60 secondes.',
                    )
                _update_presence_journal(scan)
            else:
                refused_badge = inactive_badge or badge
                scan = Scan.objects.create(
                    event_id=event_id, badge=refused_badge, checkpoint=checkpoint,
                    local_time=local_time, estimated_time=estimated_time,
                    device_id=device_id, device_record=device, server_time=server_time,
                    clock_offset_seconds=clock_offset,
                    result=Scan.Result.REFUSED,
                )
                if inactive_badge is not None:
                    create_anomaly(
                        inactive_badge.student, 'badge_inactif_utilise', estimated_time,
                        scan, 'Utilisation d’un badge inactif.',
                    )

            if result == Scan.Result.ACCEPTED:
                _mark_late_passage_caught_up(scan)
            acknowledgements.append({
                'event_id': str(event_id),
                'status': 'created',
                'resultat': result,
            })
    return {'acks': acknowledgements, 'server_time': server_time}


def _mark_late_passage_caught_up(scan):
    day = timezone.localtime(scan.estimated_time).date()
    expected = RouteWindow.objects.filter(
        student=scan.badge.student,
        checkpoint=scan.checkpoint,
        direction=scan.direction,
        active=True,
        days__contains=[timezone.localtime(scan.estimated_time).isoweekday()],
    )
    for window in expected:
        Anomaly.objects.filter(
            student=scan.badge.student,
            kind='passage_manquant',
            date_jour=day,
            route_window=window,
            state=Anomaly.State.PROVISIONAL,
        ).update(state=Anomaly.State.CAUGHT_UP)


@transaction.atomic
def rebuild_presence_journal(day):
    """Reconstruit une journée entière depuis les passages acceptés."""
    start = timezone.make_aware(datetime.combine(day, datetime.min.time()))
    end = timezone.make_aware(datetime.combine(day + timedelta(days=1), datetime.min.time()))
    PresenceJournal.objects.filter(date_jour=day).delete()
    scans = Scan.objects.filter(
        result=Scan.Result.ACCEPTED,
        estimated_time__gte=start,
        estimated_time__lt=end,
        badge__isnull=False,
    ).select_related('badge').order_by('estimated_time')
    for scan in scans:
        _update_presence_journal(scan)
    return PresenceJournal.objects.filter(date_jour=day).count()


def reconstruire_journal(day):
    return rebuild_presence_journal(day)
def summary_stats(class_name,day):
    qs=Student.objects.filter(class_name=class_name,active=True); total=qs.count()
    scans=Scan.objects.filter(
        badge__student__in=qs,
        estimated_time__date=day,
        result=Scan.Result.ACCEPTED,
    )
    arrived=scans.filter(direction=RouteWindow.Direction.ENTRY).values('badge__student_id').distinct().count()
    late=Anomaly.objects.filter(
        student__in=qs,
        occurred_at__date=day,
        kind='hors_fenetre',
        scan__direction=RouteWindow.Direction.ENTRY,
    ).values('student_id').distinct().count()
    anomalies=Anomaly.objects.filter(
        student__in=qs,
        occurred_at__date=day,
        kind__in=['hors_fenetre','point_inattendu','double_scan','passage_manquant','badge_inactif_utilise'],
    ).count()
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
