from django.contrib import admin
from django.urls import path
from django.contrib.auth.views import LoginView, LogoutView
from .views import list_buckets, bucket_contents, upload_file,create_video,user_logout,delete_compositions
from django.conf import settings
from django.conf.urls.static import static
from . import views
from django.urls import reverse_lazy
from django.urls import re_path
from django.urls import include
from .views import S3BucketAjaxView,create_bucket
from django.views.generic import RedirectView

urlpatterns = [
    path("django-admin/", admin.site.urls),
    path('', RedirectView.as_view(pattern_name='admin-dashboard', permanent=False), name='home'),
    path('service/', views.service, name='service'),
    path('portfolio/', views.portfolio, name='portfolio'),
    path('register/', views.register, name='register'),
    path('mint/', views.mint_site, name='mint_site'),
    path('mint/random/', views.mint_random_page, name='mint_random'),
    path('mint/<slug:page_slug>/', views.mint_composition_page, name='mint_composition'),

    # Custom Admin Panel
    path('my-admin/', views.admin_login, name='admin-login'),
    path('admin-dashboard/', views.admin_dashboard, name='admin-dashboard'),
    path('mint-settings/', views.mint_collection_settings, name='mint-collection-settings'),
    path('composition-add/', views.add_composition, name='composition-add'),
    path('composition-edit/<int:composition_id>/', views.add_composition, name='composition-edit'),
    path('composition-view/', views.composition_view, name='composition-view'),
    path('associations-studio/', views.associations_studio, name='associations_studio'),
    path('associations-compare/', views.associations_compare, name='associations_compare'),
    path('aggregation-library/', views.aggregation_library, name='aggregation_library'),
    path('composition/<int:composition_id>/set-ready/', views.set_composition_ready, name='set_composition_ready'),
    path('composition/<int:composition_id>/duplicate/', views.composition_duplicate, name='composition_duplicate'),
    path('compositions/seed-character/', views.seed_character_compositions, name='seed_character_compositions'),
    # Legacy plural URL — bookmarks and muscle memory used /compositions/<id>/
    path(
        "compositions/<int:composition_id>/",
        RedirectView.as_view(pattern_name="composition_detail", permanent=False),
        name="composition_detail_legacy",
    ),
    path('series-library/', views.series_library, name='series-library'),
    path('series-library/<int:series_id>/', views.series_detail, name='series-detail'),
    path('composition-preview/<int:composition_id>/', views.composition_preview_image, name='composition-preview'),
    path('new-source/', views.new_source, name='new-source'),
    path('source-library/', views.source_library, name='source-library'),
    path('source-library/refresh/', views.refresh_source_library, name='refresh_source_library'),
    path('source-library/dedupe/', views.dedupe_source_folders, name='dedupe_source_folders'),
    path('ingestion/', views.ingestion_batches, name='ingestion_batches'),
    path('ingestion/<int:batch_id>/', views.ingestion_batch_detail, name='ingestion_batch_detail'),
    path('ingestion/<int:batch_id>/seed-preview/', views.ingestion_batch_seed_preview, name='ingestion_batch_seed_preview'),
    path('ingestion/<int:batch_id>/status.json', views.ingestion_batch_status, name='ingestion_batch_status'),
    path('ingestion/<int:batch_id>/bulk-ajax/', views.ingestion_batch_bulk_ajax, name='ingestion_batch_bulk_ajax'),
    path('ingestion/campaigns/tick/', views.ingestion_campaign_tick, name='ingestion_campaign_tick'),
    path('ingestion/item/<int:item_id>/decision/', views.ingestion_item_decision, name='ingestion_item_decision'),
    path('audio-sources/new/', views.new_audio_source, name='new-audio-source'),
    path('audio-sources/', views.audio_source_library, name='audio-source-library'),
    path('audio-sources/map/', views.audio_source_map_json, name='audio_source_map_json'),
    path('audio-sources/noise-studio/delete-track/', views.delete_noise_studio_track, name='delete_noise_studio_track'),
    path('audio-sources/create/', views.create_audio_source, name='create_audio_source'),
    path('audio-sources/noise-studio/save/', views.noise_studio_save, name='noise_studio_save'),
    path('audio-sources/noise-studio/', views.noise_studio, name='noise-studio'),
    path('audio-sources/<str:source_name>/', views.audio_source_contents, name='audio_source_contents'),
    path('audio-sources/<str:source_name>/rename/', views.rename_audio_source, name='rename_audio_source'),
    path('audio-sources/<str:source_name>/upload/', views.upload_audio_to_source, name='upload_audio_to_source'),
    path('audio-sources/<str:source_name>/delete-files/', views.delete_audio_source_files, name='delete_audio_source_files'),
    path('audio-sources/<str:source_name>/download-files/', views.download_audio_source_files, name='download_audio_source_files'),
    path('audio-sources/delete/', views.delete_audio_sources, name='delete_audio_sources'),
    path('dashboard/', views.admin_dashboard, name='dashboard'),
    path('delete_compositions/', views.delete_compositions, name='delete_compositions'),
    path('assign-compositions-series/', views.assign_compositions_series, name='assign_compositions_series'),
    path('composition/<int:composition_id>/', views.composition_detail, name='composition_detail'),
    path('composition/<int:composition_id>/nft/image.jpg', views.composition_nft_image, name='composition_nft_image'),
    path('composition/<int:composition_id>/nft/video.mp4', views.composition_nft_video, name='composition_nft_video'),
    path('composition/<int:composition_id>/nft/metadata.json', views.composition_nft_metadata, name='composition_nft_metadata'),
    path('composition/<int:composition_id>/nft/status.json', views.composition_nft_status, name='composition_nft_status'),
    path('composition/<int:composition_id>/nft/prepare/', views.composition_prepare_nft_version, name='composition_prepare_nft_version'),
    path('composition/<int:composition_id>/nft/record-mint/', views.composition_record_nft_mint, name='composition_record_nft_mint'),
    path('nft/version/<int:nft_id>/image.jpg', views.composition_nft_version_image, name='composition_nft_version_image'),
    path('nft/version/<int:nft_id>/video.mp4', views.composition_nft_version_video, name='composition_nft_version_video'),
    path('nft/version/<int:nft_id>/collector-video.mp4', views.composition_nft_version_collector_video, name='composition_nft_version_collector_video'),
    path('nft/version/<int:nft_id>/metadata.json', views.composition_nft_version_metadata, name='composition_nft_version_metadata'),
    path('composition/<int:composition_id>/nft/generate/', views.composition_generate_nft, name='composition_generate_nft'),
    path('composition/<int:composition_id>/nft/media/generate/', views.composition_generate_nft_media, name='composition_generate_nft_media'),
    path('nft/media/generate-all/', views.generate_all_nft_media, name='generate_all_nft_media'),
    path('nft/media/generate-selected/', views.generate_selected_nft_media, name='generate_selected_nft_media'),
    path('composition/<int:composition_id>/delete/', views.composition_delete, name='composition_delete'),
    path('composition/<int:composition_id>/latest-render/', views.composition_latest_render, name='composition_latest_render'),
    path('composition/<int:composition_id>/render-export/', views.render_composition_export, name='render_composition_export'),
    path('composition/<int:composition_id>/publish/diff/', views.composition_publish_diff, name='composition_publish_diff'),
    path('composition/<int:composition_id>/publish/release/', views.composition_publish_release, name='composition_publish_release'),
    path('composition/<int:composition_id>/publish/rollback/<int:release_id>/', views.composition_release_rollback, name='composition_release_rollback'),
    path('composition/<int:composition_id>/files/inventory/', views.composition_file_inventory, name='composition_file_inventory'),
    path('composition/<int:composition_id>/files/action/', views.composition_file_action, name='composition_file_action'),
    path('source-library/delete/', views.delete_buckets, name='delete_buckets'),
    path('source-library/<str:bucket_name>/trim-borders/', views.trim_source_borders, name='trim_source_borders'),
    path('source-library/download/', views.download_buckets, name='download_buckets'),
    path('delete-bucket/', views.delete_bucket, name='delete_bucket'),
    path('delete-bucket-files/<str:bucket_name>/<path:file_name>/', views.delete_file_from_bucket, name='delete_file_from_bucket'),
    path('generate-video/<int:comp_id>/', views.generate_video, name='generate_video'),

    # Login & Logout 
    path('login/', LoginView.as_view(template_name='login.html', redirect_authenticated_user=True), name='login'),
    path('logout/', views.user_logout, name='logout'),  # Use your custom logout view

    # S3 Bucket URLs
    path('s3/', list_buckets, name='list_buckets'),
    path('s3/<str:bucket_name>/', bucket_contents, name='bucket_contents'),
    path('s3/<str:bucket_name>/upload/', upload_file, name='upload_file'),
    path('s3/<str:bucket_name>/rename/', views.rename_source, name='rename_source'),
    path('s3/<str:bucket_name>/delete-files/', views.delete_source_files, name='delete_source_files'),
    path('s3/<str:bucket_name>/download-files/', views.download_source_files, name='download_source_files'),
    path('s3/<str:bucket_name>/delete-duplicates/', views.delete_duplicate_source_images, name='delete_duplicate_source_images'),
    path('s3/<str:bucket_name>/remove-stray-files/', views.remove_stray_source_files, name='remove_stray_source_files'),
    path("create-bucket/", create_bucket, name="create_bucket"),


    path("create-video/", create_video, name="create-video"),
    path("training-set-media/<path:file_name>", views.training_set_media_file, name="training_set_media_file"),
    path("source-media/<str:source_name>/<path:file_name>", views.source_media_file, name="source_media_file"),
    path("association-chain-media/<str:chain_name>/<path:file_name>", views.association_chain_media_file, name="association_chain_media_file"),
    path("source-thumbnail/<str:source_name>/<path:file_name>", views.source_thumbnail_image, name="source_thumbnail_image"),
    path("source-preview-assets/", views.source_preview_assets, name="source_preview_assets"),
    path("api/associations/next/", views.associations_next_asset, name="associations_next_asset"),
    path("api/associations/candidates/", views.associations_candidates, name="associations_candidates"),
    path("api/associations/select-winner/", views.associations_select_winner, name="associations_select_winner"),
    path("api/associations/bootstrap-training/", views.associations_bootstrap_training, name="associations_bootstrap_training"),
    path("api/associations/feedback-stats/", views.associations_feedback_stats, name="associations_feedback_stats"),
    path("api/associations/image-proxy/", views.associations_image_proxy, name="associations_image_proxy"),
    path("api/associations/feedback/", views.associations_feedback, name="associations_feedback"),
    path("api/associations/store/", views.associations_store_asset, name="associations_store_asset"),
    path("api/compositions/by-hashtag/<slug:hashtag>/", views.compositions_by_hashtag, name="compositions_by_hashtag"),
    path("audio-source-media/<str:source_name>/<path:file_name>", views.audio_source_media_file, name="audio_source_media_file"),
    re_path(r"^media/renders/render_(?P<composition_id>\d+)_.*\.mp4$", views.legacy_render_redirect, name="legacy_render_redirect"),
    path("thoughts/<slug:slug>/", views.monologue_public_page, name="monologue_public"),
    path("api/monologue/<slug:slug>/segment/", views.monologue_next_segment, name="monologue_segment"),
    path("api/monologue/<slug:slug>/background-images/", views.monologue_background_images, name="monologue_background_images"),
    path("character-thoughts/", views.monologue_list, name="monologue_list"),
    path("character-thoughts/lab/", views.thoughtscape_lab, name="thoughtscape_lab"),
    path("character-thoughts/wall/", views.wall_operator, name="wall_operator"),
    path("wall/player/<str:screen_id>/", views.wall_player, name="wall_player"),
    path("api/thoughtscape/lab/simulate/", views.thoughtscape_lab_simulate, name="thoughtscape_lab_simulate"),
    path("api/wall/state", views.wall_state, name="wall_state"),
    path("api/wall/control", views.wall_control, name="wall_control"),
    path("api/wall/heartbeat", views.wall_heartbeat, name="wall_heartbeat"),
    path("character-thoughts/advanced/", views.advanced_thoughts, name="advanced_thoughts"),
    path("character-thoughts/advanced/new/", views.advanced_thoughts_edit, name="advanced_thoughts_add"),
    path("character-thoughts/advanced/<int:scenario_id>/edit/", views.advanced_thoughts_edit, name="advanced_thoughts_edit"),
    path("character-thoughts/advanced/<int:scenario_id>/delete/", views.advanced_thoughts_delete, name="advanced_thoughts_delete"),
    path("character-thoughts/new/", views.monologue_edit, name="monologue_add"),
    path("character-thoughts/<int:persona_id>/edit/", views.monologue_edit, name="monologue_edit"),
    path("character-thoughts/<int:persona_id>/ai-research/", views.monologue_ai_research, name="monologue_ai_research"),
    path("character-thoughts/<int:persona_id>/delete/", views.monologue_delete, name="monologue_delete"),
    path("advanced-thoughts/<slug:slug>/", views.advanced_thoughts_public, name="advanced_thoughts_public"),
    path("api/advanced-thoughts/<slug:slug>/tick/", views.advanced_thoughts_tick, name="advanced_thoughts_tick"),
    path("sim-artwork/", include("djangoscrap.sim_artwork.urls")),
    path("collect/<slug:page_slug>/", views.composition_collect_page, name="composition_collect_page"),
    path('<slug:page_slug>/', views.composition_public_page, name='composition_public_page'),
     
]+ static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

# Serving media files during development
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    
    
