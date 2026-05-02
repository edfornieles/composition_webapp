from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("djangoscrap", "0007_series_and_random_scope"),
    ]

    operations = [
        migrations.AddField(
            model_name="composition",
            name="filter_intensity",
            field=models.PositiveIntegerField(default=40),
        ),
        migrations.AddField(
            model_name="composition",
            name="filter_preset",
            field=models.CharField(
                choices=[
                    ("none", "None"),
                    ("dream", "Dream"),
                    ("noir", "Noir"),
                    ("acid", "Acid"),
                    ("glitch", "Glitch"),
                ],
                default="none",
                max_length=30,
            ),
        ),
    ]
