from django.db import migrations


def rename_kind(apps, schema_editor):
    CompositionMediaAsset = apps.get_model("djangoscrap", "CompositionMediaAsset")
    CompositionMediaAsset.objects.filter(kind="preview_15s").update(kind="preview_10s")


def reverse_rename_kind(apps, schema_editor):
    CompositionMediaAsset = apps.get_model("djangoscrap", "CompositionMediaAsset")
    CompositionMediaAsset.objects.filter(kind="preview_10s").update(kind="preview_15s")


class Migration(migrations.Migration):

    dependencies = [
        ("djangoscrap", "0053_nftmintvoucher"),
    ]

    operations = [
        migrations.RunPython(rename_kind, reverse_rename_kind),
    ]
