"""High priority security tests for rotating student QR credentials."""
import base64
import os
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from .badge_security import (
    BadgeRejected,
    create_badge,
    rotating_token,
    validate_rotating_token,
)
from .models import Badge, Checkpoint, DeviceClock, Student
from .services import accept_scan


User = get_user_model()
TEST_KEY = base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode()


class RotatingBadgeSecurityTests(TestCase):
    def setUp(self):
        self.key_patch = patch.dict(os.environ, {"BADGE_SECRET_KEY": TEST_KEY})
        self.key_patch.start()
        self.addCleanup(self.key_patch.stop)
        self.student_user = User.objects.create_user(
            username="student-security-test", role="student"
        )
        self.student = Student.objects.create(
            name="Élève Test", class_name="6e", school="École Test",
            account=self.student_user,
        )
        self.badge, self.enrollment = create_badge(self.student)

    def test_signature_tampering_and_out_of_window_tokens_are_rejected(self):
        now = timezone.now()
        token = rotating_token(
            self.badge.badge_id,
            base64.b32decode(self.enrollment["secret"] + "=" * (-len(self.enrollment["secret"]) % 8)),
            now.timestamp(),
        )
        badge, _ = validate_rotating_token(token, now)
        self.assertEqual(badge.pk, self.badge.pk)

        parts = token.split(".")
        parts[-1] = ("A" if parts[-1][0] != "A" else "B") + parts[-1][1:]
        with self.assertRaises(BadgeRejected):
            validate_rotating_token(".".join(parts), now)

        with self.assertRaises(BadgeRejected):
            validate_rotating_token(token, now + timedelta(seconds=45))

    def test_inactive_student_badge_is_rejected(self):
        self.student.active = False
        self.student.save(update_fields=["active"])
        now = timezone.now()
        secret = base64.b32decode(
            self.enrollment["secret"] + "=" * (-len(self.enrollment["secret"]) % 8)
        )
        with self.assertRaises(BadgeRejected):
            validate_rotating_token(
                rotating_token(self.badge.badge_id, secret, now.timestamp()), now
            )

    def test_same_qr_time_step_cannot_create_a_second_passage(self):
        now = timezone.now().replace(microsecond=0)
        checkpoint = Checkpoint.objects.create(label="Entrée", kind="gate")
        device_id = "security-test-device"
        DeviceClock.objects.create(
            device_id=device_id, offset_seconds=0, synchronized_at=now
        )
        secret = base64.b32decode(
            self.enrollment["secret"] + "=" * (-len(self.enrollment["secret"]) % 8)
        )
        data = {
            "token": rotating_token(self.badge.badge_id, secret, now.timestamp()),
            "eventId": str(uuid.uuid4()), "deviceId": device_id,
            "checkpointId": checkpoint.pk,
            "localTime": now.isoformat(), "estimatedTime": now.isoformat(),
        }
        accept_scan(data, now=now)
        data["eventId"] = str(uuid.uuid4())
        with self.assertRaises(BadgeRejected):
            accept_scan(data, now=now)

    def test_only_student_account_can_fetch_its_own_qr_credential(self):
        client = APIClient()
        other_user = User.objects.create_user(
            username="parent-security-test", role="parent"
        )
        client.force_authenticate(other_user)
        self.assertEqual(client.get("/api/my/badge/").status_code, 403)

        client.force_authenticate(self.student_user)
        response = client.get("/api/my/badge/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["badge_id"], str(self.badge.badge_id))
        self.assertTrue(response.data["secret"])
        self.assertIn("no-store", response["Cache-Control"])
        self.assertNotIn("student_id", response.data)

        self.badge.state = Badge.State.REVOKED
        self.badge.save(update_fields=["state"])
        self.assertEqual(client.get("/api/my/badge/").status_code, 404)
