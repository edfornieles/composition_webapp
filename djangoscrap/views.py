import io, json, zipfile

from botocore.exceptions import BotoCoreError
from django.db.models import Q
from moviepy import ImageClip, CompositeVideoClip,AudioFileClip

from django.core.paginator import Paginator

from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.models import User
from django.contrib.auth import login, authenticate,logout
from django.db import transaction
from django.http import JsonResponse, HttpResponseRedirect, FileResponse, HttpResponse
from django.shortcuts import render, redirect,get_object_or_404
from django.views.decorators.csrf import csrf_exempt

from .celery_app import classic_task,left_to_right_task,tunnel_task
from .models import Composition, Profile, Source, VideoComposition, BackgroundImage, ForegroundImage
from .video_processing import combine_video_with_audio
from .forms import SourceForm
from .utils import *


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

@staff_member_required
def source_library(request):
    search_query = request.GET.get("search", "").strip()

    sources = Source.objects.all().order_by('last_scraped')
    if search_query:
        sources = sources.filter(
            Q(name__icontains=search_query) |
            Q(type__icontains=search_query) |
            Q(source_id__icontains=search_query)
        )
    s3_buckets = os.getenv("R2_BUCKETS_NAME").split(",")

    bucket_thumbnails = {}
    all_available_images = []  # For fallback

    for bucket_name in s3_buckets:
        try:
            response = s3.list_objects_v2(Bucket=bucket_name)
            contents = response.get("Contents", [])
            image_files = [obj for obj in contents if obj["Key"].lower().endswith((".png", ".jpg", ".jpeg"))]
            if image_files:
                # Save random image for this bucket
                image_file = random.choice(image_files)
                image_key = image_file["Key"]

                image_url = f"{os.getenv('R2_PUBLIC_URL')}/{image_key}"
                bucket_thumbnails[bucket_name] = image_url

                # Save all image URLs for fallback
                for img in image_files:
                    all_available_images.append(f"{os.getenv('R2_PUBLIC_URL')}/{img['Key']}")
        except ClientError as e:
            print(f"Error accessing bucket {bucket_name}: {e}")
            continue

    # Assign thumbnails
    for source in sources:
        if source.name in bucket_thumbnails:
            source.thumbnail = bucket_thumbnails[source.name]
        elif all_available_images:
            source.thumbnail = random.choice(all_available_images)
        else:
            source.thumbnail = None  # fallback to placeholder in template

    paginator = Paginator(sources, 10)  # Show 10 per page
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    return render(request, "admin/source-library.html", {"page_obj": page_obj})


def list_sources(request):
    """ List all S3 buckets with sample image """
    source_names = os.getenv("R2_BUCKETS_NAME", "").split(",")  # comma-separated list
    all_sources = []

    for source in source_names:
        image_url = get_sample_image_url(source)
        all_sources.append({
            "Name": source,
            # "CreationDate": source["CreationDate"],
            "image_url": image_url
        })

    return render(request, 'admin/sources.html', {'sources': all_sources})


def source_contents(request, source_name):
    """ Get contents of a selected bucket """
    objects = s3.list_objects_v2(Bucket=source_name).get("Contents", [])
    paginator = Paginator(objects, 25)  # Show 25 objects per page
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    return render(request, 'admin/source_contents.html', {
        'page_obj': page_obj,
        'source_name': source_name,
        'prefix_url': os.getenv('R2_PUBLIC_URL'),
    })


