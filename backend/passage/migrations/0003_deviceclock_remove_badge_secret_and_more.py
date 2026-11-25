import base64
import os
import uuid
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.db import migrations, models
import django.db.models.deletion


def _key():
    value = os.getenv('BADGE_SECRET_KEY', '')
    if not value:
        raise RuntimeError('BADGE_SECRET_KEY doit être définie avant de migrer les badges existants.')
    try:
        padded = value + '=' * (-len(value) % 4)
        key = base64.urlsafe_b64decode(padded.encode('ascii'))
    except (ValueError, UnicodeEncodeError) as exc:
        raise RuntimeError('BADGE_SECRET_KEY doit être une clé base64 URL-safe valide.') from exc
    if len(key) != 32:
        raise RuntimeError('BADGE_SECRET_KEY doit décoder exactement 32 octets.')
    return key


def _aad(badge_id):
    return b'afric-educ.badge-secret.v1:' + uuid.UUID(str(badge_id)).bytes


def encrypt_legacy_badges(apps, schema_editor):
    Badge = apps.get_model('passage', 'Badge')
    key = _key()
    cipher = AESGCM(key)
    alias = schema_editor.connection.alias
    for badge in Badge.objects.using(alias).all().iterator():
        badge_id = badge.badge_id or uuid.uuid4()
        try:
            secret = bytes.fromhex(badge.secret)
        except ValueError as exc:
            raise RuntimeError(f'Le secret historique du badge interne {badge.pk} n’est pas hexadécimal.') from exc
        if len(secret) != 32:
            raise RuntimeError(f'Le secret historique du badge interne {badge.pk} ne fait pas 32 octets.')
        nonce = os.urandom(12)
        encrypted = cipher.encrypt(nonce, secret, _aad(badge_id))
        encoded = base64.urlsafe_b64encode(nonce + encrypted).rstrip(b'=').decode('ascii')
        badge.badge_id = badge_id
        badge.secret_ciphertext = 'v1:' + encoded
        badge.save(using=alias, update_fields=['badge_id', 'secret_ciphertext'])


def decrypt_legacy_badges(apps, schema_editor):
    Badge = apps.get_model('passage', 'Badge')
    cipher = AESGCM(_key())
    alias = schema_editor.connection.alias
    for badge in Badge.objects.using(alias).all().iterator():
        if not badge.secret_ciphertext:
            continue
        encoded = badge.secret_ciphertext.split(':', 1)[1]
        encoded += '=' * (-len(encoded) % 4)
        payload = base64.urlsafe_b64decode(encoded.encode('ascii'))
        secret = cipher.decrypt(payload[:12], payload[12:], _aad(badge.badge_id))
        badge.secret = secret.hex()
        badge.save(using=alias, update_fields=['secret'])


class Migration(migrations.Migration):
    dependencies = [('passage', '0002_syncreceipt')]
    operations = [
        migrations.CreateModel(
            name='DeviceClock',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('device_id', models.CharField(max_length=100, unique=True)),
                ('offset_seconds', models.FloatField()),
                ('synchronized_at', models.DateTimeField()),
            ],
        ),
        migrations.AddField(
            model_name='badge', name='badge_id',
            field=models.UUIDField(default=uuid.uuid4, editable=False, null=True, unique=True),
        ),
        migrations.AddField(
            model_name='badge', name='secret_ciphertext',
            field=models.TextField(null=True),
        ),
        migrations.RunPython(encrypt_legacy_badges, decrypt_legacy_badges),
        migrations.AlterField(
            model_name='badge', name='badge_id',
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
        migrations.AlterField(
            model_name='badge', name='secret_ciphertext',
            field=models.TextField(),
        ),
        migrations.RemoveField(model_name='badge', name='secret'),
        migrations.RemoveField(model_name='scan', name='clock_skew'),
        migrations.RemoveField(model_name='scan', name='token'),
        migrations.AddField(
            model_name='scan', name='clock_offset_seconds',
            field=models.FloatField(default=0),
        ),
        migrations.AddField(
            model_name='scan', name='clock_suspect',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='scan', name='estimated_time',
            field=models.DateTimeField(null=True),
        ),
        migrations.CreateModel(
            name='DeviceRateWindow',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('device_id', models.CharField(max_length=100)),
                ('window_start', models.DateTimeField()),
                ('attempts', models.PositiveSmallIntegerField(default=0)),
            ],
            options={'constraints': [models.UniqueConstraint(fields=('device_id', 'window_start'), name='unique_device_rate_window')]},
        ),
        migrations.CreateModel(
            name='BadgeUsage',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('counter', models.PositiveBigIntegerField()),
                ('used_at', models.DateTimeField(auto_now_add=True)),
                ('badge', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='usages', to='passage.badge')),
                ('scan', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='badge_usage', to='passage.scan')),
            ],
            options={'constraints': [models.UniqueConstraint(fields=('badge', 'counter'), name='unique_badge_counter_usage')]},
        ),
    ]
