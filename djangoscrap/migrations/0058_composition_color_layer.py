from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("djangoscrap", "0057_composition_overlay_opacity"),
    ]

    operations = [
        migrations.AddField(
            model_name="composition",
            name="color_layer_enabled",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="composition",
            name="color_layer_color",
            field=models.CharField(default="#000000", max_length=9),
        ),
        migrations.AddField(
            model_name="composition",
            name="color_layer_opacity",
            field=models.FloatField(default=0.3),
        ),
        migrations.AddField(
            model_name="composition",
            name="color_layer_target",
            field=models.CharField(
                choices=[
                    ("background", "Above background"),
                    ("overlay", "Above overlay"),
                ],
                default="background",
                max_length=16,
            ),
        ),
    ]
