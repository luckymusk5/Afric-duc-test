import base64
import os
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from .badge_security import (BadgeRejected, create_badge, decrypt_secret,
    rfc6238_code, rotating_token, validate_rotating_token)
from .models import (Anomaly, Badge, BadgeUsage, Checkpoint, DeviceClock,
    RouteWindow, Scan, Student)
from .services import accept_scan, summarize, summary_stats

User = get_user_model()
TEST_KEY = base64.urlsafe_b64encode(bytes(range(32))).rstrip(b'=').decode()


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class PassageTests(TestCase):
    def setUp(self):
        self.key_patch = patch.dict(os.environ, {'BADGE_SECRET_KEY': TEST_KEY})
        self.key_patch.start()
        self.addCleanup(self.key_patch.stop)
        self.admin = User.objects.create_user(username='admin', password='AdminPass123!', role='admin')
        self.agent = User.objects.create_user(username='agent', password='AgentPass123!', role='agent')
        self.parent = User.objects.create_user(username='parent', password='ParentPass123!', role='parent')
        self.other = User.objects.create_user(username='other', password='ParentPass123!', role='parent')
        self.student = Student.objects.create(name='Élève A', class_name='6e A', school='Test')
        self.student.parents.add(self.parent)
        self.hidden = Student.objects.create(name='Élève B', class_name='6e A', school='Test')
        self.hidden.parents.add(self.other)
        self.cp = Checkpoint.objects.create(label='Entrée')
        self.badge, self.enrollment = create_badge(self.student)
        self.secret = decrypt_secret(self.badge.secret_ciphertext, self.badge.badge_id)
        self.now = timezone.now().replace(microsecond=0)
        self.device = 'device-1'
        DeviceClock.objects.create(device_id=self.device, offset_seconds=0, synchronized_at=self.now)
        RouteWindow.objects.create(student=self.student, checkpoint=self.cp, sequence=1,
            start_time='00:00', end_time='23:59', days=[1, 2, 3, 4, 5, 6, 7])

    def payload(self, event_id=None, token=None, when=None, device=None, local=None, offset=0):
        when = when or self.now
        return {'eventId': event_id or uuid.uuid4(),
            'token': token or rotating_token(self.badge.badge_id, self.secret, when.timestamp()),
            'checkpointId': self.cp.id, 'localTime': local or when,
            'estimatedTime': when, 'deviceId': device or self.device,
            'clockOffsetSeconds': offset}

    def test_valid_rotating_token_is_accepted(self):
        result = accept_scan(self.payload(), now=self.now)
        self.assertFalse(result['duplicate'])
        self.assertEqual(Scan.objects.count(), 1)

    def test_replay_counter_with_new_event_is_rejected(self):
        accept_scan(self.payload(), now=self.now)
        with self.assertRaises(BadgeRejected):
            accept_scan(self.payload(), now=self.now)
        self.assertEqual(BadgeUsage.objects.count(), 1)

    def test_token_older_than_sixty_seconds_is_rejected(self):
        old = self.now - timedelta(seconds=61)
        token = rotating_token(self.badge.badge_id, self.secret, old.timestamp())
        with self.assertRaises(BadgeRejected):
            validate_rotating_token(token, self.now, now=self.now)

    def test_tampered_signature_is_rejected(self):
        data = self.payload()
        badge_id, counter, signature = data['token'].split('.')
        replacement = 'A' if signature[0] != 'A' else 'B'
        data['token'] = badge_id + '.' + counter + '.' + replacement + signature[1:]
        with self.assertRaises(BadgeRejected):
            accept_scan(data, now=self.now)

    def test_inactive_badge_is_rejected_and_anomaly_recorded(self):
        for state in (Badge.State.SUSPENDED, Badge.State.LOST, Badge.State.REVOKED):
            self.badge.state = state
            self.badge.save(update_fields=['state'])
            with self.assertRaises(BadgeRejected):
                validate_rotating_token(rotating_token(self.badge.badge_id, self.secret, self.now.timestamp()), self.now, now=self.now)
            self.badge.state = Badge.State.ACTIVE
        self.assertTrue(Anomaly.objects.filter(kind='badge_inactif_utilise').exists())

    def test_reissue_revokes_old_badge_and_issues_distinct_secret(self):
        from .badge_security import reissue_badge
        new_badge, enrollment = reissue_badge(self.student)
        self.badge.refresh_from_db()
        self.assertEqual(self.badge.state, Badge.State.REVOKED)
        self.assertEqual(self.badge.replaced_by, new_badge)
        self.assertNotEqual(enrollment['secret'], self.enrollment['secret'])
        with self.assertRaises(BadgeRejected):
            validate_rotating_token(rotating_token(self.badge.badge_id, self.secret, self.now.timestamp()), self.now, now=self.now)

    def test_token_contains_no_student_identifier_or_name(self):
        token = rotating_token(self.badge.badge_id, self.secret, self.now.timestamp())
        self.assertNotIn(str(self.student.pk), token.split('.'))
        self.assertNotIn(self.student.name, token)
        self.assertNotIn(self.student.name, self.enrollment['secret'])

    def test_rfc6238_appendix_b_vector(self):
        self.assertEqual(rfc6238_code(b'12345678901234567890', 59), '94287082')

    def test_calibrated_two_hour_local_clock_is_accepted(self):
        DeviceClock.objects.filter(device_id=self.device).update(offset_seconds=-7200)
        local = self.now + timedelta(hours=2)
        result = accept_scan(self.payload(local=local, offset=-7200), now=self.now)
        scan = Scan.objects.get(pk=result['id'])
        self.assertFalse(scan.clock_suspect)

    def test_encrypted_secret_is_not_stored_as_plaintext(self):
        self.assertNotIn(self.secret.hex(), self.badge.secret_ciphertext)
        self.assertEqual(decrypt_secret(self.badge.secret_ciphertext, self.badge.badge_id), self.secret)

    def test_event_idempotency(self):
        payload = self.payload()
        first = accept_scan(payload, now=self.now)
        second = accept_scan(payload, now=self.now)
        self.assertFalse(first['duplicate'])
        self.assertTrue(second['duplicate'])
        self.assertEqual(Scan.objects.count(), 1)

    def test_parent_cannot_read_other_student_by_id(self):
        client = APIClient(); client.force_authenticate(self.parent)
        self.assertEqual(client.get(f'/api/students/{self.hidden.id}/').status_code, 404)

    def test_logout_revokes_current_access_token(self):
        access = AccessToken.for_user(self.parent); refresh = RefreshToken.for_user(self.parent)
        client = APIClient(); client.credentials(HTTP_AUTHORIZATION=f'Bearer {access}')
        self.assertEqual(client.get('/api/me/').status_code, 200)
        self.assertEqual(client.post('/api/auth/logout/', {'refresh': str(refresh)}, format='json').status_code, 204)
        # DRF renvoie 403 ici car SessionAuthentication est le premier backend
        # et n’émet pas de challenge; le jeton révoqué ne donne plus accès.
        self.assertIn(client.get('/api/me/').status_code, (401, 403))

    @override_settings(ALLOWED_HOSTS=['testserver'])
    def test_summary_template_uses_computed_counts(self):
        stats = summary_stats('6e A', timezone.localdate()); result = summarize(stats)
        self.assertEqual(result['origin'], 'template'); self.assertEqual(result['stats'], stats)
