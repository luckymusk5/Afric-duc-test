import hashlib
import json
import secrets
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from datetime import date, datetime, time, timedelta, timezone as dt_timezone
from django.contrib.auth import get_user_model
from django.conf import settings
from django.contrib.auth import authenticate, login as django_login, logout as django_logout
from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie, csrf_protect
from rest_framework import status, viewsets
from rest_framework.authentication import SessionAuthentication
from rest_framework.decorators import action, api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework_simplejwt.views import TokenObtainPairView
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken
from rest_framework_simplejwt.exceptions import TokenError
from .models import Student, Checkpoint, Badge, Scan, Anomaly, OutboxMessage, RevokedAccessToken, DeviceClock, Device, DeviceAssignment, RouteWindow, AuditLog
from .serializers import StudentSerializer, StudentProvisionSerializer, AccountProvisionSerializer, AdminAccountSerializer, CheckpointSerializer, ScanSerializer, AnomalySerializer, MessageSerializer, BadgeSerializer, DeviceTimeSyncSerializer, PassageSyncEventSerializer, DeviceEnrollmentSerializer, SessionLoginSerializer, DeviceStateSerializer, BadgeStateSerializer, BoundedTokenObtainPairSerializer
from .permissions import IsAdmin, IsAgent, IsStaffRole
from .services import summary_stats, summarize, close_windows, create_anomaly, sync_passage_events, _window_times
from .badge_security import reissue_badge, record_device_clock


class LoginThrottle(AnonRateThrottle):
    rate = '5/minute'


class ThrottledLoginView(TokenObtainPairView):
    permission_classes = [AllowAny]
    throttle_classes = [LoginThrottle]
    serializer_class = BoundedTokenObtainPairSerializer


class StudentViewSet(viewsets.ModelViewSet):
    serializer_class = StudentSerializer

    def get_serializer_class(self):
        return StudentProvisionSerializer if self.action == 'create' else StudentSerializer

    def get_queryset(self):
        if self.request.user.role == 'admin':
            return Student.objects.all().select_related('account').prefetch_related('parents')
        if self.request.user.role == 'parent':
            return Student.objects.filter(parents=self.request.user)
        return Student.objects.none()

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        student = serializer.save()
        return Response(
            StudentSerializer(student, context=self.get_serializer_context()).data,
            status=status.HTTP_201_CREATED,
        )

    def get_permissions(self):
        if self.action in ('create', 'update', 'partial_update', 'destroy', 'reissue_badge'):
            return [IsAdmin()]
        return [IsAuthenticated()]

    @action(detail=True, methods=['post'], url_path='badges/reissue', permission_classes=[IsAdmin])
    def reissue_badge(self, request, pk=None):
        student = self.get_object()
        badge, _ = reissue_badge_for_student(student)
        return Response({'badge_id': str(badge.badge_id), 'state': badge.state}, status=status.HTTP_201_CREATED)


# Keep the endpoint name distinct from the viewset action name.
reissue_badge_for_student = reissue_badge


def _manage_accounts(request, role):
    User = get_user_model()
    if request.method == 'GET':
        accounts = User.objects.filter(role=role, is_active=True).order_by('last_name', 'first_name', 'username')
        return Response(AdminAccountSerializer(accounts, many=True).data)
    serializer = AccountProvisionSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    account = User.objects.create_user(
        username=data['username'], email=data['username'], password=data['password'],
        first_name=data['first_name'], last_name=data['last_name'], role=role,
    )
    return Response(AdminAccountSerializer(account).data, status=status.HTTP_201_CREATED)


@api_view(['GET', 'POST'])
@permission_classes([IsAdmin])
def parents(request):
    return _manage_accounts(request, 'parent')


@api_view(['GET', 'POST'])
@permission_classes([IsAdmin])
def agents(request):
    return _manage_accounts(request, 'agent')


