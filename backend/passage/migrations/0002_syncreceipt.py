from django.db import migrations,models

class Migration(migrations.Migration):
    dependencies=[('passage','0001_initial')]
    operations=[
        migrations.CreateModel(
            name='SyncReceipt',
            fields=[
                ('id',models.BigAutoField(auto_created=True,primary_key=True,serialize=False,verbose_name='ID')),
                ('event_id',models.UUIDField(unique=True)),
                ('payload',models.JSONField(blank=True,default=dict)),
                ('state',models.CharField(choices=[('pending','En attente de publication'),('queued','En file RabbitMQ'),('processing','En traitement'),('processed','Enregistré en base'),('rejected','Rejeté'),('failed','Échec définitif')],default='pending',max_length=16)),
                ('task_id',models.CharField(blank=True,max_length=64)),
                ('error',models.CharField(blank=True,max_length=250)),
                ('received_at',models.DateTimeField(auto_now_add=True)),
                ('updated_at',models.DateTimeField(auto_now=True)),
            ],
        ),
    ]
