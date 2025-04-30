import os
import boto3
from django.shortcuts import render, redirect,get_object_or_404
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.models import User
from django.contrib.auth import login, authenticate,logout
from django.core.paginator import Paginator
from django.core.files.base import ContentFile
from django.conf import settings
from django.core.paginator import Paginator
from django.core.files.storage import default_storage
from .models import Composition, Profile, S3Bucket, Bucket,VideoComposition, BackgroundImage, ForegroundImage
from .video_processing import combine_video_with_audio,create_video_ffmpegNew
import ffmpeg
from django.core.files import File
from django.core.files.temp import NamedTemporaryFile
from PIL import Image, UnidentifiedImageError
from ajax_datatable.views import AjaxDatatableView
from .forms import BucketForm,CompositionForm
from botocore.exceptions import ClientError,BotoCoreError,NoCredentialsError
import uuid
from django.http import JsonResponse, HttpResponse, HttpResponseRedirect, FileResponse
import re
import io
import json
from django.views.decorators.csrf import csrf_exempt
from django.db import transaction
from moviepy.editor import ImageSequenceClip ,VideoFileClip, ImageClip, CompositeVideoClip,AudioFileClip, concatenate_videoclips
import moviepy.editor as mp
import random, json, tempfile, zipfile, os
import string
from datetime import datetime
import subprocess
from celery_app import classic_task,left_to_right_task,tunnel_task,right_to_left_task
from django.utils.crypto import get_random_string  # ✅ FIXED: Import added

try:
    from PIL import ImageResampling  # For newer Pillow versi ons
    RESAMPLING_METHOD = ImageResampling.LANCZOS
except ImportError:
    RESAMPLING_METHOD = Image.LANCZOS  # Fallback for older versions

# Initialize S3 Client
s3 = boto3.client('s3')
BUCKET_NAME = "composition-final"  # Replace with your actual bucket name
#S3_FOLDER = "classic-musical/"  # Default S3 folder for uploads

# Ensure media directories exist
UPLOAD_DIR = "media/uploads/"
VIDEO_DIR = "media/videos/"
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(VIDEO_DIR, exist_ok=True)

def create_video(request):
    if request.method == "POST" and request.FILES:
        background_images = request.FILES.getlist("background")
        foreground_images = request.FILES.getlist("foreground")
        audio_file = request.FILES.get("audio")

        # Create a VideoComposition object
        video_comp = VideoComposition(audio=audio_file)
        video_comp.save()

        # Save background images
        bg_paths = []
        for bg in background_images:
            bg_obj = BackgroundImage(video=video_comp, image=bg)
            bg_obj.save()
            bg_paths.append(bg_obj.image.path)

        # Save foreground images
        fg_paths = []
        for fg in foreground_images:
            fg_obj = ForegroundImage(video=video_comp, image=fg)
            fg_obj.save()
            fg_paths.append(fg_obj.image.path)

        # Define video duration per image
        duration = 0.5  # Set duration per image
        bg_clips = [ImageClip(bg).set_duration(duration).resize((1280, 720)) for bg in bg_paths]
        fg_clips = [ImageClip(fg).set_duration(duration).resize((400, 300)).set_position(("center", "center")) for fg in fg_paths]

        # Concatenate clips
        final_bg_clip = concatenate_videoclips(bg_clips, method="compose")
        final_fg_clip = concatenate_videoclips(fg_clips, method="compose")

        # Merge background and foreground
        final_clip = CompositeVideoClip([final_bg_clip, final_fg_clip])

        # Add audio if available
        if audio_file:
            audio_path = video_comp.audio.path
            audio = AudioFileClip(audio_path).set_duration(final_clip.duration)
            final_clip = final_clip.set_audio(audio)

        # Export final video
        output_video_path = os.path.join(VIDEO_DIR, f"output_{video_comp.id}.mp4")
        final_clip.write_videofile(output_video_path, fps=24)

        # Save the output video path
        video_comp.output_video.name = f"videos/output_{video_comp.id}.mp4"
        video_comp.save()

        return render(request, "admin/upload_file.html", {"video_path": video_comp.output_video.url})

    return render(request, "admin/upload_file.html")

# Initialize S3 Client
aws_access_key = os.getenv("AWS_ACCESS_KEY_ID")
aws_secret_key = os.getenv("AWS_SECRET_ACCESS_KEY")
aws_region = os.getenv("AWS_S3_REGION_NAME", "us-east-1")

if not aws_access_key or not aws_secret_key:
    raise ValueError("AWS credentials not found!")

s3 = boto3.client(
    "s3",
    aws_access_key_id=aws_access_key,
    aws_secret_access_key=aws_secret_key,
    region_name=aws_region,
)

def is_valid_bucket_name(bucket_name):
    """Check if the S3 bucket name follows AWS naming rules"""
    return bool(re.match(r'^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$', bucket_name))

@staff_member_required
def create_bucket(request):
    if request.method == "POST":
        form = BucketForm(request.POST)
        if form.is_valid():
            bucket_name_raw = form.cleaned_data["name"]
            new_bucket_name = bucket_name_raw.lower().replace(" ", "-")

            # 🔁 Check if bucket exists locally (in DB)
            if Bucket.objects.filter(name__iexact=bucket_name_raw).exists():
                form.add_error("name", "A bucket with this name already exists!.")
                return render(request, "admin/new-source.html", {"form": form})

            # ✅ Initialize S3 client
            s3 = boto3.client(
                "s3",
                aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
                aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
                region_name=settings.AWS_S3_REGION_NAME,
            )

            # 🔁 Check if bucket exists on AWS S3
            try:
                s3.head_bucket(Bucket=new_bucket_name)
                # If no exception, bucket exists
                form.add_error("name", "A bucket with this name already exists on AWS S3.")
                return render(request, "admin/new-source.html", {"form": form})
            except ClientError as e:
                error_code = int(e.response['Error']['Code'])
                if error_code == 404:
                    pass  # Bucket does not exist — continue
                elif error_code == 403:
                    # The bucket exists but you don't own it (still a conflict)
                    form.add_error("name", "A bucket with this name already exists!")
                    return render(request, "admin/new-source.html", {"form": form})
                else:
                    # Unknown error
                    messages.error(request, f"Unexpected error checking S3: {e}")
                    return redirect("list_buckets")

            # ✅ Save to DB now that everything checks out
            bucket = form.save(commit=False)
            bucket.name = bucket_name_raw  # or new_bucket_name if you want the slugified one
            bucket.save()

            # ✅ Create the bucket on S3
            try:
                if settings.AWS_S3_REGION_NAME == "us-east-1":
                    response = s3.create_bucket(Bucket=new_bucket_name)
                else:
                    response = s3.create_bucket(
                        Bucket=new_bucket_name,
                        CreateBucketConfiguration={
                            "LocationConstraint": settings.AWS_S3_REGION_NAME
                        },
                    )
                print(f"S3 bucket creation response: {response}")
                messages.success(request, f"S3 Bucket '{new_bucket_name}' created successfully!")
            except ClientError as e:
                print(f"Error creating bucket: {e}")
                messages.error(request, f"Error creating S3 bucket: {e}")
                return redirect("list_buckets")

            # ✅ Disable block public access
            try:
                s3.put_public_access_block(
                    Bucket=new_bucket_name,
                    PublicAccessBlockConfiguration={
                        "BlockPublicAcls": False,
                        "IgnorePublicAcls": False,
                        "BlockPublicPolicy": False,
                        "RestrictPublicBuckets": False,
                    },
                )
                messages.success(request, f"Disabled 'Block Public Access' for '{new_bucket_name}'!")
            except ClientError as e:
                print("Error disabling Block Public Access:", e)
                messages.error(request, f"Error disabling Block Public Access: {e}")
                return redirect("list_buckets")

            # ✅ Add public read policy
            bucket_policy = {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Sid": "PublicReadGetObject",
                        "Effect": "Allow",
                        "Principal": "*",
                        "Action": "s3:GetObject",
                        "Resource": f"arn:aws:s3:::{new_bucket_name}/*",
                    }
                ],
            }

            try:
                s3.put_bucket_policy(
                    Bucket=new_bucket_name,
                    Policy=json.dumps(bucket_policy),
                )
                messages.success(request, f"Public access enabled for '{new_bucket_name}'!")
            except ClientError as e:
                print("Error applying bucket policy:", e)
                messages.error(request, f"Error setting bucket policy: {e}")
                return redirect("list_buckets")

            return redirect("list_buckets")

    else:
        form = BucketForm()

    return render(request, "admin/new-source.html", {"form": form})
   
