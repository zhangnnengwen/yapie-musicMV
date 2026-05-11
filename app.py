#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from functools import wraps
from pathlib import Path
from types import SimpleNamespace

import requests
from flask import Flask, g, jsonify, redirect, render_template, request, send_from_directory, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import split_runninghub_mv as split_mv


app = Flask(__name__)
app.config["UPLOAD_FOLDER"] = "uploads"
app.config["OUTPUT_FOLDER"] = "outputs"
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024
app.secret_key = os.getenv("FLASK_SECRET_KEY", "change-this-secret-key")

API_BASE_URL = "https://www.runninghub.cn"
DEFAULT_WORKFLOW_ID = "2025258518208737281"
DEFAULT_IMAGE_NODE_ID = "343"
DEFAULT_IMAGE_FIELD_NAME = "image"
DEFAULT_AUDIO_NODE_ID = "243"
DEFAULT_AUDIO_FIELD_NAME = "audio"
USE_RUNNINGHUB_PROXY = os.getenv("RUNNINGHUB_USE_PROXY", "").lower() in {"1", "true", "yes", "on"}

if not USE_RUNNINGHUB_PROXY:
    for proxy_env_name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(proxy_env_name, None)
    os.environ["NO_PROXY"] = "www.runninghub.cn,runninghub.cn,127.0.0.1,localhost"

runninghub_session = requests.Session()
runninghub_session.trust_env = USE_RUNNINGHUB_PROXY
runninghub_session.proxies = {} if not USE_RUNNINGHUB_PROXY else runninghub_session.proxies

os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
os.makedirs(app.config["OUTPUT_FOLDER"], exist_ok=True)

split_jobs = {}
split_jobs_lock = threading.Lock()
USAGE_DB_PATH = os.path.join(app.config["OUTPUT_FOLDER"], "usage.db")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "admin")
DEFAULT_ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
DEFAULT_ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin")
ALLOW_REGISTRATION = os.getenv("ALLOW_REGISTRATION", "1").lower() in {"1", "true", "yes", "on"}


def _now_iso():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _init_usage_db():
    with sqlite3.connect(USAGE_DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS usage_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                feature TEXT NOT NULL,
                status TEXT NOT NULL,
                user_id TEXT,
                ip_address TEXT,
                user_agent TEXT,
                job_id TEXT,
                task_id TEXT,
                details TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_created_at ON usage_events(created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_feature ON usage_events(feature)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_user_id ON usage_events(user_id)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            )
            """
        )
        user_columns = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "active" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)")
        configured_admin = conn.execute(
            "SELECT id FROM users WHERE username = ? AND role = 'admin' LIMIT 1",
            (DEFAULT_ADMIN_USERNAME,),
        ).fetchone()
        admin_exists = conn.execute("SELECT 1 FROM users WHERE role = 'admin' LIMIT 1").fetchone()
        if configured_admin and "ADMIN_PASSWORD" in os.environ:
            conn.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (generate_password_hash(DEFAULT_ADMIN_PASSWORD), configured_admin[0]),
            )
        elif not admin_exists:
            conn.execute(
                """
                INSERT INTO users (username, password_hash, role, created_at)
                VALUES (?, ?, 'admin', ?)
                """,
                (DEFAULT_ADMIN_USERNAME, generate_password_hash(DEFAULT_ADMIN_PASSWORD), _now_iso()),
            )


def _client_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    return request.remote_addr or ""


def _usage_user_id():
    if getattr(g, "current_user", None):
        return g.current_user["username"]
    return (
        request.headers.get("X-User-Id")
        or request.form.get("userId")
        or request.form.get("user_id")
        or request.args.get("userId")
        or request.args.get("user_id")
        or ""
    )


def _db_row_to_dict(row):
    return dict(row) if row else None


def _get_user_by_id(user_id):
    if not user_id:
        return None
    with sqlite3.connect(USAGE_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return _db_row_to_dict(conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())


def _get_user_by_username(username):
    with sqlite3.connect(USAGE_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return _db_row_to_dict(conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone())


def _create_user(username, password, role="user"):
    username = (username or "").strip()
    if not username or not password:
        raise ValueError("请输入用户名和密码")
    if len(username) > 64:
        raise ValueError("用户名不能超过 64 个字符")
    if role not in {"user", "admin"}:
        raise ValueError("角色只能是普通用户或管理员")
    with sqlite3.connect(USAGE_DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO users (username, password_hash, role, active, created_at)
            VALUES (?, ?, ?, 1, ?)
            """,
            (username, generate_password_hash(password), role, _now_iso()),
        )


def _admin_count(exclude_user_id=None):
    with sqlite3.connect(USAGE_DB_PATH) as conn:
        if exclude_user_id is None:
            row = conn.execute("SELECT COUNT(*) FROM users WHERE role = 'admin' AND active = 1").fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) FROM users WHERE role = 'admin' AND active = 1 AND id != ?",
                (exclude_user_id,),
            ).fetchone()
    return int(row[0] or 0)


