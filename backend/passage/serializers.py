from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from datetime import time
from django.utils import timezone
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from .models import Student, Checkpoint, Badge, Scan, Anomaly, OutboxMessage
from .models import RouteWindow
from .badge_security import create_badge


def _ensure_default_entry_window(student):
    checkpoint = Checkpoint.objects.filter(label__iexact='Portail entrée').first()
    if checkpoint and not RouteWindow.objects.filter(
        student=student, checkpoint=checkpoint, direction=RouteWindow.Direction.ENTRY, active=True,
    ).exists():
        RouteWindow.objects.create(
            student=student, checkpoint=checkpoint, sequence=1,
            start_time=time(7, 0), end_time=time(7, 45), days=[1, 2, 3, 4, 5],
            direction=RouteWindow.Direction.ENTRY,
        )


def _next_student_identifier():
    """Allocate a year-prefixed student login ID while serializing admin enrollments."""
    User = get_user_model()
    User.objects.select_for_update().filter(role=User.Role.ADMIN).order_by('pk').first()
    year = str(timezone.localdate().year)
    used = User.objects.filter(
        role=User.Role.STUDENT, username__startswith=year,
    ).values_list('username', flat=True)
    sequence = max(
        (int(value[4:]) for value in used if len(value) == 8 and value[4:].isdigit()),
        default=0,
    ) + 1
    while sequence <= 9999:
        identifier = f'{year}{sequence:04d}'
        if not User.objects.filter(username=identifier).exists():
            return identifier
        sequence += 1
    raise serializers.ValidationError('La séquence d’identifiants élèves de cette année est épuisée.')


class ValidatedPasswordField(serializers.CharField):
    def __init__(self, **kwargs):
        kwargs.setdefault('max_length', 1024)
        super().__init__(min_length=8, write_only=True, trim_whitespace=False, **kwargs)

    def to_internal_value(self, data):
        value = super().to_internal_value(data)
        try:
            validate_password(value)
        except DjangoValidationError as error:
            raise serializers.ValidationError(error.messages) from error
        return value


class SessionLoginSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=150, allow_blank=False, trim_whitespace=True)
    password = serializers.CharField(max_length=1024, allow_blank=False, trim_whitespace=False, write_only=True)


class BoundedTokenObtainPairSerializer(TokenObtainPairSerializer):
    password = serializers.CharField(max_length=1024, allow_blank=False, trim_whitespace=False, write_only=True)


class DeviceStateSerializer(serializers.Serializer):
    active = serializers.BooleanField()


class BadgeStateSerializer(serializers.Serializer):
    state = serializers.ChoiceField(choices=Badge.State.choices)


class ParentProvisionSerializer(serializers.Serializer):
    username = serializers.EmailField(max_length=150)
    password = ValidatedPasswordField()
    first_name = serializers.CharField(max_length=150)
    last_name = serializers.CharField(max_length=150)


class StudentProvisionSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=120)
    class_name = serializers.CharField(max_length=80)
    school = serializers.CharField(max_length=120)
    active = serializers.BooleanField(default=True)
    password = ValidatedPasswordField()
    parent_id = serializers.IntegerField(required=False, min_value=1)
    new_parent = ParentProvisionSerializer(required=False)

    def validate(self, attrs):
        User = get_user_model()
        existing_parent = attrs.get('parent_id')
        new_parent = attrs.get('new_parent')
        if bool(existing_parent) == bool(new_parent):
            raise serializers.ValidationError(
                'Sélectionnez un parent existant ou renseignez un nouveau compte parent.'
            )
        if existing_parent and not User.objects.filter(
            pk=existing_parent, role=User.Role.PARENT, is_active=True,
        ).exists():
            raise serializers.ValidationError({'parent_id': 'Le compte parent est invalide.'})
        if new_parent:
            if User.objects.filter(username=new_parent['username']).exists():
                raise serializers.ValidationError({'new_parent': {'username': 'Cet identifiant parent est déjà utilisé.'}})
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        User = get_user_model()
        parent_id = validated_data.pop('parent_id', None)
        parent_data = validated_data.pop('new_parent', None)
        password = validated_data.pop('password')
        if parent_id:
            parent = User.objects.select_for_update().get(
                pk=parent_id, role=User.Role.PARENT, is_active=True,
            )
        else:
            parent = User.objects.create_user(
                username=parent_data['username'],
                email=parent_data['username'],
                password=parent_data['password'],
                first_name=parent_data['first_name'],
                last_name=parent_data['last_name'],
                role=User.Role.PARENT,
            )
        account = User.objects.create_user(
            username=_next_student_identifier(),
            password=password,
            first_name=validated_data['name'],
            role=User.Role.STUDENT,
        )
        student = Student.objects.create(account=account, **validated_data)
        student.parents.add(parent)
        create_badge(student)  # Never return the badge secret to the admin UI.
        _ensure_default_entry_window(student)
        return student


class AccountProvisionSerializer(serializers.Serializer):
    username = serializers.EmailField(max_length=150)
    password = ValidatedPasswordField()
    first_name = serializers.CharField(max_length=150)
    last_name = serializers.CharField(max_length=150)

    def validate_username(self, value):
        if get_user_model().objects.filter(username=value).exists():
            raise serializers.ValidationError('Cet identifiant est déjà utilisé.')
        return value


class AdminAccountSerializer(serializers.ModelSerializer):
    name = serializers.SerializerMethodField()

    class Meta:
        model = get_user_model()
        fields = ['id', 'name', 'username', 'first_name', 'last_name', 'date_joined', 'is_active']

    def get_name(self, obj):
        return obj.get_full_name() or obj.username


