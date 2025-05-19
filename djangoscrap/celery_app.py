from celery import Celery
import os
import boto3
import ffmpeg
import subprocess
from django.contrib import messages
from PIL import Image, UnidentifiedImageError
from botocore.exceptions import ClientError,BotoCoreError,NoCredentialsError
from django.db import transaction
from djangoscrap.video_processing import (
    combine_video_with_audio, 
    create_video_ffmpegNew
)
# 🔹 Set the Django settings module
from moviepy import ImageSequenceClip ,VideoFileClip, ImageClip, CompositeVideoClip,AudioFileClip, concatenate_videoclips
import random
import string
from datetime import datetime
import django
from celery import shared_task
 
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "djangoscrap.settings")
django.setup()

from djangoscrap.models import Composition


try:
    from PIL import ImageResampling  # For newer Pillow versi ons
    RESAMPLING_METHOD = ImageResampling.LANCZOS
except ImportError:
    RESAMPLING_METHOD = Image.LANCZOS  # Fallback for older versions
    
# Define paths for downloaded images & videos 
TEMP_BG_FOLDER = "media/temp_s3_back_files"
TEMP_FG_FOLDER = "media/temp_s3_fore_files"
VIDEO_DIR = "media/videos"
TEMP_IMAGE_FOLDER = "media/temp_images"
THUMBNAIL_DIR = "static/composition_thumbnails"
AUDIO_DIR = "media/audio"
MERGED_IMAGE_DIR = "media/merged_images"
UPLOAD_DIR = "media/uploads/"
 
# Ensure necessary directories exist
os.makedirs(TEMP_BG_FOLDER, exist_ok=True)
os.makedirs(TEMP_FG_FOLDER, exist_ok=True)
os.makedirs(VIDEO_DIR, exist_ok=True)
os.makedirs(THUMBNAIL_DIR, exist_ok=True)
os.makedirs(AUDIO_DIR, exist_ok=True)
os.makedirs(MERGED_IMAGE_DIR, exist_ok=True)
os.makedirs(TEMP_IMAGE_FOLDER, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)


# Initialize S3 Client
s3 = boto3.client('s3')
BUCKET_NAME = "composition-final"  # Replace with your actual bucket name
#S3_FOLDER = "classic-musical/"  # Default S3 folder for uploads


app = Celery(
    "tasks",
    broker="redis://localhost:6379/0",  # Redis URL
    backend="redis://localhost:6379/0",  # Store task results in Redis
)

def create_composite_images(image_dir, output_dir, mode="leftright"):
    os.makedirs(output_dir, exist_ok=True)
    images = sorted([
        os.path.join(image_dir, f)
        for f in os.listdir(image_dir)
        if f.lower().endswith(('.jpg', '.jpeg', '.png'))
    ])

    composite_paths = []

    for i in range(0, len(images) - 1, 2):  # Pairwise
        img1 = Image.open(images[i])
        img2 = Image.open(images[i + 1])

        # First, crop both to match 1920x1080 if needed (center-crop)
        def center_crop(img):
            w, h = img.size
            target_w, target_h = 1920, 1080
            left = (w - target_w) // 2 if w > target_w else 0
            top = (h - target_h) // 2 if h > target_h else 0
            right = left + min(target_w, w)
            bottom = top + min(target_h, h)
            return img.crop((left, top, right, bottom)).resize((1920, 1080))

        img1 = center_crop(img1)
        img2 = center_crop(img2)

        if mode == "leftright":
            left = img1.crop((0, 0, 960, 1080))
            right = img2.crop((960, 0, 1920, 1080))
            new_img = Image.new("RGB", (1920, 1080))
            new_img.paste(left, (0, 0))
            new_img.paste(right, (960, 0))
        else:
            top = img1.crop((0, 0, 1920, 540))
            bottom = img2.crop((0, 540, 1920, 1080))
            new_img = Image.new("RGB", (1920, 1080))
            new_img.paste(top, (0, 0))
            new_img.paste(bottom, (0, 540))

        out_path = os.path.join(output_dir, f"frame_{i // 2:04d}.jpg")
        new_img.save(out_path)
        composite_paths.append(out_path)

    return composite_paths

