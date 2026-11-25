from datetime import date
from django.utils import timezone
from rest_framework import viewsets
from rest_framework.decorators import api_view,permission_classes
from rest_framework.permissions import IsAuthenticated,AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from uuid import UUID
try:
    from celery.exceptions import OperationalError as BrokerError
    from .tasks import process_scan
except ModuleNotFoundError as exc:
    if exc.name not in ('celery','kombu'):
        raise
    BrokerError=RuntimeError
    process_scan=None
from rest_framework_simplejwt.views import TokenObtainPairView
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.exceptions import TokenError
from datetime import datetime,timezone as dt_timezone
from .models import RevokedAccessToken
from .models import Student,Checkpoint,Badge,Scan,Anomaly,OutboxMessage,SyncReceipt
from .serializers import StudentSerializer,CheckpointSerializer,ScanInputSerializer,ScanSerializer,AnomalySerializer,MessageSerializer
from .permissions import IsAdmin,IsStaffRole
from .services import badge_token,verify_token,summary_stats,summarize,close_windows
class LoginThrottle(AnonRateThrottle):
    rate='5/minute'
class ThrottledLoginView(TokenObtainPairView):
    permission_classes=[AllowAny]
    throttle_classes=[LoginThrottle]
class StudentViewSet(viewsets.ModelViewSet):
    serializer_class=StudentSerializer
    def get_queryset(self):
        if self.request.user.role=='admin':return Student.objects.all()
        if self.request.user.role=='parent':return Student.objects.filter(parents=self.request.user)
        return Student.objects.none()
    def get_permissions(self):
        return [IsAdmin()] if self.action in ('create','update','partial_update','destroy') else [IsAuthenticated()]
class CheckpointViewSet(viewsets.ModelViewSet):
    queryset=Checkpoint.objects.all(); serializer_class=CheckpointSerializer; permission_classes=[IsAdmin]
class ScanViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class=ScanSerializer
    permission_classes=[IsAuthenticated]
    def get_queryset(self):
        qs=Scan.objects.select_related('badge','checkpoint','badge__student').order_by('-received_at')
        if self.request.user.role=='parent':return qs.filter(badge__student__parents=self.request.user)
        if self.request.user.role in ('admin','agent'):return qs[:500]
        return qs.none()
class AnomalyViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class=AnomalySerializer
    def get_queryset(self):
        qs=Anomaly.objects.select_related('student').order_by('-occurred_at')
        if self.request.user.role=='parent':return qs.filter(student__parents=self.request.user)
        return qs if self.request.user.role=='admin' else qs.none()
class MessageViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class=MessageSerializer; permission_classes=[IsAdmin]
    queryset=OutboxMessage.objects.select_related('parent','anomaly').all()
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def me(request):
    return Response({'id':request.user.id,'name':request.user.get_full_name() or request.user.username,'role':request.user.role})
@api_view(['GET'])
@permission_classes([IsAdmin])
def badge_qr(request,student_id):
    badge=Badge.objects.filter(student_id=student_id,state='active').first()
    if not badge:return Response({'detail':'Badge actif introuvable'},status=404)
    return Response({'token':badge_token(badge),'expiresInSeconds':60})
@api_view(['POST'])
@permission_classes([IsStaffRole])
def sync_scans(request):
    items=request.data.get('scans')
    if not isinstance(items,list) or len(items)>100:return Response({'detail':'Lot invalide (100 scans maximum).'},status=400)
    queued=[]; errors=[]
    for raw in items:
        if not isinstance(raw,dict):errors.append({'error':'Données invalides'});continue
        serializer=ScanInputSerializer(data=raw)
        if not serializer.is_valid():errors.append({'eventId':raw.get('eventId'),'error':'Données invalides'});continue
        data=serializer.validated_data;event_id=data['eventId']
        stored=Scan.objects.filter(event_id=event_id).first()
        if stored:
            queued.append({'eventId':str(event_id),'state':'processed'});continue
        receipt=SyncReceipt.objects.filter(event_id=event_id).first()
        if receipt and receipt.state=='processed':
            queued.append({'eventId':str(event_id),'state':'processed'});continue
        if receipt and receipt.state in ('queued','processing'):
            queued.append({'eventId':str(event_id),'state':receipt.state});continue
        if receipt and receipt.state in ('rejected','failed'):
            errors.append({'eventId':str(event_id),'error':receipt.error or 'Synchronisation en échec.'});continue
        if receipt:
            payload=receipt.payload
        else:
            if not verify_token(data['token']):
                errors.append({'eventId':str(event_id),'error':'Badge invalide ou expiré'});continue
            if not Checkpoint.objects.filter(pk=data['checkpointId']).exists():
                errors.append({'eventId':str(event_id),'error':'Point de contrôle inconnu'});continue
            payload={'eventId':str(event_id),'token':data['token'],'checkpointId':data['checkpointId'],'localTime':data['localTime'].isoformat(),'deviceId':data['deviceId']}
            receipt=SyncReceipt.objects.create(event_id=event_id,payload=payload)
        receipt.state='queued';receipt.error='';receipt.save(update_fields=['state','error','updated_at'])
        try:
            if process_scan is None:raise BrokerError('Celery ne figure pas dans les dépendances installées.')
            result=process_scan.apply_async(args=[str(event_id)],task_id=str(event_id))
            receipt.task_id=result.id
            receipt.save(update_fields=['task_id','updated_at'])
            queued.append({'eventId':str(event_id),'state':'queued'})
        except BrokerError:
            receipt.state='pending';receipt.error='Celery/RabbitMQ indisponible; réessayez après installation ou rétablissement du broker.'
            receipt.save(update_fields=['state','error','updated_at'])
            errors.append({'eventId':str(event_id),'error':receipt.error})
    return Response({'queued':queued,'errors':errors})
@api_view(['GET'])
@permission_classes([IsStaffRole])
def sync_status(request):
    raw=request.query_params.get('eventIds','')
    try:
        ids=[UUID(value) for value in raw.split(',') if value]
    except ValueError:return Response({'detail':'Identifiant d’événement invalide.'},status=400)
    if len(ids)>100:return Response({'detail':'100 identifiants maximum.'},status=400)
    items=SyncReceipt.objects.filter(event_id__in=ids).values('event_id','state','error')
    return Response({'items':[{'eventId':str(x['event_id']),'state':x['state'],'error':x['error']} for x in items]})
@api_view(['GET'])
@permission_classes([IsAdmin])
def daily_summary(request):
    class_name=request.query_params.get('class','6e A'); day=request.query_params.get('date',str(timezone.localdate()))
    try:date.fromisoformat(day)
    except ValueError:return Response({'detail':'Date invalide'},status=400)
    return Response(summarize(summary_stats(class_name,day)))
@api_view(['POST'])
@permission_classes([IsAdmin])
def close_anomalies(request):
    return Response({'created':close_windows()})

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def logout(request):
    token=request.auth
    RevokedAccessToken.objects.get_or_create(jti=token.get('jti'),defaults={'expires_at':datetime.fromtimestamp(token.get('exp'),tz=dt_timezone.utc)})
    refresh=request.data.get('refresh')
    if refresh:
        try:RefreshToken(refresh).blacklist()
        except TokenError:pass
    return Response(status=204)