def _admin_users_data():
    with sqlite3.connect(USAGE_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(
            """
            SELECT
                u.id,
                u.username,
                u.role,
                u.active,
                u.created_at,
                COUNT(e.id) AS usage_count,
                MAX(e.created_at) AS last_seen
            FROM users u
            LEFT JOIN usage_events e ON e.user_id = u.username
            GROUP BY u.id, u.username, u.role, u.active, u.created_at
            ORDER BY u.role = 'admin' DESC, u.active DESC, u.created_at DESC
            """
        )]


@app.before_request
def _load_current_user():
    g.current_user = _get_user_by_id(session.get("user_id"))


def _wants_json():
    return request.path.startswith("/api/") or "application/json" in request.headers.get("Accept", "")


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not g.current_user:
            if _wants_json():
                return jsonify({"success": False, "message": "Login required"}), 401
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not g.current_user:
            if _wants_json():
                return jsonify({"success": False, "message": "Admin login required"}), 401
            return redirect(url_for("admin_login", next=request.path))
        if g.current_user["role"] != "admin":
            if _wants_json():
                return jsonify({"success": False, "message": "Forbidden"}), 403
            return "Forbidden", 403
        return view(*args, **kwargs)
    return wrapped


def _current_username():
    return g.current_user["username"] if getattr(g, "current_user", None) else ""


def _current_user_id():
    return g.current_user["id"] if getattr(g, "current_user", None) else None


def _can_access_task(task_id):
    if getattr(g, "current_user", None) and g.current_user["role"] == "admin":
        return True
    with sqlite3.connect(USAGE_DB_PATH) as conn:
        row = conn.execute(
            "SELECT 1 FROM usage_events WHERE task_id = ? AND user_id = ? LIMIT 1",
            (task_id, _current_username()),
        ).fetchone()
    return bool(row)


def _can_access_split_job(job):
    if not job:
        return False
    if getattr(g, "current_user", None) and g.current_user["role"] == "admin":
        return True
    return job.get("owner_user_id") == _current_user_id()


def _can_access_output(filename):
    if getattr(g, "current_user", None) and g.current_user["role"] == "admin":
        return True
    with sqlite3.connect(USAGE_DB_PATH) as conn:
        row = conn.execute(
            """
            SELECT 1
            FROM usage_events
            WHERE user_id = ? AND details LIKE ?
            LIMIT 1
            """,
            (_current_username(), f"%{filename}%"),
        ).fetchone()
    return bool(row)


def _safe_next(default_endpoint):
    next_url = request.args.get("next")
    if next_url and next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    return url_for(default_endpoint)


def _record_usage(feature, status="started", *, job_id=None, task_id=None, details=None):
    try:
        payload = details or {}
        with sqlite3.connect(USAGE_DB_PATH) as conn:
            conn.execute(
                """
                INSERT INTO usage_events (
                    created_at, feature, status, user_id, ip_address,
                    user_agent, job_id, task_id, details
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _now_iso(),
                    feature,
                    status,
                    _usage_user_id(),
                    _client_ip(),
                    request.headers.get("User-Agent", ""),
                    job_id,
                    task_id,
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
    except Exception as exc:
        app.logger.warning("Failed to record usage event: %s", exc)


def _record_job_usage(job_id, feature, status, *, details=None):
    try:
        with sqlite3.connect(USAGE_DB_PATH) as conn:
            conn.execute(
                """
                INSERT INTO usage_events (
                    created_at, feature, status, user_id, ip_address,
                    user_agent, job_id, task_id, details
                )
                SELECT ?, ?, ?, user_id, ip_address, user_agent, ?, NULL, ?
                FROM usage_events
                WHERE job_id = ?
                ORDER BY id ASC
                LIMIT 1
                """,
                (
                    _now_iso(),
                    feature,
                    status,
                    job_id,
                    json.dumps(details or {}, ensure_ascii=False),
                    job_id,
                ),
            )
    except Exception as exc:
        app.logger.warning("Failed to record job usage event: %s", exc)


def _admin_authorized():
    if getattr(g, "current_user", None) and g.current_user["role"] == "admin":
        return True
    token = request.args.get("token") or request.headers.get("X-Admin-Token")
    return bool(ADMIN_TOKEN) and token == ADMIN_TOKEN


def _usage_summary(limit=100):
    with sqlite3.connect(USAGE_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        totals = [dict(row) for row in conn.execute(
            """
            SELECT feature, status, COUNT(*) AS count
            FROM usage_events
            GROUP BY feature, status
            ORDER BY count DESC, feature ASC
            """
        )]
        users = [dict(row) for row in conn.execute(
            """
            SELECT COALESCE(NULLIF(user_id, ''), ip_address) AS user_key,
                   COUNT(*) AS count,
                   MAX(created_at) AS last_seen
            FROM usage_events
            GROUP BY user_key
            ORDER BY count DESC, last_seen DESC
            LIMIT 50
            """
        )]
        recent = [dict(row) for row in conn.execute(
            """
            SELECT id, created_at, feature, status, user_id, ip_address,
                   user_agent, job_id, task_id, details
            FROM usage_events
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )]
    return {"totals": totals, "users": users, "recent": recent}


