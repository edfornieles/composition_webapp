from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("djangoscrap", "0029_ingestionbatch_ai_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="ingestionbatch",
            name="campaign_cadence_minutes",
            field=models.PositiveIntegerField(default=120),
        ),
        migrations.AddField(
            model_name="ingestionbatch",
            name="campaign_enabled",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="ingestionbatch",
            name="campaign_last_report",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="ingestionbatch",
            name="campaign_last_run_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="ingestionbatch",
            name="campaign_last_status",
            field=models.CharField(blank=True, default="", max_length=24),
        ),
        migrations.AddField(
            model_name="ingestionbatch",
            name="campaign_max_candidates_per_run",
            field=models.PositiveIntegerField(default=180),
        ),
        migrations.AddField(
            model_name="ingestionbatch",
            name="campaign_max_imports_per_run",
            field=models.PositiveIntegerField(default=90),
        ),
        migrations.AddField(
            model_name="ingestionbatch",
            name="campaign_max_pages_per_run",
            field=models.PositiveIntegerField(default=4),
        ),
        migrations.AddField(
            model_name="ingestionbatch",
            name="campaign_mode",
            field=models.CharField(default="hybrid", max_length=20),
        ),
        migrations.AddField(
            model_name="ingestionbatch",
            name="campaign_next_run_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="ingestionbatch",
            name="campaign_runs_count",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="ingestionbatch",
            name="campaign_state",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="ingestionbatch",
            name="campaign_target_count",
            field=models.PositiveIntegerField(default=800),
        ),
    ]
