from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("djangoscrap", "0037_composition_nft_description_composition_nft_enabled_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="composition",
            name="overlay_rotate",
            field=models.BooleanField(default=False),
        ),
    ]
