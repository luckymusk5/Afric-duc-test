"""Tests prioritaires de l’API de synchronisation hors ligne."""
import base64
import hashlib
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta
from threading import Barrier
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import close_old_connections
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from .badge_security import create_badge, decrypt_secret, rotating_token
from .models import (
    Anomaly,
    Device,
    DeviceAssignment,
    OutboxMessage,
    PresenceJournal,
    RouteWindow,
    Scan,
    Student,
)
from .services import close_windows, rebuild_presence_journal, sync_passage_events


User = get_user_model()
TEST_KEY = base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode()


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class PassageSyncPriorityTests(TestCase):
    """Couvre l’idempotence, l’autorité du terminal et le rollback du lot."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.key_patch = patch.dict(os.environ, {"BADGE_SECRET_KEY": TEST_KEY})
        cls.key_patch.start()

    @classmethod
    def tearDownClass(cls):
        cls.key_patch.stop()
        super().tearDownClass()

    def setUp(self):
        self.agent = User.objects.create_user(
            username="sync-agent", password="AgentPass123!", role="agent"
        )
        self.parent = User.objects.create_user(
            username="sync-parent", password="ParentPass123!", role="parent"
        )
        self.checkpoint = self._checkpoint("Portail entrée")
        self.untrusted_checkpoint = self._checkpoint("Point envoyé par le navigateur")
        self.device_secret = "test-device-secret-never-used-in-production"
        self.device = Device.objects.create(
            token_hash=hashlib.sha256(self.device_secret.encode()).hexdigest()
        )
        DeviceAssignment.objects.create(
            device=self.device,
            checkpoint=self.checkpoint,
            starts_at=timezone.now() - timedelta(days=2),
        )
        self.client = APIClient()
        self.client.force_authenticate(self.agent)

    def _checkpoint(self, label):
        from .models import Checkpoint

        return Checkpoint.objects.create(label=label, kind="gate")

    def _student_event(self, when=None, event_id=None, name="Élève"):
        student = Student.objects.create(
            name=name, class_name="6e", school="École Test"
        )
        event, badge = self._event_for_student(student, when=when, event_id=event_id)
        return event, student, badge

    def _event_for_student(self, student, when=None, event_id=None):
        badge, _ = create_badge(student)
        when = when or timezone.now().replace(microsecond=0)
        if when < badge.issued_at:
            badge.issued_at = when - timedelta(seconds=1)
            badge.save(update_fields=["issued_at"])
        secret = decrypt_secret(badge.secret_ciphertext, badge.badge_id)
        token = rotating_token(badge.badge_id, secret, when.timestamp())
        event = {
            "event_id": str(event_id or uuid.uuid4()),
            "token": token,
            "local_time": when.isoformat(),
            "est_time": when.isoformat(),
        }
        return event, badge

    def _post(self, events):
        return self.client.post(
            "/api/passages/sync",
            {"events": events},
            format="json",
            HTTP_X_DEVICE_TOKEN=self.device_secret,
        )

    def test_twenty_events_retried_three_times_create_exactly_twenty_passages(self):
        events = [self._student_event(name=f"Élève {i}")[0] for i in range(20)]

        first = self._post(events)
        retries = [self._post(events) for _ in range(3)]

        self.assertEqual(first.status_code, 200)
        self.assertEqual([ack["status"] for ack in first.data["acks"]], ["created"] * 20)
        self.assertEqual([ack["resultat"] for ack in first.data["acks"]], ["accepte"] * 20)
        for retry in retries:
            self.assertEqual(retry.status_code, 200)
            self.assertEqual([ack["status"] for ack in retry.data["acks"]], ["duplicate"] * 20)
        self.assertEqual(Scan.objects.count(), 20)
        self.assertEqual(PresenceJournal.objects.count(), 20)

    def test_partial_eight_then_full_twenty_creates_twenty_passages(self):
        events = [self._student_event(name=f"Partiel {i}")[0] for i in range(20)]

        partial = self._post(events[:8])
        complete = self._post(events)

        self.assertEqual(partial.status_code, 200)
        self.assertEqual(complete.status_code, 200)
        self.assertEqual(Scan.objects.count(), 20)
        self.assertEqual(PresenceJournal.objects.count(), 20)
        self.assertEqual(
            sum(ack["status"] == "duplicate" for ack in complete.data["acks"]), 8
        )

    def test_duplicate_ack_returns_the_original_refusal_without_student_identity(self):
        event = self._student_event()[0]
        event["token"] = "jeton-invalide"

        first = self._post([event])
        retry = self._post([event])

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.data["acks"][0]["resultat"], "refuse")
        self.assertEqual(retry.data["acks"][0]["status"], "duplicate")
        self.assertEqual(retry.data["acks"][0]["resultat"], "refuse")
        self.assertEqual(set(retry.data["acks"][0]), {"event_id", "status", "resultat"})

    def test_internal_error_on_fourth_event_rolls_back_the_whole_batch(self):
        events = [self._student_event(name=f"Rollback {i}")[0] for i in range(8)]
        from . import services

        real_validate = services.validate_rotating_token
        calls = 0

        def fail_on_fourth(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 4:
                raise RuntimeError("erreur injectée")
            return real_validate(*args, **kwargs)

        self.client.raise_request_exception = False
        with patch("passage.services.validate_rotating_token", side_effect=fail_on_fourth):
            response = self._post(events)

        self.assertEqual(response.status_code, 500)
        self.assertNotIn("acks", getattr(response, "data", {}))
        self.assertEqual(Scan.objects.count(), 0)
        self.assertEqual(PresenceJournal.objects.count(), 0)

    def test_checkpoint_from_request_is_ignored_in_favour_of_device_assignment(self):
        event = self._student_event()[0]
        event.update({
            "checkpoint_id": self.untrusted_checkpoint.pk,
            "point_controle": self.untrusted_checkpoint.pk,
            "direction": "exit",
            "student_id": 999999,
        })

        response = self._post([event])

        self.assertEqual(response.status_code, 200)
        scan = Scan.objects.get(event_id=event["event_id"])
        self.assertEqual(scan.checkpoint_id, self.checkpoint.pk)
        self.assertEqual(scan.direction, RouteWindow.Direction.ENTRY)
        self.assertIsNotNone(scan.badge)

    def test_parent_cannot_sync_and_inactive_device_cannot_sync(self):
        event = self._student_event()[0]
        self.client.force_authenticate(self.parent)
        self.assertEqual(self._post([event]).status_code, 403)
        self.assertEqual(Scan.objects.count(), 0)

        self.client.force_authenticate(self.agent)
        self.device.active = False
        self.device.save(update_fields=["active"])
        self.assertEqual(self._post([event]).status_code, 403)
        self.assertEqual(Scan.objects.count(), 0)

    def test_invalid_event_rejects_the_entire_batch_before_any_write(self):
        valid = self._student_event()[0]
        invalid = {"event_id": "pas-un-uuid", "token": "invalide"}

        response = self._post([valid, invalid])

        self.assertEqual(response.status_code, 422)
        self.assertEqual(Scan.objects.count(), 0)
        self.assertEqual(PresenceJournal.objects.count(), 0)

    def test_clock_skew_is_flagged_but_corrected_server_estimate_is_used(self):
        estimated = timezone.now().replace(microsecond=0)
        event, student, _ = self._student_event(when=estimated)
        event["local_time"] = (estimated + timedelta(hours=2)).isoformat()

        response = self._post([event])

        self.assertEqual(response.status_code, 200)
        scan = Scan.objects.get(event_id=event["event_id"])
        self.assertTrue(scan.clock_suspect)
        self.assertEqual(scan.estimated_time, estimated)
        self.assertTrue(Anomaly.objects.filter(student=student, kind="horloge_appareil").exists())
        self.assertEqual(
            PresenceJournal.objects.get(student=student).heure_entree,
            estimated,
        )

    def test_missing_passage_is_idempotent_and_late_scan_marks_it_caught_up(self):
        day = timezone.localdate() - timedelta(days=1)
        student = Student.objects.create(name="Rattrapage", class_name="6e", school="Test")
        student.parents.add(self.parent)
        window = RouteWindow.objects.create(
            student=student,
            checkpoint=self.checkpoint,
            sequence=1,
            start_time=time(7),
            end_time=time(7, 45),
            days=[day.isoweekday()],
            direction=RouteWindow.Direction.ENTRY,
        )

        self.assertEqual(close_windows(now=timezone.now()), 1)
        self.assertEqual(close_windows(now=timezone.now()), 0)
        anomaly = Anomaly.objects.get(student=student, route_window=window)
        self.assertEqual(anomaly.state, Anomaly.State.PROVISIONAL)

        late_event, _ = self._event_for_student(
            student,
            when=timezone.make_aware(datetime.combine(day, time(7, 10))),
        )

        response = self._post([late_event])

        self.assertEqual(response.status_code, 200)
        anomaly.refresh_from_db()
        self.assertEqual(anomaly.state, Anomaly.State.CAUGHT_UP)
        self.assertEqual(OutboxMessage.objects.count(), 0)

    def test_rebuilding_journal_is_idempotent(self):
        event, student, _ = self._student_event()
        response = self._post([event])
        self.assertEqual(response.status_code, 200)
        day = timezone.localdate()

        self.assertEqual(rebuild_presence_journal(day), 1)
        first = PresenceJournal.objects.get(student=student, date_jour=day)
        self.assertEqual(rebuild_presence_journal(day), 1)
        second = PresenceJournal.objects.get(student=student, date_jour=day)
        self.assertEqual(second.date_jour, first.date_jour)
        self.assertEqual(second.heure_entree, first.heure_entree)
        self.assertEqual(second.heure_sortie, first.heure_sortie)

    def test_entry_and_exit_arriving_out_of_order_keep_both_journal_times(self):
        day = timezone.localdate() - timedelta(days=1)
        entry_checkpoint = self.checkpoint
        exit_checkpoint = self._checkpoint("Portail sortie")
        assignment = DeviceAssignment.objects.get(device=self.device)
        assignment.starts_at = timezone.make_aware(
            datetime.combine(day - timedelta(days=1), time.min)
        )
        assignment.ends_at = timezone.make_aware(datetime.combine(day, time(12)))
        assignment.save(update_fields=["starts_at", "ends_at"])
        DeviceAssignment.objects.create(
            device=self.device,
            checkpoint=exit_checkpoint,
            starts_at=timezone.make_aware(datetime.combine(day, time(12))),
        )
        student = Student.objects.create(name="Présence", class_name="6e", school="Test")
        badge, _ = create_badge(student)
        badge.issued_at = timezone.make_aware(
            datetime.combine(day - timedelta(days=1), time.min)
        )
        badge.save(update_fields=["issued_at"])
        secret = decrypt_secret(badge.secret_ciphertext, badge.badge_id)
        entry_time = timezone.make_aware(datetime.combine(day, time(7, 10)))
        exit_time = timezone.make_aware(datetime.combine(day, time(15, 40)))
        entry = {
            "event_id": uuid.uuid4(), "token": rotating_token(badge.badge_id, secret, entry_time.timestamp()),
            "local_time": entry_time, "est_time": entry_time,
        }
        exit_event = {
            "event_id": uuid.uuid4(), "token": rotating_token(badge.badge_id, secret, exit_time.timestamp()),
            "local_time": exit_time, "est_time": exit_time,
        }
        RouteWindow.objects.create(
            student=student, checkpoint=entry_checkpoint, sequence=1,
            start_time=time(7), end_time=time(8), days=[1, 2, 3, 4, 5, 6, 7],
            direction=RouteWindow.Direction.ENTRY,
        )
        RouteWindow.objects.create(
            student=student, checkpoint=exit_checkpoint, sequence=2,
            start_time=time(15), end_time=time(16), days=[1, 2, 3, 4, 5, 6, 7],
            direction=RouteWindow.Direction.EXIT,
        )

        result = sync_passage_events(self.device, [exit_event, entry])

        self.assertEqual(len(result["acks"]), 2)
        journal = PresenceJournal.objects.get(student=student, date_jour=day)
        self.assertEqual(journal.heure_entree, entry_time)
        self.assertEqual(journal.heure_sortie, exit_time)


class ConcurrentPassageSyncTests(TransactionTestCase):
    """Le verrou PostgreSQL du terminal sérialise les mêmes événements concurrents."""

    reset_sequences = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.key_patch = patch.dict(os.environ, {"BADGE_SECRET_KEY": TEST_KEY})
        cls.key_patch.start()

    @classmethod
    def tearDownClass(cls):
        cls.key_patch.stop()
        super().tearDownClass()

    def test_two_concurrent_requests_create_only_one_scan(self):
        from .models import Checkpoint

        checkpoint = Checkpoint.objects.create(label="Portail concurrent", kind="gate")
        device = Device.objects.create(
            token_hash=hashlib.sha256(b"concurrent-device").hexdigest()
        )
        DeviceAssignment.objects.create(
            device=device, checkpoint=checkpoint,
            starts_at=timezone.now() - timedelta(minutes=1),
        )
        student = Student.objects.create(name="Concurrence", class_name="6e", school="Test")
        badge, _ = create_badge(student)
        secret = decrypt_secret(badge.secret_ciphertext, badge.badge_id)
        when = timezone.now().replace(microsecond=0) + timedelta(seconds=2)
        RouteWindow.objects.create(
            student=student, checkpoint=checkpoint, sequence=1,
            start_time=time.min, end_time=time.max, days=[1, 2, 3, 4, 5, 6, 7],
            direction=RouteWindow.Direction.ENTRY,
        )
        event = {
            "event_id": uuid.uuid4(),
            "token": rotating_token(badge.badge_id, secret, when.timestamp()),
            "local_time": when,
            "est_time": when,
        }
        barrier = Barrier(2)
        device_pk = device.pk

        def submit_same_event():
            close_old_connections()
            try:
                thread_device = Device.objects.get(pk=device_pk)
                barrier.wait(timeout=10)
                return sync_passage_events(thread_device, [event])["acks"][0]
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            acknowledgements = list(pool.map(lambda _: submit_same_event(), range(2)))

        self.assertEqual(sorted(ack["status"] for ack in acknowledgements), ["created", "duplicate"])
        self.assertEqual(Scan.objects.count(), 1)
        self.assertEqual(PresenceJournal.objects.count(), 1)
