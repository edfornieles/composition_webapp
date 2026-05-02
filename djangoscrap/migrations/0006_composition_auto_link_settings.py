from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("djangoscrap", "0005_composition_playback_speed"),
    ]

    operations = [
        migrations.AddField(
            model_name="composition",
            name="auto_link_delay_seconds",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="composition",
            name="random_link_enabled",
            field=models.BooleanField(default=False),
        ),
    ]
