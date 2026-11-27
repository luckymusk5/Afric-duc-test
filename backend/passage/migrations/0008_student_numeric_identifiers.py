from collections import defaultdict
from datetime import timezone as datetime_timezone
import re
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import migrations


IDENTIFIER_PATTERN = re.compile(r'^(\d{4})(\d{4})$')


def convert_student_identifiers(apps, schema_editor):
    User = apps.get_model(*settings.AUTH_USER_MODEL.split('.'))
    alias = schema_editor.connection.alias
    students = list(
        User.objects.using(alias).filter(role='student').order_by('date_joined', 'pk')
    )
    local_zone = ZoneInfo(settings.TIME_ZONE)
    grouped = defaultdict(list)
    for student in students:
        joined_at = student.date_joined
        if joined_at.tzinfo is None:
            joined_at = joined_at.replace(tzinfo=datetime_timezone.utc)
        year = joined_at.astimezone(local_zone).year
        grouped[str(year)].append(student)

    valid_existing = set()
    for year, accounts in grouped.items():
        for student in accounts:
            match = IDENTIFIER_PATTERN.fullmatch(student.username)
            if match and match.group(1) == year and int(match.group(2)) > 0:
                valid_existing.add(student.pk)

    # Free the unique username values before assigning the new IDs.
    for student in students:
        if student.pk not in valid_existing:
            User.objects.using(alias).filter(pk=student.pk).update(
                username=f'__student_id_migration_{student.pk}__', email='',
            )
        elif student.email:
            User.objects.using(alias).filter(pk=student.pk).update(email='')

    used_usernames = set(
        User.objects.using(alias).values_list('username', flat=True)
    )
    for year, accounts in grouped.items():
        for student in accounts:
            if student.pk in valid_existing:
                continue
            sequence = 1
            while sequence <= 9999:
                identifier = f'{year}{sequence:04d}'
                if identifier not in used_usernames:
                    User.objects.using(alias).filter(pk=student.pk).update(username=identifier)
                    used_usernames.add(identifier)
                    break
                sequence += 1
            if sequence > 9999:
                raise RuntimeError(f'No student identifiers available for {year}.')


class Migration(migrations.Migration):
    dependencies = [
        ('passage', '0007_routewindowexception'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunPython(convert_student_identifiers),
    ]
