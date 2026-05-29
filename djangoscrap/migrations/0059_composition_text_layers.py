from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("djangoscrap", "0058_composition_color_layer"),
    ]

    operations = [
        migrations.AddField(
            model_name="composition",
            name="text_layers",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
