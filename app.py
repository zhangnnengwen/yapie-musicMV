#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import os
import subprocess
import sys
import uuid

import requests
from flask import Flask, jsonify, render_template, request, send_from_directory, url_for


app = Flask(__name__)
app.config["UPLOAD_FOLDER"] = "uploads"
app.config["OUTPUT_FOLDER"] = "outputs"
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024

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


@app.route("/")
def home():
    return render_template("home.html")


@app.route("/lyrics")
def lyrics_page():
    return render_template("lyrics.html")


@app.route("/ai-video")
def ai_video_page():
    return render_template("ai-video.html")


@app.route("/upload", methods=["POST"])
def upload():
    try:
        video_file = request.files.get("video")
        lrc_file = request.files.get("lrc")

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
            return jsonify(
                {
                    "success": True,
                    "message": "视频生成成功",
                    "download_url": url_for("download", filename=f"{task_id}_output.mp4"),
                }
            )

        return jsonify({"success": False, "message": f"视频生成失败: {result.stderr}"})

    except Exception as exc:
        return jsonify({"success": False, "message": str(exc)})


@app.route("/download/<filename>")
def download(filename):
    return send_from_directory(app.config["OUTPUT_FOLDER"], filename, as_attachment=True)


@app.route("/api/get-app-info", methods=["GET"])
def get_app_info():
    try:
        api_key = request.args.get("apiKey")
        webapp_id = request.args.get("webappId")

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
        return jsonify(
            {
                "success": True,
                "webappName": data.get("webappName"),
                "nodeInfoList": data.get("nodeInfoList", []),
                "covers": data.get("covers", []),
            }
        )

    except Exception as exc:
        return jsonify({"success": False, "message": str(exc)})


@app.route("/api/generate-video", methods=["POST"])
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
        return jsonify({"success": False, "message": str(exc)})


@app.route("/api/task-status/<task_id>", methods=["GET"])
def get_task_status(task_id):
    try:
        api_key = request.args.get("apiKey")

        if not api_key:
            return jsonify({"success": False, "message": "请提供 API Key"})

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
