from django.contrib import admin

from .models import AdvancedThoughtScenario, CompositionMediaAsset, MintCollectionSettings, MonologuePersona
from .monologue_compat import monologue_visual_columns_ready


@admin.register(MonologuePersona)
class MonologuePersonaAdmin(admin.ModelAdmin):
    list_display = ("title", "slug", "archetype", "is_published", "updated_at")
    list_filter = ("is_published",)
    search_fields = ("title", "slug", "character_name", "archetype")
    prepopulated_fields = {"slug": ("title",)}
    ordering = ("-updated_at",)

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if not monologue_visual_columns_ready():
            qs = qs.defer("cached_visual_urls", "visual_cache_updated_at")
        return qs


@admin.register(AdvancedThoughtScenario)
class AdvancedThoughtScenarioAdmin(admin.ModelAdmin):
    list_display = ("title", "slug", "archetype", "is_published", "updated_at")
    list_filter = ("is_published", "archetype")
    search_fields = ("title", "slug", "character_name", "archetype")
    ordering = ("-updated_at",)


@admin.register(MintCollectionSettings)
class MintCollectionSettingsAdmin(admin.ModelAdmin):
    list_display = ("collection_title", "updated_at")


@admin.register(CompositionMediaAsset)
class CompositionMediaAssetAdmin(admin.ModelAdmin):
    list_display = ("composition", "kind", "status", "duration_seconds", "generated_at")
    list_filter = ("kind", "status")
    search_fields = ("composition__name",)