@api_view(['GET'])
@permission_classes([IsAdmin])
def daily_attendance(request):
    raw_day = request.query_params.get('date', str(timezone.localdate()))
    try:
        selected_day = date.fromisoformat(raw_day)
    except ValueError:
        return Response({'detail': 'Date invalide.'}, status=status.HTTP_400_BAD_REQUEST)
    start = timezone.make_aware(datetime.combine(selected_day, time.min), timezone.get_current_timezone())
    end = start + timedelta(days=1)
    scans = Scan.objects.filter(
        result=Scan.Result.ACCEPTED, estimated_time__gte=start, estimated_time__lt=end,
        badge__student__active=True,
    ).select_related('badge__student', 'checkpoint').order_by('estimated_time', 'pk')
    states = {}
    for scan in scans:
        student = scan.badge.student
        state = states.setdefault(student.pk, {'student': student, 'first_entry': None, 'current_entry': None, 'latest': None, 'morning_entry': None})
        if scan.direction == 'entry':
            state['first_entry'] = state['first_entry'] or scan.estimated_time
            state['current_entry'] = scan.estimated_time
            if timezone.localtime(scan.estimated_time).time() < time(12, 0):
                state['morning_entry'] = state['morning_entry'] or scan.estimated_time
        state['latest'] = scan
    classes = {}
    present_students = []
    for student in Student.objects.filter(active=True).order_by('class_name', 'name'):
        entry = states.get(student.pk)
        group = classes.setdefault(student.class_name, {'class_name': student.class_name, 'total_students': 0, 'entered_today': 0, 'entered_morning': 0, 'currently_present': 0})
        group['total_students'] += 1
        if entry and entry['first_entry']:
            group['entered_today'] += 1
        if entry and entry['morning_entry']:
            group['entered_morning'] += 1
        if entry and entry['latest'] and entry['latest'].direction == 'entry':
            latest = entry['latest']
            group['currently_present'] += 1
            present_students.append({
                'id': student.pk, 'name': student.name, 'class_name': student.class_name,
                'entered_at': entry['current_entry'].isoformat(),
                'last_seen_at': latest.estimated_time.isoformat(),
                'checkpoint': latest.checkpoint.label if latest.checkpoint else None,
            })
    totals = {
        'students': sum(group['total_students'] for group in classes.values()),
        'entered_today': sum(group['entered_today'] for group in classes.values()),
        'entered_morning': sum(group['entered_morning'] for group in classes.values()),
        'currently_present': len(present_students),
    }
    recap = f"Aujourd’hui, {totals['entered_today']} élèves sont entrés, dont {totals['entered_morning']} avant midi. {totals['currently_present']} sont actuellement dans l’établissement."
    return Response({
        'date': selected_day.isoformat(), 'generated_at': timezone.now().isoformat(),
        'totals': totals, 'classes': list(classes.values()),
        'present_students': present_students, 'recap': recap,
    })


def _analytics_day_data(selected_day, class_name=None):
    start = timezone.make_aware(datetime.combine(selected_day, time.min), timezone.get_current_timezone())
    end = start + timedelta(days=1)
    students_qs = Student.objects.filter(active=True)
    if class_name is not None:
        students_qs = students_qs.filter(class_name=class_name)
    students = list(students_qs.order_by('class_name', 'name'))
    scans = Scan.objects.filter(
        result=Scan.Result.ACCEPTED, estimated_time__gte=start, estimated_time__lt=end,
        badge__student__in=students,
    ).select_related('badge__student', 'checkpoint').order_by('estimated_time', 'pk')
    scan_map = {student.pk: [] for student in students}
    for scan in scans:
        scan_map[scan.badge.student_id].append(scan)
    records = []
    for student in students:
        student_scans = scan_map[student.pk]
        entry_scans = [scan for scan in student_scans if scan.direction == RouteWindow.Direction.ENTRY]
        exit_scans = [scan for scan in student_scans if scan.direction == RouteWindow.Direction.EXIT]
        latest = student_scans[-1] if student_scans else None
        presence = 'present' if latest and latest.direction == RouteWindow.Direction.ENTRY else ('exited' if latest else 'absent')
        records.append({
            'id': student.pk, 'name': student.name, 'class_name': student.class_name,
            'status': presence,
            'entered_at': [scan.estimated_time.isoformat() for scan in entry_scans],
            'exited_at': [scan.estimated_time.isoformat() for scan in exit_scans],
            'events': [{
                'direction': scan.direction, 'time': scan.estimated_time.isoformat(),
                'checkpoint': scan.checkpoint.label if scan.checkpoint else None,
            } for scan in student_scans],
        })
    return records


