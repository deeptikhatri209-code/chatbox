from __future__ import annotations

import os
import re
import string
import uuid
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory, url_for
from flask_socketio import SocketIO, emit, join_room, leave_room
from werkzeug.utils import secure_filename

BASE = Path(__file__).resolve().parent
UPLOADS = BASE / "uploads"
UPLOADS.mkdir(exist_ok=True)

MAX_FILE_SIZE = 8 * 1024 * 1024
ALLOWED_EXTENSIONS = {
    "png", "jpg", "jpeg", "gif", "webp",
    "pdf", "txt", "doc", "docx", "zip",
    "mp3", "wav", "ogg", "webm", "m4a", "mp4",
}

POSITIVE_WORDS = {
    "good", "great", "love", "thanks", "happy",
    "nice", "awesome", "best", "cool",
}
NEGATIVE_WORDS = {
    "bad", "hate", "sad", "angry", "worst",
    "awful", "problem", "boring",
}

app = Flask(__name__)
app.config["SECRET_KEY"] = "live-chat-secret"
app.config["MAX_CONTENT_LENGTH"] = MAX_FILE_SIZE

socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="threading",
    max_http_buffer_size=MAX_FILE_SIZE,
)

users = {}
rooms = {}
tokens = {}


def current_time():
    return datetime.now(timezone.utc).strftime("%H:%M")


def people_in_room(room):
    names = []
    for sid in rooms.get(room, set()):
        if sid in users:
            names.append(users[sid]["name"])
    return sorted(names)


def get_sentiment(text):
    text = text.lower()
    text = text.translate(str.maketrans("", "", string.punctuation))
    words = set(re.findall(r"[a-z]+", text))

    pos = len(words & POSITIVE_WORDS)
    neg = len(words & NEGATIVE_WORDS)

    if pos > neg:
        return "positive"
    if neg > pos:
        return "negative"
    return "neutral"


def is_allowed_file(filename):
    if "." not in filename:
        return False
    ext = filename.rsplit(".", 1)[1].lower()
    return ext in ALLOWED_EXTENSIONS


def remove_user(sid, announce=True):
    user = users.pop(sid, None)
    if not user:
        return

    tokens.pop(user.get("token", ""), None)

    room = user["room"]
    if room in rooms:
        rooms[room].discard(sid)
        if not rooms[room]:
            del rooms[room]

    try:
        leave_room(room)
    except Exception:
        pass

    if announce:
        emit("system", {"text": f"{user['name']} left.", "time": current_time()}, to=room)
        people = people_in_room(room)
        emit("roster", {"people": people, "count": len(people)}, to=room)


@app.get("/")
def home():
    return send_from_directory(BASE, "index.html")


@app.get("/uploads/<path:filename>")
def get_uploaded_file(filename):
    return send_from_directory(UPLOADS, filename)


@app.post("/api/upload")
def upload_file():
    token = (request.form.get("token") or "").strip()
    kind = (request.form.get("kind") or "file").strip().lower()
    sid = tokens.get(token)

    if not sid or sid not in users:
        return jsonify({"error": "Join a room first."}), 401
    if "file" not in request.files:
        return jsonify({"error": "No file."}), 400

    file = request.files["file"]
    if not file or not file.filename:
        return jsonify({"error": "Empty file."}), 400

    original_name = secure_filename(file.filename) or "file"
    if kind == "voice" and "." not in original_name:
        original_name += ".webm"

    if not is_allowed_file(original_name):
        return jsonify({"error": "File type not allowed."}), 400

    ext = original_name.rsplit(".", 1)[1].lower()
    saved_name = f"{uuid.uuid4().hex}.{ext}"
    file.save(UPLOADS / saved_name)

    is_image = ext in {"png", "jpg", "jpeg", "gif", "webp"}
    is_audio = kind == "voice" or ext in {"mp3", "wav", "ogg", "webm", "m4a"}
    if kind == "voice" or (is_audio and not is_image):
        media_kind = "voice"
    elif is_image:
        media_kind = "image"
    else:
        media_kind = "file"

    user = users[sid]
    room = user["room"]
    file_url = url_for("get_uploaded_file", filename=saved_name)

    message = {
        "name": user["name"],
        "text": "",
        "time": current_time(),
        "sentiment": "neutral",
        "media": {"kind": media_kind, "url": file_url, "name": original_name},
    }

    socketio.emit("chat", {**message, "self": True}, to=sid)
    socketio.emit("chat", {**message, "self": False}, to=room, skip_sid=sid)
    return jsonify({"ok": True})


@socketio.on("join")
def on_join(data):
    name = (data.get("name") or "").strip()[:24]
    room = (data.get("room") or "").strip().lower()[:24]

    if not name or not room:
        emit("error", {"message": "Name and room required."})
        return

    sid = request.sid

    old_token = None
    if sid in users:
        old_token = users[sid].get("token")
        remove_user(sid, announce=False)

    token = old_token or uuid.uuid4().hex
    users[sid] = {"name": name, "room": room, "token": token}
    tokens[token] = sid
    rooms.setdefault(room, set()).add(sid)
    join_room(room)

    people = people_in_room(room)

    emit("joined", {
        "name": name,
        "room": room,
        "people": people,
        "count": len(people),
        "token": token,
    })

    emit("system", {"text": f"{name} is online.", "time": current_time()}, to=room, include_self=False)
    emit("roster", {"people": people, "count": len(people)}, to=room)


@socketio.on("message")
def on_message(data):
    sid = request.sid
    if sid not in users:
        emit("error", {"message": "Reconnecting… try again."})
        return

    text = (data.get("text") or "").strip()
    if not text or len(text) > 500:
        return

    user = users[sid]
    message = {
        "name": user["name"],
        "text": text,
        "time": current_time(),
        "sentiment": get_sentiment(text),
    }

    emit("chat", {**message, "self": True})
    emit("chat", {**message, "self": False}, to=user["room"], include_self=False)


@socketio.on("typing")
def on_typing(data):
    sid = request.sid
    if sid not in users:
        return

    user = users[sid]
    emit(
        "typing",
        {"name": user["name"], "active": bool(data.get("active"))},
        to=user["room"],
        include_self=False,
    )


@socketio.on("disconnect")
def on_disconnect():
    remove_user(request.sid, announce=True)


if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "5000"))
    print(f"Chat running → http://127.0.0.1:{port}")
    socketio.run(
        app,
        host=host,
        port=port,
        debug=False,
        use_reloader=False,
        allow_unsafe_werkzeug=True,
    )
