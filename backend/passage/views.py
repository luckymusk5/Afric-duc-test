from datetime import date, datetime, timezone as dt_timezone
from uuid import UUID
from celery.exceptions import OperationalError as BrokerError
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework_simplejwt.views import TokenObtainPairView
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.exceptions import TokenError
from .models import Student, Checkpoint, Badge, Scan, Anomaly, OutboxMessage, SyncReceipt, RevokedAccessToken, DeviceClock
from .serializers import StudentSerializer, CheckpointSerializer, ScanInputSerializer, ScanSerializer, AnomalySerializer, MessageSerializer, BadgeSerializer, DeviceTimeSyncSerializer
from .permissions import IsAdmin, IsStaffRole
from .services import summary_stats, summarize, close_windows, create_anomaly
from .badge_security import BadgeRejected, reissue_badge, record_device_attempt, record_device_clock, validate_rotating_token
from .tasks import process_scan


class LoginThrottle(AnonRateThrottle):
    rate = '5/minute'


class ThrottledLoginView(TokenObtainPairView):
    permission_classes = [AllowAny]
    throttle_classes = [LoginThrottle]


class StudentViewSet(viewsets.ModelViewSet):
    serializer_class = StudentSerializer

    def get_queryset(self):
        if self.request.user.role == 'admin':
            return Student.objects.all()
        if self.request.user.role == 'parent':
            return Student.objects.filter(parents=self.request.user)
        return Student.objects.none()

    def get_permissions(self):
        if self.action in ('create', 'update', 'partial_update', 'destroy', 'reissue_badge'):
            return [IsAdmin()]
        return [IsAuthenticated()]

    @action(detail=True, methods=['post'], url_path='badges/reissue', permission_classes=[IsAdmin])
    def reissue_badge(self, request, pk=None):
        student = self.get_object()
        badge, enrollment = reissue_badge_for_student(student)
        return Response({'badge_id': str(badge.badge_id), 'enrollment': enrollment}, status=status.HTTP_201_CREATED)


# Keep the endpoint name distinct from the viewset action name.
reissue_badge_for_student = reissue_badge


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
    return Response({'id': request.user.id, 'name': request.user.get_full_name() or request.user.username, 'role': request.user.role})


@api_view(['POST'])
@permission_classes([IsStaffRole])
def device_time_sync(request):
    serializer = DeviceTimeSyncSerializer(data=request.data)
    if not serializer.is_valid():
        return Response({'detail': 'Requête invalide.'}, status=status.HTTP_400_BAD_REQUEST)
    server_time = timezone.now()
    offset = record_device_clock(serializer.validated_data['deviceId'], serializer.validated_data['localTime'], server_time)
    return Response({'serverTime': server_time.isoformat(), 'offsetSeconds': offset})


@api_view(['PATCH'])
@permission_classes([IsAdmin])
def set_badge_state(request, badge_id):
    badge = Badge.objects.filter(badge_id=badge_id).first()
    if badge is None:
        return Response({'detail': 'Badge introuvable.'}, status=status.HTTP_404_NOT_FOUND)
    new_state = request.data.get('state')
    if new_state not in Badge.State.values:
        return Response({'detail': 'État invalide.'}, status=status.HTTP_400_BAD_REQUEST)
    if badge.state == Badge.State.REVOKED and new_state != Badge.State.REVOKED:
        return Response({'detail': 'Un badge révoqué ne peut pas être réactivé.'}, status=status.HTTP_400_BAD_REQUEST)
    badge.state = new_state
    badge.save(update_fields=['state'])
    return Response(BadgeSerializer(badge).data)


