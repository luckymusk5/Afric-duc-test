from rest_framework.routers import DefaultRouter
from django.urls import path,include
from .views import StudentViewSet,CheckpointViewSet,ScanViewSet,AnomalyViewSet,MessageViewSet,me,badge_qr,sync_scans,daily_summary,close_anomalies,sync_status
router=DefaultRouter(); router.register('students',StudentViewSet,basename='student'); router.register('checkpoints',CheckpointViewSet); router.register('scans',ScanViewSet,basename='scan'); router.register('anomalies',AnomalyViewSet,basename='anomaly'); router.register('outbox',MessageViewSet)
urlpatterns=[path('scans/sync/',sync_scans),path('scans/sync/status/',sync_status),path('summaries/daily/',daily_summary),path('anomalies/close-windows/',close_anomalies),path('me/',me),path('students/<int:student_id>/badge-token/',badge_qr),path('',include(router.urls))]