def create_video_ffmpeg(image_dir, output_path, audio_path=None, mode="leftright", fps=1, resolution=(1920, 1080)):
    composite_dir = os.path.join(image_dir, "composites_" + mode)
    create_composite_images(image_dir, composite_dir, mode=mode)

    image_files = sorted([
        f for f in os.listdir(composite_dir)
        if f.lower().endswith((".jpg", ".jpeg", ".png"))
    ])

    if not image_files:
        print("❌ No images found for video generation.")
        return None

    concat_file_path = os.path.abspath(os.path.join(composite_dir, "images.txt")).replace("\\", "/")
    with open(concat_file_path, "w") as f:
        for image in image_files:
            full_path = os.path.abspath(os.path.join(composite_dir, image)).replace("\\", "/")
            f.write(f"file '{full_path}'\n")
            f.write("duration 1\n")
        f.write(f"file '{full_path}'\n")  # Repeat last image for final frame hold

    command = [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", concat_file_path
    ]

    # Add audio if exists
    audio_path_clean = os.path.abspath(audio_path).replace("\\", "/") if audio_path else None
    if audio_path_clean and os.path.isfile(audio_path_clean):
        command += ["-i", audio_path_clean]
        command += ["-map", "0:v:0", "-map", "1:a:0"]
    else:
        print(f"⚠️ No valid audio file provided or found at: {audio_path_clean}")

    # Full-resolution, cropped filter to avoid padding
    scale_crop_filter = (
        f"scale=w={resolution[0]}:h={resolution[1]}:force_original_aspect_ratio=increase,"
        f"crop={resolution[0]}:{resolution[1]}"
    )
    command += ["-vf", scale_crop_filter]

    command += ["-c:v", "libx264", "-r", "30", "-pix_fmt", "yuv420p", "-shortest"]
    command.append(os.path.abspath(output_path).replace("\\", "/"))

    try:
        result = subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        print(f"✅ FFmpeg stdout:\n{result.stdout.decode()}")
        print(f"⚠️ FFmpeg stderr:\n{result.stderr.decode()}")
        return output_path
    except subprocess.CalledProcessError as e:
        print(f"❌ FFmpeg failed with code {e.returncode}")
        print(f"Error output:\n{e.stderr.decode()}")
        return None
    


@app.task
def long_running_task(data):
    import time
    #print(f"Processing: {data}")
    time.sleep(10)  # Simulating a long task
    #if __name__ == "__main__":
    left_folder = "left"
    right_folder = "right"
    output_folder = "merged"
    output_video = "output.mp4"

    merged_images = merge_images(left_folder, right_folder, output_folder)

    if merged_images:
        create_video_ffmpeg(output_folder, output_video, fps=1)
    else:
        print("❌ No merged images found to create video.")
    return f"Task completed with data: {data}"

def ensure_even_dimensions(img):
    """Ensure image dimensions are even for FFmpeg compatibility."""
    width, height = img.size
    new_width = width if width % 2 == 0 else width + 1
    new_height = height if height % 2 == 0 else height + 1

    if (width, height) != (new_width, new_height):
        img = img.resize((new_width, new_height), Image.ANTIALIAS)
    
    return img

