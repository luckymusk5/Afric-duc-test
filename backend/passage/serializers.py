from rest_framework import serializers
from .models import Student,Checkpoint,Badge,RouteWindow,Scan,Anomaly,OutboxMessage
class StudentSerializer(serializers.ModelSerializer):
    class Meta: model=Student; fields=['id','name','class_name','school','active']
class CheckpointSerializer(serializers.ModelSerializer):
    class Meta: model=Checkpoint; fields='__all__'
class ScanInputSerializer(serializers.Serializer):
    eventId=serializers.UUIDField(); token=serializers.CharField(max_length=220); checkpointId=serializers.IntegerField(); localTime=serializers.DateTimeField(); deviceId=serializers.CharField(max_length=100)
class ScanSerializer(serializers.ModelSerializer):
    checkpoint=serializers.CharField(source='checkpoint.label',read_only=True)
    class Meta: model=Scan; fields=['id','event_id','checkpoint','local_time','received_at','device_id','clock_skew']
class AnomalySerializer(serializers.ModelSerializer):
    student=StudentSerializer(read_only=True)
    class Meta: model=Anomaly; fields=['id','student','kind','occurred_at','details']
class MessageSerializer(serializers.ModelSerializer):
    class Meta: model=OutboxMessage; fields=['id','message','state','reason','created_at']
