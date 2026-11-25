import base64
import hashlib
import hmac
import os
import re
import struct
import uuid
from datetime import datetime
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction, models
from django.utils import timezone
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from .models import Badge, BadgeUsage, Anomaly, DeviceClock, DeviceRateWindow, Scan
from .notifications import notification_gateway

COUNTER_PERIOD_SECONDS = 15
_SIGNATURE_PATTERN = re.compile(r'^[A-Za-z0-9_-]{22}$')
_COUNTER_PATTERN = re.compile(r'^(0|[1-9][0-9]{0,12})$')


class BadgeRejected(Exception):
    """Generic internal refusal; callers must not expose its reason."""


def _key_bytes(value=None):
    encoded = value if value is not None else os.getenv('BADGE_SECRET_KEY', '')
    if not encoded:
        raise ImproperlyConfigured('BADGE_SECRET_KEY is required to encrypt badge secrets.')
    try:
        padded = encoded + '=' * (-len(encoded) % 4)
        key = base64.urlsafe_b64decode(padded.encode('ascii'))
    except (ValueError, UnicodeEncodeError) as exc:
        raise ImproperlyConfigured('BADGE_SECRET_KEY must be URL-safe base64 for 32 bytes.') from exc
    if len(key) != 32:
        raise ImproperlyConfigured('BADGE_SECRET_KEY must decode to exactly 32 bytes.')
    return key


def _aad(badge_id):
    return b'afric-educ.badge-secret.v1:' + uuid.UUID(str(badge_id)).bytes


def encrypt_secret(secret, badge_id, key=None):
    if len(secret) != 32:
        raise ValueError('Badge secrets must be exactly 32 random bytes.')
    nonce = os.urandom(12)
    encrypted = AESGCM(_key_bytes(key)).encrypt(nonce, secret, _aad(badge_id))
    value = base64.urlsafe_b64encode(nonce + encrypted).rstrip(b'=').decode('ascii')
    return 'v1:' + value


def decrypt_secret(ciphertext, badge_id, key=None):
    try:
        version, encoded = ciphertext.split(':', 1)
        if version != 'v1':
            raise ValueError('Unsupported encrypted badge secret version.')
        padded = encoded + '=' * (-len(encoded) % 4)
        payload = base64.urlsafe_b64decode(padded.encode('ascii'))
        secret = AESGCM(_key_bytes(key)).decrypt(payload[:12], payload[12:], _aad(badge_id))
    except (ValueError, InvalidTag, UnicodeEncodeError) as exc:
        raise ImproperlyConfigured('Could not decrypt badge secret; verify BADGE_SECRET_KEY.') from exc
    if len(secret) != 32:
        raise ImproperlyConfigured('Decrypted badge secret has an invalid length.')
    return secret


def create_badge(student):
    badge_id = uuid.uuid4()
    secret = os.urandom(32)
    badge = Badge.objects.create(
        student=student,
        badge_id=badge_id,
        secret_ciphertext=encrypt_secret(secret, badge_id),
    )
    enrollment = {
        'badge_id': str(badge_id),
        'secret': base64.b32encode(secret).decode('ascii').rstrip('='),
    }
    return badge, enrollment


@transaction.atomic
def reissue_badge(student):
    old_badges = list(Badge.objects.select_for_update().filter(student=student).exclude(state=Badge.State.REVOKED))
    new_badge, enrollment = create_badge(student)
    for old_badge in old_badges:
        old_badge.state = Badge.State.REVOKED
        old_badge.replaced_by = new_badge
        old_badge.save(update_fields=['state', 'replaced_by'])
    return new_badge, enrollment


