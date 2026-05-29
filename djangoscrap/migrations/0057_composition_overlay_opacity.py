from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("djangoscrap", "0056_chatsession_state"),
    ]

    operations = [
        migrations.AddField(
            model_name="composition",
            name="overlay_opacity",
            field=models.FloatField(default=1.0),
        ),
    ]
