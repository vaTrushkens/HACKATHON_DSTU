from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("companies", "0001_initial")]
    operations = [
        migrations.AddField(model_name="company", name="external_code", field=models.CharField(max_length=128, blank=True)),
        migrations.AddField(model_name="company", name="dataset_id", field=models.UUIDField(null=True, blank=True, db_index=True)),
    ]
