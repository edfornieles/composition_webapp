from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("djangoscrap", "0008_composition_filters"),
    ]

    operations = [
        migrations.AlterField(
            model_name="composition",
            name="filter_preset",
            field=models.CharField(default="none", max_length=50),
        ),
        migrations.AddField(
            model_name="composition",
            name="filter_param_1",
            field=models.FloatField(default=0.5),
        ),
        migrations.AddField(
            model_name="composition",
            name="filter_param_2",
            field=models.FloatField(default=0.5),
        ),
        migrations.AddField(
            model_name="composition",
            name="filter_param_3",
            field=models.FloatField(default=0.5),
        ),
    ]
