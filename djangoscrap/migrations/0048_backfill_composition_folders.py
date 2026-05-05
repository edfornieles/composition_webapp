import uuid
from django.db import migrations


def assign_folders(apps, schema_editor):
    Composition = apps.get_model("djangoscrap", "Composition")
    for comp in Composition.objects.filter(grid_id="").only("id", "grid_id", "grid_cell_index"):
        comp.grid_id = uuid.uuid4().hex[:10]
        if not comp.grid_cell_index:
            comp.grid_cell_index = 1
        comp.save(update_fields=["grid_id", "grid_cell_index"])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("djangoscrap", "0047_composition_grid_cell_index_composition_grid_id_and_more"),
    ]
    operations = [
        migrations.RunPython(assign_folders, noop_reverse),
    ]
