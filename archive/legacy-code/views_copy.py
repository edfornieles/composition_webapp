import os
import boto3
from django.shortcuts import render, redirect
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.models import User
from django.contrib.auth import login, authenticate
from django.core.paginator import Paginator
from django.core.files.base import ContentFile
from django.conf import settings
from django.core.files.storage import default_storage
from .models import Composition, Profile, S3Bucket, Bucket
from .video_processing import create_final_video
import ffmpeg
from django.core.files import File
from django.core.files.temp import NamedTemporaryFile
from PIL import Image
import io
from ajax_datatable.views import AjaxDatatableView
from .forms import BucketForm
from botocore.exceptions import ClientError
import uuid
import re
import json
from django.views.decorators.csrf import csrf_exempt


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
    """Create a new S3 bucket, save metadata locally, and upload metadata to S3"""
    if request.method == "POST":
        form = BucketForm(request.POST)
        if form.is_valid():
            bucket = form.save()  # Save locally
            
            # ✅ Format bucket name (must be unique, lowercase, no spaces)
            new_bucket_name = bucket.name.lower().replace(" ", "-")

            # ✅ Create the new S3 bucket (Check if region is us-east-1)
            try:
                if settings.AWS_S3_REGION_NAME == "us-east-1":
                    s3.create_bucket(Bucket=new_bucket_name)
                else:
                    s3.create_bucket(
                        Bucket=new_bucket_name,
                        CreateBucketConfiguration={"LocationConstraint": settings.AWS_S3_REGION_NAME}
                    )
                
                messages.success(request, f"S3 Bucket '{new_bucket_name}' created successfully!")
            except ClientError as e:
                error_code = e.response["Error"]["Code"]
                if error_code == "BucketAlreadyOwnedByYou":
                    messages.warning(request, f"Bucket '{new_bucket_name}' already exists.")
                elif error_code == "BucketAlreadyExists":
                    messages.error(request, f"Bucket name '{new_bucket_name}' is already taken by another user. Try a different name.")
                else:
                    messages.error(request, f"Error creating S3 bucket: {e}")
                return redirect("new-source")

             # ✅ Set bucket policy to allow public access
            bucket_policy = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "PublicReadAccess",
            "Effect": "Allow",
            "Principal": "*",
            "Action": "s3:GetObject",
            "Resource": f"arn:aws:s3:::{new_bucket_name}/*"
        }
    ]
}

            
            try:
                s3.put_bucket_policy(
                    Bucket=new_bucket_name,
                    Policy=str(bucket_policy).replace("'", '"')  # Ensure valid JSON format
                )
            except ClientError as e:
                messages.error(request, f"Error setting bucket policy: {e}")
                return redirect("list_buckets")


           

    else:
        form = BucketForm()

    return render(request, "admin/new-source.html", {"form": form})

### S3 Bucket Management

def list_buckets(request):
    """ List all S3 buckets """
    buckets = s3.list_buckets()["Buckets"]
    return render(request, 'admin/s3_buckets.html', {'buckets': buckets})

def bucket_contents(request, bucket_name):
    """ Get contents of a selected bucket """
    objects = s3.list_objects_v2(Bucket=bucket_name).get("Contents", [])
    return render(request, 'admin/bucket_contents.html', {'objects': objects, 'bucket_name': bucket_name})


@csrf_exempt
def upload_file(request, bucket_name):
    """ Upload file to a selected S3 bucket """
    if request.method == "POST":
        print("POST request received")  # ✅ Debugging
        print("FILES:", request.FILES)  # ✅ Debugging

        if not request.FILES.get("file"):
            print("⚠ No file found in request.FILES!")  # ✅ Debugging
            messages.error(request, "No file selected!")
            return redirect("bucket_contents", bucket_name=bucket_name)

        file = request.FILES["file"]
        print(f"✅ Uploading {file.name} to S3 bucket: {bucket_name}")  # ✅ Debugging

        try:
            file_data = file.read()
            s3.put_object(
                Bucket=bucket_name,
                Key=file.name,
                Body=file_data,
                ContentType=file.content_type,  # ✅ Ensure correct MIME type
            )
            messages.success(request, f"File '{file.name}' uploaded to bucket '{bucket_name}' successfully!")
            print(f"✅ File '{file.name}' uploaded successfully!")  # ✅ Debugging
        except ClientError as e:
            print(f"❌ Error uploading file: {e}")  # ✅ Debugging
            messages.error(request, f"Error uploading file: {e}")
            return redirect("bucket_contents", bucket_name=bucket_name)

        return redirect("bucket_contents", bucket_name=bucket_name)

    return render(request, "admin/upload.html", {"bucket_name": bucket_name})


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

@staff_member_required
def add_composition(request):
    if request.method == "POST":
        background_video = request.FILES["background_video"]
        foreground_video = request.FILES["foreground_video"]
        audio_file = request.FILES.get("audio_file")
        name = os.path.splitext(background_video.name)[0]
        comp_type = request.POST.get("background_transition")

        composition = Composition.objects.create(
            name=name,
            type=comp_type,
            background_video=background_video,
            foreground_video=foreground_video,
            audio_file=audio_file,
            brightness=int(request.POST["brightness"]),
            saturation=int(request.POST["saturation"]),
            opacity=int(request.POST["opacity"]),
            transition=comp_type,
        )

        output_path = f"media/videos/final_{composition.id}.mp4"
        create_final_video(
            composition.background_video.path,
            composition.foreground_video.path,
            composition.audio_file.path if composition.audio_file else None,
            output_path
        )

        thumbnail_path = f"media/composition_thumbnails/thumbnail_{composition.id}.jpg"
        generate_video_thumbnail(output_path, thumbnail_path)

        with open(thumbnail_path, "rb") as thumb_file:
            temp_thumb = NamedTemporaryFile(suffix=".jpg")
            temp_thumb.write(thumb_file.read())
            temp_thumb.flush()
            composition.img.save(f"thumbnail_{composition.id}.jpg", File(temp_thumb), save=True)
        
        temp_thumb.close()

        messages.success(request, "Composition added successfully!")
        return redirect("composition-view")

    return render(request, "admin/composition.html")

def generate_video_thumbnail(video_path, thumbnail_path, time="00:00:01"):
    try:
        (
            ffmpeg
            .input(video_path, ss=time)
            .output(thumbnail_path, vframes=1)
            .run(capture_stdout=True, capture_stderr=True)
        )
    except ffmpeg.Error as e:
        print(f"Error generating thumbnail: {e}")

@staff_member_required
def composition_view(request):
    compositions_list = Composition.objects.all().order_by("-id")
    paginator = Paginator(compositions_list, 10)
    page_number = request.GET.get("page")
    compositions = paginator.get_page(page_number)
    return render(request, "admin/composition-view.html", {"compositions": compositions})

@staff_member_required
def new_source(request):
    return render(request, "admin/new-source.html")

@staff_member_required
def source_library(request):
    return render(request, "admin/source-library.html")