@staff_member_required
def create_or_edit_source(request, source_id=None):
    is_edit = source_id is not None
    source = get_object_or_404(Source, id=source_id) if is_edit else None

    form = SourceForm(request.POST or None, instance=source)

    if request.method == "POST":
        if form.is_valid():
            bucket_name_raw = form.cleaned_data["name"]
            new_bucket_name = bucket_name_raw.lower().replace(" ", "-")

            if not is_edit:
                # Check if bucket name exists in DB
                if Source.objects.filter(name__iexact=bucket_name_raw).exists():
                    form.add_error("name", "A bucket with this name already exists!")
                    return render(request, "admin/new-update-source.html", {
                        "form": form,
                        "is_edit": False,
                    })

                # Check if bucket exists in Cloudflare
                try:
                    s3.head_bucket(Bucket=new_bucket_name)
                    form.add_error("name", "A bucket with this name already exists on the storage provider.")
                    return render(request, "admin/new-update-source.html", {
                        "form": form,
                        "is_edit": False,
                    })
                except ClientError as e:
                    error_code = int(e.response['Error']['Code'])
                    if error_code == 404:
                        pass  # Bucket doesn't exist
                    elif error_code == 403:
                        form.add_error("name", "Bucket exists but is not accessible (likely owned by someone else).")
                        return render(request, "admin/new-update-source.html", {
                            "form": form,
                            "is_edit": False,
                        })
                    else:
                        messages.error(request, f"Unexpected error checking bucket: {e}")
                        return redirect("list_sources")

            # Save to DB
            bucket = form.save(commit=False)
            bucket.name = bucket_name_raw
            bucket.save()

            if not is_edit:
                # Create the bucket on Cloudflare
                try:
                    s3.create_bucket(Bucket=new_bucket_name)
                    messages.success(request, f"Bucket '{new_bucket_name}' created successfully.")
                except ClientError as e:
                    messages.error(request, f"Error creating bucket: {e}")
                    return redirect("list_sources")
            else:
                messages.success(request, f"Source '{bucket_name_raw}' updated successfully.")

            return redirect("list_sources")

    return render(request, "admin/new-update-source.html", {
        "form": form,
        "is_edit": is_edit,
        "source": source,
    })


@csrf_exempt
@staff_member_required
def delete_source(request):
    errors = []
    selected = request.POST.getlist("source") or [request.POST.get("source")]
    for source in selected:
        try:
            delete_bucket_objects(source)
            s3.delete_bucket(Bucket=source)
            Source.objects.filter(name=source).delete()
        except Exception as e:
            errors.append(f"{source}: {e}")
            messages.error(request, f"Error deleting {source}: {e}")

    if errors:
        messages.warning(request, "Some sources could not be deleted.")
    else:
        messages.success(request, "Selected sources deleted successfully.")

    return HttpResponseRedirect(request.META.get('HTTP_REFERER'))


@csrf_exempt
@staff_member_required
def download_source(request):
    selected = request.POST.getlist('source')
    zip_buffer = io.BytesIO()

    try:
        with zipfile.ZipFile(zip_buffer, "w") as zip_file:
            for source in selected:
                try:
                    objects = s3.list_objects_v2(Bucket=source)
                    contents = objects.get('Contents', [])

                    for obj in contents:
                        key = obj['Key']
                        file_obj = s3.get_object(Bucket=source, Key=key)
                        content = file_obj['Body'].read()
                        zip_path = f"{source}/{key}"
                        zip_file.writestr(zip_path, content)

                except Exception as e:
                    messages.error(request, f"Failed to download from {source}: {e}")

        # Finalize the ZIP file and seek to the start of the buffer
        zip_buffer.seek(0)

        # Ensure the buffer is actually a valid zip file
        try:
            with zipfile.ZipFile(zip_buffer, "r") as check_zip:
                print(f"ZIP contains: {check_zip.namelist()}")
        except zipfile.BadZipFile:
            print("The generated file is not a valid ZIP file")
            raise

    except Exception as e:
        print(f"Error creating ZIP: {e}")
        return HttpResponse(status=500)  # Return 500 if ZIP creation failed

    # Return the zip buffer as a downloadable file
    zip_buffer.seek(0)  # Ensure we're back at the start
    return FileResponse(zip_buffer, as_attachment=True, filename="buckets.zip")


