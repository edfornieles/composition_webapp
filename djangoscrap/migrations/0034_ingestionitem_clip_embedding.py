# Adds a text field to IngestionItem for storing a base64-packed float16 CLIP
# ViT-B/32 embedding. Used by views._detect_duplicate for semantic near-duplicate
# rejection; empty string means the embedding hasn't been computed (or CLIP is
# unavailable / the item is a video).

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("djangoscrap", "0033_composition_overlay_frame_margin"),
    ]

    operations = [
        migrations.AddField(
            model_name="ingestionitem",
            name="clip_embedding",
            field=models.TextField(blank=True, default=""),
        ),
    ]