def merge_images(background_files, foreground_files, output_folder):
    if not isinstance(output_folder, str):
        raise ValueError("❌ 'output_folder' must be a string, not a list or other type.")

    merged_images = []
    os.makedirs(output_folder, exist_ok=True)

    for i, (bg_path, fg_path) in enumerate(zip(background_files, foreground_files)):
        try:
            bg_image = Image.open(bg_path).convert("RGBA")
            fg_image = Image.open(fg_path).convert("RGBA")

            # Resize foreground to match background height
            fg_image = fg_image.resize((int(bg_image.width * 0.5), bg_image.height))

            # Merge left-to-right
            new_width = bg_image.width + fg_image.width
            merged_image = Image.new("RGBA", (new_width, bg_image.height))

            merged_image.paste(bg_image, (0, 0))
            merged_image.paste(fg_image, (bg_image.width, 0), fg_image)  # With alpha

            # ✅ Ensure even dimensions BEFORE saving
            merged_image = ensure_even_dimensions(merged_image)

            output_path = os.path.join(output_folder, f"merged_{i:03}.png")
            merged_image.save(output_path)
            merged_images.append(output_path)
            print(f"✅ Merged image saved: {output_path}")

        except Exception as e:
            print(f"❌ Error merging images: {e}")

    return merged_images
    

def create_final_videoNew(background_images, foreground_images, audio_path, output_path):
    clips = []

    # Ensure we have equal numbers of background and foreground images
    min_length = min(len(background_images), len(foreground_images))

    for i in range(min_length):
        bg_image = background_images[i]
        fg_image = foreground_images[i]

        # Create image clips
        bg_clip = ImageClip(bg_image, duration=1)  # Background image stays for 0.5 sec
        fg_clip = ImageClip(fg_image, duration=1).resize(0.6)  # Foreground resized to 60% and stays for 0.5 sec

        # Merge foreground on top of background (centered)
        merged_clip = CompositeVideoClip([bg_clip, fg_clip.set_position("center")])
        clips.append(merged_clip)

    # Concatenate all merged clips
    final_video = concatenate_videoclips(clips, method="compose")

    # Add audio if available
    if audio_path and os.path.exists(audio_path):
        audio = AudioFileClip(audio_path)
        final_video = final_video.set_audio(audio)

    # Export final video
    final_video.write_videofile(
        output_path,
        codec="libx264",
        fps=24,
        audio_codec="aac",
        preset="medium"
    )

# Celery task to handle video generation (with 0.5 sec per image)
@shared_task
def classic_task(**params):
    ids = params.get("ids")
    selected_type = params.get("selected_type")
    selected_background_bucket = params.get("selected_background_bucket")
    selected_foreground_bucket = params.get("selected_foreground_bucket")
    audio_path = params.get("audio_file_path")
    print(f"Processing composition {ids} with type {selected_type}")

    # Generate output name
    def generate_auto_name():
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        random_str = ''.join(random.choices(string.ascii_letters + string.digits, k=6))
        return f"composition_{timestamp}_{random_str}"

    auto_name = generate_auto_name()
    output_path = f"{VIDEO_DIR}/{auto_name}.mp4"
    video_filename = f"{auto_name}.mp4"

    downloaded_background_files = []
    downloaded_foreground_files = []

    # ✅ Download Background Imag es
    if selected_background_bucket:
        downloaded_background_files = download_s3_files(selected_background_bucket, TEMP_BG_FOLDER)
        print(f"✅ Downloaded Background Files: {downloaded_background_files}")
 
    # ✅ Download Foreground Images
    if selected_foreground_bucket:
        downloaded_foreground_files = download_s3_files(selected_foreground_bucket, TEMP_FG_FOLDER)
        print(f"✅ Downloaded Foreground Files: {downloaded_foreground_files}")

    if not downloaded_background_files or not downloaded_foreground_files:
        messages.error(request, "Error: No valid background or foreground files were downloaded.")
        return redirect("composition-add")

    valid_bg_images = validate_images(downloaded_background_files)
    valid_fg_images = validate_images(downloaded_foreground_files)

    #print(f"✅ Background images: {valid_bg_images}")
    #print(f"✅ Foreground images: {valid_fg_images}")

    try:
        # Generate video
        create_final_videoNew(valid_bg_images, valid_fg_images, audio_path, output_path)

        # Upload video to S3
        s3_key = os.path.basename(output_path)
        with open(output_path, "rb") as video_file:
            s3.upload_fileobj(video_file, BUCKET_NAME, s3_key)

        # ✅ Construct Public S3 URL
        s3_video_url = f"https://{BUCKET_NAME}.s3.amazonaws.com/{s3_key}"
        print(f"✅ S3 Upload Successful: {s3_video_url}")
        
        # ✅ Upload Audio File (if exists)
        s3_audio_url = None
        if audio_path and os.path.exists(audio_path):
            audio_key = f"{os.path.basename(audio_path)}"
            with open(audio_path, "rb") as audio_file_obj:
                s3.upload_fileobj(audio_file_obj, BUCKET_NAME, audio_key)
            s3_audio_url = f"https://{BUCKET_NAME}.s3.amazonaws.com/{audio_key}"
            print(f"✅ S3 Audio Upload Successful: {s3_audio_url}")
    
        # ✅ Update Composition Status After S3 Upload
        Composition.objects.filter(id=ids).update(
            status="Completed",
            final_video=video_filename,
        )
        print(f"✅ Composition {ids} marked as Completed")
        
    except (BotoCoreError, ClientError) as e:
        print(f"S3 Upload Failed: {e}")
        return

    finally:
        # Clean up local temp files
        try:
            if os.path.exists(bg_local_dir):
                shutil.rmtree(bg_local_dir)
            if os.path.exists(fg_local_dir):
                shutil.rmtree(fg_local_dir)
            if os.path.exists(output_path):
                os.remove(output_path)
            print("Temporary files and folders cleaned up.")
        except Exception as cleanup_err:
            print(f"Error during cleanup: {cleanup_err}")