@csrf_exempt
@staff_member_required
def upload_file(request, source_name):
    if request.method == 'POST':
        files = request.FILES.getlist('files')

        if len(files) > 50:
            messages.error(request, "You can upload a maximum of 50 files at once.")
            return redirect(request.path)

        for file in files:
            try:
                s3.upload_fileobj(
                    file,
                    source_name,
                    file.name,
                    ExtraArgs={'ContentType': file.content_type}
                )
                messages.success(request, f"Uploaded: {file.name}")
            except Exception as e:
                messages.error(request, f"Error uploading {file.name}: {e}")
                return redirect('source_contents', source_name=source_name)

        return redirect("sources")

    return render(request, 'admin/upload.html', {'source_name': source_name})


@csrf_exempt
@staff_member_required
def delete_file_from_source(request, source_name, file_name):
    try:
        s3.delete_object(Bucket=source_name, Key=file_name)
        messages.success(request, f"{file_name} deleted.")
    except Exception as e:
        messages.error(request, f"Failed to delete {file_name}: {e}")

    return redirect('source_contents', source_name=source_name)

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
    local_buckets = Source.objects.values_list('name', flat=True)

    buckets = os.getenv("R2_BUCKETS_NAME").split(",")

    matching_buckets = list(set(local_buckets) & set(buckets))
   
    if request.method == "POST":
        ensure_directories()
        composition_type = request.POST.get("type")
        source_type = request.POST.get("source_type")

        base_url = request.POST.get("base_url", "").rstrip("/")
        url_slug = request.POST.get("url_slug", "").lstrip("/")
        link_to = request.POST.get("linkto", "").lstrip("/")
        slug = url_slug or generate_auto_name()
        full_url = f"{base_url}/{url_slug}" if base_url and url_slug else None

        if url_slug and Composition.objects.filter(slug=url_slug).exists():
            messages.error(request, f"Error: The slug '{url_slug}' already exists. Please choose a different one.")
            return redirect("composition-add")

        selected_background_bucket = request.POST.get("bg_bucket1")
        selected_foreground_bucket = request.POST.get("fg_bucket1")

        background_video = request.FILES.get("background_video")
        foreground_video = request.FILES.get("foreground_video")
        audio_file = request.FILES.get("audio_file")

        bg_bucket2 = request.POST.get("bg_bucket2")
        bg_bucket3 = request.POST.get("bg_bucket3")
        bg_bucket4 = request.POST.get("bg_bucket4")

        fg_bucket2 = request.POST.get("fg_bucket2")
        fg_bucket3 = request.POST.get("fg_bucket3")
        fg_bucket4 = request.POST.get("fg_bucket4")

        downloaded_background_files = []

        # ✅ Download Background Images
        if selected_background_bucket:
            downloaded_background_files = download_s3_files(selected_background_bucket, TEMP_BG_FOLDER)
            print(f"✅ Downloaded Background Files: {downloaded_background_files}")

        auto_name = generate_auto_name()

        # ✅ Generate Thumbnail from first background image (safe)
        thumbnail_path = generate_thumbnail(downloaded_background_files[0], os.path.join(THUMBNAIL_DIR, f"thumbnail_{auto_name}.jpg")) if downloaded_background_files else None
        audio_path = None
        if audio_file:
            audio_path = os.path.join(AUDIO_DIR, audio_file.name)
            if source_type == "upload":
                background_path = os.path.join(VIDEO_DIR, background_video.name)
                foreground_path = os.path.join(VIDEO_DIR, foreground_video.name)

                with open(background_path, "wb") as f:
                    f.write(background_video.read())

                with open(foreground_path, "wb") as f:
                    f.write(foreground_video.read())

                output_path = f"{VIDEO_DIR}/{auto_name}.mp4"
                combine_video_with_audio(background_path, foreground_path, audio_path, output_path)
            with open(audio_path, "wb") as f:
                f.write(audio_file.read())
            if os.path.exists(audio_path):
                try:
                    audio_key = f"{os.path.basename(audio_path)}"
                    print("audio key files:",audio_key)

                    with open(audio_path, "rb") as audio_file_obj:
                        s3.upload_fileobj(audio_file_obj, os.getenv("IDRIVE_BUCKET"))

                    s3_audio_url = f"{os.getenv('R2_PUBLIC_URL')}/{audio_key}"
                    print(f"✅ S3 Audio Upload Successful: {s3_audio_url}")

                except (BotoCoreError, ClientError) as e:
                    print(f"❌ S3 Upload Failed: {e}")
                    messages.error(request, f"Error uploading media to S3: {e}")
                    return redirect("composition-add")

        comps = Composition.objects.create(
            name=auto_name,
            type=source_type if source_type else composition_type.title(),
            background_video="null",
            foreground_video="null",
            audio_file=audio_path,
            background_brightness=request.POST.get("background_brightness"),
            background_saturation=request.POST.get("background_saturation"),
            background_opacity=request.POST.get("background_opacity"),
            background_transition=request.POST.get("background_transition"),
            foreground_brightness=request.POST.get("foreground_brightness"),
            foreground_opacity=request.POST.get("foreground_opacity"),
            foreground_saturation=request.POST.get("foreground_saturation"),
            foreground_transition=request.POST.get("foreground_transition"),
            img=thumbnail_path,
            url=full_url,
            page_url=link_to,
            slug=slug,
            bg_bucket1=selected_background_bucket,
            bg_bucket2=bg_bucket2,
            bg_bucket3=bg_bucket3,
            bg_bucket4=bg_bucket4,

            fg_bucket1=selected_foreground_bucket,
            fg_bucket2=fg_bucket2,
            fg_bucket3=fg_bucket3,
            fg_bucket4=fg_bucket4,
            status="uncompleted" if source_type == "s3" else "completed"
        )

        comID = comps.id  # ✅ Ensure `comID` is an integer

        params_dict = {
            "selected_type": str(composition_type),
            "selected_background_bucket": str(selected_background_bucket),
            "selected_foreground_bucket": str(selected_foreground_bucket),
            "audio_file_path": str(audio_path),
            "ids": int(comID)  # Ensure it's an integer
        }
        #classic_task.delay(**params_dict)
        #classic_task.delay(selected_background_bucket, selected_foreground_bucket, saved_path);

        messages.success(request, "🎉 Composition added successfully!")
        return redirect("composition-view")

    return render(request, "admin/composition.html", {
        "buckets": matching_buckets
    })
    