_init_usage_db()


def _runninghub_post(path, *, json_body=None, data=None, files=None, timeout=60):
    response = runninghub_session.post(
        f"{API_BASE_URL}{path}",
        json=json_body,
        data=data,
        files=files,
        proxies={} if not USE_RUNNINGHUB_PROXY else None,
        timeout=timeout,
    )
    if response.status_code != 200:
        raise RuntimeError(f"RunningHub API 调用失败: HTTP {response.status_code} {response.text[:300]}")
    return response.json()


def _upload_runninghub_resource(api_key, file_storage, label="文件"):
    if not file_storage or file_storage.filename == "":
        raise ValueError(f"请上传{label}")

    file_storage.stream.seek(0)
    result = _runninghub_post(
        "/task/openapi/upload",
        data={"apiKey": api_key, "fileType": "input"},
        files={
            "file": (
                file_storage.filename,
                file_storage.stream,
                file_storage.mimetype or "application/octet-stream",
            )
        },
        timeout=120,
    )
    if result.get("code") != 0:
        raise RuntimeError(result.get("msg") or result.get("message") or f"{label}上传到 RunningHub 失败")

    file_name = (result.get("data") or {}).get("fileName")
    if not file_name:
        raise RuntimeError("RunningHub 上传响应缺少 data.fileName")
    return file_name


def _upsert_node_info(node_info_list, node_id, field_name, field_value):
    node_id = str(node_id)
    for item in node_info_list:
        if str(item.get("nodeId")) == node_id and item.get("fieldName") == field_name:
            item["fieldValue"] = field_value
            return node_info_list

    node_info_list.append(
        {
            "nodeId": node_id,
            "fieldName": field_name,
            "fieldValue": field_value,
        }
    )
    return node_info_list


def _normalize_runninghub_outputs(outputs):
    normalized = []
    for output in outputs or []:
        url = output.get("fileUrl") or output.get("url")
        if not url:
            continue
        normalized.append(
            {
                "url": url,
                "fileUrl": url,
                "type": output.get("fileType") or output.get("outputType"),
                "nodeId": output.get("nodeId"),
                "taskCostTime": output.get("taskCostTime"),
            }
        )
    return normalized


def _file_meta(file_storage):
    if not file_storage:
        return None
    return {
        "filename": file_storage.filename,
        "content_type": file_storage.mimetype,
        "content_length": request.content_length,
    }


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = _get_user_by_username(username)
        if user and user["active"] and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["role"] = user["role"]
            _record_usage("login", "success", details={"username": username})
            return redirect(_safe_next("home"))
        _record_usage("login", "failed", details={"username": username})
        return render_template("login.html", error="用户名或密码错误", allow_registration=ALLOW_REGISTRATION)
    return render_template("login.html", allow_registration=ALLOW_REGISTRATION)


@app.route("/register", methods=["GET", "POST"])
def register():
    if not ALLOW_REGISTRATION:
        return "Registration disabled", 403
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        if password != confirm_password:
            return render_template("register.html", error="两次输入的密码不一致")
        try:
            _create_user(username, password, role="user")
        except sqlite3.IntegrityError:
            return render_template("register.html", error="用户名已存在")
        except ValueError as exc:
            return render_template("register.html", error=str(exc))
        _record_usage("register", "success", details={"username": username})
        return redirect(url_for("login"))
    return render_template("register.html")


@app.route("/logout")
def logout():
    _record_usage("logout", "success")
    session.clear()
    return redirect(url_for("login"))


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = _get_user_by_username(username)
        if user and user["active"] and user["role"] == "admin" and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["role"] = user["role"]
            _record_usage("admin_login", "success", details={"username": username})
            return redirect(_safe_next("admin_usage_page"))
        _record_usage("admin_login", "failed", details={"username": username})
        return render_template("admin_login.html", error="管理员账号或密码错误")
    return render_template("admin_login.html")


@app.route("/")
@login_required
def home():
    _record_usage("home_page", "view")
    return render_template("home.html", current_user=g.current_user)


@app.route("/lyrics")
@login_required
def lyrics_page():
    _record_usage("lyrics_page", "view")
    return render_template("lyrics.html", current_user=g.current_user)


@app.route("/ai-video")
@login_required
def ai_video_page():
    _record_usage("ai_video_page", "view")
    return render_template("ai-video.html", current_user=g.current_user)


