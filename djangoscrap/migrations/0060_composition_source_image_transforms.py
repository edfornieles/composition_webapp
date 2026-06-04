from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("djangoscrap", "0059_composition_text_layers"),
    ]

    operations = [
        migrations.AddField(
            model_name="composition",
            name="source_image_transforms",
            field=models.JSONField(
                default=dict,
                blank=True,
                help_text='Per-image transform overrides. Keys are relative paths (source/file.jpg), values are {"flipH": bool, "flipV": bool, "crop": [x, y, w, h] or null}.',
            ),
        ),
    ]