@api_view(['POST'])
@permission_classes([IsStaffRole])
def sync_scans(request):
    items = request.data.get('scans')
    if not isinstance(items, list) or len(items) > 20:
        return Response({'detail': 'Lot invalide.'}, status=status.HTTP_400_BAD_REQUEST)
    queued = []
    errors = []
    for raw in items:
        if not isinstance(raw, dict):
            errors.append({'error': 'Badge refusé'})
            continue
        serializer = ScanInputSerializer(data=raw)
        if not serializer.is_valid():
            errors.append({'eventId': raw.get('eventId'), 'error': 'Badge refusé'})
            continue
        data = serializer.validated_data
        event_id = data['eventId']
        stored = Scan.objects.filter(event_id=event_id).first()
        if stored:
            queued.append({'eventId': str(event_id), 'state': 'processed'})
            continue
        receipt = SyncReceipt.objects.filter(event_id=event_id).first()
        if receipt and receipt.state == SyncReceipt.State.PROCESSED:
            queued.append({'eventId': str(event_id), 'state': 'processed'})
            continue
        if receipt and receipt.state in (SyncReceipt.State.QUEUED, SyncReceipt.State.PROCESSING):
            queued.append({'eventId': str(event_id), 'state': receipt.state})
            continue
        if receipt and receipt.state in (SyncReceipt.State.REJECTED, SyncReceipt.State.FAILED):
            errors.append({'eventId': str(event_id), 'error': 'Badge refusé'})
            continue
        if receipt:
            payload = receipt.payload
        else:
            device_id = data['deviceId']
            if not DeviceClock.objects.filter(device_id=device_id).exists():
                errors.append({'eventId': str(event_id), 'error': 'Badge refusé'})
                continue
            if not record_device_attempt(device_id):
                errors.append({'eventId': str(event_id), 'error': 'Badge refusé'})
                continue
            try:
                validate_rotating_token(data['token'], data['estimatedTime'])
            except BadgeRejected:
                errors.append({'eventId': str(event_id), 'error': 'Badge refusé'})
                continue
            if not Checkpoint.objects.filter(pk=data['checkpointId']).exists():
                errors.append({'eventId': str(event_id), 'error': 'Badge refusé'})
                continue
            payload = {
                'eventId': str(event_id),
                'token': data['token'],
                'checkpointId': data['checkpointId'],
                'localTime': data['localTime'].isoformat(),
                'estimatedTime': data['estimatedTime'].isoformat(),
                'deviceId': device_id,
                'clockOffsetSeconds': data['clockOffsetSeconds'],
            }
            receipt = SyncReceipt.objects.create(event_id=event_id, payload=payload)
        receipt.state = SyncReceipt.State.QUEUED
        receipt.error = ''
        receipt.save(update_fields=['state', 'error', 'updated_at'])
        try:
            result = process_scan.apply_async(args=[str(event_id)], task_id=str(event_id))
            receipt.task_id = result.id
            receipt.save(update_fields=['task_id', 'updated_at'])
            queued.append({'eventId': str(event_id), 'state': SyncReceipt.State.QUEUED})
        except BrokerError:
            receipt.state = SyncReceipt.State.PENDING
            receipt.error = 'RabbitMQ publication failed.'
            receipt.save(update_fields=['state', 'error', 'updated_at'])
            errors.append({'eventId': str(event_id), 'error': 'Badge refusé'})
    return Response({'queued': queued, 'errors': errors})


@api_view(['GET'])
@permission_classes([IsStaffRole])
def sync_status(request):
    raw = request.query_params.get('eventIds', '')
    try:
        ids = [UUID(value) for value in raw.split(',') if value]
    except ValueError:
        return Response({'detail': 'Identifiant invalide.'}, status=status.HTTP_400_BAD_REQUEST)
    if len(ids) > 100:
        return Response({'detail': 'Trop d’identifiants.'}, status=status.HTTP_400_BAD_REQUEST)
    items = SyncReceipt.objects.filter(event_id__in=ids).values('event_id', 'state')
    return Response({'items': [{'eventId': str(item['event_id']), 'state': item['state'], 'error': 'Badge refusé' if item['state'] in ('rejected', 'failed') else ''} for item in items]})


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


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def logout(request):
    token = request.auth
    RevokedAccessToken.objects.get_or_create(jti=token.get('jti'), defaults={'expires_at': datetime.fromtimestamp(token.get('exp'), tz=dt_timezone.utc)})
    refresh = request.data.get('refresh')
    if refresh:
        try:
            RefreshToken(refresh).blacklist()
        except TokenError:
            pass
    return Response(status=status.HTTP_204_NO_CONTENT)
