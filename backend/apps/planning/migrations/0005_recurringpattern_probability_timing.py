from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("planning", "0004_alter_integrationconfig_kind_alter_plannedevent_kind"),
    ]

    operations = [
        migrations.AddField(
            model_name="recurringpattern",
            name="probability",
            field=models.FloatField(default=1.0),
        ),
        migrations.AddField(
            model_name="recurringpattern",
            name="timing_std_days",
            field=models.FloatField(default=0.0),
        ),
        migrations.AddField(
            model_name="recurringpattern",
            name="purpose_hint",
            field=models.CharField(blank=True, max_length=64),
        ),
    ]
