import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('passage', '0003_deviceclock_remove_badge_secret_and_more')]

    operations = [
        migrations.AlterField(
            model_name='user',
            name='role',
            field=models.CharField(
                choices=[('admin', 'Responsable'), ('agent', 'Agent'), ('parent', 'Parent'), ('student', 'Élève')],
                default='parent',
                max_length=12,
            ),
        ),
        migrations.AddField(
            model_name='student',
            name='account',
            field=models.OneToOneField(
                blank=True,
                limit_choices_to={'role': 'student'},
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='student_profile',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
