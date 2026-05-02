from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("djangoscrap", "0034_ingestionitem_clip_embedding"),
    ]

    operations = [
        migrations.AddField(
            model_name="composition",
            name="allowed_personas",
            field=models.ManyToManyField(
                blank=True,
                help_text="If set, this composition is limited to the selected characters in Thoughtscape.",
                related_name="allowed_compositions",
                to="djangoscrap.monologuepersona",
            ),
        ),
    ]
