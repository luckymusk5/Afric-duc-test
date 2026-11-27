import hashlib
import os
import secrets
import uuid
from datetime import datetime, time

from django.contrib.auth import get_user_model
from django.core.management import BaseCommand, CommandError, call_command
from django.utils import timezone

from passage.badge_security import counter_for_time, create_badge
from passage.models import (
    Anomaly,
    Badge,
    BadgeUsage,
    Checkpoint,
    Device,
    DeviceAssignment,
    RouteWindow,
    Scan,
    Student,
)
from passage.services import rebuild_presence_journal


class Command(BaseCommand):
    help = 'Crée un jeu de données de démonstration de façon idempotente.'

    def handle(self, *args, **kwargs):
        call_command('migrate', interactive=False, verbosity=0)
        User = get_user_model()
        demo_accounts = [
            ('admin@afric-educ.test', 'Responsable Démo', 'admin', 'DEMO_ADMIN_PASSWORD'),
            ('agent@afric-educ.test', 'Agent Démo', 'agent', 'DEMO_AGENT_PASSWORD'),
            ('parent1@afric-educ.test', 'Parent A', 'parent', 'DEMO_PARENT1_PASSWORD'),
            ('parent2@afric-educ.test', 'Parent B', 'parent', 'DEMO_PARENT2_PASSWORD'),
        ]
        passwords = {entry[3]: os.getenv(entry[3], '') for entry in demo_accounts}
        missing_passwords = [name for name, value in passwords.items() if not value]
        if missing_passwords:
            raise CommandError(
                'Définissez les mots de passe de démonstration requis dans l’environnement: '
                + ', '.join(missing_passwords),
            )
        users = {}
        for username, name, role, password_key in demo_accounts:
            user, created = User.objects.get_or_create(
                username=username,
                defaults={
                    'email': username,
                    'first_name': name,
                    'role': role,
                },
            )
            if created:
                user.set_password(passwords[password_key])
                user.save(update_fields=['password'])
            users[username] = user

        students = []
        for name in ['Amina N.', 'Paul T.', 'Mireille K.', 'Joel M.', 'Sarah E.']:
            student, _ = Student.objects.get_or_create(
                name=name,
                class_name='6e A',
                school='École Démo',
            )
            students.append(student)
        for student, username in zip(
            students[:4],
            ['parent1@afric-educ.test', 'parent1@afric-educ.test',
             'parent2@afric-educ.test', 'parent2@afric-educ.test'],
        ):
            student.parents.add(users[username])

        entry, _ = Checkpoint.objects.get_or_create(
            label='Portail entrée', defaults={'kind': 'gate'},
        )
        exit_point, _ = Checkpoint.objects.get_or_create(
            label='Portail sortie', defaults={'kind': 'gate'},
        )
        day = timezone.localdate()
        for student in students:
            if not Badge.objects.filter(student=student, state=Badge.State.ACTIVE).exists():
                create_badge(student)
            RouteWindow.objects.update_or_create(
                student=student,
                checkpoint=entry,
                sequence=1,
                defaults={
                    'start_time': time(7, 0),
                    'end_time': time(7, 45),
                    'days': [1, 2, 3, 4, 5],
                    'direction': RouteWindow.Direction.ENTRY,
                },
            )
            if student != students[1]:
                RouteWindow.objects.update_or_create(
                    student=student,
                    checkpoint=exit_point,
                    sequence=2,
                    defaults={
                        'start_time': time(15, 30),
                        'end_time': time(16, 15),
                        'days': [1, 2, 3, 4, 5],
                        'direction': RouteWindow.Direction.EXIT,
                    },
                )
        RouteWindow.objects.filter(
            student=students[1], checkpoint=exit_point,
        ).update(active=False)

        for index, checkpoint in enumerate((entry, exit_point), start=1):
            device_id = uuid.uuid5(uuid.NAMESPACE_URL, f'afric-educ-demo-device-{index}')
            device, created = Device.objects.get_or_create(
                device_id=device_id,
                defaults={
                    'token_hash': hashlib.sha256(
                        secrets.token_bytes(32),
                    ).hexdigest(),
                },
            )
            if created or not DeviceAssignment.objects.filter(
                device=device, ends_at__isnull=True,
            ).exists():
                DeviceAssignment.objects.create(device=device, checkpoint=checkpoint)

        samples = [
            (students[0], entry, time(8, 10), 'hors_fenetre'),
            (students[1], exit_point, time(15, 40), 'point_inattendu'),
            (students[2], entry, time(7, 11), 'double_scan'),
            (students[2], entry, time(7, 11, 45), None),
        ]
        for index, (student, checkpoint, clock_time, anomaly_kind) in enumerate(samples):
            occurred_at = timezone.make_aware(datetime.combine(day, clock_time))
            # Keep each day's demo scans distinct. Reusing yesterday's scan while
            # calculating today's badge counter makes BadgeUsage try to attach a
            # second counter to the same one-to-one scan on startup.
            event_id = uuid.uuid5(
                uuid.NAMESPACE_URL,
                f'afric-educ-demo-scan-{day.isoformat()}-{index}',
            )
            scan, _ = Scan.objects.get_or_create(
                device_id='seed-demo',
                event_id=event_id,
                defaults={
                    'badge': student.badges.filter(state=Badge.State.ACTIVE).first(),
                    'checkpoint': checkpoint,
                    'local_time': occurred_at,
                    'estimated_time': occurred_at,
                    'server_time': occurred_at,
                    'direction': (
                        RouteWindow.Direction.EXIT
                        if checkpoint == exit_point
                        else RouteWindow.Direction.ENTRY
                    ),
                    'result': Scan.Result.ACCEPTED,
                },
            )
            BadgeUsage.objects.get_or_create(
                badge=scan.badge,
                counter=counter_for_time(occurred_at.timestamp()),
                defaults={'scan': scan},
            )
            if anomaly_kind:
                Anomaly.objects.get_or_create(
                    student=student,
                    kind=anomaly_kind,
                    occurred_at=occurred_at,
                    scan=scan,
                    defaults={'details': 'Exemple de démonstration.'},
                )

        rebuild_presence_journal(day)
        self.stdout.write(self.style.SUCCESS('Données de démonstration prêtes.'))
