from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("djangoscrap", "0042_compositionrelease_and_taxonomy"),
    ]

    operations = [
        migrations.AddField(
            model_name="composition",
            name="publish_excluded_files",
            field=models.JSONField(blank=True, default=list),
        ),
    ]

