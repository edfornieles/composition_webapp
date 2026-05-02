from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("djangoscrap", "0011_composition_source_playback_mode"),
    ]

    operations = [
        migrations.AddField(
            model_name="composition",
            name="overlay_sources",
            field=models.JSONField(default=list),
        ),
        migrations.AddField(
            model_name="composition",
            name="overlay_speed",
            field=models.FloatField(default=1.0),
        ),
    ]