@staff_member_required
def new_source(request):
    return render(request, "admin/new-source.html")


@staff_member_required
def source_library(request):
    sources = Bucket.objects.all().order_by('-last_scraped')

    s3 = boto3.client(
        "s3",
        aws_access_key_id=aws_access_key,
        aws_secret_access_key=aws_secret_key,
        region_name=aws_region,
    )

    local_buckets = Bucket.objects.values_list('name', flat=True)

    try:
        s3_buckets = s3.list_buckets()["Buckets"]
        s3_bucket_names = [bucket["Name"] for bucket in s3_buckets]
    except ClientError as e:
        messages.error(request, f"Error fetching S3 buckets: {e}")
        s3_bucket_names = []

    matching_buckets = set(local_buckets) & set(s3_bucket_names)
    #print("✅ Matching S3 Buckets:", matching_buckets)

    bucket_thumbnails = {}
    all_available_images = []  # For fallback

    for bucket_name in matching_buckets:
        try:
            response = s3.list_objects_v2(Bucket=bucket_name)
            contents = response.get("Contents", [])
            image_files = [obj for obj in contents if obj["Key"].lower().endswith((".png", ".jpg", ".jpeg"))]
            if image_files:
                # Save random image for this bucket
                image_file = random.choice(image_files)
                image_key = image_file["Key"]
                image_url = f"https://{bucket_name}.s3.amazonaws.com/{image_key}"
                bucket_thumbnails[bucket_name] = image_url

                # Save all image URLs for fallback
                for img in image_files:
                    all_available_images.append(f"https://{bucket_name}.s3.amazonaws.com/{img['Key']}")
        except ClientError as e:
            print(f"Error accessing bucket {bucket_name}: {e}")
            continue

    # Assign thumbnails
    for bucket in sources:
        if bucket.name in bucket_thumbnails:
            bucket.thumbnail = bucket_thumbnails[bucket.name]
        elif all_available_images:
            bucket.thumbnail = random.choice(all_available_images)
        else:
            bucket.thumbnail = None  # fallback to placeholder in template

    return render(request, "admin/source-library.html", {"compositions": sources})

### S3 Bucket Management

def get_sample_image_url(bucket_name):
    """ Get the first image (JPG, PNG, JPEG, GIF) from the bucket """
    try:
        response = s3.list_objects_v2(Bucket=bucket_name)
        if "Contents" in response:
            for obj in response["Contents"]:
                key = obj["Key"].lower()
                if key.endswith(('.jpg', '.jpeg', '.png', '.gif')):
                    # Generate a public URL if the bucket/object is public
                    return f"https://{bucket_name}.s3.amazonaws.com/{obj['Key']}"
        return None
    except Exception as e:
        print(f"Error accessing {bucket_name}: {e}")
        return None

def list_buckets(request):
    """ List all S3 buckets with sample image """
    response = s3.list_buckets()
    buckets_data = []

    for bucket in response.get("Buckets", []):
        image_url = get_sample_image_url(bucket["Name"])
        buckets_data.append({
            "Name": bucket["Name"],
            "CreationDate": bucket["CreationDate"],
            "image_url": image_url
        })

    return render(request, 'admin/s3_buckets.html', {'buckets': buckets_data})
 
def bucket_contents(request, bucket_name):
    """ Get contents of a selected bucket """
    objects = s3.list_objects_v2(Bucket=bucket_name).get("Contents", [])
    return render(request, 'admin/bucket_contents.html', {'objects': objects, 'bucket_name': bucket_name})

@csrf_exempt
def delete_bucket(request):
    if request.method == 'POST':
        bucket_name = request.POST.get('bucket_name')
        if bucket_name:
            s3 = boto3.client('s3')
            try:
                # First delete all objects inside the bucket
                objects = s3.list_objects_v2(Bucket=bucket_name)
                if 'Contents' in objects:
                    for obj in objects['Contents']:
                        s3.delete_object(Bucket=bucket_name, Key=obj['Key'])
                
                # Then delete the bucket
                s3.delete_bucket(Bucket=bucket_name)
                messages.success(request, f"Bucket '{bucket_name}' deleted successfully.")
            except Exception as e:
                messages.error(request, f"Error deleting bucket: {e}")
    return redirect('list_buckets')  # replace with your actual bucket list view name
     
@csrf_exempt
@staff_member_required
def delete_buckets(request):
    if request.method == "POST":
        selected = request.POST.getlist("buckets")
        s3 = boto3.client('s3', aws_access_key_id=aws_access_key,
                          aws_secret_access_key=aws_secret_key,
                          region_name=aws_region)
        for bucket_name in selected:
            try:
                # Delete all objects in the bucket
                objects = s3.list_objects_v2(Bucket=bucket_name)
                for obj in objects.get('Contents', []):
                    s3.delete_object(Bucket=bucket_name, Key=obj['Key'])

                # Delete bucket
                s3.delete_bucket(Bucket=bucket_name)

                # Delete from local DB
                Bucket.objects.filter(name=bucket_name).delete()

            except Exception as e:
                messages.error(request, f"Error deleting {bucket_name}: {e}")
        messages.success(request, "Selected buckets deleted successfully.")
    return HttpResponseRedirect(request.META.get('HTTP_REFERER'))


