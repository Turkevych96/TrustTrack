from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ledger', '0009_user_profile_module_preferences'),
    ]

    operations = [
        migrations.AddField(
            model_name='userprofile',
            name='telegram_language',
            field=models.CharField(
                choices=[('en', 'English'), ('ru', 'Russian')],
                default='en',
                max_length=2,
            ),
        ),
    ]