def _late_days_by_student(students, through_day):
    if not students:
        return {}
    week_start = through_day - timedelta(days=6)
    start = timezone.make_aware(datetime.combine(week_start, time.min), timezone.get_current_timezone())
    end = timezone.make_aware(datetime.combine(through_day + timedelta(days=1), time.min), timezone.get_current_timezone())
    anomalies = Anomaly.objects.filter(
        student__in=students, kind='hors_fenetre',
        occurred_at__gte=start, occurred_at__lt=end,
        scan__result=Scan.Result.ACCEPTED, scan__direction=RouteWindow.Direction.ENTRY,
    ).select_related('student', 'scan', 'scan__checkpoint')
    windows = RouteWindow.objects.filter(
        student__in=students, active=True, direction=RouteWindow.Direction.ENTRY,
    ).select_related('checkpoint').prefetch_related('exceptions')
    route_map = {}
    for window in windows:
        route_map.setdefault((window.student_id, window.checkpoint_id), []).append(window)
    late_days = {}
    for anomaly in anomalies:
        scan = anomaly.scan
        local = timezone.localtime(scan.estimated_time)
        day = local.date()
        route_windows = route_map.get((anomaly.student_id, scan.checkpoint_id), [])
        is_late = False
        for window in route_windows:
            if day.isoweekday() not in window.days:
                continue
            times = _window_times(window, day)
            if times and local.time().replace(tzinfo=None) > times[1]:
                is_late = True
                break
        if is_late:
            late_days.setdefault(anomaly.student_id, set()).add(day.isoformat())
    return late_days


@api_view(['GET'])
@permission_classes([IsAdmin])
def school_analytics(request):
    try:
        selected_day = date.fromisoformat(request.query_params.get('date', str(timezone.localdate())))
    except ValueError:
        return Response({'detail': 'Date invalide.'}, status=status.HTTP_400_BAD_REQUEST)
    AuditLog.objects.create(user=request.user, action='consulted_school_analytics')
    records = _analytics_day_data(selected_day)
    student_ids = [record['id'] for record in records]
    late_days = _late_days_by_student(student_ids, selected_day)
    classes = {}
    for record in records:
        group = classes.setdefault(record['class_name'], {
            'class_name': record['class_name'], 'total': 0, 'present': 0, 'exited': 0,
            'absent': 0, 'entered': 0, 'late_students': 0,
        })
        group['total'] += 1
        group[record['status']] += 1
        group['entered'] += bool(record['entered_at'])
        group['late_students'] += len(late_days.get(record['id'], set())) >= 3
    return Response({
        'date': selected_day.isoformat(), 'generated_at': timezone.now().isoformat(),
        'classes': sorted(classes.values(), key=lambda item: item['class_name'].casefold()),
    })


@api_view(['GET'])
@permission_classes([IsAdmin])
def class_analytics(request):
    class_name = request.query_params.get('class_name', '').strip()
    if not class_name:
        return Response({'detail': 'La classe est obligatoire.'}, status=status.HTTP_400_BAD_REQUEST)
    try:
        selected_day = date.fromisoformat(request.query_params.get('date', str(timezone.localdate())))
    except ValueError:
        return Response({'detail': 'Date invalide.'}, status=status.HTTP_400_BAD_REQUEST)
    AuditLog.objects.create(user=request.user, action='consulted_class_analytics')
    records = _analytics_day_data(selected_day, class_name)
    late_days = _late_days_by_student([record['id'] for record in records], selected_day)
    for record in records:
        record['late_days_last_7'] = len(late_days.get(record['id'], set()))
    hourly = [{'hour': hour, 'entries': 0, 'exits': 0} for hour in range(6, 19)]
    for record in records:
        for event in record['events']:
            local = timezone.localtime(datetime.fromisoformat(event['time']))
            if 6 <= local.hour <= 18:
                hourly[local.hour - 6]['entries' if event['direction'] == 'entry' else 'exits'] += 1
    totals = {
        'total': len(records),
        'present': sum(record['status'] == 'present' for record in records),
        'exited': sum(record['status'] == 'exited' for record in records),
        'absent': sum(record['status'] == 'absent' for record in records),
        'entered': sum(bool(record['entered_at']) for record in records),
        'late_students': sum(record['late_days_last_7'] >= 3 for record in records),
    }
    return Response({
        'date': selected_day.isoformat(), 'class_name': class_name, 'generated_at': timezone.now().isoformat(),
        'totals': totals, 'students': records, 'hourly': hourly,
    })