@csrf_exempt
@staff_member_required
def download_buckets(request):
    if request.method == "POST":
        selected = request.POST.getlist("buckets")
        s3 = boto3.client('s3', aws_access_key_id=aws_access_key,
                          aws_secret_access_key=aws_secret_key,
                          region_name=aws_region)

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as zip_file:
            for bucket_name in selected:
                try:
                    objects = s3.list_objects_v2(Bucket=bucket_name)
                    for obj in objects.get('Contents', []):
                        key = obj['Key']
                        file_obj = s3.get_object(Bucket=bucket_name, Key=key)
                        content = file_obj['Body'].read()
                        zip_path = f"{bucket_name}/{key}"
                        zip_file.writestr(zip_path, content)
                except Exception as e:
                    messages.error(request, f"Failed to download from {bucket_name}: {e}")

        zip_buffer.seek(0)
        return FileResponse(zip_buffer, as_attachment=True, filename="buckets.zip")

from django.urls import reverse

@csrf_exempt
@staff_member_required
def upload_file(request, bucket_name):
    if request.method == 'POST':
        files = request.FILES.getlist('files')

        if len(files) > 50:
            messages.error(request, "You can upload a maximum of 50 files at once.")
            return redirect(request.path)

        s3 = boto3.client(
            's3',
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
            region_name=settings.AWS_S3_REGION_NAME
        )

        has_error = False

        for file in files:
            try:
                s3.upload_fileobj(
                    file,
                    bucket_name,
                    file.name,
                    ExtraArgs={'ContentType': file.content_type}
                )
                messages.success(request, f"Uploaded: {file.name}")
            except Exception as e:
                has_error = True
                messages.error(request, f"Error uploading {file.name}: {e}")

        # ✅ Redirect to bucket_contents if all uploaded successfully
        if not has_error:
            return redirect('bucket_contents', bucket_name=bucket_name)

    return render(request, 'admin/upload.html', {'bucket_name': bucket_name})



@csrf_exempt
@staff_member_required
def delete_file_from_bucket(request, bucket_name, file_name):
    if request.method == 'POST':
        s3 = boto3.client(
            's3',
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
            region_name=settings.AWS_S3_REGION_NAME
        )
        try:
            s3.delete_object(Bucket=bucket_name, Key=file_name)
            messages.success(request, f"{file_name} deleted.")
        except Exception as e:
            messages.error(request, f"Failed to delete {file_name}: {e}")
        
        return redirect('bucket_contents', bucket_name=bucket_name)
     
class S3BucketAjaxView(AjaxDatatableView):
    model = S3Bucket
    initial_order = [["created_at", "desc"]]

    column_defs = [
        {"name": "id", "title": "ID"},
        {"name": "name", "title": "Bucket Name"},
        {"name": "created_at", "title": "Created At"},
    ]

### **User Registration & Authentication**

def register(request):
    if request.method == "POST":
        email = request.POST["email"]
        username = request.POST["username"]
        password = request.POST["password"]
        confirm_password = request.POST["confirm_password"]

        if password != confirm_password:
            messages.error(request, "Passwords do not match!")
            return redirect("register")

        if User.objects.filter(username=username).exists():
            messages.error(request, "Username already taken!")
            return redirect("register")

        if User.objects.filter(email=email).exists():
            messages.error(request, "Email already registered!")
            return redirect("register")

        user = User.objects.create_user(username=username, email=email, password=password)
        Profile.objects.get_or_create(user=user)
        messages.success(request, "Account created successfully! You can now log in.")
        return redirect("login")

    return render(request, "register.html")
### **Admin & Composition Management**
 
def home(request):
    return render(request, 'home.html')

def service(request):
    return render(request, 'service-detail.html')

def portfolio(request):
    return render(request, 'profile-detail.html')

def admin_login(request):
    if request.method == "POST":
        username = request.POST["username"]
        password = request.POST["password"]
        user = authenticate(request, username=username, password=password)

        if user is not None and user.is_staff:
            login(request, user)
            Profile.objects.get_or_create(user=user)
            return redirect("admin-dashboard")
        else:
            messages.error(request, "Invalid admin credentials!")
            return redirect("admin-login")

    return render(request, "admin/admin_login.html")

@staff_member_required
def admin_dashboard(request):
    return render(request, "admin/dashboard.html")

def convert_media_to_video(media_files, output_path, fps=24, duration_per_frame=2):
    """
    Converts a mix of images, GIFs, and videos into a video sequence.
    Each image is displayed for 1 second, and each frame of a video is displayed for 0.5 seconds.
    """
    clips = []

    for file in media_files:
        if file.lower().endswith((".mp4", ".mov", ".avi", ".mkv", ".gif")):
            video_clip = VideoFileClip(file)
            video_clip = video_clip.set_fps(2)  # 0.5 seconds per frame (2 FPS)
            clips.append(video_clip)
        else:
            img_clip = ImageSequenceClip([file], durations=[1])  # 1 second per image
            clips.append(img_clip)

    if not clips:
        return None

    final_clip = concatenate_videoclips(clips, method="compose")
    final_clip.write_videofile(output_path, fps=fps)
    
    return output_path

def ensure_even_dimensions(img):
    """Ensure image dimensions are even for FFmpeg compatibility."""
    width, height = img.size
    new_width = width if width % 2 == 0 else width + 1
    new_height = height if height % 2 == 0 else height + 1
    
    if (width, height) != (new_width, new_height):
        img = img.resize((new_width, new_height), RESAMPLING_METHOD)
    
    return img

def is_valid_image(file_path):
    """Check if the file is a valid image format."""
    try:
        with Image.open(file_path) as img:
            img.verify()  # Verify if it's an actual image
        return True
    except (IOError, UnidentifiedImageError):
        return False  # Not a valid image