@app.route("/admin/usage")
@admin_required
def admin_usage_page():
    return render_template("admin_usage.html", data=_usage_summary(), token=request.args.get("token", ""), current_user=g.current_user)


@app.route("/api/admin/usage")
@admin_required
def admin_usage_api():
    return jsonify({"success": True, **_usage_summary(limit=int(request.args.get("limit", 100)))})


@app.route("/admin/users", methods=["GET", "POST"])
@admin_required
def admin_users_page():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        role = request.form.get("role", "user")
        try:
            _create_user(username, password, role=role)
            _record_usage("admin_user_manage", "created", details={"username": username, "role": role})
            return redirect(url_for("admin_users_page", message="账号已创建"))
        except sqlite3.IntegrityError:
            return redirect(url_for("admin_users_page", error="用户名已存在"))
        except ValueError as exc:
            return redirect(url_for("admin_users_page", error=str(exc)))

    return render_template(
        "admin_users.html",
        users=_admin_users_data(),
        current_user=g.current_user,
        message=request.args.get("message"),
        error=request.args.get("error"),
    )


@app.route("/admin/users/<int:user_id>/update", methods=["POST"])
@admin_required
def admin_update_user(user_id):
    user = _get_user_by_id(user_id)
    if not user:
        return redirect(url_for("admin_users_page", error="账号不存在"))

    role = request.form.get("role", user["role"])
    active = request.form.get("active") == "1"
    password = request.form.get("password", "").strip()
    if role not in {"user", "admin"}:
        return redirect(url_for("admin_users_page", error="角色不合法"))
    if user_id == _current_user_id() and (role != "admin" or not active):
        return redirect(url_for("admin_users_page", error="不能取消自己的管理员权限或禁用自己"))
    if user["role"] == "admin" and (role != "admin" or not active) and _admin_count(exclude_user_id=user_id) == 0:
        return redirect(url_for("admin_users_page", error="至少需要保留一个启用的管理员账号"))

    with sqlite3.connect(USAGE_DB_PATH) as conn:
        conn.execute(
            "UPDATE users SET role = ?, active = ? WHERE id = ?",
            (role, 1 if active else 0, user_id),
        )
        if password:
            conn.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (generate_password_hash(password), user_id),
            )
    _record_usage(
        "admin_user_manage",
        "updated",
        details={"username": user["username"], "role": role, "active": active, "password_reset": bool(password)},
    )
    return redirect(url_for("admin_users_page", message="账号已更新"))


@app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def admin_delete_user(user_id):
    user = _get_user_by_id(user_id)
    if not user:
        return redirect(url_for("admin_users_page", error="账号不存在"))
    if user_id == _current_user_id():
        return redirect(url_for("admin_users_page", error="不能删除当前登录账号"))
    if user["role"] == "admin" and _admin_count(exclude_user_id=user_id) == 0:
        return redirect(url_for("admin_users_page", error="至少需要保留一个启用的管理员账号"))

    with sqlite3.connect(USAGE_DB_PATH) as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    _record_usage("admin_user_manage", "deleted", details={"username": user["username"], "role": user["role"]})
    return redirect(url_for("admin_users_page", message="账号已删除"))


@app.route("/upload", methods=["POST"])
@login_required
def upload():
    try:
        video_file = request.files.get("video")
        lrc_file = request.files.get("lrc")
        _record_usage(
            "lyrics_overlay",
            "started",
            details={"video": _file_meta(video_file), "lrc": _file_meta(lrc_file)},
        )

        if not video_file or video_file.filename == "":
            return jsonify({"success": False, "message": "请上传视频文件"})

        if not lrc_file or lrc_file.filename == "":
            return jsonify({"success": False, "message": "请上传歌词文件"})

        task_id = str(uuid.uuid4())
        video_ext = os.path.splitext(video_file.filename)[1] or ".mp4"
        video_path = os.path.join(app.config["UPLOAD_FOLDER"], f"{task_id}_video{video_ext}")
        lrc_path = os.path.join(app.config["UPLOAD_FOLDER"], f"{task_id}_lyrics.lrc")
        output_path = os.path.join(app.config["OUTPUT_FOLDER"], f"{task_id}_output.mp4")

        video_file.save(video_path)
        lrc_file.save(lrc_path)

        cmd = [
            sys.executable,
            "add_lyrics_to_video.py",
            "-v",
            video_path,
            "-l",
            lrc_path,
            "-o",
            output_path,
            "-fs",
            str(request.form.get("fontsize", 48)),
            "-fc",
            request.form.get("fontcolor", "white"),
            "-pos",
            request.form.get("position", "bottom"),
            "-m",
            str(request.form.get("margin", 50)),
            "-td",
            str(request.form.get("titleduration", 5)),
        ]

        if request.form.get("showtitle", "on") != "on":
            cmd.append("-notitle")

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        if result.returncode == 0:
            _record_usage("lyrics_overlay", "success", job_id=task_id, details={"output": f"{task_id}_output.mp4"})
            return jsonify(
                {
                    "success": True,
                    "message": "视频生成成功",
                    "download_url": url_for("download", filename=f"{task_id}_output.mp4"),
                }
            )

        return jsonify({"success": False, "message": f"视频生成失败: {result.stderr}"})

    except Exception as exc:
        _record_usage("lyrics_overlay", "failed", details={"error": str(exc)})
        return jsonify({"success": False, "message": str(exc)})


