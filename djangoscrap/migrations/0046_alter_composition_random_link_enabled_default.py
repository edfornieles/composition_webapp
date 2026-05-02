from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("djangoscrap", "0045_compositionnft_local_collector_video_file_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="composition",
            name="random_link_enabled",
            field=models.BooleanField(default=True),
        ),
    ]