@staff_member_required
def composition_view(request):
    compositions_list = Composition.objects.all().order_by("-id")
    paginator = Paginator(compositions_list, 10)
    page_number = request.GET.get("page")
    compositions = paginator.get_page(page_number)
   
    return render(request, "admin/composition-view.html", {"compositions": compositions})


def user_logout(request):
    logout(request)
    messages.success(request, "You have been logged out successfully.")
    return redirect('admin-login')  # Replace 'login' with the name of your login page URL


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
                        response = s3.head_object(Bucket=os.getenv("IDRIVE_BUCKET"), Key=s3_key)
                        if response['ResponseMetadata']['HTTPStatusCode'] == 200:
                            print(f"✅ File found in S3: {s3_key}, proceeding to delete...")

                            # ✅ Delete the file from S3
                            delete_response = s3.delete_object(Bucket=os.getenv("IDRIVE_BUCKET"), Key=s3_key)

                            # ✅ Confirm deletion
                            if delete_response.get("ResponseMetadata", {}).get("HTTPStatusCode") in [204, 200]:
                                successfully_deleted_s3_keys.append(s3_key)
                                print(f"✅ Successfully deleted from S3: {s3_key}")
                            else:
                                failed_s3_keys.append(s3_key)
                                print(f"⚠️ Warning: S3 did not confirm deletion: {s3_key}")

                    except s3.exceptions.ClientError as e:
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