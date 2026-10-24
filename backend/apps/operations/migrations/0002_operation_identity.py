from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("operations", "0001_initial")]
    operations = [migrations.AlterUniqueTogether(name="operation", unique_together={("company", "operation_date", "operation_id")})]
