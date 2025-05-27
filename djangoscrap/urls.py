from django.urls import path
from django.contrib.auth.views import LoginView
from django.conf import settings
from django.conf.urls.static import static
from . import views
urlpatterns = [
    # Login & Logout
    path('login/', LoginView.as_view(template_name='login.html', redirect_authenticated_user=True), name='login'),
    path('logout/', views.user_logout, name='logout'),

    path('', views.home, name='home'),
    path('service/', views.service, name='service'),
    path('portfolio/', views.portfolio, name='portfolio'),
    path('register/', views.register, name='register'),

    # Custom Admin Panel
    path('admin/', views.admin_login, name='admin-login'),
    path('admin-dashboard/', views.admin_dashboard, name='admin-dashboard'),
    path('dashboard/', views.admin_dashboard, name='dashboard'),

    # Composition URLs
    path('composition-add/', views.add_composition, name='composition-add'),
    path('composition-view/', views.composition_view, name='composition-view'),
    path('delete_compositions/', views.delete_compositions, name='delete_compositions'),
    path('composition/<slug:slug>/', views.composition_detail, name='composition_detail'),

    # Source URLs
    path('sources/', views.list_sources, name='list_sources'),
    path('source/<str:source_name>/', views.source_contents, name='source_contents'),
    path('source/<str:source_name>/upload/', views.upload_file, name='upload_file'),
    path("create-source/", views.create_or_edit_source, name="create_source"),
    path("edit-source/<int:source_id>", views.create_or_edit_source, name="edit_source"),
    path('source-library/', views.source_library, name='source_library'),
    path('source-library/delete/', views.delete_source, name='delete_source'),
    path('source-library/download/', views.download_source, name='download_source'),
    path('source-file-delete/<str:source_name>/<path:file_name>/', views.delete_file_from_source, name='delete_file_from_source'),

    path('generate-video/<int:comp_id>/', views.generate_video, name='generate_video'),
    path("create-video/", views.create_video, name="create-video"),

]

# Serving media files during development
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    
    
