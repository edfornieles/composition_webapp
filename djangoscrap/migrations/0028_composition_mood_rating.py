from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("djangoscrap", "0027_composition_landscape_only"),
    ]

    operations = [
        migrations.AddField(
            model_name="composition",
            name="mood_rating",
            field=models.CharField(
                choices=[("chill", "Chill"), ("mid", "Mid"), ("agro", "Agro")],
                default="mid",
                max_length=10,
            ),
        ),
    ]
