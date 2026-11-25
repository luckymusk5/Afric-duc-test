import uuid
from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    class Role(models.TextChoices):
        ADMIN = 'admin', 'Responsable'
        AGENT = 'agent', 'Agent'
        PARENT = 'parent', 'Parent'
        STUDENT = 'student', 'Élève'

    role = models.CharField(max_length=12, choices=Role.choices, default=Role.PARENT)


class Student(models.Model):
    name = models.CharField(max_length=120)
    class_name = models.CharField(max_length=80)
    school = models.CharField(max_length=120)
    active = models.BooleanField(default=True)
    account = models.OneToOneField('User', null=True, blank=True, on_delete=models.SET_NULL, related_name='student_profile', limit_choices_to={'role': 'student'})
    parents = models.ManyToManyField(User, related_name='children', limit_choices_to={'role': 'parent'})

    def __str__(self):
        return self.name


class Checkpoint(models.Model):
    label = models.CharField(max_length=100)
    kind = models.CharField(max_length=30, default='gate')

    def __str__(self):
        return self.label


class Badge(models.Model):
    class State(models.TextChoices):
        ACTIVE = 'active', 'Actif'
        SUSPENDED = 'suspended', 'Suspendu'
        LOST = 'lost', 'Perdu'
        REVOKED = 'revoked', 'Révoqué'

    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='badges')
    badge_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    secret_ciphertext = models.TextField()
    state = models.CharField(max_length=12, choices=State.choices, default=State.ACTIVE)
    issued_at = models.DateTimeField(auto_now_add=True)
    replaced_by = models.ForeignKey('self', null=True, blank=True, on_delete=models.SET_NULL)


class BadgeUsage(models.Model):
    badge = models.ForeignKey(Badge, on_delete=models.CASCADE, related_name='usages')
    counter = models.PositiveBigIntegerField()
    scan = models.OneToOneField('Scan', on_delete=models.CASCADE, related_name='badge_usage')
    used_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['badge', 'counter'], name='unique_badge_counter_usage')]


class RouteWindow(models.Model):
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='route_windows')
    checkpoint = models.ForeignKey(Checkpoint, on_delete=models.CASCADE)
    sequence = models.PositiveSmallIntegerField()
    start_time = models.TimeField()
    end_time = models.TimeField()
    days = models.JSONField(default=list)


class Scan(models.Model):
    event_id = models.UUIDField(unique=True)
    badge = models.ForeignKey(Badge, on_delete=models.PROTECT)
    checkpoint = models.ForeignKey(Checkpoint, on_delete=models.PROTECT)
    local_time = models.DateTimeField()
    estimated_time = models.DateTimeField(null=True)
    received_at = models.DateTimeField(auto_now_add=True)
    device_id = models.CharField(max_length=100)
    clock_offset_seconds = models.FloatField(default=0)
    clock_suspect = models.BooleanField(default=False)


class Anomaly(models.Model):
    student = models.ForeignKey(Student, on_delete=models.CASCADE)
    kind = models.CharField(max_length=40)
    occurred_at = models.DateTimeField()
    scan = models.ForeignKey(Scan, null=True, blank=True, on_delete=models.SET_NULL)
    details = models.CharField(max_length=250, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['student', 'kind', 'occurred_at', 'scan'], name='unique_student_anomaly')]


class OutboxMessage(models.Model):
    parent = models.ForeignKey(User, on_delete=models.CASCADE)
    anomaly = models.ForeignKey(Anomaly, on_delete=models.CASCADE)
    message = models.CharField(max_length=250)
    state = models.CharField(max_length=16, default='queued')
    reason = models.CharField(max_length=120, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['parent', 'anomaly'], name='unique_parent_anomaly_message')]


class AISummary(models.Model):
    cache_key = models.CharField(max_length=64, unique=True)
    model = models.CharField(max_length=100)
    text = models.TextField()
    stats = models.JSONField()
    origin = models.CharField(max_length=12)
    reason = models.CharField(max_length=200, blank=True)
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    estimated_cost = models.DecimalField(max_digits=9, decimal_places=6, default=0)
    created_at = models.DateTimeField(auto_now_add=True)


class AuditLog(models.Model):
    user = models.ForeignKey(User, null=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=120)
    created_at = models.DateTimeField(auto_now_add=True)


class AIUsageLog(models.Model):
    cache_key = models.CharField(max_length=64)
    model = models.CharField(max_length=100)
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    estimated_cost = models.DecimalField(max_digits=9, decimal_places=6, default=0)
    outcome = models.CharField(max_length=16)
    created_at = models.DateTimeField(auto_now_add=True)


class RevokedAccessToken(models.Model):
    jti = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(auto_now_add=True)


class SyncReceipt(models.Model):
    class State(models.TextChoices):
        PENDING = 'pending', 'En attente de publication'
        QUEUED = 'queued', 'En file RabbitMQ'
        PROCESSING = 'processing', 'En traitement'
        PROCESSED = 'processed', 'Enregistré en base'
        REJECTED = 'rejected', 'Rejeté'
        FAILED = 'failed', 'Échec définitif'

    event_id = models.UUIDField(unique=True)
    payload = models.JSONField(default=dict, blank=True)
    state = models.CharField(max_length=16, choices=State.choices, default=State.PENDING)
    task_id = models.CharField(max_length=64, blank=True)
    error = models.CharField(max_length=250, blank=True)
    received_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class DeviceClock(models.Model):
    device_id = models.CharField(max_length=100, unique=True)
    offset_seconds = models.FloatField()
    synchronized_at = models.DateTimeField()


class DeviceRateWindow(models.Model):
    device_id = models.CharField(max_length=100)
    window_start = models.DateTimeField()
    attempts = models.PositiveSmallIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['device_id', 'window_start'], name='unique_device_rate_window')]