@app.task(bind=True)
def tunnel_task(self, params_tunnel):
    selected_background_buckets = params_tunnel.get("selected_background_buckets", [])
    composition_id = params_tunnel.get("ids")
    audio_path = params_tunnel.get("audio_file_path")

    print(f"🔔 Starting tunnel_task | Audio Path: {audio_path}")

    downloaded_images = []

    # 1. Download all background images from the selected S3 buckets
    for bucket in selected_background_buckets:
        try:
            response = s3.list_objects_v2(Bucket=bucket)
            if "Contents" not in response:
                print(f"⚠️ No files found in bucket: {bucket}")
                continue

            for obj in response["Contents"]:
                file_key = obj["Key"]
                file_path = os.path.join(TEMP_IMAGE_FOLDER, os.path.basename(file_key))
                s3.download_file(bucket, file_key, file_path)
                downloaded_images.append(file_path)
                
        except ClientError as e:
            print(f"❌ Error fetching files from bucket {bucket}: {e}")
        print("download images:", downloaded_images)
    if not downloaded_images:
        print("❌ No valid images found for merging.")
        return


    # 3. Generate unique name for video file
    def generate_auto_name():
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        rand_str = ''.join(random.choices(string.ascii_letters + string.digits, k=6))
        return f"composition_{timestamp}_{rand_str}"

    auto_name = generate_auto_name()
    video_filename = f"{auto_name}.mp4"
    output_path = os.path.join(VIDEO_DIR, video_filename)

    # 4. Create final video with optional audio
    output_video = create_video_ffmpegNew(
        downloaded_images, output_path, fps=1, audio_path=audio_path
    )


    if not output_video or not os.path.exists(output_video) or os.path.getsize(output_video) == 0:
        print("❌ Final video creation failed or file is empty.")
        return

    # 5. Upload video to S3
    s3_key = os.path.basename(output_path)
    try:
        with open(output_path, "rb") as video_file:
            s3.upload_fileobj(video_file, BUCKET_NAME, s3_key)
        s3_video_url = f"https://{BUCKET_NAME}.s3.amazonaws.com/{s3_key}"
        print(f"✅ Video uploaded to S3: {s3_video_url}")
    except (BotoCoreError, ClientError) as e:
        print(f"❌ Video upload to S3 failed: {e}")
        return

    # 6. Upload audio to S3 (if it exists)
    s3_audio_url = None
    if audio_path and os.path.exists(audio_path):
        try:
            audio_key = os.path.basename(audio_path)
            with open(audio_path, "rb") as audio_file_obj:
                s3.upload_fileobj(audio_file_obj, BUCKET_NAME, audio_key)
            s3_audio_url = f"https://{BUCKET_NAME}.s3.amazonaws.com/{audio_key}"
            print(f"✅ Audio uploaded to S3: {s3_audio_url}")
        except Exception as e:
            print(f"❌ Audio upload failed: {e}")
    else:
        print("⚠️ Audio path is invalid or file does not exist.")

    # 7. Update Composition entry in DB
    try:
        print(f"🔍 Updating composition ID: {composition_id}")
        updated_count = Composition.objects.filter(id=composition_id).update(
            status="Completed",
            final_video=video_filename,
        )
        print(f"✅ DB update successful. Rows affected: {updated_count}")
    except Exception as e:
        print(f"❌ DB update failed: {e}")
    # 8. Clean up temporary downloaded images
    try:
        for image_path in downloaded_images:
            if os.path.exists(image_path):
                os.remove(image_path)
        print("🧹 Temporary downloaded images removed.")
    except Exception as e:
        print(f"⚠️ Error cleaning up temporary images: {e}")
    
