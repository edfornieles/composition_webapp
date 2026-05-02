from django.db import migrations, models
import django.db.models.deletion
from django.utils.timezone import now


class Migration(migrations.Migration):

    dependencies = [
        ("djangoscrap", "0013_composition_overlay_scale"),
    ]

    operations = [
        migrations.CreateModel(
            name="IngestionBatch",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("title", models.CharField(blank=True, default="", max_length=255)),
                ("source_kind", models.CharField(default="upload", max_length=30)),
                ("destination_mode", models.CharField(default="new", max_length=20)),
                ("destination_source_name", models.CharField(blank=True, default="", max_length=255)),
                ("status", models.CharField(choices=[("draft", "Draft"), ("review", "Review"), ("published", "Published")], default="draft", max_length=20)),
                ("created_at", models.DateTimeField(default=now)),
                ("published_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="IngestionItem",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("original_url", models.TextField(blank=True, default="")),
                ("file_path", models.CharField(max_length=600)),
                ("media_kind", models.CharField(default="image", max_length=10)),
                ("decision", models.CharField(choices=[("pending", "Pending"), ("accepted", "Accepted"), ("rejected", "Rejected")], default="pending", max_length=20)),
                ("sha256", models.CharField(blank=True, default="", max_length=64)),
                ("created_at", models.DateTimeField(default=now)),
                ("batch", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="items", to="djangoscrap.ingestionbatch")),
            ],
            options={"ordering": ["id"]},
        ),
    ]

