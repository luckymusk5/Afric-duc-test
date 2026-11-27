from rest_framework.routers import DefaultRouter
from django.urls import path,include
from .views import StudentViewSet,CheckpointViewSet,ScanViewSet,AnomalyViewSet,MessageViewSet,BadgeViewSet,me,my_badge_credential,sync_passages,daily_summary,daily_attendance,school_analytics,class_analytics,student_attendance_history,parents,agents,close_anomalies,device_time_sync,set_badge_state,enroll_device,reassign_device,set_device_state,session_csrf,session_login,non_treated_anomaly_count
router=DefaultRouter(); router.register('students',StudentViewSet,basename='student'); router.register('checkpoints',CheckpointViewSet); router.register('scans',ScanViewSet,basename='scan'); router.register('anomalies',AnomalyViewSet,basename='anomaly'); router.register('outbox',MessageViewSet); router.register('badges',BadgeViewSet,basename='badge')
urlpatterns=[
    path('auth/csrf/',session_csrf),
    path('auth/session/',session_login),
    path('passages/sync',sync_passages),
    path('devices/',enroll_device),
    path('devices/<uuid:device_id>/assign/',reassign_device),
    path('devices/<uuid:device_id>/state/',set_device_state),
    path('devices/time-sync/',device_time_sync),
    path('badges/<uuid:badge_id>/state/',set_badge_state),
    path('summaries/daily/',daily_summary),
    path('attendance/daily/',daily_attendance),
    path('analytics/school/',school_analytics),
    path('analytics/class/',class_analytics),
    path('analytics/students/<int:student_id>/history/',student_attendance_history),
    path('parents/',parents),
    path('agents/',agents),
    path('anomalies/close-windows/',close_anomalies),
    path('anomalies/non-treated-count/',non_treated_anomaly_count),
    path('my/badge/',my_badge_credential),
    path('me/',me),
    path('',include(router.urls)),
]
