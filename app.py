# video-sprite-generator/app.py
import os
import requests
import subprocess
import math
import uuid
import shutil # For safely removing directories
from flask import Flask, request, render_template, send_file, jsonify
from urllib.parse import urlparse

app = Flask(__name__)

# Configuration for temporary storage folders
# These folders are ephemeral on Render's free tier and data will be lost
# after instance restarts or scales down.
app.config['UPLOAD_FOLDER'] = 'temp_videos'
app.config['THUMBNAIL_FOLDER'] = 'temp_thumbnails'

# Ensure temporary directories exist when the app starts
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['THUMBNAIL_FOLDER'], exist_ok=True)

# Thumbnail generation parameters
# Adjust these values based on desired quality and performance
THUMB_WIDTH = 160
THUMB_HEIGHT = 90
THUMBS_PER_ROW = 10  # Number of thumbnails in a row for the sprite image
SPRITE_FILENAME = "sprite.jpg" # Name for the generated sprite image
VTT_FILENAME = "sprite.vtt"   # Name for the generated VTT file

# Interval (in seconds) between extracted thumbnails for the sprite
# A smaller interval means more thumbnails and a larger sprite/VTT.
THUMBNAIL_INTERVAL = 5 

def download_video(video_url, output_path):
    """
    Downloads a video from a given URL to a specified local path.
    Handles basic error checking for the download.
    """
    try:
        app.logger.info(f"Attempting to download video from: {video_url}")
        # Use stream=True to handle potentially large files
        response = requests.get(video_url, stream=True, timeout=120) # Increased timeout
        response.raise_for_status() # Raise an exception for bad HTTP status codes (4xx or 5xx)

        with open(output_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        app.logger.info(f"Video downloaded successfully to: {output_path}")
        return True
    except requests.exceptions.RequestException as e:
        app.logger.error(f"Network or request error downloading video {video_url}: {e}")
        return False
    except Exception as e:
        app.logger.error(f"An unexpected error occurred during video download {video_url}: {e}")
        return False

def get_video_duration(video_path):
    """
    Retrieves the duration of a video file using ffprobe.
    ffprobe is part of the FFmpeg suite.
    """
    try:
        cmd = [
            'ffprobe',
            '-v', 'error',           # Suppress verbose output, show only errors
            '-show_entries', 'format=duration', # Show only the duration entry
            '-of', 'default=noprint_wrappers=1:nokey=1', # Output only the value
            video_path
        ]
        # Run the command and capture its output
        result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=20) # Increased timeout
        duration = float(result.stdout.strip())
        app.logger.info(f"Video duration for {video_path}: {duration} seconds")
        return duration
    except FileNotFoundError:
        app.logger.error("FFprobe not found. Ensure FFmpeg is installed and in PATH.")
        return None
    except subprocess.CalledProcessError as e:
        app.logger.error(f"FFprobe failed for {video_path}: {e.stderr.strip()}")
        return None
    except ValueError:
        app.logger.error(f"Could not parse duration from ffprobe output: {result.stdout.strip()}")
        return None
    except Exception as e:
        app.logger.error(f"An unexpected error occurred getting video duration for {video_path}: {e}")
        return None