class StudentSerializer(serializers.ModelSerializer):
    has_account = serializers.SerializerMethodField()
    login_identifier = serializers.SerializerMethodField()
    parents = serializers.SerializerMethodField()
    parent_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1), write_only=True, required=False,
    )
    login_password = ValidatedPasswordField(required=False)

    class Meta:
        model = Student
        fields = ['id', 'name', 'class_name', 'school', 'active', 'has_account', 'login_identifier', 'parents', 'parent_ids', 'login_password']
        read_only_fields = ['has_account']

    def get_has_account(self, obj):
        return obj.account_id is not None

    def get_login_identifier(self, obj):
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        if not obj.account_id or not user or not user.is_authenticated:
            return None
        if user.role == 'admin' or (user.role == 'student' and obj.account_id == user.pk):
            return obj.account.username
        return None

    def get_parents(self, obj):
        request = self.context.get('request')
        parents = obj.parents.all()
        if request and getattr(request.user, 'role', None) == 'parent':
            parents = parents.filter(pk=request.user.pk)
        return [
            {'id': parent.id, 'name': parent.get_full_name() or parent.username, 'username': parent.username}
            for parent in parents.order_by('last_name', 'first_name', 'username')
        ]

    def validate(self, attrs):
        if self.instance and self.instance.account_id and attrs.get('login_password'):
            raise serializers.ValidationError({'login_password': 'Un compte élève existe déjà pour cet élève.'})
        parent_ids = attrs.get('parent_ids')
        if parent_ids is not None:
            if not parent_ids or len(parent_ids) != len(set(parent_ids)):
                raise serializers.ValidationError({'parent_ids': 'Au moins un parent distinct est obligatoire.'})
            User = get_user_model()
            parents = User.objects.filter(pk__in=parent_ids, role=User.Role.PARENT, is_active=True)
            if parents.count() != len(parent_ids):
                raise serializers.ValidationError({'parent_ids': 'Un ou plusieurs comptes parents sont invalides.'})
        if self.instance and not self.instance.parents.exists() and not parent_ids:
            raise serializers.ValidationError({'parent_ids': 'L’élève doit être lié à au moins un parent.'})
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        password = validated_data.pop('login_password', None)
        student = super().create(validated_data)
        if password:
            User = get_user_model()
            student.account = User.objects.create_user(username=_next_student_identifier(), password=password, role='student', first_name=student.name)
            student.save(update_fields=['account'])
            _ensure_default_entry_window(student)
            if not student.badges.filter(state=Badge.State.ACTIVE).exists():
                create_badge(student)
        return student

    @transaction.atomic
    def update(self, instance, validated_data):
        parent_ids = validated_data.pop('parent_ids', None)
        password = validated_data.pop('login_password', None)
        student = super().update(instance, validated_data)
        if parent_ids is not None:
            student.parents.set(parent_ids)
        if password:
            User = get_user_model()
            student.account = User.objects.create_user(username=_next_student_identifier(), password=password, role='student', first_name=student.name)
            student.save(update_fields=['account'])
            _ensure_default_entry_window(student)
            if not student.badges.filter(state=Badge.State.ACTIVE).exists():
                create_badge(student)
        if not student.parents.exists():
            raise serializers.ValidationError({'parent_ids': 'Chaque élève doit être lié à au moins un parent.'})
        return student


class CheckpointSerializer(serializers.ModelSerializer):
    class Meta:
        model = Checkpoint
        fields = '__all__'


class ScanInputSerializer(serializers.Serializer):
    eventId = serializers.UUIDField()
    token = serializers.CharField(max_length=80, trim_whitespace=False)
    checkpointId = serializers.IntegerField(min_value=1)
    localTime = serializers.DateTimeField()
    estimatedTime = serializers.DateTimeField()
    deviceId = serializers.CharField(max_length=100, trim_whitespace=True)
    clockOffsetSeconds = serializers.FloatField(min_value=-86400, max_value=86400)


class DeviceTimeSyncSerializer(serializers.Serializer):
    deviceId = serializers.CharField(max_length=100, trim_whitespace=True)
    localTime = serializers.DateTimeField()


class PassageSyncEventSerializer(serializers.Serializer):
    event_id = serializers.UUIDField()
    token = serializers.CharField(max_length=80, trim_whitespace=False, allow_blank=False)
    local_time = serializers.DateTimeField()
    est_time = serializers.DateTimeField()


class DeviceEnrollmentSerializer(serializers.Serializer):
    checkpoint_id = serializers.IntegerField(write_only=True, min_value=1)
    device_id = serializers.UUIDField(required=False)


class ScanSerializer(serializers.ModelSerializer):
    checkpoint = serializers.CharField(source='checkpoint.label', read_only=True)

    class Meta:
        model = Scan
        fields = ['id', 'event_id', 'checkpoint', 'local_time', 'estimated_time', 'received_at', 'device_id', 'clock_suspect']


class BadgeSerializer(serializers.ModelSerializer):
    student_id = serializers.IntegerField(read_only=True)
    replaced_by_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = Badge
        fields = ['badge_id', 'student_id', 'state', 'issued_at', 'replaced_by_id']


class AnomalySerializer(serializers.ModelSerializer):
    student = StudentSerializer(read_only=True)

    class Meta:
        model = Anomaly
        fields = ['id', 'student', 'kind', 'occurred_at', 'details', 'state']


class MessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = OutboxMessage
        fields = ['id', 'message', 'state', 'reason', 'created_at']