def merge_images(left_files, right_files, output_folder):
    if not os.path.exists(output_folder):
        os.makedirs(output_folder)
    
    left_files = [f for f in left_files if is_valid_image(f)]
    right_files = [f for f in right_files if is_valid_image(f)]
    
    merged_image_paths = []
    for i, (left_path, right_path) in enumerate(zip(left_files, right_files)):
        left_img = Image.open(left_path).convert("RGB")
        right_img = Image.open(right_path).convert("RGB")
        
        left_crop = left_img.crop((left_img.width // 2, 0, left_img.width, left_img.height))
        right_crop = right_img.crop((0, 0, right_img.width // 2, right_img.height))
        
        merged_img = Image.new('RGB', (left_crop.width + right_crop.width, left_img.height))
        merged_img.paste(left_crop, (0, 0))
        merged_img.paste(right_crop, (left_crop.width, 0))
        
        merged_img = ensure_even_dimensions(merged_img)
        merged_img_path = os.path.join(output_folder, f"merged_{i:03d}.png")  
        merged_img.save(merged_img_path)
        merged_image_paths.append(merged_img_path)
    
    return merged_image_paths

def create_video_ffmpeg(image_folder, output_video, fps=1):
    files = sorted([f for f in os.listdir(image_folder) if f.endswith('.png')])
    
    if not files:
        print("❌ No images found in merged folder. Exiting.")
        return None  # Return None if no images exist
    
    command = [
        "ffmpeg", "-y", "-framerate", str(fps), "-i", os.path.join(image_folder, "merged_%03d.png"),
        "-vf", "format=yuv420p", "-c:v", "libx264", "-r", "30", "-movflags", "+faststart", output_video
    ]
    
    result = subprocess.run(command, capture_output=True, text=True)
    
    if result.returncode == 0:
        print(f"✅ Video created successfully: {output_video}")
        return output_video
    else:
        print("❌ Error creating video. FFmpeg output:")
        print(result.stderr)
        return None  # Return None if video creation fails
   


@staff_member_required
def add_composition(request):
    # Define paths for downloaded images & videos 
    TEMP_BG_FOLDER = "media/temp_s3_back_files"
    TEMP_FG_FOLDER = "media/temp_s3_fore_files"
    VIDEO_DIR = "media/videos"
    TEMP_IMAGE_FOLDER = "media/temp_images"
    THUMBNAIL_DIR = "static/composition_thumbnails"
    AUDIO_DIR = "compositions/audios"
    MERGED_IMAGE_DIR = "media/merged_images"
    
    # Ensure necessary directories exist
    os.makedirs(TEMP_BG_FOLDER, exist_ok=True)
    os.makedirs(TEMP_FG_FOLDER, exist_ok=True)
    os.makedirs(VIDEO_DIR, exist_ok=True)
    os.makedirs(THUMBNAIL_DIR, exist_ok=True)
    os.makedirs(AUDIO_DIR, exist_ok=True)
    os.makedirs(MERGED_IMAGE_DIR, exist_ok=True)  
    os.makedirs(TEMP_IMAGE_FOLDER, exist_ok=True)

    # Fetch all bucket names from the database
    local_buckets = Bucket.objects.values_list('name', flat=True)

    # Fetch all existing S3 buckets from AWS
    try:
        s3_buckets = s3.list_buckets()["Buckets"]
        s3_bucket_names = [bucket["Name"] for bucket in s3_buckets]
    except ClientError as e:
        messages.error(request, f"Error fetching S3 buckets: {e}")
        s3_bucket_names = []

    # Filter only matching buckets
    matching_buckets = list(set(local_buckets) & set(s3_bucket_names))
    #print("✅ S3 Buckets:", matching_buckets)
   
    if request.method == "POST":
        if request.POST.get("type") == "classic":
            # Retrieve form data
            if request.POST.get("source_type") == "s3":
                selected_type = request.POST.get("type")  # classic, tunnel, right-to-left, left-to-right
                selected_background_bucket = request.POST.get("bg_bucket1")
                selected_foreground_bucket = request.POST.get("fg_bucket1")
                audio_file = request.FILES.get("audio_file")
                background_brightness = request.POST.get("background_brightness")
                background_saturation = request.POST.get("background_saturation")
                background_opacity = request.POST.get("background_opacity")
                background_transition = request.POST.get("background_transition")
                foreground_brightness = request.POST.get("foreground_brightness")
                foreground_saturation = request.POST.get("foreground_saturation")
                foreground_opacity = request.POST.get("foreground_opacity")
                foreground_transition = request.POST.get("foreground_transition")
                bg_bucket2 = request.POST.get("bg_bucket2")
                bg_bucket3 = request.POST.get("bg_bucket3")
                bg_bucket4 = request.POST.get("bg_bucket4")

                fg_bucket2 = request.POST.get("fg_bucket2")
                fg_bucket3 = request.POST.get("fg_bucket3")
                fg_bucket4 = request.POST.get("fg_bucket4")
                
                base_url = request.POST.get("base_url", "").rstrip("/")
                url_slug = request.POST.get("url_slug", "").lstrip("/")
                linkto = request.POST.get("linkto", "").lstrip("/")
                
                # 🔁 Validate slug manually (don't auto-generate)
                if url_slug and Composition.objects.filter(slug=url_slug).exists():
                    messages.error(request, f"Error: The slug '{url_slug}' already exists. Please choose a different one.")
                    return redirect("composition-add")
                
                # ✅ Construct full URL and slug
                full_url = f"{base_url}/{url_slug}" if base_url and url_slug else None
                slug = url_slug or ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
                
                downloaded_background_files = []

                # ✅ Download Background Images
                if selected_background_bucket:
                    downloaded_background_files = download_s3_files(selected_background_bucket, TEMP_BG_FOLDER)
                    print(f"✅ Downloaded Background Files: {downloaded_background_files}")

                # ✅ Save Audio File
                audio_path = None
                if audio_file:
                    audio_path = os.path.join(AUDIO_DIR, audio_file.name)
                    with open(audio_path, "wb") as f:
                        f.write(audio_file.read())
                
                def generate_auto_name():
                    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
                    random_str = ''.join(random.choices(string.ascii_letters + string.digits, k=6))
                    return f"composition_{timestamp}_{random_str}"

                auto_name = generate_auto_name()
                output_path = f"{VIDEO_DIR}/{auto_name}.mp4"
                video_filename = f"{auto_name}.mp4"
                audio_filename = f"{audio_file}"
                
                # ✅ Generate Thumbnail from first background image (safe)
                thumbnail_path = os.path.join(THUMBNAIL_DIR, f"thumbnail_{auto_name}.jpg")
                first_bg_image = downloaded_background_files[0] if downloaded_background_files else None

                if first_bg_image:
                    try:
                        os.makedirs(os.path.dirname(thumbnail_path), exist_ok=True)
                        ext = os.path.splitext(first_bg_image)[1].lower()
                        from PIL import Image

                        if ext in [".jpg", ".jpeg", ".png", ".webp"]:
                            img = Image.open(first_bg_image).convert("RGB")
                            img.save(thumbnail_path, "JPEG")
                            print(f"✅ Thumbnail (image) saved at: {thumbnail_path}")

                        elif ext == ".gif":
                            img = Image.open(first_bg_image)
                            img.seek(0)  # Use the first frame of the GIF
                            img.convert("RGB").save(thumbnail_path, "JPEG")
                            print(f"✅ Thumbnail (GIF) saved at: {thumbnail_path}")

                        elif ext == ".mp4":
                            with VideoFileClip(first_bg_image) as clip:
                                frame = clip.get_frame(0)  # Get the first frame
                                img = Image.fromarray(frame).convert("RGB")
                                img.save(thumbnail_path, "JPEG")
                                print(f"✅ Thumbnail (video) saved at: {thumbnail_path}")

                        else:
                            print(f"❌ Unsupported format for thumbnail: {ext}")
                            thumbnail_path = None

                    except Exception as e:
                        print(f"❌ Error saving thumbnail: {e}")
                        thumbnail_path = None
                try:
                    
                    # ✅ Upload Audio File (if exists)
                    s3_audio_url = None
                    if audio_path and os.path.exists(audio_path):
                        audio_key = f"{os.path.basename(audio_path)}"
                        print("audio key files:",audio_key)
                        with open(audio_path, "rb") as audio_file_obj:
                            s3.upload_fileobj(audio_file_obj, BUCKET_NAME, audio_key)
                        s3_audio_url = f"https://{BUCKET_NAME}.s3.amazonaws.com/{audio_key}"
                        print(f"✅ S3 Audio Upload Successful: {s3_audio_url}")

                except (BotoCoreError, ClientError) as e:
                    print(f"❌ S3 Upload Failed: {e}")
                    messages.error(request, f"Error uploading media to S3: {e}")
                    return redirect("composition-add")
                
                comps = Composition.objects.create(
                    name=auto_name,
                    type=selected_type,
                    background_video="null",
                    foreground_video="null",
                    audio_file=audio_path,
                    background_brightness=background_brightness,
                    background_saturation=background_saturation,
                    background_opacity=background_opacity,
                    background_transition=background_transition,
                    foreground_brightness=foreground_brightness,
                    foreground_opacity=foreground_opacity,
                    foreground_saturation=foreground_saturation,
                    foreground_transition=foreground_transition,
                    img=thumbnail_path,  # you may want to upload this too
                    url=full_url,
                    page_url=linkto,
                    slug=slug,  # 🔥 Added
                    bg_bucket1=selected_background_bucket,
                    bg_bucket2=bg_bucket2,
                    bg_bucket3=bg_bucket3,
                    bg_bucket4=bg_bucket4,

                    fg_bucket1=selected_foreground_bucket,
                    fg_bucket2=fg_bucket2,
                    fg_bucket3=fg_bucket3,
                    fg_bucket4=fg_bucket4,
                    status="uncompleted"
                )

                comID = comps.id  # ✅ Ensure `comID` is an integer
             
                params_dict = {
                    "selected_type": str(selected_type),
                    "selected_background_bucket": str(selected_background_bucket),
                    "selected_foreground_bucket": str(selected_foreground_bucket),
                    "audio_file_path": str(audio_path),
                    "ids": int(comID)  # Ensure it's an integer
                }
                
                

                    # ✅ Correct way to pass the dictionary
                    #classic_task.delay(**params_dict)
            
                #classic_task.delay(selected_background_bucket, selected_foreground_bucket, saved_path);

                messages.success(request, "🎉 Composition added successfully!")
                return redirect("composition-view")
            
            #Uploads
            else:
               selected_type = request.POST.get("upload")
            # Retrieve files from request
            stype = request.POST.get("type")
            background_video = request.FILES.get("background_video")
            foreground_video = request.FILES.get("foreground_video")
            audio_file = request.FILES.get("audio_file")
            background_brightness = request.POST.get("background_brightness")
            background_saturation = request.POST.get("background_saturation")
            background_opacity = request.POST.get("background_opacity")
            background_transition = request.POST.get("background_transition")
            foreground_brightness = request.POST.get("foreground_brightness")
            foreground_saturation = request.POST.get("foreground_saturation")
            foreground_opacity = request.POST.get("foreground_opacity")
            foreground_transition = request.POST.get("foreground_transition")
            base_url = request.POST.get("base_url", "").rstrip("/")
            url_slug = request.POST.get("url_slug", "").lstrip("/")
            linkto = request.POST.get("linkto", "").lstrip("/")

            # 🔁 Validate slug manually (don't auto-generate)
            if url_slug and Composition.objects.filter(slug=url_slug).exists():
                messages.error(request, f"Error: The slug '{url_slug}' already exists. Please choose a different one.")
                return redirect("composition-add")

            # ✅ Construct full URL and slug
            full_url = f"{base_url}/{url_slug}" if base_url and url_slug else None
            slug = url_slug or ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))

            # Debugging print
            print(f"Background Video: {background_video}, Foreground Video: {foreground_video}, Audio : {audio_file}")

            # Validate required files
            if not background_video or not foreground_video:
                messages.error(request, "Error: Missing background or foreground video.")
                return redirect("composition-add")

            if not audio_file:
                messages.error(request, "Error: No audio file uploaded.")
                return redirect("composition-add")

            # Generate unique filename
            def generate_auto_name():
                timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
                random_str = ''.join(random.choices(string.ascii_letters + string.digits, k=6))
                return f"composition_{timestamp}_{random_str}"

            auto_name = generate_auto_name()

            # Save files to disk
            background_path = os.path.join(VIDEO_DIR, background_video.name)
            #print("Video Bc" , background_path)
            foreground_path = os.path.join(VIDEO_DIR, foreground_video.name)
            #print("Video Fc" , foreground_path)
            audio_path = os.path.join(AUDIO_DIR, audio_file.name)

            with open(background_path, "wb") as f:
                f.write(background_video.read())

            with open(foreground_path, "wb") as f:
                f.write(foreground_video.read())

            with open(audio_path, "wb") as f:
                f.write(audio_file.read())

            # Define Output Path
            output_path = f"{VIDEO_DIR}/{auto_name}.mp4"
            video_filename = f"{auto_name}.mp4"
            audio_filename = f"{audio_file}"

            # ✅ Pass file paths (strings) instead of TemporaryUploadedFile objects
            combine_video_with_audio(background_path, foreground_path, audio_path, output_path)

            # Generate Thumbnail
            thumbnail_path = os.path.join(THUMBNAIL_DIR, f"thumbnail_{auto_name}.jpg")
            generate_video_thumbnail(output_path, thumbnail_path)

            if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
                    messages.error(request, f"Error: Final video not found or empty at {output_path}")
                    return redirect("composition-add")

            s3_key = f"{os.path.basename(output_path)}"

            try:
                    with open(output_path, "rb") as video_file:
                        s3.upload_fileobj(video_file, BUCKET_NAME, s3_key)

                    # ✅ Construct Public S3 URL
                    s3_video_url = f"https://{BUCKET_NAME}.s3.amazonaws.com/{s3_key}"
                    print(f"✅ S3 Upload Successful: {s3_video_url}")

            except (BotoCoreError, ClientError) as e:
                    print(f"❌ S3 Upload Failed: {e}")
                    messages.error(request, f"Error uploading video to S3: {e}")
                    return redirect("composition-add")

            # Save Composition in Database
            Composition.objects.create(
                name=auto_name,
                type=stype,
                background_video="null",
                foreground_video="null",
                audio_file=audio_path,
                final_video=video_filename,
                img=thumbnail_path,
                background_brightness=background_brightness,
                background_saturation=background_saturation,
                background_opacity=background_opacity,
                background_transition=background_transition,
                foreground_brightness=foreground_brightness,
                foreground_opacity=foreground_opacity,
                foreground_saturation=foreground_saturation,
                foreground_transition=foreground_transition,
                url=full_url,
                page_url=linkto,
                slug=slug,  # 🔥 Added
                status="Completed"
            )

            messages.success(request, "🎉 Composition added successfully!")
            return redirect("composition-view")

                
            
        #Tunnel  Buckets
        elif request.POST.get("type") == "tunnel":
            
            selected_background_buckets = request.POST.getlist("bg_bucket1")
            background_brightness = request.POST.get("background_brightness")
            background_saturation = request.POST.get("background_saturation")
            background_opacity = request.POST.get("background_opacity")
            background_transition = request.POST.get("background_transition")
            audio_file = request.FILES.get("audio_file")
            bg_bucket1 = request.POST.get("bg_bucket1")
            bg_bucket2 = request.POST.get("bg_bucket2")
            bg_bucket3 = request.POST.get("bg_bucket3")
            bg_bucket4 = request.POST.get("bg_bucket4")

            base_url = request.POST.get("base_url", "").rstrip("/")
            url_slug = request.POST.get("url_slug", "").lstrip("/")
            linkto = request.POST.get("linkto", "").lstrip("/")

            # 🔁 Validate slug manually (don't auto-generate)
            if url_slug and Composition.objects.filter(slug=url_slug).exists():
                messages.error(request, f"Error: The slug '{url_slug}' already exists. Please choose a different one.")
                return redirect("composition-add")

            # ✅ Construct full URL and slug
            full_url = f"{base_url}/{url_slug}" if base_url and url_slug else None
            slug = url_slug or ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
            
            downloaded_background_files = []

            # ✅ Download Background Images
            if selected_background_buckets:
                for bucket_name in selected_background_buckets:
                    if bucket_name:
                        files = download_s3_files(bucket_name, TEMP_BG_FOLDER)
                        downloaded_background_files.extend(files)
                print(f"✅ Downloaded Background Files: {downloaded_background_files}")

           
            if not selected_background_buckets:
                print("❌ No S3 bucket selected.")
                return

            
            # ✅ Generate unique name for video
            def generate_auto_name():
                    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")  # Get current timestamp
                    random_str = ''.join(random.choices(string.ascii_letters + string.digits, k=6))  # Random 6-character string
                    return f"composition_{timestamp}_{random_str}"

            auto_name = generate_auto_name()
            
            
            # ✅ Create Video from Merged Images
            output_path = os.path.join(VIDEO_DIR, f"{auto_name}.mp4")
            video_filename = f"{auto_name}.mp4"
            
            # ✅ Save Audio File
            audio_path = None
            if audio_file:
                audio_path = os.path.join(AUDIO_DIR, audio_file.name)
                with open(audio_path, "wb") as f:
                    f.write(audio_file.read())
                    
            # ✅ Generate Thumbnail from first background image (safe)
            thumbnail_path = os.path.join(THUMBNAIL_DIR, f"thumbnail_{auto_name}.jpg")
            first_bg_image = downloaded_background_files[0] if downloaded_background_files else None

            if first_bg_image:
                try:
                    os.makedirs(os.path.dirname(thumbnail_path), exist_ok=True)
                    ext = os.path.splitext(first_bg_image)[1].lower()
                    from PIL import Image

                    if ext in [".jpg", ".jpeg", ".png", ".webp"]:
                        img = Image.open(first_bg_image).convert("RGB")
                        img.save(thumbnail_path, "JPEG")
                        print(f"✅ Thumbnail (image) saved at: {thumbnail_path}")

                    elif ext == ".gif":
                        img = Image.open(first_bg_image)
                        img.seek(0)  # Use the first frame of the GIF
                        img.convert("RGB").save(thumbnail_path, "JPEG")
                        print(f"✅ Thumbnail (GIF) saved at: {thumbnail_path}")

                    elif ext == ".mp4":
                        with VideoFileClip(first_bg_image) as clip:
                            frame = clip.get_frame(0)  # Get the first frame
                            img = Image.fromarray(frame).convert("RGB")
                            img.save(thumbnail_path, "JPEG")
                            print(f"✅ Thumbnail (video) saved at: {thumbnail_path}")

                    else:
                        print(f"❌ Unsupported format for thumbnail: {ext}")
                        thumbnail_path = None

                except Exception as e:
                    print(f"❌ Error saving thumbnail: {e}")
                    thumbnail_path = None
            
            # ✅ Save Audio File Securely
            saved_path = None
            if audio_file:
                # Generate a secure, unique filename
                filename = get_random_string(12) + os.path.splitext(audio_file.name)[1]
                file_path = os.path.join("uploads", filename)  # Relative path inside MEDIA_ROOT
                
                # Save the file securely
                saved_path = default_storage.save(file_path, ContentFile(audio_file.read()))
           
            # ✅ Save Composition in Database
            comps = Composition.objects.create(
                        name=auto_name,
                        type="Tunnel",
                        background_video="null",
                        foreground_video="null",
                        background_brightness=background_brightness,
                        background_saturation=background_saturation,
                        background_opacity=background_opacity,
                        background_transition=background_transition,
                        img=thumbnail_path,
                        url=full_url,
                        audio_file=audio_path,
                        slug=slug,  # 🔥 Added
                        bg_bucket1=bg_bucket1,
                        bg_bucket2=bg_bucket2,
                        bg_bucket3=bg_bucket3,
                        bg_bucket4=bg_bucket4,
                        status="uncompleted",
                        page_url=linkto,
                    )

            comID = comps.id  # ✅ Ensure `comID` is an integer
            params_tunnel = {
                "selected_background_buckets": selected_background_buckets, 
                "audio_file_path": str(saved_path),
                "ids": int(comID)
            }
            # ✅ Correct way to pass the dictionary
            #tunnel_task.delay(params_tunnel)
            
            print("🎉 Tunnel composition added successfully!")
            return redirect("composition-view")
            
            # LEFT  TO RIGHT
        elif request.POST.get("type") == "left-to-right":
            bg_bucket = request.POST.get("bg_bucket1")
            fg_bucket = request.POST.get("fg_bucket1")
            audio_file = request.FILES.get("audio_file")
            background_brightness = request.POST.get("background_brightness")
            background_saturation = request.POST.get("background_saturation")
            background_opacity = request.POST.get("background_opacity")
            background_transition = request.POST.get("background_transition")
            foreground_brightness = request.POST.get("foreground_brightness")
            foreground_saturation = request.POST.get("foreground_saturation")
            foreground_opacity = request.POST.get("foreground_opacity")
            foreground_transition = request.POST.get("foreground_transition")
            bg_bucket2 = request.POST.get("bg_bucket2")
            bg_bucket3 = request.POST.get("bg_bucket3")
            bg_bucket4 = request.POST.get("bg_bucket4")

            fg_bucket2 = request.POST.get("fg_bucket2")
            fg_bucket3 = request.POST.get("fg_bucket3")
            fg_bucket4 = request.POST.get("fg_bucket4")
            base_url = request.POST.get("base_url", "").rstrip("/")
            url_slug = request.POST.get("url_slug", "").lstrip("/")
            linkto = request.POST.get("linkto", "").lstrip("/")
 

            # 🔁 Validate slug manually (don't auto-generate)
            if url_slug and Composition.objects.filter(slug=url_slug).exists():
                messages.error(request, f"Error: The slug '{url_slug}' already exists. Please choose a different one.")
                return redirect("composition-add")

            # ✅ Construct full URL and slug
            full_url = f"{base_url}/{url_slug}" if base_url and url_slug else None
            slug = url_slug or ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
            
            downloaded_background_files = []
            downloaded_foreground_files = []

            # ✅ Download Background Images
            if bg_bucket:
                downloaded_background_files = download_s3_files(bg_bucket, TEMP_BG_FOLDER)
                print(f"✅ Downloaded Background Files: {downloaded_background_files}")

            
            if not bg_bucket or not fg_bucket:
                messages.error(request, "❌ Please select background and foreground sources.")
                return redirect("composition-add")

            # Generate unique name
            def generate_auto_name():
                timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
                random_str = ''.join(random.choices(string.ascii_letters + string.digits, k=6))
                return f"composition_{timestamp}_{random_str}"

            auto_name = generate_auto_name()
            output_path = os.path.join(VIDEO_DIR, f"{auto_name}.mp4")
            audio_filename = f"{audio_file}"
           
            # ✅ Generate Thumbnail from first background image (safe)
            thumbnail_path = os.path.join(THUMBNAIL_DIR, f"thumbnail_{auto_name}.jpg")
            first_bg_image = downloaded_background_files[0] if downloaded_background_files else None

            if first_bg_image:
                try:
                    os.makedirs(os.path.dirname(thumbnail_path), exist_ok=True)
                    ext = os.path.splitext(first_bg_image)[1].lower()
                    from PIL import Image

                    if ext in [".jpg", ".jpeg", ".png", ".webp"]:
                        img = Image.open(first_bg_image).convert("RGB")
                        img.save(thumbnail_path, "JPEG")
                        print(f"✅ Thumbnail (image) saved at: {thumbnail_path}")

                    elif ext == ".gif":
                        img = Image.open(first_bg_image)
                        img.seek(0)  # Use the first frame of the GIF
                        img.convert("RGB").save(thumbnail_path, "JPEG")
                        print(f"✅ Thumbnail (GIF) saved at: {thumbnail_path}")

                    elif ext == ".mp4":
                        with VideoFileClip(first_bg_image) as clip:
                            frame = clip.get_frame(0)  # Get the first frame
                            img = Image.fromarray(frame).convert("RGB")
                            img.save(thumbnail_path, "JPEG")
                            print(f"✅ Thumbnail (video) saved at: {thumbnail_path}")

                    else:
                        print(f"❌ Unsupported format for thumbnail: {ext}")
                        thumbnail_path = None

                except Exception as e:
                    print(f"❌ Error saving thumbnail: {e}")
                    thumbnail_path = None
                    
            # ✅ Save Audio File
            audio_path = None
            if audio_file:
                audio_path = os.path.join(AUDIO_DIR, audio_file.name)
                with open(audio_path, "wb") as f:
                    f.write(audio_file.read())
                    
            # Create DB entry (video paths will be updated later)
            comps = Composition.objects.create(
                name=auto_name,
                type="left-to-right",
                
                background_video="null",
                foreground_video="null",
                background_brightness=background_brightness,
                background_saturation=background_saturation,
                background_opacity=background_opacity,
                background_transition=background_transition,
                foreground_brightness=foreground_brightness,
                foreground_opacity=foreground_opacity,
                foreground_saturation=foreground_saturation,
                foreground_transition=foreground_transition,
                img=thumbnail_path,
                url=full_url,
                page_url=linkto,
                audio_file=audio_path,
                slug=slug,  # 🔥 Added
                bg_bucket1=bg_bucket,
                bg_bucket2=bg_bucket2,
                bg_bucket3=bg_bucket3,
                bg_bucket4=bg_bucket4, 

                fg_bucket1=fg_bucket,
                fg_bucket2=fg_bucket2,
                fg_bucket3=fg_bucket3,
                fg_bucket4=fg_bucket4,
                status="uncompleted"
            )

            params_left = {
                "background_bucket": bg_bucket,
                "foreground_bucket": fg_bucket,
                "audio_file_path": str(audio_path),
                "ids": comps.id
            }

            # Call Celery Task
            #left_to_right_task.delay(params_left)

            messages.success(request, "🎉 Left to Right Composition added successfully!")
            return redirect("composition-view")
            
        else:
            print("test")
            #return False
          
    return render(request, "admin/composition.html", {
        "buckets": matching_buckets
    })
def download_s3_files(bucket_name, download_folder):
    
    downloaded_files = []
    try:
        response = s3.list_objects_v2(Bucket=bucket_name)
        if "Contents" in response:
            for obj in response["Contents"]:
                file_key = obj["Key"]
                file_path = os.path.join(download_folder, os.path.basename(file_key))
                s3.download_file(bucket_name, file_key, file_path)
                downloaded_files.append(file_path)
    except ClientError as e:
        messages.error(f"Error fetching files from S3 bucket '{bucket_name}': {e}")
    return downloaded_files 
    

def convert_images_to_video(images, output_path, duration_per_image=2, target_size=(1280, 720)):
    """Convert a list of images into a video, ensuring all images are the same size."""
    if not images:
        return None  # No images, return None

    resized_images = []

    for img_path in images:
        img = Image.open(img_path)
        img = img.resize(target_size, Image.LANCZOS)  # ✅ Resize image
        temp_path = img_path.replace(".jpg", "_resized.jpg").replace(".png", "_resized.png")
        img.save(temp_path)
        resized_images.append(temp_path)

    if len(resized_images) == 1:
        # ✅ If only one image, use ImageClip instead of ImageSequenceClip
        video = mp.ImageClip(resized_images[0], duration=duration_per_image)
    else:
        # ✅ Use ImageSequenceClip for multiple images
        video = mp.ImageSequenceClip(resized_images, fps=1 / duration_per_image)

    # ✅ Write video file with correct format
    video.write_videofile(output_path, fps=24, codec="libx264")

    return output_path


def validate_images(image_list):
    """Check if images exist and are valid."""
    valid_images = []
    for img in image_list:
        if not os.path.exists(img):
            print(f"❌ Error: File {img} not found")
        else:
            try:
                with Image.open(img) as img_obj:
                    img_obj.verify()  # Verify the image
                    valid_images.append(img)  # Add to valid list
                    print(f"✅ Valid image: {img}")
            except Exception as e:
                print(f"❌ Invalid image: {img}, Error: {e}")

    return valid_images  # ✅ Return only valid images



def delete_temp_files(folder_path):
    """Delete all files in a given folder."""
    for file in os.listdir(folder_path):
        file_path = os.path.join(folder_path, file)
        try:
            if os.path.isfile(file_path):
                os.remove(file_path)
        except Exception as e:
            print(f"Error deleting file {file_path}: {e}")



def generate_video_thumbnail(video_path, thumbnail_path, time="00:00:01"):
    try:
        (
            ffmpeg
            .input(video_path, ss=time)
            .output(thumbnail_path, vframes=1)
            .run(capture_stdout=True, capture_stderr=True)
        )
        print(f"✅ Thumbnail created at: {thumbnail_path}")
    except ffmpeg.Error as e:
        print(f"❌ Error generating thumbnail: {e.stderr.decode()}")



def upload_to_s3(file_path, s3_key, bucket_name="your-s3-bucket-name"):
    """Uploads a file to S3 under the specified key."""
    try:
        if not os.path.exists(file_path):
            print(f"❌ Error: File not found - {file_path}")
            return False

        print(f"✅ Uploading {file_path} to S3: s3://{bucket_name}/{s3_key}")

        s3.upload_file(file_path, bucket_name, s3_key)

        # ✅ Generate public URL
        file_url = f"https://{bucket_name}.s3.amazonaws.com/{s3_key}"
        print(f"✅ File uploaded successfully: {file_url}")
        return file_url

    except NoCredentialsError:
        print("❌ AWS credentials not found!")
        return None
    except Exception as e:
        print(f"❌ Error uploading to S3: {e}")
        return None




@staff_member_required
def composition_view(request):
    compositions_list = Composition.objects.all().order_by("-id")
    paginator = Paginator(compositions_list, 10)
    page_number = request.GET.get("page")
    compositions = paginator.get_page(page_number)
    
    
    buckets = s3.list_buckets()["Buckets"]
   
    return render(request, "admin/composition-view.html", {"compositions": compositions})


def user_logout(request):
    logout(request)
    messages.success(request, "You have been logged out successfully.")
    return redirect('admin-login')  # Replace 'login' with the name of your login page URL


# ✅ Initialize S3 client
s3_client = boto3.client('s3')
BUCKET_NAME = 'composition-final'  # Replace with your actual bucket name

def delete_compositions(request):
    """Ensures AWS S3 videos are deleted first before removing records from the Composition table."""

    if request.method == 'POST':
        try:
            # ✅ Get selected composition IDs from request
            ids_to_delete = json.loads(request.POST.get('compositions_to_delete', '[]'))
            print(f"🗑️ Selected IDs for deletion: {ids_to_delete}")  # Debugging log
            
            if not ids_to_delete:
                messages.error(request, "No compositions selected for deletion.")
                return redirect('composition-view')

            # ✅ Retrieve compositions from the database
            compositions = Composition.objects.filter(id__in=ids_to_delete)
            print(f"✅ Found {compositions.count()} compositions in the database.")  # Debugging log

            if not compositions.exists():
                messages.error(request, "No matching compositions found.")
                return redirect('composition-view')

            # ✅ Step 1: Delete videos from S3 FIRST
            successfully_deleted_s3_keys = []  # Track successfully deleted files
            failed_s3_keys = []  # Track failed deletions

            for comp in compositions:
                if comp.final_video:  # Check if the field is not empty
                    s3_key = str(comp.final_video.name).strip().lstrip("/")  # Convert FieldFile to string

                    try:
                        # ✅ Ensure the file exists in S3 before deleting
                        response = s3_client.head_object(Bucket=BUCKET_NAME, Key=s3_key)
                        if response['ResponseMetadata']['HTTPStatusCode'] == 200:
                            print(f"✅ File found in S3: {s3_key}, proceeding to delete...")

                            # ✅ Delete the file from S3
                            delete_response = s3_client.delete_object(Bucket=BUCKET_NAME, Key=s3_key)

                            # ✅ Confirm deletion
                            if delete_response.get("ResponseMetadata", {}).get("HTTPStatusCode") in [204, 200]:
                                successfully_deleted_s3_keys.append(s3_key)
                                print(f"✅ Successfully deleted from S3: {s3_key}")
                            else:
                                failed_s3_keys.append(s3_key)
                                print(f"⚠️ Warning: S3 did not confirm deletion: {s3_key}")

                    except s3_client.exceptions.ClientError as e:
                        error_code = e.response['Error']['Code']
                        if error_code == "404":
                            print(f"⚠️ File not found in S3: {s3_key}, skipping S3 deletion.")
                            successfully_deleted_s3_keys.append(s3_key)  # Allow deletion if file is already missing
                        else:
                            print(f"❌ S3 Error: {e}")
                            messages.error(request, f"S3 deletion failed: {e}")
                            return redirect('composition-view')  # **Stop execution if S3 deletion fails unexpectedly**

            # ✅ Step 2: If all S3 deletions were successful, delete from DB
            if set(successfully_deleted_s3_keys) == set([str(comp.final_video.name).strip().lstrip("/") for comp in compositions if comp.final_video]):
                with transaction.atomic():
                    deleted_count = compositions.delete()[0]  # Returns the number of deleted records
                    print(f"✅ Deleted {deleted_count} compositions from DB")

                messages.success(request, f"✅ {deleted_count} seletcted compositions deleted successfully!")
            else:
                messages.warning(request, f"⚠️ Some videos could not be deleted from S3: {failed_s3_keys}")

        except Exception as e:
            print(f"❌ Error: {e}")
            messages.error(request, f"An error occurred: {e}")

    return redirect('composition-view') 
    
def composition_detail(request, slug):
    composition = get_object_or_404(Composition, slug=slug)
    return render(request, 'admin/composition_detail.html', {'composition': composition})


@csrf_exempt
def generate_video(request, comp_id):
    if request.method == 'POST':
        try:
            comp = Composition.objects.get(id=comp_id)
            comp.status = "Processing"
            comp.save()
            data = json.loads(request.body)
            selected_type = data.get("selected_type", "").lower().replace("-", "_")
            ids = int(data.get("id"))
            background = data.get("background")
            foreground = data.get("foreground")
            audio_path = data.get("audio_path")
            print("🔥 TYPE RECEIVED:", foreground)

            if selected_type == "classic":
                classic_task.delay(
                    ids=ids,
                    selected_type=selected_type,
                    selected_background_bucket=background,
                    selected_foreground_bucket=foreground,
                    audio_file_path=audio_path
                )

            elif selected_type == "tunnel":
                tunnel_task.delay({
                    "ids": ids,
                    "selected_background_buckets": background if isinstance(background, list) else [background],
                    "audio_file_path": audio_path
                })

            elif selected_type == "left_to_right":
                left_to_right_task.delay({
                    "ids": ids,
                    "background_bucket": background,
                    "foreground_bucket": foreground,
                    "audio_file_path": audio_path
                })

            else:
                return JsonResponse({"success": False, "error": f"Invalid type: {selected_type}"})
            return JsonResponse({'success': comp.id})
        except Composition.DoesNotExist:
            return JsonResponse({'success': False, 'error': 'Composition not found'})
    return JsonResponse({'success': False, 'error': 'Invalid request'})