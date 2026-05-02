from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("djangoscrap", "0010_composition_filter_settings"),
    ]

    operations = [
        migrations.AddField(
            model_name="composition",
            name="source_playback_mode",
            field=models.CharField(
                choices=[
                    ("chronological", "Chronological"),
                    ("random", "Randomize across selected sources"),
                ],
                default="chronological",
                max_length=20,
            ),
        ),
    ]

