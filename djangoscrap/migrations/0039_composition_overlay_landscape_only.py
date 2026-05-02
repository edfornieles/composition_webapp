from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("djangoscrap", "0038_composition_overlay_rotate"),
    ]

    operations = [
        migrations.AddField(
            model_name="composition",
            name="overlay_landscape_only",
            field=models.BooleanField(default=False),
        ),
    ]