@app.task
def left_to_right_task(params_left):
    
    selected_background_bucket = params_left.get("background_bucket")
    selected_foreground_bucket = params_left.get("foreground_bucket")
    ids = params_left.get("ids")
    audio_path = params_left.get("audio_file_path")
   
    # 1. Download files
    downloaded_bg_files = download_s3_files(selected_background_bucket, TEMP_BG_FOLDER)
    downloaded_fg_files = download_s3_files(selected_foreground_bucket, TEMP_FG_FOLDER)

    if not downloaded_bg_files or not downloaded_fg_files:
        print("❌ No valid background or foreground files.")
        Composition.objects.filter(id=ids).update(status="failed")
        return

    # 2. Merge Images
    merged_images = merge_images(downloaded_bg_files, downloaded_fg_files, MERGED_IMAGE_DIR)
    if not merged_images:
        print("❌ Image merging failed.")
        Composition.objects.filter(id=ids).update(status="failed")
        return
 
    # 3. Create video
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    random_str = ''.join(random.choices(string.ascii_letters + string.digits, k=6))
    auto_name = f"composition_{timestamp}_{random_str}"
    
    output_path = os.path.join(VIDEO_DIR, f"{auto_name}.mp4")
    video_filename = f"{auto_name}.mp4"
    
    final_video_path = create_video_ffmpeg(MERGED_IMAGE_DIR, output_path, fps=1,audio_path=audio_path)
    print("final video:",final_video_path)

    if not final_video_path or not os.path.exists(final_video_path) or os.path.getsize(final_video_path) == 0:
        print("❌ Final video not created or empty.")
        Composition.objects.filter(id=ids).update(status="failed")
        return

    # 4. Generate thumbnail
    thumbnail_path = os.path.join(THUMBNAIL_DIR, f"thumbnail_{auto_name}.jpg")
    generate_video_thumbnail(final_video_path, thumbnail_path)

    # 5. Upload to S3
    s3_key = os.path.basename(final_video_path)
    try:
        with open(final_video_path, "rb") as video_file:
            s3.upload_fileobj(video_file, BUCKET_NAME, s3_key)

        s3_video_url = f"https://{BUCKET_NAME}.s3.amazonaws.com/{s3_key}"
        print(f"✅ S3 Upload Successful: {s3_video_url}")
        
        # ✅ Upload Audio File (if exists)
        s3_audio_url = None
        if audio_path and os.path.exists(audio_path):
            audio_key = f"{os.path.basename(audio_path)}"
            with open(audio_path, "rb") as audio_file_obj:
                s3.upload_fileobj(audio_file_obj, BUCKET_NAME, audio_key)
            s3_audio_url = f"https://{BUCKET_NAME}.s3.amazonaws.com/{audio_key}"
            print(f"✅ S3 Audio Upload Successful: {s3_audio_url}")

        # ✅ Save the correct S3 URL in the DB
        Composition.objects.filter(id=ids).update(
           
            final_video=video_filename,
            img=thumbnail_path,  # you may want to upload this too
            status="Completed"
        )
        print(f"✅ Composition {ids} marked as Completed")
    except Exception as e:
        print(f"❌ S3 Upload or DB update failed: {e}")
        Composition.objects.filter(id=ids).update(status="failed")
    

