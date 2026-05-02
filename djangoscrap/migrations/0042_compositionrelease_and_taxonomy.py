from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ("djangoscrap", "0041_compositionnft"),
    ]

    operations = [
        migrations.AddField(
            model_name="composition",
            name="composition_characters",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="composition",
            name="composition_emotions",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="composition",
            name="composition_themes",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.CreateModel(
            name="CompositionRelease",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("version_number", models.PositiveIntegerField()),
                ("status", models.CharField(choices=[("draft", "Draft"), ("published", "Published"), ("failed", "Failed")], default="draft", max_length=20)),
                ("publish_mode", models.CharField(default="update", max_length=20)),
                ("release_note", models.CharField(blank=True, default="", max_length=500)),
                ("r2_bucket", models.CharField(blank=True, default="", max_length=255)),
                ("r2_prefix", models.CharField(blank=True, default="", max_length=500)),
                ("manifest", models.JSONField(blank=True, default=list)),
                ("manifest_hash", models.CharField(blank=True, default="", max_length=128)),
                ("uploaded_count", models.PositiveIntegerField(default=0)),
                ("deleted_count", models.PositiveIntegerField(default=0)),
                ("metadata_snapshot", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("composition", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="releases", to="djangoscrap.composition")),
            ],
            options={
                "ordering": ["composition_id", "-version_number"],
            },
        ),
        migrations.CreateModel(
            name="CompositionReleaseFile",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("relative_path", models.CharField(max_length=700)),
                ("file_size", models.BigIntegerField(default=0)),
                ("sha256", models.CharField(blank=True, default="", max_length=128)),
                ("mime_type", models.CharField(blank=True, default="", max_length=255)),
                ("included", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("release", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="files", to="djangoscrap.compositionrelease")),
            ],
            options={
                "ordering": ["relative_path"],
            },
        ),
        migrations.AddConstraint(
            model_name="compositionrelease",
            constraint=models.UniqueConstraint(fields=("composition", "version_number"), name="unique_composition_release_version"),
        ),
        migrations.AddConstraint(
            model_name="compositionreleasefile",
            constraint=models.UniqueConstraint(fields=("release", "relative_path"), name="unique_composition_release_file_path"),
        ),
    ]

