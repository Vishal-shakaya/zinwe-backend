"""
Renewals: a payment can extend an active plan. Each approved payment records the period it paid for.

Backfill: payments already approved get the one-month period they started (approval time + 1 month,
same rule billing.verify_payment used), so payment history shows a period for every cycle.
"""
import calendar

from django.db import migrations, models


def _add_month(moment):
    index = moment.month
    year, month = moment.year + index // 12, index % 12 + 1
    return moment.replace(year=year, month=month, day=min(moment.day, calendar.monthrange(year, month)[1]))


def backfill_periods(apps, schema_editor):
    ManualPayment = apps.get_model("z_auth", "ManualPayment")
    for payment in ManualPayment.objects.filter(status="verified", reviewed_at__isnull=False, covers_from__isnull=True):
        payment.covers_from = payment.reviewed_at
        payment.covers_until = _add_month(payment.reviewed_at)
        payment.save(update_fields=["covers_from", "covers_until"])


class Migration(migrations.Migration):

    dependencies = [
        ('z_auth', '0009_customer_remark'),
    ]

    operations = [
        migrations.AddField(
            model_name='manualpayment',
            name='covers_from',
            field=models.DateTimeField(blank=True, help_text='Start of the period this payment paid for (set when approved).', null=True),
        ),
        migrations.AddField(
            model_name='manualpayment',
            name='covers_until',
            field=models.DateTimeField(blank=True, help_text='End of that period.', null=True),
        ),
        migrations.AddField(
            model_name='manualpayment',
            name='is_renewal',
            field=models.BooleanField(default=False, help_text='Paid while the plan was already active, to extend it (renewal).'),
        ),
        migrations.RunPython(backfill_periods, migrations.RunPython.noop),
    ]
