from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [
        ("djangoscrap", "0006_composition_auto_link_settings"),
    ]

    operations = [
        migrations.CreateModel(
            name="Series",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=255, unique=True)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
            ],
            options={"ordering": ["name"]},
        ),
        migrations.AddField(
            model_name="composition",
            name="random_link_scope",
            field=models.CharField(
                choices=[("all", "All compositions"), ("same_series", "Same series only")],
                default="all",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="composition",
            name="series",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="compositions",
                to="djangoscrap.series",
            ),
        ),
    ]
