from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ledger', '0010_userprofile_telegram_language'),
    ]

    operations = [
        migrations.AddField(
            model_name='userprofile',
            name='payment_due_notifications',
            field=models.BooleanField(default=True),
        ),
    ]
