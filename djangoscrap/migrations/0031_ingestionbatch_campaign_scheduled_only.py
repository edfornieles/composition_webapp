from django.db import migrations, models


def forwards_scheduled_only(apps, schema_editor):
    IngestionBatch = apps.get_model("djangoscrap", "IngestionBatch")
    IngestionBatch.objects.filter(campaign_mode__in=["manual", "hybrid"]).update(campaign_mode="scheduled")


class Migration(migrations.Migration):
    dependencies = [
        ("djangoscrap", "0030_ingestionbatch_campaign_fields"),
    ]

    operations = [
        migrations.AlterField(
            model_name="ingestionbatch",
            name="campaign_mode",
            field=models.CharField(default="scheduled", max_length=20),
        ),
        migrations.RunPython(forwards_scheduled_only, migrations.RunPython.noop),
    ]
