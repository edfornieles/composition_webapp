from django.db import models
from django.contrib.auth.models import User

class Profile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    mobile = models.CharField(max_length=15, blank=True, null=True)
    gender = models.CharField(max_length=10, blank=True, null=True)
    dob = models.DateField(blank=True, null=True)
    address = models.TextField(blank=True, null=True)

    def __str__(self):
        return self.user.username


class Composition(models.Model):
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("processing", "Processing"),
        ("completed", "Completed"),
        ("failed", "Failed"),
    ]

    TYPE_CHOICES = [
        ('classic', 'Classic'),
        ('tunnel', 'Tunnel'),
        ('topbottom', 'Top/Bottom'),
        ('leftright', 'Left/Right'),
    ]

    name = models.CharField(max_length=255)
    type =  models.CharField(max_length=20, choices=TYPE_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    url = models.URLField(max_length=500, blank=True, null=True)
    img = models.ImageField(upload_to="composition_thumbnails/", null=True, blank=True)
    background_video = models.FileField(upload_to="videos/backgrounds/")
    foreground_video = models.FileField(upload_to="videos/foregrounds/")
    audio_file = models.FileField(upload_to="audio/", blank=True, null=True)
    final_video = models.FileField(upload_to="videos/final/", blank=True, null=True)

    brightness = models.IntegerField(default=50)
    saturation = models.IntegerField(default=50)
    opacity = models.IntegerField(default=100)
    transition = models.CharField(max_length=20, choices=[("fade", "Fade"), ("crossfade", "Crossfade")])

    background_sources = models.JSONField(default=list)
    foreground_sources = models.JSONField(default=list)

    def __str__(self):
        return self.name
    

class Source(models.Model):
    name = models.CharField(max_length=255)
    type = models.CharField(max_length=100)
    source_id = models.CharField(max_length=255)
    last_scraped = models.DateField()
    query_no = models.BigIntegerField()
    initial = models.IntegerField()
    max_num = models.IntegerField()
    # created_at = models.DateTimeField(auto_now_add=True, null=True)
    # updated_at = models.DateTimeField(auto_now=True, null=True)
    def __str__(self):
        return self.name


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
