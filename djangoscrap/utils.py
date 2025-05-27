import boto3, ffmpeg, os, re, random, string, subprocess
import moviepy as mp

from datetime import datetime
from botocore.exceptions import ClientError, NoCredentialsError
from moviepy import ImageSequenceClip ,VideoFileClip, concatenate_videoclips
from PIL import Image, UnidentifiedImageError

from django.contrib import messages


try:
    from PIL import ImageResampling  # For newer Pillow versi ons
    RESAMPLING_METHOD = ImageResampling.LANCZOS
except ImportError:
    RESAMPLING_METHOD = Image.LANCZOS  # Fallback for older versions

# Initialize S3 Client
s3 = None
def set_r2_client():
    global s3
    if s3 is None:
        s3 = boto3.client(
        "s3",
            endpoint_url=os.getenv('R2_ENDPOINT'),
            aws_access_key_id=os.getenv('R2_ACCESS_KEY'),
            aws_secret_access_key=os.getenv('R2_SECRET_KEY'),
            region_name="auto",
        )

set_r2_client()

TEMP_BG_FOLDER = "media/temp_s3_back_files"
TEMP_FG_FOLDER = "media/temp_s3_fore_files"
VIDEO_DIR = "media/videos"
TEMP_IMAGE_FOLDER = "media/temp_images"
THUMBNAIL_DIR = "static/composition_thumbnails"
AUDIO_DIR = "compositions/audios"
MERGED_IMAGE_DIR = "media/merged_images"


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


def get_sample_image_url(bucket_name):
    """ Get the first image (JPG, PNG, JPEG, GIF) from the bucket """
    try:
        response = s3.list_objects_v2(Bucket=bucket_name)
        contents = response.get("Contents", [])
        for obj in contents:
            key = obj["Key"].lower()
            if key.endswith(('.jpg', '.jpeg', '.png', '.gif')):
                # Generate a public URL if the bucket/object is public
                return f"{os.getenv('R2_PUBLIC_URL')}/{obj['Key']}"
        return None
    except Exception as e:
        print(f"Error accessing {bucket_name}: {e}")
        return None


def is_valid_bucket_name(bucket_name):
    """Check if the S3 bucket name follows AWS naming rules"""
    return bool(re.match(r'^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$', bucket_name))


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
        file_url = f"{os.getenv('R2_PUBLIC_URL')}/{s3_key}"
        print(f"✅ File uploaded successfully: {file_url}")
        return file_url

    except NoCredentialsError:
        print("❌ AWS credentials not found!")
        return None
    except Exception as e:
        print(f"❌ Error uploading to S3: {e}")
        return None

def ensure_directories():
    for folder in [TEMP_BG_FOLDER, TEMP_FG_FOLDER, VIDEO_DIR, TEMP_IMAGE_FOLDER, THUMBNAIL_DIR, AUDIO_DIR, MERGED_IMAGE_DIR]:
        os.makedirs(folder, exist_ok=True)

def generate_auto_name():
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    rand = ''.join(random.choices(string.ascii_letters + string.digits, k=6))
    return f"composition_{timestamp}_{rand}"

def generate_thumbnail(source_path, output_path):
    try:
        ext = os.path.splitext(source_path)[1].lower()
        if ext in [".jpg", ".jpeg", ".png", ".webp"]:
            img = Image.open(source_path).convert("RGB")
        elif ext == ".gif":
            img = Image.open(source_path)
            img.seek(0)
            img = img.convert("RGB")
        elif ext == ".mp4":
            with VideoFileClip(source_path) as clip:
                frame = clip.get_frame(0)
                img = Image.fromarray(frame).convert("RGB")
        else:
            print(f"❌ Unsupported format for thumbnail: {ext}")
            return None
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        img.save(output_path, "JPEG")
        return output_path
    except Exception as e:
        print(f"❌ Error generating thumbnail: {e}")
        return None

def save_uploaded_file(uploaded_file, path):
    with open(path, "wb") as f:
        f.write(uploaded_file.read())
    return path

def delete_bucket_objects(bucket_name):
    continuation_token = None
    while True:
        list_kwargs = {'Bucket': bucket_name}
        if continuation_token:
            list_kwargs['ContinuationToken'] = continuation_token

        response = s3.list_objects_v2(**list_kwargs)
        for obj in response.get('Contents', []):
            s3.delete_object(Bucket=bucket_name, Key=obj['Key'])

        if not response.get('IsTruncated'):
            break
        continuation_token = response.get('NextContinuationToken')
