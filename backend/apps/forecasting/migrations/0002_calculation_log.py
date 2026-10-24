from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("forecasting", "0001_initial")]
    operations = [migrations.AddField(model_name="forecastrun", name="calculation_log", field=models.JSONField(default=list))]
