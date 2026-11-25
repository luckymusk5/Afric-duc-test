from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import User,Student,Checkpoint,Badge,RouteWindow,Scan,Anomaly,OutboxMessage,AISummary,AuditLog
admin.site.register(User,UserAdmin)
@admin.register(Badge)
class BadgeAdmin(admin.ModelAdmin):
    list_display=('badge_id','student','state','issued_at')
    list_filter=('state',)
    readonly_fields=('badge_id','student','secret_ciphertext','issued_at','replaced_by')
    exclude=('secret_ciphertext',)

for model in [Student,Checkpoint,RouteWindow,Scan,Anomaly,OutboxMessage,AISummary,AuditLog]: admin.site.register(model)
