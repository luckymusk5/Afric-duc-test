from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import User,Student,Checkpoint,Badge,RouteWindow,Scan,Anomaly,OutboxMessage,AISummary,AuditLog
admin.site.register(User,UserAdmin)
for model in [Student,Checkpoint,Badge,RouteWindow,Scan,Anomaly,OutboxMessage,AISummary,AuditLog]: admin.site.register(model)
