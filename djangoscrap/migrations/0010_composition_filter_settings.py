from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("djangoscrap", "0009_composition_filter_params"),
    ]

    operations = [
        migrations.AddField(
            model_name="composition",
            name="filter_settings",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
