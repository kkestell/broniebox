import atexit
import os
import json
import time
import threading
import subprocess
import random
from pathlib import Path

import RPi.GPIO as GPIO
from flask import (
    Flask,
    request,
    render_template,
    redirect,
    url_for,
    flash,
    send_from_directory
)
from flask_socketio import SocketIO
from werkzeug.utils import secure_filename
from mfrc522 import SimpleMFRC522
import simpleaudio as sa

# ------------------------------------------------
# Constants
# ------------------------------------------------
LED_PIN: int = 17
UPLOAD_FOLDER = Path("media")
MAPPING_FILE = Path("tag_mappings.json")
ALLOWED_EXTENSIONS = {"mp3", "wav", "ogg"}

# ------------------------------------------------
# LED Control Helpers
# ------------------------------------------------
def led_setup() -> None:
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(LED_PIN, GPIO.OUT, initial=GPIO.LOW)

def led_on() -> None:
    GPIO.output(LED_PIN, GPIO.HIGH)

def led_off() -> None:
    GPIO.output(LED_PIN, GPIO.LOW)

def led_flash_scanning() -> None:
    # Rapid flash for scanning, e.g., 5 quick blinks
    for _ in range(5):
        GPIO.output(LED_PIN, GPIO.HIGH)
        time.sleep(0.1)
        GPIO.output(LED_PIN, GPIO.LOW)
        time.sleep(0.1)

def led_flash_error() -> None:
    # Flash LED three times quickly to indicate error
    for _ in range(3):
        GPIO.output(LED_PIN, GPIO.HIGH)
        time.sleep(0.2)
        GPIO.output(LED_PIN, GPIO.LOW)
        time.sleep(0.2)

# ------------------------------------------------
# Flask / SocketIO App Setup
# ------------------------------------------------
app = Flask(__name__)
app.secret_key = "BX-12"
socketio = SocketIO(app, cors_allowed_origins="*")

tag_mapping: dict = {}
playback_thread = None
stop_playback = threading.Event()
current_track = None

sounds = {
    "scanning": sa.WaveObject.from_wave_file("sfx/scanning.wav"),
    "registered": sa.WaveObject.from_wave_file("sfx/registered.wav"),
    "play": sa.WaveObject.from_wave_file("sfx/play.wav"),
    "error": sa.WaveObject.from_wave_file("sfx/error.wav"),
    "boot": sa.WaveObject.from_wave_file("sfx/boot.wav"),
    "delete": sa.WaveObject.from_wave_file("sfx/delete.wav")
}

UPLOAD_FOLDER.mkdir(exist_ok=True)

@app.route("/")
def index():
    audio_files = sorted(
        f for f in os.listdir(UPLOAD_FOLDER)
        if any(f.endswith(ext) for ext in ALLOWED_EXTENSIONS)
    )
    return render_template(
        "index.html",
        mappings=tag_mapping,
        audio_files=audio_files,
        current_track=current_track,
        volume=_get_volume()
    )

@app.route("/upload", methods=["POST"])
def upload_file():
    if "file" not in request.files:
        flash("No file part", "danger")
        return redirect(url_for("index"))

    file = request.files["file"]
    if file.filename == "":
        flash("No selected file", "danger")
        return redirect(url_for("index"))

    if file and _allowed_file(file.filename):
        filename = secure_filename(file.filename)
        destination = UPLOAD_FOLDER / filename
        file.save(str(destination))
        flash("File uploaded successfully", "success")
    else:
        flash("Invalid file type", "danger")

    audio_files = sorted(
        f for f in os.listdir(UPLOAD_FOLDER)
        if any(f.endswith(ext) for ext in ALLOWED_EXTENSIONS)
    )
    socketio.emit(
        "refresh_data",
        {
            "mappings": tag_mapping,
            "audio_files": audio_files
        },
        to=None
    )
    return redirect(url_for("index"))

@app.route("/media/<filename>")
def serve_audio(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)

@socketio.on("register_tag")
def handle_register_tag(data):
    audio_file = data.get("audio_file")
    if not audio_file:
        return {"status": "error", "message": "No audio file selected"}
    try:
        _stop_playback_thread()
        # Flash LED to indicate scanning in progress
        led_flash_scanning()
        scanning = sounds["scanning"].play()

        reader = SimpleMFRC522()
        tag_id = str(reader.read_id())
        if scanning.is_playing():
            scanning.stop()

        sounds["registered"].play()
        tag_mapping[tag_id] = audio_file
        _save_mappings_to_file()
        _start_playback_thread(tag_mapping)
        return {
            "status": "success",
            "message": f"Tag {tag_id} registered successfully",
            "mappings": tag_mapping
        }
    except Exception as e:
        led_flash_error()  # Flash LED on error
        sounds["error"].play()
        return {"status": "error", "message": f"Error reading tag: {str(e)}"}
    finally:
        # Do not cleanup GPIO here so the LED remains controllable.
        pass

@socketio.on("unregister_tag")
def handle_unregister_tag(data):
    tag_id = data.get("tag_id")
    if not tag_id:
        return {"status": "error", "message": "No tag specified"}
    if tag_id in tag_mapping:
        del tag_mapping[tag_id]
        _save_mappings_to_file()
        sounds["delete"].play()
        return {
            "status": "success",
            "message": "Tag unregistered successfully",
            "mappings": tag_mapping
        }
    return {"status": "error", "message": "Tag not found"}