@app.route("/download/<filename>")
@login_required
def download(filename):
    if not _can_access_output(filename):
        return "Forbidden", 403
    return send_from_directory(app.config["OUTPUT_FOLDER"], filename, as_attachment=True)


@app.route("/preview/<filename>")
@login_required
def preview(filename):
    if not _can_access_output(filename):
        return "Forbidden", 403
    return send_from_directory(app.config["OUTPUT_FOLDER"], filename, as_attachment=False)


def _set_split_job(job_id, **updates):
    with split_jobs_lock:
        job = split_jobs.setdefault(job_id, {})
        job.update(updates)


def _get_split_job(job_id):
    with split_jobs_lock:
        job = split_jobs.get(job_id)
        return dict(job) if job else None


def _parse_float_form(name, default):
    raw = request.form.get(name)
    if raw in (None, ""):
        return default
    return float(raw)


def _parse_int_form(name, default=None):
    raw = request.form.get(name)
    if raw in (None, ""):
        return default
    return int(raw)


def _save_upload(file_storage, path):
    if not file_storage or file_storage.filename == "":
        return None
    os.makedirs(os.path.dirname(path), exist_ok=True)
    file_storage.save(path)
    return path


def _save_lrc_upload_or_text(file_storage, text, path):
    if file_storage and file_storage.filename:
        return _save_upload(file_storage, path)
    if text:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as file_obj:
            file_obj.write(text)
        return path
    return None


