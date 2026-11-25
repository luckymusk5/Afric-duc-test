from rest_framework import serializers
from .models import Student, Checkpoint, Badge, Scan, Anomaly, OutboxMessage


class StudentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Student
        fields = ['id', 'name', 'class_name', 'school', 'active']


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
        fields = ['id', 'student', 'kind', 'occurred_at', 'details']


class MessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = OutboxMessage
        fields = ['id', 'message', 'state', 'reason', 'created_at']