def counter_for_time(unix_seconds):
    return int(float(unix_seconds) // COUNTER_PERIOD_SECONDS)


def signature_for(secret, badge_id, counter):
    message = f'{uuid.UUID(str(badge_id))}.{int(counter)}'.encode('ascii')
    digest = hmac.new(secret, message, hashlib.sha256).digest()[:16]
    return base64.urlsafe_b64encode(digest).rstrip(b'=').decode('ascii')


def rotating_token(badge_id, secret, unix_seconds):
    badge_id = str(uuid.UUID(str(badge_id)))
    counter = counter_for_time(unix_seconds)
    return f'{badge_id}.{counter}.{signature_for(secret, badge_id, counter)}'


def parse_rotating_token(token):
    if not isinstance(token, str) or len(token) > 80:
        raise BadgeRejected
    parts = token.split('.')
    if len(parts) != 3:
        raise BadgeRejected
    badge_text, counter_text, signature = parts
    try:
        badge_id = uuid.UUID(badge_text)
    except (ValueError, AttributeError):
        raise BadgeRejected from None
    if str(badge_id) != badge_text or not _COUNTER_PATTERN.fullmatch(counter_text) or not _SIGNATURE_PATTERN.fullmatch(signature):
        raise BadgeRejected
    try:
        raw_signature = base64.urlsafe_b64decode(signature + '==')
    except ValueError:
        raise BadgeRejected from None
    if len(raw_signature) != 16:
        raise BadgeRejected
    return badge_id, int(counter_text), raw_signature


def _record_inactive_use(badge, now):
    anomaly, created = Anomaly.objects.get_or_create(
        student=badge.student,
        kind='badge_inactif_utilise',
        occurred_at=now,
        scan=None,
        defaults={'details': 'Tentative avec un badge non actif.'},
    )
    if created:
        for parent in badge.student.parents.all():
            notification_gateway.queue(parent, anomaly)


def validate_rotating_token(token, estimated_time, now=None):
    badge_id, counter, supplied_signature = parse_rotating_token(token)
    badge = Badge.objects.select_related('student').filter(badge_id=badge_id).first()
    if badge is None:
        raise BadgeRejected
    now = now or timezone.now()
    if badge.state != Badge.State.ACTIVE or not badge.student.active:
        _record_inactive_use(badge, now)
        raise BadgeRejected
    try:
        secret = decrypt_secret(badge.secret_ciphertext, badge.badge_id)
    except ImproperlyConfigured:
        raise
    expected = base64.urlsafe_b64decode(signature_for(secret, badge_id, counter) + '==')
    if not hmac.compare_digest(expected, supplied_signature):
        raise BadgeRejected
    if not isinstance(estimated_time, datetime):
        raise BadgeRejected
    if timezone.is_naive(estimated_time):
        estimated_time = timezone.make_aware(estimated_time, timezone.get_current_timezone())
    estimated_counter = counter_for_time(estimated_time.timestamp())
    if abs(counter - estimated_counter) > 1:
        raise BadgeRejected
    return badge, counter


def record_device_clock(device_id, local_time, server_time=None):
    server_time = server_time or timezone.now()
    if timezone.is_naive(local_time):
        local_time = timezone.make_aware(local_time, timezone.get_current_timezone())
    offset = (server_time - local_time).total_seconds()
    DeviceClock.objects.update_or_create(
        device_id=device_id,
        defaults={'offset_seconds': offset, 'synchronized_at': server_time},
    )
    return offset


def record_device_attempt(device_id, now=None, limit=60):
    now = now or timezone.now()
    window_start = now.replace(second=0, microsecond=0)
    with transaction.atomic():
        window, _ = DeviceRateWindow.objects.get_or_create(
            device_id=device_id,
            window_start=window_start,
            defaults={'attempts': 0},
        )
        updated = DeviceRateWindow.objects.filter(pk=window.pk, attempts__lt=limit).update(attempts=models.F('attempts') + 1)
    return bool(updated)


def rfc6238_code(secret, unix_seconds, digits=8, step=30, hash_name='sha1'):
    """RFC 6238 Appendix B calculation, kept as an independent test oracle."""
    counter = counter_for_time(unix_seconds) if step == 30 else int(unix_seconds // step)
    digest = hmac.new(secret, struct.pack('>Q', counter), getattr(hashlib, hash_name)).digest()
    offset = digest[-1] & 0x0F
    binary = struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(binary % (10 ** digits)).zfill(digits)