@api_view(['GET'])
@permission_classes([IsAdmin])
def student_attendance_history(request, student_id):
    try:
        selected_day = date.fromisoformat(request.query_params.get('date', str(timezone.localdate())))
    except ValueError:
        return Response({'detail': 'Date invalide.'}, status=status.HTTP_400_BAD_REQUEST)
    student = Student.objects.filter(pk=student_id, active=True).first()
    if student is None:
        return Response({'detail': 'Élève introuvable.'}, status=status.HTTP_404_NOT_FOUND)
    AuditLog.objects.create(user=request.user, action='consulted_student_attendance')
    records = _analytics_day_data(selected_day, student.class_name)
    record = next((item for item in records if item['id'] == student.pk), None)
    late_days = _late_days_by_student([student.pk], selected_day)
    return Response({
        'date': selected_day.isoformat(), 'student': {'id': student.pk, 'name': student.name, 'class_name': student.class_name},
        'status': record['status'] if record else 'absent', 'events': record['events'] if record else [],
        'late_days_last_7': len(late_days.get(student.pk, set())),
    })


class CheckpointViewSet(viewsets.ModelViewSet):
    queryset = Checkpoint.objects.all()
    serializer_class = CheckpointSerializer

    def get_permissions(self):
        return [IsAdmin()] if self.action in ('create', 'update', 'partial_update', 'destroy') else [IsStaffRole()]


class BadgeViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = BadgeSerializer
    permission_classes = [IsAdmin]
    queryset = Badge.objects.select_related('student', 'replaced_by').all().order_by('-issued_at')


class ScanViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = ScanSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = Scan.objects.select_related('badge', 'checkpoint', 'badge__student').order_by('-received_at')
        if self.request.user.role == 'parent':
            return qs.filter(badge__student__parents=self.request.user)
        if self.request.user.role in ('admin', 'agent'):
            return qs[:500]
        return qs.none()


class AnomalyViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = AnomalySerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = Anomaly.objects.select_related('student').order_by('-occurred_at')
        if self.request.user.role == 'parent':
            return qs.filter(student__parents=self.request.user)
        return qs if self.request.user.role == 'admin' else qs.none()


class MessageViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = MessageSerializer
    permission_classes = [IsAdmin]
    queryset = OutboxMessage.objects.select_related('parent', 'anomaly').all()


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def me(request):
    student_id = getattr(getattr(request.user, 'student_profile', None), 'id', None) if request.user.role == 'student' else None
    return Response({'id': request.user.id, 'name': request.user.get_full_name() or request.user.username, 'role': request.user.role, 'studentId': student_id})


@ensure_csrf_cookie
def session_csrf(request):
    from django.middleware.csrf import get_token
    return JsonResponse({'csrfToken': get_token(request)})


@csrf_protect
def session_login(request):
    if request.method != 'POST':
        return JsonResponse({'detail': 'Méthode non autorisée.'}, status=405)
    try:
        body = json.loads(request.body)
    except (TypeError, ValueError):
        body = {}
    serializer = SessionLoginSerializer(data=body)
    if not serializer.is_valid():
        return JsonResponse({'detail': 'Identifiant ou mot de passe invalide.'}, status=401)
    user = authenticate(
        request,
        username=serializer.validated_data['username'],
        password=serializer.validated_data['password'],
    )
    if user is None or not user.is_active:
        return JsonResponse({'detail': 'Identifiant ou mot de passe invalide.'}, status=401)
    django_login(request, user)
    return JsonResponse({'authenticated': True})


