from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("djangoscrap", "0012_composition_overlay_layer"),
    ]

    operations = [
        migrations.AddField(
            model_name="composition",
            name="overlay_scale",
            field=models.FloatField(default=1.0),
        ),
    ]

