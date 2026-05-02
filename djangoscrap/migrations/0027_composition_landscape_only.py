from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("djangoscrap", "0026_composition_ready_for_deployment"),
    ]

    operations = [
        migrations.AddField(
            model_name="composition",
            name="landscape_only",
            field=models.BooleanField(default=False),
        ),
    ]