@socketio.on("delete_file")
def handle_delete_file(data):
    filename = data.get("filename")
    if not filename:
        return {"status": "error", "message": "No filename specified"}
    try:
        os.remove(UPLOAD_FOLDER / filename)
        global tag_mapping
        tag_mapping = {
            tag: audio
            for tag, audio in tag_mapping.items()
            if audio != filename
        }
        _save_mappings_to_file()
        audio_files = sorted(
            f for f in os.listdir(UPLOAD_FOLDER)
            if any(f.endswith(ext) for ext in ALLOWED_EXTENSIONS)
        )
        sounds["delete"].play()
        return {
            "status": "success",
            "message": "File deleted successfully",
            "mappings": tag_mapping,
            "audio_files": audio_files
        }
    except Exception as e:
        led_flash_error()
        return {"status": "error", "message": f"Error deleting file: {str(e)}"}

@socketio.on("stop_playback")
def handle_stop_playback():
    global current_track
    subprocess.run(["pkill", "-9", "mpg123"], check=False)
    current_track = None
    led_off()  # Turn LED off when stopped
    socketio.emit("track_update", {"current_track": None})
    return {
        "status": "success",
        "message": "Playback stopped"
    }

@socketio.on("set_volume")
def handle_set_volume(data):
    level = int(data.get("level", 80))
    actual_level = _set_volume(level)
    socketio.emit("volume_update", {"volume": actual_level})
    return {
        "status": "success",
        "message": f"Volume set to {actual_level}%",
        "volume": actual_level
    }

def _allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def _load_mappings_from_file() -> None:
    global tag_mapping
    try:
        with MAPPING_FILE.open("r") as f:
            tag_mapping = json.load(f)
    except FileNotFoundError:
        tag_mapping = {}

def _save_mappings_to_file() -> None:
    with MAPPING_FILE.open("w") as f:
        json.dump(tag_mapping, f, indent=2)

def _monitor_playback(process: subprocess.Popen, filename: str) -> None:
    global current_track
    process.wait()
    current_track = None
    # Turn LED off after playback finishes
    led_off()
    socketio.emit("track_update", {"current_track": None})

def _start_playback_thread(mappings: dict) -> None:
    global playback_thread, stop_playback
    stop_playback.clear()
    playback_thread = threading.Thread(target=_player, args=(mappings,), daemon=True)
    playback_thread.start()

def _stop_playback_thread() -> None:
    global playback_thread
    if playback_thread and playback_thread.is_alive():
        subprocess.run(["pkill", "-9", "mpg123"], check=False)
        stop_playback.set()
        playback_thread.join(timeout=2)

def _set_volume(level: int) -> int:
    level = max(0, min(100, level))
    # Remap volume to the specific mixer range as needed
    remapped_level = 25 + ((level - 1) * (90 - 25) / 99) if level > 0 else 0
    os.system(f"amixer set PCM {remapped_level}%")
    return level

def _get_volume() -> int:
    output = subprocess.check_output(
        "amixer get PCM | grep -o '[0-9]*%' | head -1",
        shell=True
    ).decode()
    return int(output.strip().replace('%', ''))

def _player(local_mapping: dict) -> None:
    global current_track
    time.sleep(5)  # Initial delay before scanning starts
    reader = SimpleMFRC522()
    current_process = None
    current_file = None
    monitor_thread = None

    try:
        while not stop_playback.is_set():
            tag_id = reader.read_id_no_block()
            if not tag_id:
                time.sleep(0.1)
                continue

            filename = local_mapping.get(str(tag_id))
            if not filename:
                led_flash_error()
                sounds["error"].play()
                current_track = None
                socketio.emit("track_update", {"current_track": None})
                time.sleep(1)
                continue

            # If the same file is already playing, skip
            if filename == current_file:
                time.sleep(0.1)
                continue

            # Stop current process if playing
            if current_process and current_process.poll() is None:
                current_process.terminate()
                try:
                    current_process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    current_process.kill()

            if monitor_thread and monitor_thread.is_alive():
                monitor_thread.join(timeout=1)

            sounds["play"].play()
            current_file = filename
            current_track = Path(filename).stem
            socketio.emit("track_update", {"current_track": current_track})

            # Turn LED on while playing
            led_on()

            current_process = subprocess.Popen(
                ["mpg123", str(UPLOAD_FOLDER / filename)]
            )
            monitor_thread = threading.Thread(
                target=_monitor_playback,
                args=(current_process, filename),
                daemon=True
            )
            monitor_thread.start()

            time.sleep(0.1)
    finally:
        if current_process and current_process.poll() is None:
            current_process.terminate()
            try:
                current_process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                current_process.kill()
        if monitor_thread and monitor_thread.is_alive():
            monitor_thread.join(timeout=1)
        current_track = None
        led_off()
        socketio.emit("track_update", {"current_track": None})

def main() -> None:
    GPIO.setwarnings(False)
    led_setup()
    _load_mappings_from_file()
    _start_playback_thread(tag_mapping)
    _set_volume(50)
    sounds["boot"].play()

    # Ensure GPIO is cleaned up at program exit
    atexit.register(GPIO.cleanup)

    socketio.run(app, host="0.0.0.0", port=5000, debug=False, allow_unsafe_werkzeug=True)

if __name__ == "__main__":
    main()