@api_view(['POST'])
@permission_classes([IsAdmin])
def enroll_device(request):
    serializer = DeviceEnrollmentSerializer(data=request.data)
    if not serializer.is_valid():
        return Response({'detail': 'Requête invalide.'}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
    checkpoint = Checkpoint.objects.filter(
        pk=serializer.validated_data['checkpoint_id'],
    ).first()
    if checkpoint is None:
        return Response({'detail': 'Point de contrôle invalide.'}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
    device_id = serializer.validated_data.get('device_id')
    if device_id and Device.objects.filter(device_id=device_id).exists():
        return Response({'detail': 'Cet appareil est déjà enrôlé.'}, status=status.HTTP_409_CONFLICT)
    device_token = secrets.token_urlsafe(32)
    device = Device.objects.create(
        **({'device_id': device_id} if device_id else {}),
        token_hash=hashlib.sha256(device_token.encode('utf-8')).hexdigest(),
    )
    DeviceAssignment.objects.create(device=device, checkpoint=checkpoint)
    return Response({
        'device_id': str(device.device_id),
        'device_token': device_token,
        'checkpoint': checkpoint.label,
    }, status=status.HTTP_201_CREATED)


@api_view(['POST'])
@permission_classes([IsAdmin])
def reassign_device(request, device_id):
    device = Device.objects.filter(device_id=device_id).first()
    serializer = DeviceEnrollmentSerializer(data=request.data)
    if device is None or not device.active:
        return Response({'detail': 'Appareil introuvable.'}, status=status.HTTP_404_NOT_FOUND)
    if not serializer.is_valid():
        return Response({'detail': 'Requête invalide.'}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
    checkpoint = Checkpoint.objects.filter(
        pk=serializer.validated_data['checkpoint_id'],
    ).first()
    if checkpoint is None:
        return Response({'detail': 'Point de contrôle invalide.'}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
    now = timezone.now()
    with transaction.atomic():
        DeviceAssignment.objects.filter(device=device, ends_at__isnull=True).update(ends_at=now)
        DeviceAssignment.objects.create(
            device=device, checkpoint=checkpoint, starts_at=now,
        )
    return Response({'device_id': str(device.device_id), 'checkpoint': checkpoint.label})


@api_view(['PATCH'])
@permission_classes([IsAdmin])
def set_device_state(request, device_id):
    device = Device.objects.filter(device_id=device_id).first()
    serializer = DeviceStateSerializer(data=request.data if isinstance(request.data, dict) else {})
    if device is None or not serializer.is_valid():
        return Response({'detail': 'Appareil ou état invalide.'}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
    device.active = serializer.validated_data['active']
    device.save(update_fields=['active'])
    return Response({'device_id': str(device.device_id), 'active': device.active})


@api_view(['POST'])
@authentication_classes([SessionAuthentication])
@permission_classes([IsAgent])
def sync_passages(request):
    device_token = request.headers.get('X-Device-Token', '')
    token_hash = hashlib.sha256(device_token.encode('utf-8')).hexdigest()
    device = Device.objects.filter(token_hash=token_hash, active=True).first()
    if device is None:
        return Response({'detail': 'Appareil non autorisé.'}, status=status.HTTP_403_FORBIDDEN)
    events = request.data.get('events') if isinstance(request.data, dict) else None
    if not isinstance(events, list) or not events or len(events) > 100:
        return Response({'detail': 'Lot invalide.'}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
    validated = []
    for raw in events:
        if not isinstance(raw, dict):
            return Response({'detail': 'Événement invalide.'}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        serializer = PassageSyncEventSerializer(data=raw)
        if not serializer.is_valid():
            return Response({'detail': 'Événement invalide.'}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        validated.append(serializer.validated_data)
    result = sync_passage_events(device, validated)
    return Response({
        'acks': result['acks'],
        'server_time': result['server_time'].isoformat(),
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def my_badge_credential(request):
    if request.user.role != 'student':
        return Response({'detail': 'Accès réservé aux élèves.'}, status=status.HTTP_403_FORBIDDEN)
    student = getattr(request.user, 'student_profile', None)
    if not student or not student.active:
        return Response({'detail': 'Compte élève sans badge actif.'}, status=status.HTTP_404_NOT_FOUND)
    service_token = settings.STUDENT_QR_SERVICE_TOKEN
    if len(service_token) < 32:
        return Response(
            {'detail': 'Le service QR élève n’est pas configuré.'},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
            headers={'Retry-After': '5'},
        )
    unix_seconds = timezone.now().timestamp()
    payload = json.dumps({
        'user_id': request.user.pk,
        'unix_seconds': unix_seconds,
    }).encode('utf-8')
    service_request = Request(
        f"{settings.STUDENT_QR_SERVICE_URL.rstrip('/')}/v1/token",
        data=payload,
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {service_token}',
        },
        method='POST',
    )
    try:
        with urlopen(service_request, timeout=2) as service_response:
            generated = json.loads(service_response.read())
        if (
            not isinstance(generated, dict)
            or not isinstance(generated.get('badge_id'), str)
            or not isinstance(generated.get('token'), str)
            or not isinstance(generated.get('counter'), int)
            or not isinstance(generated.get('expires_at'), (int, float))
        ):
            raise ValueError('Invalid QR service response.')
    except HTTPError as error:
        if error.code == 404:
            return Response({'detail': 'Aucun badge actif pour cet élève.'}, status=status.HTTP_404_NOT_FOUND)
        return Response(
            {'detail': 'Le service de génération QR est temporairement indisponible.'},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
            headers={'Retry-After': '2'},
        )
    except (URLError, TimeoutError, OSError, ValueError, TypeError):
        return Response(
            {'detail': 'Le service de génération QR est temporairement indisponible.'},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
            headers={'Retry-After': '2'},
        )
    return Response({
        'badge_id': generated['badge_id'],
        'token': generated['token'],
        'counter': generated['counter'],
        'expires_at': generated['expires_at'],
        'server_time': timezone.now().isoformat(),
    })


@api_view(['POST'])
@authentication_classes([SessionAuthentication])
@permission_classes([IsAgent])
def device_time_sync(request):
    device_token = request.headers.get('X-Device-Token', '')
    token_hash = hashlib.sha256(device_token.encode('utf-8')).hexdigest()
    device = Device.objects.filter(token_hash=token_hash, active=True).first()
    if device is None:
        return Response({'detail': 'Appareil non autorisé.'}, status=status.HTTP_403_FORBIDDEN)
    payload = request.data if isinstance(request.data, dict) else {}
    serializer = DeviceTimeSyncSerializer(data={
        'deviceId': str(device.device_id),
        'localTime': payload.get('local_time', payload.get('localTime')),
    })
    if not serializer.is_valid():
        return Response({'detail': 'Requête invalide.'}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
    server_time = timezone.now()
    offset = record_device_clock(serializer.validated_data['deviceId'], serializer.validated_data['localTime'], server_time)
    return Response({'serverTime': server_time.isoformat(), 'offsetSeconds': offset})


@api_view(['PATCH'])
@permission_classes([IsAdmin])
def set_badge_state(request, badge_id):
    badge = Badge.objects.filter(badge_id=badge_id).first()
    if badge is None:
        return Response({'detail': 'Badge introuvable.'}, status=status.HTTP_404_NOT_FOUND)
    serializer = BadgeStateSerializer(data=request.data if isinstance(request.data, dict) else {})
    if not serializer.is_valid():
        return Response({'detail': 'État invalide.'}, status=status.HTTP_400_BAD_REQUEST)
    new_state = serializer.validated_data['state']
    if badge.state == Badge.State.REVOKED and new_state != Badge.State.REVOKED:
        return Response({'detail': 'Un badge révoqué ne peut pas être réactivé.'}, status=status.HTTP_400_BAD_REQUEST)
    badge.state = new_state
    badge.save(update_fields=['state'])
    return Response(BadgeSerializer(badge).data)


@api_view(['GET'])
@permission_classes([IsAdmin])
def daily_summary(request):
    class_name = request.query_params.get('class', '6e A')
    day = request.query_params.get('date', str(timezone.localdate()))
    try:
        date.fromisoformat(day)
    except ValueError:
        return Response({'detail': 'Date invalide'}, status=status.HTTP_400_BAD_REQUEST)
    return Response(summarize(summary_stats(class_name, day)))


@api_view(['POST'])
@permission_classes([IsAdmin])
def close_anomalies(request):
    return Response({'created': close_windows()})


@api_view(['GET'])
@permission_classes([IsAdmin])
def non_treated_anomaly_count(request):
    count = Anomaly.objects.filter(
        state__in=[Anomaly.State.OPEN, Anomaly.State.PROVISIONAL],
    ).count()
    return Response({'non_treated': count})


@api_view(['POST'])
@authentication_classes([SessionAuthentication])
@permission_classes([AllowAny])
def logout(request):
    data = request.data if isinstance(request.data, dict) else {}
    token = request.auth
    access_value = data.get('access')
    if not isinstance(access_value, str) or not access_value:
        authorization = request.headers.get('Authorization', '').split()
        access_value = authorization[1] if len(authorization) == 2 and authorization[0].lower() == 'bearer' else ''
    if token is None and isinstance(access_value, str) and 0 < len(access_value) <= 4096:
        try:
            token = AccessToken(access_value)
        except TokenError:
            # An expired access token can no longer authorize requests.
            token = None
    if token is not None:
        RevokedAccessToken.objects.get_or_create(
            jti=token.get('jti'),
            defaults={
                'expires_at': datetime.fromtimestamp(
                    token.get('exp'), tz=dt_timezone.utc,
                ),
            },
        )
    # Always destroy a Django session too, even when the request also carries JWTs.
    django_logout(request)
    refresh = data.get('refresh')
    if isinstance(refresh, str) and 0 < len(refresh) <= 4096:
        try:
            RefreshToken(refresh).blacklist()
        except TokenError:
            pass
    return Response(status=status.HTTP_204_NO_CONTENT)
