from moviepy.editor import VideoFileClip, AudioFileClip, CompositeVideoClip, ImageClip, concatenate_videoclips
import subprocess
from PIL import Image
import os
from django.core.exceptions import ValidationError
import ffmpeg
import glob
import cv2
import numpy as np

MERGED_IMAGE_DIR = "media/merged_images"  # Change path as needed
os.makedirs(MERGED_IMAGE_DIR, exist_ok=True)  # Ensure directory exists

def create_final_video(background_path, foreground_path, audio_path, output_path):
    bg_clip = VideoFileClip(background_path)
    
    # Check if the foreground is an image or a video
    if foreground_path.lower().endswith(('.png', '.jpg', '.jpeg')):
        fg_clip = ImageClip(foreground_path, duration=1)  # Image lasts 1 sec
    else:
        fg_clip = VideoFileClip(foreground_path).subclip(0, 1)  # 0.5 sec per frame
    
    # Resize foreground to 60% of background width & 40% height
    fg_width = int(bg_clip.w * 0.6)
    fg_height = int(bg_clip.h * 0.4)
    fg_clip = fg_clip.resize(newsize=(fg_width, fg_height))

    # Position foreground at center
    final_clip = CompositeVideoClip([
        bg_clip.set_duration(fg_clip.duration),  
        fg_clip.set_position(("center", "center"))
    ])

    # Add audio if provided
    if audio_path:
        audio = AudioFileClip(audio_path)
        final_clip = final_clip.set_audio(audio)

    # Save video
    final_clip.write_videofile(
        output_path,
        codec="libx264",
        fps=24,
        audio_codec="aac",
        preset="medium"
    )

def combine_video_with_audio(background_video, foreground_video, audio_path, output_path):
    try:
        if not background_video or not foreground_video:
            raise ValueError("Background or Foreground video paths are missing.")

        # Ensure all file paths exist
        for file_path, label in [(background_video, "Background"), (foreground_video, "Foreground"), (audio_path, "Audio")]:
            if not os.path.exists(file_path):
                raise FileNotFoundError(f"{label} file not found: {file_path}")
        
        # Load video and audio
        background_clip = VideoFileClip(background_video)
        foreground_clip = VideoFileClip(foreground_video)
        audio_clip = AudioFileClip(audio_path)

        # Validate dimensions before resizing
        print(f"Background dimensions: {background_clip.w} x {background_clip.h}")
        if background_clip.w <= 0 or background_clip.h <= 0:
            raise ValueError("Background video has invalid width or height.")
        
        # Resize foreground to be 60% of background width and 40% of background height
        fg_width = max(1, int(background_clip.w * 0.6))
        fg_height = max(1, int(background_clip.h * 0.4))
        foreground_clip = foreground_clip.resize(newsize=(fg_width, fg_height))
        
        # Trim all media to match minimum duration
        min_duration = min(background_clip.duration, foreground_clip.duration, audio_clip.duration)
        background_clip = background_clip.subclip(0, min_duration)
        foreground_clip = foreground_clip.subclip(0, min_duration)
        audio_clip = audio_clip.subclip(0, min_duration)

        # Set background audio
        background_clip = background_clip.set_audio(audio_clip)

        # Overlay foreground video centered on background
        final_video = CompositeVideoClip([
            background_clip,
            foreground_clip.set_position("center")
        ])
        
        # Export final video
        final_video.write_videofile(output_path, codec="libx264", audio_codec="aac")

        return output_path

    except Exception as e:
        print(f"Error combining videos: {e}")
        raise RuntimeError(f"Error combining videos with audio: {e}")


from pathlib import Path
from moviepy.editor import ImageClip, AudioFileClip, concatenate_videoclips, vfx
import os

def create_video_ffmpegNew(
    input_dir,
    output_path,
    fps=24,
    audio_path=None,
    zoom_speed=0.05,
    start_scale=1.0,
    end_scale=1.2,
    image_duration=2.0  # seconds per image
):
    valid_exts = (".jpg", ".jpeg", ".png")

    # Get image files
    if isinstance(input_dir, (str, os.PathLike)):
        input_dir = Path(input_dir).resolve()
        image_files = sorted([
            f for f in input_dir.iterdir()
            if f.suffix.lower() in valid_exts
        ])
    elif isinstance(input_dir, list):
        image_files = [Path(f).resolve() for f in input_dir if Path(f).suffix.lower() in valid_exts]
    else:
        print("❌ Invalid input_dir type. Must be a path or list of image files.")
        return None

    if not image_files:
        print("❌ No valid image files found.")
        return None

    # Create zoomed clips
    clips = []
    for img_path in image_files:
        try:
            img_clip = ImageClip(str(img_path)).set_duration(image_duration)
            zoomed_clip = img_clip.fx(
                vfx.resize,
                lambda t: start_scale + (end_scale - start_scale) * (t / image_duration)
            ).set_position("center")
            clips.append(zoomed_clip)
        except Exception as e:
            print(f"⚠️ Skipping image {img_path}: {e}")

    if not clips:
        print("❌ No clips created.")
        return None

    final_clip = concatenate_videoclips(clips, method="compose")

    # Add audio if provided
    if audio_path and os.path.exists(audio_path):
        audio_clip = AudioFileClip(audio_path)
        duration = min(final_clip.duration, audio_clip.duration)
        final_clip = final_clip.set_audio(audio_clip.subclip(0, duration))
        final_clip = final_clip.subclip(0, duration)

    # Export video
    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    final_clip.write_videofile(str(output_path), fps=fps, audio_codec="aac")

    print(f"🎬 Video created: {output_path}")
    return str(output_path)

