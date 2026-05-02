from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("djangoscrap", "0028_composition_mood_rating"),
    ]

    operations = [
        migrations.AddField(
            model_name="ingestionbatch",
            name="ai_query_pack",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="ingestionbatch",
            name="concept_brief",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="ingestionbatch",
            name="mood_rating",
            field=models.CharField(
                choices=[("chill", "Chill"), ("mid", "Mid"), ("agro", "Agro")],
                default="mid",
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name="ingestionbatch",
            name="target_asset_count",
            field=models.PositiveIntegerField(default=800),
        ),
    ]