@app.task
def right_to_left_task(params_right):
    selected_background_bucket = params_right.get("background_bucket")
    selected_foreground_bucket = params_right.get("foreground_bucket")
    ids = params_right.get("ids")
    audio_path = params_right.get("audio_file_path")

    # 1. Download files
    downloaded_bg_files = download_s3_files(selected_background_bucket, TEMP_BG_FOLDER)
    downloaded_fg_files = download_s3_files(selected_foreground_bucket, TEMP_FG_FOLDER)

    if not downloaded_bg_files or not downloaded_fg_files:
        print("❌ (RTL) No valid background or foreground files.")
        Composition.objects.filter(id=ids).update(status="failed")
        return

    # 🔄 REVERSE FOREGROUND FILES FOR RIGHT TO LEFT
    downloaded_fg_files.reverse()

    # 2. Merge Images
    merged_images = merge_images(downloaded_bg_files, downloaded_fg_files, MERGED_IMAGE_DIR)
    if not merged_images:
        print("❌ (RTL) Image merging failed.")
        Composition.objects.filter(id=ids).update(status="failed")
        return

    # 3. Create video
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    random_str = ''.join(random.choices(string.ascii_letters + string.digits, k=6))
    auto_name = f"rtl_composition_{timestamp}_{random_str}"

    output_path = os.path.join(VIDEO_DIR, f"{auto_name}.mp4")
    video_filename = f"{auto_name}.mp4"

    final_video_path = create_video_ffmpeg(MERGED_IMAGE_DIR, output_path, fps=1,audio_path=audio_path)
    print("📽️ (RTL) Final video path:", final_video_path)

    if not final_video_path or not os.path.exists(final_video_path) or os.path.getsize(final_video_path) == 0:
        print("❌ (RTL) Final video not created or empty.")
        Composition.objects.filter(id=ids).update(status="failed")
        return

    # 4. Generate thumbnail
    thumbnail_path = os.path.join(THUMBNAIL_DIR, f"thumbnail_{auto_name}.jpg")
    generate_video_thumbnail(final_video_path, thumbnail_path)

    # 5. Upload to S3
    s3_key = os.path.basename(final_video_path)
    try:
        with open(final_video_path, "rb") as video_file:
            s3.upload_fileobj(video_file, BUCKET_NAME, s3_key)

        s3_video_url = f"https://{BUCKET_NAME}.s3.amazonaws.com/{s3_key}"
        print(f"✅ (RTL) S3 Upload Successful: {s3_video_url}")
        
        # ✅ Upload Audio File (if exists)
        s3_audio_url = None
        if audio_path and os.path.exists(audio_path):
            audio_key = f"{os.path.basename(audio_path)}"
            with open(audio_path, "rb") as audio_file_obj:
                s3.upload_fileobj(audio_file_obj, BUCKET_NAME, audio_key)
            s3_audio_url = f"https://{BUCKET_NAME}.s3.amazonaws.com/{audio_key}"
            print(f"✅ S3 Audio Upload Successful: {s3_audio_url}")

        # ✅ Save the correct S3 URL in the DB
        Composition.objects.filter(id=ids).update(
            
            final_video=video_filename,
            img=thumbnail_path,
            status="Completed"
        )
        print(f"✅ (RTL) Composition {ids} marked as Completed")
    except Exception as e:
        print(f"❌ (RTL) S3 Upload or DB update failed: {e}")
        Composition.objects.filter(id=ids).update(status="failed")


    
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
        
