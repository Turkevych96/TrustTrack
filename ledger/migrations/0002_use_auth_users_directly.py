import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def copy_person_users_to_direct_user_fields(apps, schema_editor):
    Obligation = apps.get_model('ledger', 'Obligation')
    LedgerAccount = apps.get_model('ledger', 'LedgerAccount')

    for obligation in Obligation.objects.select_related('creditor', 'borrower'):
        obligation.creditor_user_id = obligation.creditor.user_id
        obligation.borrower_user_id = obligation.borrower.user_id
        obligation.save(update_fields=['creditor_user', 'borrower_user'])

    for account in LedgerAccount.objects.select_related('person'):
        account.user_id = account.person.user_id
        account.save(update_fields=['user'])


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('ledger', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='obligation',
            name='creditor_user',
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='+',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name='obligation',
            name='borrower_user',
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='+',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name='ledgeraccount',
            name='user',
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='ledger_accounts',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.RunPython(copy_person_users_to_direct_user_fields, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name='ledgeraccount',
            name='person',
        ),
        migrations.RemoveField(
            model_name='obligation',
            name='borrower',
        ),
        migrations.RemoveField(
            model_name='obligation',
            name='creditor',
        ),
        migrations.RenameField(
            model_name='obligation',
            old_name='creditor_user',
            new_name='creditor',
        ),
        migrations.RenameField(
            model_name='obligation',
            old_name='borrower_user',
            new_name='borrower',
        ),
        migrations.AlterField(
            model_name='obligation',
            name='creditor',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name='credit_obligations',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name='obligation',
            name='borrower',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name='debt_obligations',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name='ledgeraccount',
            name='user',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name='ledger_accounts',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.DeleteModel(
            name='Person',
        ),
    ]
