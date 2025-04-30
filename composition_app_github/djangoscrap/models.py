from django.db import models
from django.contrib.auth.models import User
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils.timezone import now 

class Profile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    mobile = models.CharField(max_length=15, blank=True, null=True)
    gender = models.CharField(max_length=10, blank=True, null=True)
    dob = models.DateField(blank=True, null=True)
    address = models.TextField(blank=True, null=True)

    def __str__(self):
        return self.user.username

@receiver(post_save, sender=User)
def create_or_update_user_profile(sender, instance, created, **kwargs):
    Profile.objects.get_or_create(user=instance)  # Ensures a Profile is always created


from django.utils.text import slugify

class Composition(models.Model):
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("processing", "Processing"),
        ("completed", "Completed"),
        ("failed", "Failed"),
    ]

    name = models.CharField(max_length=255)
    type = models.CharField(max_length=100)
    date = models.DateTimeField(auto_now_add=True)
    img = models.ImageField(upload_to="composition_thumbnails/", null=True, blank=True)

    background_video = models.FileField(upload_to="videos/backgrounds/")
    foreground_video = models.FileField(upload_to="videos/foregrounds/")
    audio_file = models.FileField(upload_to="audio/", blank=True, null=True)

    final_video = models.FileField(upload_to="videos/final/", blank=True, null=True)
    url = models.URLField(max_length=500, blank=True, null=True)  # ✅ New field

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    brightness = models.IntegerField(default=50)
    saturation = models.IntegerField(default=50)
    opacity = models.IntegerField(default=100)
    transition = models.CharField(max_length=20, choices=[("fade", "Fade"), ("crossfade", "Crossfade")])

    background_sources = models.JSONField(default=list)
    foreground_sources = models.JSONField(default=list)

    created_at = models.DateTimeField(default=now)

    def __str__(self):
        return self.name



class S3Bucket(models.Model):
    name = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name
    

class Bucket(models.Model):
    name = models.CharField(max_length=255)
    type = models.CharField(max_length=100)
    source_id = models.CharField(max_length=255)
    last_scraped = models.DateField()
    query_no = models.BigIntegerField()
    initial = models.IntegerField()
    max_num = models.IntegerField()
    
    def __str__(self):
        return self.name
    

from django.db import models

class VideoComposition(models.Model):
    audio = models.FileField(upload_to="audios/", null=True, blank=True)
    output_video = models.FileField(upload_to="videos/", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Video Composition {self.id}"

class BackgroundImage(models.Model):
    video = models.ForeignKey(VideoComposition, on_delete=models.CASCADE, related_name="backgrounds")
    image = models.ImageField(upload_to="backgrounds/")

class ForegroundImage(models.Model):
    video = models.ForeignKey(VideoComposition, on_delete=models.CASCADE, related_name="foregrounds")
    image = models.ImageField(upload_to="foregrounds/")
