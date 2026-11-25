from django.test import TestCase,override_settings
from django.contrib.auth import get_user_model
from django.utils import timezone
from datetime import timedelta
from rest_framework.test import APIClient
from .models import Student,Checkpoint,Badge,RouteWindow,Scan,Anomaly,SyncReceipt
from .services import badge_token,accept_scan,summary_stats,summarize
from .views import BrokerError
from rest_framework_simplejwt.tokens import AccessToken,RefreshToken
import uuid,os,re,json
from unittest.mock import patch,Mock
User=get_user_model()
class PassageTests(TestCase):
 def setUp(self):
  self.admin=User.objects.create_user(username='admin',password='AdminPass123!',role='admin')
  self.agent=User.objects.create_user(username='agent',password='AgentPass123!',role='agent')
  self.parent=User.objects.create_user(username='parent',password='ParentPass123!',role='parent')
  self.other=User.objects.create_user(username='other',password='ParentPass123!',role='parent')
  self.student=Student.objects.create(name='Élève A',class_name='6e A',school='Test');self.student.parents.add(self.parent)
  self.hidden=Student.objects.create(name='Élève B',class_name='6e A',school='Test');self.hidden.parents.add(self.other)
  self.cp=Checkpoint.objects.create(label='Entrée');self.badge=Badge.objects.create(student=self.student,secret='x')
  RouteWindow.objects.create(student=self.student,checkpoint=self.cp,sequence=1,start_time='07:00',end_time='07:45',days=[1,2,3,4,5])
 def test_event_idempotency(self):
  data={'eventId':uuid.uuid4(),'token':badge_token(self.badge),'checkpointId':self.cp.id,'localTime':timezone.now(),'deviceId':'device-1'}
  first=accept_scan(data);second=accept_scan(data)
  self.assertFalse(first['duplicate']);self.assertTrue(second['duplicate']);self.assertEqual(Scan.objects.count(),1)
 def test_replayed_badge_is_rejected_and_logged(self):
  token=badge_token(self.badge)
  first={'eventId':uuid.uuid4(),'token':token,'checkpointId':self.cp.id,'localTime':timezone.now(),'deviceId':'device-1'}
  accept_scan(first)
  replay={**first,'eventId':uuid.uuid4()}
  result=accept_scan(replay)
  self.assertEqual(result.get('rejected'),'Code déjà utilisé')
  self.assertTrue(Anomaly.objects.filter(kind='badge_replay').exists())
 def test_clock_skew_uses_server_time(self):
  data={'eventId':uuid.uuid4(),'token':badge_token(self.badge),'checkpointId':self.cp.id,'localTime':timezone.now()-timedelta(hours=2),'deviceId':'device-1'}
  result=accept_scan(data);scan=Scan.objects.get(pk=result['id'])
  self.assertTrue(scan.clock_skew);self.assertTrue(Anomaly.objects.filter(kind='horloge_appareil').exists())
 def test_parent_cannot_read_other_student_by_id(self):
  client=APIClient();client.force_authenticate(self.parent)
  self.assertEqual(client.get(f'/api/students/{self.hidden.id}/').status_code,404)
  self.assertEqual(list(client.get('/api/students/').data)[0]['id'],self.student.id)
 def test_scan_sync_is_queued_and_status_is_queryable(self):
  event_id=uuid.uuid4();payload={'eventId':str(event_id),'token':badge_token(self.badge),'checkpointId':self.cp.id,'localTime':timezone.now().isoformat(),'deviceId':'device-1'}
  client=APIClient();client.force_authenticate(self.agent)
  task=Mock();task.apply_async.return_value.id=str(event_id)
  with patch('passage.views.process_scan',task):
   response=client.post('/api/scans/sync/',{'scans':[payload]},format='json')
  self.assertEqual(response.status_code,200);self.assertEqual(response.data['queued'][0]['state'],'queued')
  receipt=SyncReceipt.objects.get(event_id=event_id);self.assertEqual(receipt.state,'queued')
  status_response=client.get(f'/api/scans/sync/status/?eventIds={event_id}')
  self.assertEqual(status_response.data['items'][0]['state'],'queued')
 def test_scan_is_kept_pending_when_broker_publish_fails(self):
  event_id=uuid.uuid4();payload={'eventId':str(event_id),'token':badge_token(self.badge),'checkpointId':self.cp.id,'localTime':timezone.now().isoformat(),'deviceId':'device-1'}
  client=APIClient();client.force_authenticate(self.agent)
  task=Mock();task.apply_async.side_effect=BrokerError('broker unavailable')
  with patch('passage.views.process_scan',task):
   response=client.post('/api/scans/sync/',{'scans':[payload]},format='json')
  self.assertEqual(response.status_code,200);self.assertEqual(len(response.data['errors']),1)
  receipt=SyncReceipt.objects.get(event_id=event_id);self.assertEqual(receipt.state,'pending');self.assertTrue(receipt.payload)
 def test_logout_revokes_current_access_token(self):
  access=AccessToken.for_user(self.parent);refresh=RefreshToken.for_user(self.parent)
  client=APIClient();client.credentials(HTTP_AUTHORIZATION=f'Bearer {access}')
  self.assertEqual(client.get('/api/me/').status_code,200)
  self.assertEqual(client.post('/api/auth/logout/',{'refresh':str(refresh)},format='json').status_code,204)
  self.assertEqual(client.get('/api/me/').status_code,401)
 def test_agent_cannot_open_admin_student_list(self):
  client=APIClient();client.force_authenticate(self.agent)
  self.assertEqual(client.get('/api/students/').status_code,200)
  self.assertEqual(client.get('/api/students/').data,[])
 @override_settings(ALLOWED_HOSTS=['testserver'])
 def test_summary_template_keeps_exact_computed_counts(self):
  stats=summary_stats('6e A',timezone.localdate());result=summarize(stats)
  self.assertEqual(result['origin'],'template')
  self.assertEqual(result['stats'],stats)
  allowed=set(re.findall(r'\d+',json.dumps(stats)))
  self.assertTrue(set(re.findall(r'\d+',result['text'])).issubset(allowed))
  for number in (stats['total'],stats['arrived'],stats['late'],stats['anomalies']):self.assertIn(str(number),result['text'])
