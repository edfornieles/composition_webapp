from django.contrib import admin
from django.urls import path
from django.contrib.auth.views import LoginView, LogoutView
from .views import list_buckets, bucket_contents, upload_file,create_video,user_logout,delete_compositions
from django.conf import settings
from django.conf.urls.static import static
from . import views
from django.urls import reverse_lazy
from .views import S3BucketAjaxView,create_bucket

urlpatterns = [
    path('', views.home, name='home'),
    path('service/', views.service, name='service'),
    path('portfolio/', views.portfolio, name='portfolio'),
    path('register/', views.register, name='register'),

    # Custom Admin Panel
    path('my-admin/', views.admin_login, name='admin-login'),
    path('admin-dashboard/', views.admin_dashboard, name='admin-dashboard'),
    path('composition-add/', views.add_composition, name='composition-add'),
    path('composition-view/', views.composition_view, name='composition-view'),
    path('new-source/', views.new_source, name='new-source'),
    path('source-library/', views.source_library, name='source-library'),
    path('dashboard/', views.admin_dashboard, name='dashboard'),
    path('delete_compositions/', views.delete_compositions, name='delete_compositions'),
    path('composition/<slug:slug>/', views.composition_detail, name='composition_detail'),
    path('source-library/delete/', views.delete_buckets, name='delete_buckets'),
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
    path("create-bucket/", create_bucket, name="create_bucket"),


    path("create-video/", create_video, name="create-video"),
     
]+ static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

# Serving media files during development
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    
    
