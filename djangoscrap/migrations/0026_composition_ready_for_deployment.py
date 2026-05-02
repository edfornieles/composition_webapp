from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("djangoscrap", "0025_ingestionbatch_last_scrape_engine_summary_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="composition",
            name="ready_for_deployment",
            field=models.BooleanField(default=False),
        ),
    ]