def _run_split_video_job(job_id, options):
    try:
        _set_split_job(job_id, status="RUNNING", progress=8, message="Analyzing audio split point")

        audio_path = Path(options["audio_path"])
        image_path = Path(options["image_path"])
        lrc_path = Path(options["lrc_path"]) if options.get("lrc_path") else None
        work_dir = Path(options["work_dir"])
        output_path = Path(options["output_path"])

        split_info = split_mv.choose_split_point(
            audio_path,
            lrc_path=lrc_path,
            search_window=options["search_window"],
            min_part_seconds=options["min_part_seconds"],
            lyric_guard_seconds=options["lyric_guard_seconds"],
            max_lyric_line_seconds=options["max_lyric_line_seconds"],
            analysis_window_seconds=options["analysis_window_seconds"],
        )
        audio_duration = float(split_info["audio_duration"])
        split_seconds = float(split_info["split_seconds"])
        overlap = min(max(options["overlap_seconds"], 0.0), split_seconds, audio_duration - split_seconds)

        _set_split_job(
            job_id,
            progress=18,
            message=f"Split selected at {split_seconds:.2f}s",
            split=split_info,
        )

        part1_audio = work_dir / "part1.wav"
        part2_audio = work_dir / "part2.wav"
        part1_duration = min(audio_duration, split_seconds + overlap)
        part2_start = max(0.0, split_seconds - overlap)
        part2_duration = audio_duration - part2_start
        split_mv.cut_audio_segment(audio_path, part1_audio, 0.0, part1_duration)
        split_mv.cut_audio_segment(audio_path, part2_audio, part2_start, part2_duration)

        _set_split_job(job_id, progress=28, message="Uploading image to RunningHub")
        session = split_mv.create_session(options["use_proxy"])
        image_file_name = split_mv.upload_resource(session, options["api_key"], image_path)

        args = SimpleNamespace(
            use_proxy=options["use_proxy"],
            workflow_id=options["workflow_id"],
            image_node_id=options["image_node_id"],
            image_field_name=options["image_field_name"],
            audio_node_id=options["audio_node_id"],
            audio_field_name=options["audio_field_name"],
            retain_seconds=options["retain_seconds"],
            instance_type=options["instance_type"],
            access_password=options["access_password"],
            use_personal_queue=options["use_personal_queue"],
            poll_interval=options["poll_interval"],
            task_timeout=options["task_timeout"],
        )

        _set_split_job(job_id, progress=35, message="Running two RunningHub tasks in parallel")
        with split_mv.concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(
                    split_mv.run_one_part,
                    label="part1",
                    api_key=options["api_key"],
                    audio_path=part1_audio,
                    image_file_name=image_file_name,
                    args=args,
                    base_node_info_list=options["node_info_list"],
                    work_dir=work_dir,
                ),
                executor.submit(
                    split_mv.run_one_part,
                    label="part2",
                    api_key=options["api_key"],
                    audio_path=part2_audio,
                    image_file_name=image_file_name,
                    args=args,
                    base_node_info_list=options["node_info_list"],
                    work_dir=work_dir,
                ),
            ]
            results = []
            for index, future in enumerate(split_mv.concurrent.futures.as_completed(futures), start=1):
                results.append(future.result())
                _set_split_job(job_id, progress=35 + index * 22, message=f"RunningHub part {index} completed")

        results_by_label = {item["label"]: item for item in results}
        _set_split_job(job_id, progress=84, message="Merging videos with original audio")
        split_mv.merge_videos_with_original_audio(
            part1_video=Path(results_by_label["part1"]["video"]),
            part2_video=Path(results_by_label["part2"]["video"]),
            original_audio=audio_path,
            output_video=output_path,
            audio_duration=audio_duration,
            split_seconds=split_seconds,
            overlap_seconds=overlap,
            xfade_seconds=options["xfade_seconds"],
            transition=options["transition"],
        )

        manifest = {
            "split": split_info,
            "overlap_seconds": overlap,
            "runninghub": results_by_label,
            "final_video": str(output_path),
        }
        (work_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

        _set_split_job(
            job_id,
            status="SUCCESS",
            progress=100,
            message="Split MV generated successfully",
            preview_url=f"/preview/{output_path.name}",
            download_url=f"/download/{output_path.name}",
            manifest=str(work_dir / "manifest.json"),
        )
        _record_job_usage(
            job_id,
            "split_mv_generate",
            "success",
            details={"output": output_path.name, "split": split_info},
        )
    except Exception as exc:
        _set_split_job(job_id, status="FAILED", progress=0, message=str(exc))
        _record_job_usage(job_id, "split_mv_generate", "failed", details={"error": str(exc)})


@app.route("/api/get-app-info", methods=["GET"])
@login_required
def get_app_info():
    try:
        api_key = request.args.get("apiKey")
        webapp_id = request.args.get("webappId")
        _record_usage("runninghub_app_info", "started", details={"webappId": webapp_id})

        if not api_key or not webapp_id:
            return jsonify({"success": False, "message": "请提供 API Key 和 WebApp ID"})

        response = runninghub_session.get(
            f"{API_BASE_URL}/api/webapp/apiCallDemo",
            params={"apiKey": api_key, "webappId": webapp_id},
            proxies={} if not USE_RUNNINGHUB_PROXY else None,
            timeout=30,
        )

        if response.status_code != 200:
            return jsonify({"success": False, "message": f"API 调用失败: {response.status_code}"})

        result = response.json()
        if result.get("code") != 0:
            return jsonify({"success": False, "message": result.get("msg", "获取应用信息失败")})

        data = result.get("data", {})
        _record_usage("runninghub_app_info", "success", details={"webappId": webapp_id})
        return jsonify(
            {
                "success": True,
                "webappName": data.get("webappName"),
                "nodeInfoList": data.get("nodeInfoList", []),
                "covers": data.get("covers", []),
            }
        )

    except Exception as exc:
        _record_usage("runninghub_app_info", "failed", details={"error": str(exc)})
        return jsonify({"success": False, "message": str(exc)})


@app.route("/api/generate-video", methods=["POST"])
@login_required
def generate_video():
    try:
        api_key = request.form.get("apiKey")
        workflow_id = request.form.get("workflowId") or DEFAULT_WORKFLOW_ID
        image_node_id = request.form.get("imageNodeId") or DEFAULT_IMAGE_NODE_ID
        image_field_name = request.form.get("imageFieldName") or DEFAULT_IMAGE_FIELD_NAME
        audio_node_id = request.form.get("audioNodeId") or DEFAULT_AUDIO_NODE_ID
        audio_field_name = request.form.get("audioFieldName") or DEFAULT_AUDIO_FIELD_NAME
        node_info_list_json = request.form.get("nodeInfoList", "[]")
        retain_seconds = request.form.get("retainSeconds")
        instance_type = request.form.get("instanceType")
        access_password = request.form.get("accessPassword")
        use_personal_queue = request.form.get("usePersonalQueue")
        _record_usage(
            "single_video_generate",
            "started",
            details={
                "workflowId": workflow_id,
                "image": _file_meta(request.files.get("image") or request.files.get("file")),
                "audio": _file_meta(request.files.get("audio")),
            },
        )

        if not api_key or not workflow_id:
            return jsonify({"success": False, "message": "请提供 API Key 和 Workflow ID"})

        try:
            node_info_list = json.loads(node_info_list_json)
            if not isinstance(node_info_list, list):
                raise ValueError
        except json.JSONDecodeError:
            return jsonify({"success": False, "message": "节点参数格式错误"})
        except ValueError:
            return jsonify({"success": False, "message": "节点参数必须是数组"})

        image_file = request.files.get("image") or request.files.get("file")
        image_file_name = _upload_runninghub_resource(api_key, image_file, "图片文件")
        node_info_list = _upsert_node_info(
            node_info_list,
            image_node_id,
            image_field_name,
            image_file_name,
        )

        audio_file = request.files.get("audio")
        audio_file_name = _upload_runninghub_resource(api_key, audio_file, "音频文件")
        node_info_list = _upsert_node_info(
            node_info_list,
            audio_node_id,
            audio_field_name,
            audio_file_name,
        )

        payload = {
            "apiKey": api_key,
            "workflowId": str(workflow_id),
            "nodeInfoList": node_info_list,
        }
        if retain_seconds:
            payload["retainSeconds"] = int(retain_seconds)
        if instance_type:
            payload["instanceType"] = instance_type
        if access_password:
            payload["accessPassword"] = access_password
        if use_personal_queue:
            payload["usePersonalQueue"] = use_personal_queue.lower() in {"1", "true", "on", "yes"}

        result = _runninghub_post("/task/openapi/create", json_body=payload, timeout=60)
        if result.get("code") != 0:
            return jsonify({"success": False, "message": result.get("msg", "任务创建失败")})

        data = result.get("data", {})
        _record_usage("single_video_generate", "task_created", task_id=data.get("taskId"), details={"workflowId": workflow_id})
        return jsonify(
            {
                "success": True,
                "message": "任务创建成功",
                "taskId": data.get("taskId"),
                "taskStatus": data.get("taskStatus"),
                "netWssUrl": data.get("netWssUrl"),
                "clientId": data.get("clientId"),
                "promptTips": data.get("promptTips"),
                "uploadedImage": image_file_name,
                "uploadedAudio": audio_file_name,
            }
        )

    except Exception as exc:
        _record_usage("single_video_generate", "failed", details={"error": str(exc)})
        return jsonify({"success": False, "message": str(exc)})


@app.route("/api/mv/generate", methods=["POST"])
@app.route("/api/generate-split-video", methods=["POST"])
@login_required
def generate_split_video():
    try:
        api_key = request.form.get("apiKey")
        workflow_id = request.form.get("workflowId") or DEFAULT_WORKFLOW_ID
        image_node_id = request.form.get("imageNodeId") or DEFAULT_IMAGE_NODE_ID
        image_field_name = request.form.get("imageFieldName") or DEFAULT_IMAGE_FIELD_NAME
        audio_node_id = request.form.get("audioNodeId") or DEFAULT_AUDIO_NODE_ID
        audio_field_name = request.form.get("audioFieldName") or DEFAULT_AUDIO_FIELD_NAME
        node_info_list_json = request.form.get("nodeInfoList", "[]")

        if not api_key or not workflow_id:
            return jsonify({"success": False, "message": "Missing API Key or Workflow ID"})

        image_file = request.files.get("image") or request.files.get("file")
        audio_file = request.files.get("audio")
        lrc_file = request.files.get("lrc")
        lrc_text = request.form.get("lrcText") or request.form.get("lrc")
        lrc_required = request.path == "/api/mv/generate"
        if not image_file or image_file.filename == "":
            return jsonify({"success": False, "message": "Please upload an image"})
        if not audio_file or audio_file.filename == "":
            return jsonify({"success": False, "message": "Please upload an audio file"})
        if lrc_required and not (lrc_file and lrc_file.filename) and not lrc_text:
            return jsonify({"success": False, "message": "Please provide lrc file or lrcText"})

        try:
            node_info_list = json.loads(node_info_list_json)
            if not isinstance(node_info_list, list):
                raise ValueError
        except (json.JSONDecodeError, ValueError):
            return jsonify({"success": False, "message": "nodeInfoList must be a JSON array"})

        job_id = uuid.uuid4().hex
        image_ext = os.path.splitext(image_file.filename)[1] or ".png"
        audio_ext = os.path.splitext(audio_file.filename)[1] or ".wav"
        upload_dir = os.path.join(app.config["UPLOAD_FOLDER"], f"split_{job_id}")
        work_dir = os.path.join(app.config["OUTPUT_FOLDER"], f"split_{job_id}")
        output_path = os.path.join(app.config["OUTPUT_FOLDER"], f"{job_id}_split_output.mp4")
        image_path = os.path.join(upload_dir, f"image{image_ext}")
        audio_path = os.path.join(upload_dir, f"audio{audio_ext}")
        lrc_path = os.path.join(upload_dir, "lyrics.lrc")

        _save_upload(image_file, image_path)
        _save_upload(audio_file, audio_path)
        lrc_path = _save_lrc_upload_or_text(lrc_file, lrc_text, lrc_path)

        options = {
            "api_key": api_key,
            "workflow_id": workflow_id,
            "image_node_id": image_node_id,
            "image_field_name": image_field_name,
            "audio_node_id": audio_node_id,
            "audio_field_name": audio_field_name,
            "node_info_list": node_info_list,
            "image_path": image_path,
            "audio_path": audio_path,
            "lrc_path": lrc_path,
            "work_dir": work_dir,
            "output_path": output_path,
            "retain_seconds": _parse_int_form("retainSeconds"),
            "instance_type": request.form.get("instanceType") or None,
            "access_password": request.form.get("accessPassword") or None,
            "use_personal_queue": (request.form.get("usePersonalQueue") or "").lower() in {"1", "true", "on", "yes"},
            "use_proxy": USE_RUNNINGHUB_PROXY,
            "search_window": _parse_float_form("searchWindow", 45.0),
            "min_part_seconds": _parse_float_form("minPartSeconds", 30.0),
            "lyric_guard_seconds": _parse_float_form("lyricGuardSeconds", 1.2),
            "max_lyric_line_seconds": _parse_float_form("maxLyricLineSeconds", 8.0),
            "analysis_window_seconds": _parse_float_form("analysisWindowSeconds", 1.2),
            "overlap_seconds": _parse_float_form("overlapSeconds", 1.0),
            "xfade_seconds": _parse_float_form("xfadeSeconds", 0.5),
            "transition": request.form.get("transition") or "fade",
            "poll_interval": _parse_float_form("pollInterval", 10.0),
            "task_timeout": _parse_float_form("taskTimeout", 7200.0),
        }

        _set_split_job(
            job_id,
            status="QUEUED",
            progress=3,
            message="Split generation queued",
            created_at=time.time(),
            owner_user_id=_current_user_id(),
            owner_username=_current_username(),
        )
        _record_usage(
            "split_mv_generate",
            "started",
            job_id=job_id,
            details={
                "endpoint": request.path,
                "workflowId": workflow_id,
                "image": _file_meta(image_file),
                "audio": _file_meta(audio_file),
                "has_lrc": bool(lrc_path),
                "overlapSeconds": options["overlap_seconds"],
                "xfadeSeconds": options["xfade_seconds"],
            },
        )
        thread = threading.Thread(target=_run_split_video_job, args=(job_id, options), daemon=True)
        thread.start()

        return jsonify(
            {
                "success": True,
                "jobId": job_id,
                "message": "Split generation started",
                "statusUrl": f"/api/mv/status/{job_id}",
                "legacyStatusUrl": f"/api/split-job-status/{job_id}",
            }
        )

    except Exception as exc:
        _record_usage("split_mv_generate", "failed", details={"endpoint": request.path, "error": str(exc)})
        return jsonify({"success": False, "message": str(exc)})


@app.route("/api/mv/status/<job_id>", methods=["GET"])
@app.route("/api/split-job-status/<job_id>", methods=["GET"])
@login_required
def split_job_status(job_id):
    job = _get_split_job(job_id)
    if not job:
        return jsonify({"success": False, "message": "Split job not found"}), 404
    if not _can_access_split_job(job):
        return jsonify({"success": False, "message": "Forbidden"}), 403
    return jsonify({"success": True, **job})


@app.route("/api/task-status/<task_id>", methods=["GET"])
@login_required
def get_task_status(task_id):
    try:
        api_key = request.args.get("apiKey")

        if not api_key:
            return jsonify({"success": False, "message": "请提供 API Key"})
        if not _can_access_task(task_id):
            return jsonify({"success": False, "message": "Forbidden"}), 403

        result = _runninghub_post(
            "/task/openapi/outputs",
            json_body={"apiKey": api_key, "taskId": task_id},
            timeout=30,
        )
        code = result.get("code")

        if code == 0:
            outputs = _normalize_runninghub_outputs(result.get("data", []))
            return jsonify(
                {
                    "success": True,
                    "taskStatus": "SUCCESS",
                    "progress": 100,
                    "outputs": outputs,
                    "resultUrl": outputs[0]["url"] if outputs else None,
                }
            )

        if code == 804:
            return jsonify(
                {
                    "success": True,
                    "taskStatus": "RUNNING",
                    "progress": 50,
                    "netWssUrl": (result.get("data") or {}).get("netWssUrl"),
                }
            )

        if code == 813:
            return jsonify({"success": True, "taskStatus": "QUEUED", "progress": 20})

        if code == 805:
            failed_reason = (result.get("data") or {}).get("failedReason")
            return jsonify(
                {
                    "success": True,
                    "taskStatus": "FAILED",
                    "progress": 0,
                    "message": result.get("msg", "任务失败"),
                    "failedReason": failed_reason,
                }
            )

        return jsonify(
            {
                "success": False,
                "message": result.get("msg") or result.get("message") or "查询任务结果失败",
                "raw": result,
            }
        )

    except Exception as exc:
        return jsonify({"success": False, "message": str(exc)})


if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=5000)