def generate_individual_thumbnails(video_path, output_dir, duration):
    """
    Generates a series of individual thumbnail images from the video
    at predefined intervals.
    Returns a list of paths to the generated thumbnail files.
    """
    thumbnail_paths = []
    num_thumbnails = math.ceil(duration / THUMBNAIL_INTERVAL)
    app.logger.info(f"Preparing to generate {num_thumbnails} thumbnails for {video_path}")

    for i in range(num_thumbnails):
        timestamp = i * THUMBNAIL_INTERVAL
        
        # Format timestamp for FFmpeg's -ss (seek) option: HH:MM:SS.ms
        # Ensure timestamp does not exceed video duration
        actual_timestamp = min(timestamp, duration - 0.1) # Adjust slightly to avoid EOF issues

        hours = int(actual_timestamp // 3600)
        minutes = int((actual_timestamp % 3600) // 60)
        seconds = int(actual_timestamp % 60)
        milliseconds = int((actual_timestamp - math.floor(actual_timestamp)) * 1000)
        timestamp_str = f"{hours:02}:{minutes:02}:{seconds:02}.{milliseconds:03}"

        thumb_filename = f"thumb_{i:04d}.jpg" # Use zero-padded numbers for sorting
        thumb_path = os.path.join(output_dir, thumb_filename)

        cmd = [
            'ffmpeg',
            '-ss', timestamp_str, # Seek to this timestamp
            '-i', video_path,    # Input video file
            '-vframes', '1',     # Extract only one frame
            '-vf', f'scale={THUMB_WIDTH}:{THUMB_HEIGHT}', # Scale frame to desired dimensions
            '-q:v', '2',         # Quality for JPEG (2 is good, 1 is best, 31 is worst)
            thumb_path
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=30) # Increased timeout
            
            # Verify if the thumbnail file was actually created and is not empty
            if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
                thumbnail_paths.append(thumb_path)
                app.logger.debug(f"Generated thumbnail: {thumb_path} at {timestamp_str}")
            else:
                app.logger.warning(f"Thumbnail file {thumb_path} not created or is empty. Skipping.")

        except subprocess.CalledProcessError as e:
            app.logger.error(f"FFmpeg error generating thumbnail {thumb_path} at {timestamp_str}: {e.stderr.decode().strip()}")
            app.logger.error(f"FFmpeg command failed: {' '.join(cmd)}")
            continue # Continue to the next thumbnail even if one fails
        except subprocess.TimeoutExpired:
            app.logger.error(f"FFmpeg timeout generating thumbnail {thumb_path} at {timestamp_str}")
            continue
        except Exception as e:
            app.logger.error(f"An unexpected error occurred during thumbnail generation for {video_path}: {e}")
            continue
    return thumbnail_paths

def create_sprite_image(thumbnail_paths, output_path):
    """
    Stitches a list of individual thumbnail images into a single sprite image
    using FFmpeg's tile filter.
    """
    if not thumbnail_paths:
        app.logger.warning("No valid thumbnail paths provided to create sprite image. Skipping sprite creation.")
        return False

    # Calculate number of rows needed for the sprite
    num_thumbnails = len(thumbnail_paths)
    # Ensure there's at least one row, even if only one thumbnail
    num_rows = max(1, math.ceil(num_thumbnails / THUMBS_PER_ROW))

    # Create a temporary concat file list for FFmpeg
    input_list_path = os.path.join(os.path.dirname(output_path), "input_thumbs.txt")
    try:
        with open(input_list_path, 'w') as f:
            # Get the directory where input_thumbs.txt resides for relative paths
            input_list_dir = os.path.dirname(input_list_path)
            for p in sorted(thumbnail_paths): # Sort to ensure correct order
                # Write paths relative to the input_thumbs.txt file's directory
                relative_path = os.path.relpath(p, input_list_dir)
                f.write(f"file '{relative_path}'\n")

        # Command to create the sprite image
        cmd = [
            'ffmpeg',
            '-f', 'concat',   # Input format is a concat list
            '-safe', '0',     # Required for 'file' protocol with arbitrary paths
            '-i', input_list_path, # Input file list
            '-vf', f'tile={THUMBS_PER_ROW}x{num_rows}', # Apply tile filter
            '-q:v', '2',      # Output quality for JPEG
            output_path
        ]
        app.logger.info(f"Creating sprite image with command: {' '.join(cmd)}")
        # Execute the FFmpeg command. Crucially, set cwd to the directory where input_thumbs.txt is.
        result = subprocess.run(cmd, check=True, capture_output=True, timeout=120, cwd=input_list_dir) # Increased timeout significantly
        app.logger.info(f"Sprite image created successfully at: {output_path}")
        return True
    except FileNotFoundError:
        app.logger.error("FFmpeg not found. Ensure FFmpeg is installed and in PATH.")
        return False
    except subprocess.CalledProcessError as e:
        app.logger.error(f"FFmpeg error creating sprite image: {e.stderr.decode().strip()}")
        app.logger.error(f"FFmpeg command failed: {' '.join(cmd)}")
        return False
    except subprocess.TimeoutExpired:
        app.logger.error(f"FFmpeg timeout creating sprite image for {output_path}")
        return False
    except Exception as e:
        app.logger.error(f"An unexpected error occurred during sprite creation: {e}")
        return False
    finally:
        if os.path.exists(input_list_path):
            try:
                os.remove(input_list_path) # Always clean up the temporary list file
            except OSError as e:
                app.logger.error(f"Error cleaning up input_thumbs.txt: {e}")


def create_vtt_file(output_dir, num_thumbnails, interval, sprite_url_path):
    """
    Creates the WebVTT file that links to the sprite image and defines
    the coordinates of each thumbnail within the sprite.
    """
    vtt_path = os.path.join(output_dir, VTT_FILENAME)
    try:
        with open(vtt_path, 'w') as f:
            f.write("WEBVTT\n\n")
            
            for i in range(num_thumbnails):
                start_time_sec = i * interval
                end_time_sec = start_time_sec + interval

                # Ensure end_time doesn't exceed 23:59:59.999 (VTT spec limit for cue timestamps)
                # And ensure it doesn't go beyond the video's actual end (though not critical for VTT itself)
                end_time_sec = min(end_time_sec, 86399.999) 

                # Format times for VTT (HH:MM:SS.mmm)
                start_h = int(start_time_sec // 3600)
                start_m = int((start_time_sec % 3600) // 60)
                start_s = int(start_time_sec % 60)
                start_ms = int((start_time_sec - math.floor(start_time_sec)) * 1000)
                
                end_h = int(end_time_sec // 3600)
                end_m = int((end_time_sec % 3600) // 60)
                end_s = int(end_time_sec % 60)
                end_ms = int((end_time_sec - math.floor(end_time_sec)) * 1000)

                start_time_str = f"{start_h:02}:{start_m:02}:{start_s:02}.{start_ms:03}"
                end_time_str = f"{end_h:02}:{end_m:02}:{end_s:02}.{end_ms:03}"

                # Calculate XYWH coordinates for each thumbnail within the sprite
                col = i % THUMBS_PER_ROW
                row = i // THUMBS_PER_ROW
                
                x = col * THUMB_WIDTH
                y = row * THUMB_HEIGHT
                
                f.write(f"{start_time_str} --> {end_time_str}\n")
                # The sprite_url_path should be the URL where the sprite image is accessible
                f.write(f"{sprite_url_path}#xywh={x},{y},{THUMB_WIDTH},{THUMB_HEIGHT}\n\n")
        app.logger.info(f"VTT file created successfully at: {vtt_path}")
        return True
    except Exception as e:
        app.logger.error(f"Error creating VTT file: {e}")
        return False

# Route for the main application page
@app.route('/')
def index():
    return render_template('index.html')

# API endpoint to generate thumbnails and VTT
@app.route('/generate', methods=['POST'])
def generate():
    video_url = request.form.get('video_url') # Use .get() for safer access
    if not video_url:
        return jsonify({"status": "error", "message": "Video URL is required."}), 400

    # Generate a unique ID for this processing job
    # This ID will be used for a temporary directory to store assets
    job_id = str(uuid.uuid4())
    temp_job_dir = os.path.join(app.config['THUMBNAIL_FOLDER'], job_id)
    os.makedirs(temp_job_dir, exist_ok=True)
    app.logger.info(f"Started processing job: {job_id}")

    video_filename = f"input_{job_id}.mp4"
    video_path = os.path.join(app.config['UPLOAD_FOLDER'], video_filename)

    # Initialize thumbnail_paths here to prevent UnboundLocalError
    thumbnail_paths = [] 

    try:
        app.logger.info(f"Job {job_id}: Downloading video from {video_url}...")
        if not download_video(video_url, video_path):
            return jsonify({"status": "error", "message": "Failed to download video. Please check the URL or network access."}), 500

        duration = get_video_duration(video_path)
        if duration is None or duration <= 0:
            return jsonify({"status": "error", "message": "Could not get valid video duration. Video might be corrupted or empty."}), 500
        
        # Adjust thumbnail interval for very short videos to avoid excessive frames or no frames
        if duration < THUMBNAIL_INTERVAL:
            app.logger.warning(f"Job {job_id}: Video is very short ({duration:.2f}s) for interval {THUMBNAIL_INTERVAL}s. Adjusting interval for VTT.")
            # For very short videos, take one thumbnail at the beginning
            # The interval for VTT calculation should be duration itself or a small fixed value
            thumbnail_interval_for_vtt = max(1, math.floor(duration / 2)) if duration > 0 else 1 # ensures at least 1s interval for VTT
        else:
            thumbnail_interval_for_vtt = THUMBNAIL_INTERVAL

        app.logger.info(f"Job {job_id}: Video duration: {duration:.2f} seconds. Generating individual thumbnails...")
        thumbnail_paths = generate_individual_thumbnails(video_path, temp_job_dir, duration)
        
        if not thumbnail_paths:
            return jsonify({"status": "error", "message": "Failed to generate any valid thumbnails. Video might be unreadable or too short for extraction."}), 500

        app.logger.info(f"Job {job_id}: Generated {len(thumbnail_paths)} individual thumbnails. Creating sprite image...")
        sprite_output_path = os.path.join(temp_job_dir, SPRITE_FILENAME)
        if not create_sprite_image(thumbnail_paths, sprite_output_path):
            return jsonify({"status": "error", "message": "Failed to create sprite image. Check Render logs for FFmpeg details."}), 500

        app.logger.info(f"Job {job_id}: Creating VTT file...")
        # The sprite_url_path here is the *publicly accessible URL* for the sprite image
        # This assumes the sprite will be served from /thumbnails/<job_id>/sprite.jpg
        sprite_public_url = f'/thumbnails/{job_id}/{SPRITE_FILENAME}'
        if not create_vtt_file(temp_job_dir, len(thumbnail_paths), thumbnail_interval_for_vtt, sprite_public_url):
            return jsonify({"status": "error", "message": "Failed to create VTT file."}), 500

        # Construct URLs for the generated assets
        sprite_download_url = f'/thumbnails/{job_id}/{SPRITE_FILENAME}'
        vtt_download_url = f'/thumbnails/{job_id}/{VTT_FILENAME}'

        app.logger.info(f"Job {job_id}: Successfully generated sprite and VTT.")
        return jsonify({
            "status": "success",
            "message": "Sprite and VTT generated successfully!",
            "sprite_url": sprite_download_url,
            "vtt_url": vtt_download_url
        })

    except Exception as e:
        app.logger.error(f"Job {job_id}: An unhandled error occurred during generation: {e}", exc_info=True)
        return jsonify({"status": "error", "message": f"An unexpected server error occurred: {e}"}), 500
    finally:
        # IMPORTANT: Clean up ALL temporary files associated with this job
        # This is crucial for managing disk space on Render's ephemeral storage.
        if os.path.exists(video_path):
            try:
                os.remove(video_path)
                app.logger.info(f"Job {job_id}: Cleaned up input video: {video_path}")
            except OSError as e:
                app.logger.error(f"Error cleaning up video file {video_path}: {e}")
        
        # Clean up individual thumbnails (if the list was populated)
        for p in thumbnail_paths: # This loop now safe due to initialization
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError as e:
                    app.logger.error(f"Error cleaning up individual thumbnail {p}: {e}")
        
        # We leave the temp_job_dir (containing sprite and VTT) for a short period
        # to allow the client to download them. Render's ephemeral storage will
        # eventually clean this up on instance restarts/scaling.
        app.logger.info(f"Job {job_id}: Finished processing. Check logs for cleanup status.")


# Route to serve the generated sprite image and VTT file
@app.route('/thumbnails/<job_id>/<filename>')
def serve_thumbnail_assets(job_id, filename):
    # Construct a safe path to prevent directory traversal attacks
    full_path = os.path.join(app.config['THUMBNAIL_FOLDER'], job_id, filename)
    
    # Ensure the requested file is actually within the intended temporary directory
    if not os.path.exists(full_path) or not os.path.isfile(full_path):
        app.logger.warning(f"File not found or invalid path requested: {full_path}")
        return "File not found", 404
    
    app.logger.info(f"Serving asset: {full_path}")
    return send_file(full_path)

if __name__ == '__main__':
    # When running locally, debug=True provides helpful error messages
    # In production (Render), Flask's debug mode should be off for security.
    # Render automatically sets the PORT environment variable.
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))

