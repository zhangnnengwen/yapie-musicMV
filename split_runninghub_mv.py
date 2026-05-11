#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Split a long music audio file near a no-lyric / low-energy point, run two
RunningHub jobs in parallel, then merge the generated videos with the original
full audio.

Example:
  python split_runninghub_mv.py ^
    --api-key YOUR_KEY ^
    --image uploads/cover.png ^
    --audio uploads/song.wav ^
    --lrc mc/song.lrc ^
    --output outputs/final_mv.mp4
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import mimetypes
import os
import re
import subprocess
import sys
import time
import uuid
from array import array
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests


API_BASE_URL = "https://www.runninghub.cn"
DEFAULT_WORKFLOW_ID = "2025258518208737281"
DEFAULT_IMAGE_NODE_ID = "343"
DEFAULT_IMAGE_FIELD_NAME = "image"
DEFAULT_AUDIO_NODE_ID = "243"
DEFAULT_AUDIO_FIELD_NAME = "audio"


class CommandError(RuntimeError):
    pass


def log(message: str) -> None:
    print(message, flush=True)


def run_command(cmd: list[str], *, capture_stdout: bool = False) -> subprocess.CompletedProcess:
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE if capture_stdout else None,
        stderr=subprocess.PIPE,
        text=False,
    )
    if result.returncode != 0:
        stderr = (result.stderr or b"").decode("utf-8", errors="replace")
        raise CommandError(f"Command failed: {' '.join(cmd)}\n{stderr}")
    return result


def ffprobe_duration(path: Path) -> float:
    result = run_command(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_stdout=True,
    )
    raw = (result.stdout or b"").decode("utf-8", errors="replace").strip()
    return float(raw)


def ffprobe_video_info(path: Path) -> tuple[int, int, float]:
    result = run_command(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,r_frame_rate",
            "-of",
            "json",
            str(path),
        ],
        capture_stdout=True,
    )
    data = json.loads((result.stdout or b"{}").decode("utf-8", errors="replace"))
    stream = data["streams"][0]
    width = int(stream["width"])
    height = int(stream["height"])
    rate = stream.get("r_frame_rate") or "24/1"
    if "/" in rate:
        numerator, denominator = rate.split("/", 1)
        fps = float(numerator) / float(denominator)
    else:
        fps = float(rate)
    return width - width % 2, height - height % 2, fps or 24.0


def parse_lrc(path: Path) -> list[tuple[float, str]]:
    time_pattern = re.compile(r"\[(\d{2}):(\d{2})\.(\d{2,3})\]")
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        text = path.read_text(encoding="gbk")

    lyrics: list[tuple[float, str]] = []
    for line in text.splitlines():
        matches = time_pattern.findall(line)
        if not matches:
            continue
        lyric_text = time_pattern.sub("", line).strip()
        if not lyric_text:
            continue
        for minute, second, ms in matches:
            milliseconds = int(ms) * 10 if len(ms) == 2 else int(ms)
            start = int(minute) * 60 + int(second) + milliseconds / 1000
            lyrics.append((start, lyric_text))
    return sorted(lyrics, key=lambda item: item[0])


def build_lyric_ranges(
    lyrics: list[tuple[float, str]],
    audio_duration: float,
    *,
    guard_seconds: float,
    max_line_seconds: float,
) -> list[tuple[float, float]]:
    ranges: list[tuple[float, float]] = []
    for index, (start, text) in enumerate(lyrics):
        next_start = lyrics[index + 1][0] if index + 1 < len(lyrics) else audio_duration
        estimated = min(next_start, start + max(max_line_seconds, len(text) * 0.28))
        begin = max(0.0, start - guard_seconds)
        end = min(audio_duration, estimated + guard_seconds)
        if end > begin:
            ranges.append((begin, end))
    return ranges


def lyric_penalty(t: float, ranges: list[tuple[float, float]]) -> float:
    if not ranges:
        return 0.0

    nearest = float("inf")
    for begin, end in ranges:
        if begin <= t <= end:
            return 65.0
        nearest = min(nearest, abs(t - begin), abs(t - end))

    if nearest < 1.0:
        return 20.0
    if nearest < 2.0:
        return 8.0
    return 0.0


def decode_audio_pcm(path: Path, sample_rate: int = 16000) -> array:
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-f",
            "s16le",
            "-",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        stderr = (result.stderr or b"").decode("utf-8", errors="replace")
        raise CommandError(f"Failed to decode audio with ffmpeg:\n{stderr}")

    samples = array("h")
    samples.frombytes(result.stdout)
    if sys.byteorder != "little":
        samples.byteswap()
    return samples


def rms_db(samples: array, start_index: int, end_index: int) -> float:
    start_index = max(0, start_index)
    end_index = min(len(samples), end_index)
    if end_index <= start_index:
        return 0.0

    total = 0
    count = end_index - start_index
    for value in samples[start_index:end_index]:
        total += value * value

    rms = math.sqrt(total / count)
    return 20.0 * math.log10(max(rms / 32768.0, 1e-9))


def choose_split_point(
    audio_path: Path,
    *,
    lrc_path: Path | None,
    search_window: float,
    min_part_seconds: float,
    lyric_guard_seconds: float,
    max_lyric_line_seconds: float,
    analysis_window_seconds: float,
) -> dict[str, Any]:
    duration = ffprobe_duration(audio_path)
    midpoint = duration / 2.0
    lower = max(min_part_seconds, midpoint - search_window)
    upper = min(duration - min_part_seconds, midpoint + search_window)
    if lower >= upper:
        lower = max(1.0, duration * 0.25)
        upper = min(duration - 1.0, duration * 0.75)
    if lower >= upper:
        raise RuntimeError("Audio is too short to split safely.")

    lyric_ranges: list[tuple[float, float]] = []
    if lrc_path:
        lyrics = parse_lrc(lrc_path)
        lyric_ranges = build_lyric_ranges(
            lyrics,
            duration,
            guard_seconds=lyric_guard_seconds,
            max_line_seconds=max_lyric_line_seconds,
        )

    sample_rate = 16000
    samples = decode_audio_pcm(audio_path, sample_rate=sample_rate)
    half_window = analysis_window_seconds / 2.0
    best: dict[str, Any] | None = None
    t = lower
    while t <= upper:
        start = int((t - half_window) * sample_rate)
        end = int((t + half_window) * sample_rate)
        db = rms_db(samples, start, end)
        penalty = lyric_penalty(t, lyric_ranges)
        distance_penalty = abs(t - midpoint) * 0.08
        score = db + penalty + distance_penalty
        candidate = {
            "split_seconds": round(t, 3),
            "energy_db": round(db, 2),
            "lyric_penalty": round(penalty, 2),
            "distance_from_midpoint": round(abs(t - midpoint), 3),
            "score": round(score, 3),
        }
        if best is None or candidate["score"] < best["score"]:
            best = candidate
        t += 0.25

    assert best is not None
    best["audio_duration"] = round(duration, 3)
    best["search_range"] = [round(lower, 3), round(upper, 3)]
    best["used_lrc"] = bool(lrc_path)
    return best


def cut_audio_segment(input_audio: Path, output_audio: Path, start: float, duration: float) -> None:
    output_audio.parent.mkdir(parents=True, exist_ok=True)
    run_command(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{start:.3f}",
            "-i",
            str(input_audio),
            "-t",
            f"{duration:.3f}",
            "-vn",
            "-c:a",
            "pcm_s16le",
            str(output_audio),
        ]
    )


def post_json(session: requests.Session, path: str, payload: dict[str, Any], timeout: int = 60) -> dict[str, Any]:
    response = session.post(f"{API_BASE_URL}{path}", json=payload, timeout=timeout)
    if response.status_code != 200:
        raise RuntimeError(f"RunningHub HTTP {response.status_code}: {response.text[:300]}")
    return response.json()


def post_multipart(
    session: requests.Session,
    path: str,
    *,
    data: dict[str, Any],
    files: dict[str, Any],
    timeout: int = 120,
) -> dict[str, Any]:
    response = session.post(f"{API_BASE_URL}{path}", data=data, files=files, timeout=timeout)
    if response.status_code != 200:
        raise RuntimeError(f"RunningHub HTTP {response.status_code}: {response.text[:300]}")
    return response.json()


def create_session(use_proxy: bool) -> requests.Session:
    if not use_proxy:
        for proxy_env_name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            os.environ.pop(proxy_env_name, None)
        os.environ["NO_PROXY"] = "www.runninghub.cn,runninghub.cn,127.0.0.1,localhost"

    session = requests.Session()
    session.trust_env = use_proxy
    if not use_proxy:
        session.proxies = {}
    return session


def upload_resource(session: requests.Session, api_key: str, path: Path) -> str:
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    with path.open("rb") as file_obj:
        result = post_multipart(
            session,
            "/task/openapi/upload",
            data={"apiKey": api_key, "fileType": "input"},
            files={"file": (path.name, file_obj, content_type)},
            timeout=180,
        )
    if result.get("code") != 0:
        raise RuntimeError(result.get("msg") or result.get("message") or f"Upload failed: {path}")
    file_name = (result.get("data") or {}).get("fileName")
    if not file_name:
        raise RuntimeError(f"Upload response missing data.fileName: {result}")
    return file_name


def upsert_node_info(node_info_list: list[dict[str, Any]], node_id: str, field_name: str, field_value: str) -> None:
    node_id = str(node_id)
    for item in node_info_list:
        if str(item.get("nodeId")) == node_id and item.get("fieldName") == field_name:
            item["fieldValue"] = field_value
            return
    node_info_list.append({"nodeId": node_id, "fieldName": field_name, "fieldValue": field_value})


def create_runninghub_task(
    session: requests.Session,
    *,
    api_key: str,
    workflow_id: str,
    image_file_name: str,
    audio_file_name: str,
    image_node_id: str,
    image_field_name: str,
    audio_node_id: str,
    audio_field_name: str,
    base_node_info_list: list[dict[str, Any]],
    retain_seconds: int | None,
    instance_type: str | None,
    access_password: str | None,
    use_personal_queue: bool,
) -> str:
    node_info_list = json.loads(json.dumps(base_node_info_list, ensure_ascii=False))
    upsert_node_info(node_info_list, image_node_id, image_field_name, image_file_name)
    upsert_node_info(node_info_list, audio_node_id, audio_field_name, audio_file_name)
    payload: dict[str, Any] = {
        "apiKey": api_key,
        "workflowId": str(workflow_id),
        "nodeInfoList": node_info_list,
    }
    if retain_seconds is not None:
        payload["retainSeconds"] = retain_seconds
    if instance_type:
        payload["instanceType"] = instance_type
    if access_password:
        payload["accessPassword"] = access_password
    if use_personal_queue:
        payload["usePersonalQueue"] = True

    result = post_json(session, "/task/openapi/create", payload, timeout=60)
    if result.get("code") != 0:
        raise RuntimeError(result.get("msg") or result.get("message") or f"Create task failed: {result}")
    task_id = (result.get("data") or {}).get("taskId")
    if not task_id:
        raise RuntimeError(f"Create task response missing taskId: {result}")
    return str(task_id)


def normalize_outputs(outputs: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for output in outputs or []:
        url = output.get("fileUrl") or output.get("url")
        if not url:
            continue
        normalized.append(
            {
                "url": url,
                "type": output.get("fileType") or output.get("outputType"),
                "nodeId": output.get("nodeId"),
                "taskCostTime": output.get("taskCostTime"),
            }
        )
    return normalized


def wait_for_outputs(
    session: requests.Session,
    *,
    api_key: str,
    task_id: str,
    poll_interval: float,
    timeout_seconds: float,
) -> list[dict[str, Any]]:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        result = post_json(
            session,
            "/task/openapi/outputs",
            {"apiKey": api_key, "taskId": task_id},
            timeout=30,
        )
        code = result.get("code")
        if code == 0:
            outputs = normalize_outputs(result.get("data", []))
            if outputs:
                return outputs
            raise RuntimeError(f"Task {task_id} succeeded but returned no output.")
        if code == 805:
            failed_reason = (result.get("data") or {}).get("failedReason")
            raise RuntimeError(f"Task {task_id} failed: {result.get('msg')} {failed_reason or ''}")
        if code in {804, 813}:
            status = "RUNNING" if code == 804 else "QUEUED"
            log(f"[{task_id}] {status}, polling again in {poll_interval:g}s")
            time.sleep(poll_interval)
            continue
        raise RuntimeError(f"Unexpected RunningHub output response for {task_id}: {result}")
    raise TimeoutError(f"Timed out waiting for task {task_id}")


def download_file(session: requests.Session, url: str, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with session.get(url, stream=True, timeout=120) as response:
        response.raise_for_status()
        with output_path.open("wb") as file_obj:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    file_obj.write(chunk)


def extension_from_url(url: str, fallback: str = ".mp4") -> str:
    suffix = Path(urlparse(url).path).suffix
    return suffix if suffix else fallback


def run_one_part(
    *,
    label: str,
    api_key: str,
    audio_path: Path,
    image_file_name: str,
    args: argparse.Namespace,
    base_node_info_list: list[dict[str, Any]],
    work_dir: Path,
) -> dict[str, Any]:
    session = create_session(args.use_proxy)
    log(f"[{label}] Uploading audio: {audio_path.name}")
    audio_file_name = upload_resource(session, api_key, audio_path)
    log(f"[{label}] Creating RunningHub task")
    task_id = create_runninghub_task(
        session,
        api_key=api_key,
        workflow_id=args.workflow_id,
        image_file_name=image_file_name,
        audio_file_name=audio_file_name,
        image_node_id=args.image_node_id,
        image_field_name=args.image_field_name,
        audio_node_id=args.audio_node_id,
        audio_field_name=args.audio_field_name,
        base_node_info_list=base_node_info_list,
        retain_seconds=args.retain_seconds,
        instance_type=args.instance_type,
        access_password=args.access_password,
        use_personal_queue=args.use_personal_queue,
    )
    outputs = wait_for_outputs(
        session,
        api_key=api_key,
        task_id=task_id,
        poll_interval=args.poll_interval,
        timeout_seconds=args.task_timeout,
    )
    url = outputs[0]["url"]
    output_video = work_dir / f"{label}_runninghub{extension_from_url(url)}"
    log(f"[{label}] Downloading output video")
    download_file(session, url, output_video)
    return {
        "label": label,
        "task_id": task_id,
        "uploaded_audio": audio_file_name,
        "outputs": outputs,
        "video": str(output_video),
    }


def merge_videos_with_original_audio(
    *,
    part1_video: Path,
    part2_video: Path,
    original_audio: Path,
    output_video: Path,
    audio_duration: float,
    split_seconds: float,
    overlap_seconds: float,
    xfade_seconds: float,
    transition: str,
) -> None:
    width, height, fps = ffprobe_video_info(part1_video)
    fps_text = f"{fps:.3f}".rstrip("0").rstrip(".")
    video_norm = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
        f"fps={fps_text},setsar=1,format=yuv420p"
    )

    xfade_seconds = max(0.0, min(xfade_seconds, overlap_seconds * 2.0, split_seconds * 0.5))
    output_video.parent.mkdir(parents=True, exist_ok=True)

    if xfade_seconds > 0:
        part1_duration = split_seconds + xfade_seconds / 2.0
        part2_start = max(0.0, overlap_seconds - xfade_seconds / 2.0)
        part2_duration = audio_duration - split_seconds + xfade_seconds / 2.0
        offset = part1_duration - xfade_seconds
        filter_complex = (
            f"[0:v]trim=start=0:duration={part1_duration:.3f},setpts=PTS-STARTPTS,{video_norm}[v0];"
            f"[1:v]trim=start={part2_start:.3f}:duration={part2_duration:.3f},setpts=PTS-STARTPTS,{video_norm}[v1];"
            f"[v0][v1]xfade=transition={transition}:duration={xfade_seconds:.3f}:offset={offset:.3f}[v];"
            f"[2:a]atrim=start=0:duration={audio_duration:.3f},asetpts=PTS-STARTPTS[a]"
        )
    else:
        part2_start = overlap_seconds
        part2_duration = audio_duration - split_seconds
        filter_complex = (
            f"[0:v]trim=start=0:duration={split_seconds:.3f},setpts=PTS-STARTPTS,{video_norm}[v0];"
            f"[1:v]trim=start={part2_start:.3f}:duration={part2_duration:.3f},setpts=PTS-STARTPTS,{video_norm}[v1];"
            f"[v0][v1]concat=n=2:v=1:a=0[v];"
            f"[2:a]atrim=start=0:duration={audio_duration:.3f},asetpts=PTS-STARTPTS[a]"
        )

    run_command(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(part1_video),
            "-i",
            str(part2_video),
            "-i",
            str(original_audio),
            "-filter_complex",
            filter_complex,
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "20",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-shortest",
            "-movflags",
            "+faststart",
            str(output_video),
        ]
    )


def load_node_info(args: argparse.Namespace) -> list[dict[str, Any]]:
    if args.node_info_file:
        text = Path(args.node_info_file).read_text(encoding="utf-8")
    else:
        text = args.node_info_list
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("nodeInfoList must be a JSON array.")
    return data


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Split audio, run two RunningHub jobs in parallel, and merge videos with the original audio.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--api-key", default=os.getenv("RUNNINGHUB_API_KEY"), help="RunningHub API key.")
    parser.add_argument("--image", required=True, type=Path, help="Reference image input for RunningHub.")
    parser.add_argument("--audio", required=True, type=Path, help="Full original audio.")
    parser.add_argument("--lrc", type=Path, help="Optional LRC file used to avoid lyric lines near the split.")
    parser.add_argument("--output", type=Path, default=Path("outputs/final_split_mv.mp4"), help="Final merged MP4.")
    parser.add_argument("--work-dir", type=Path, help="Directory for temporary segments and downloaded videos.")
    parser.add_argument("--workflow-id", default=DEFAULT_WORKFLOW_ID)
    parser.add_argument("--image-node-id", default=DEFAULT_IMAGE_NODE_ID)
    parser.add_argument("--image-field-name", default=DEFAULT_IMAGE_FIELD_NAME)
    parser.add_argument("--audio-node-id", default=DEFAULT_AUDIO_NODE_ID)
    parser.add_argument("--audio-field-name", default=DEFAULT_AUDIO_FIELD_NAME)
    parser.add_argument("--node-info-list", default="[]", help="Base RunningHub nodeInfoList JSON.")
    parser.add_argument("--node-info-file", help="Path to a JSON file containing base nodeInfoList.")
    parser.add_argument("--retain-seconds", type=int)
    parser.add_argument("--instance-type")
    parser.add_argument("--access-password")
    parser.add_argument("--use-personal-queue", action="store_true")
    parser.add_argument("--use-proxy", action="store_true", help="Respect HTTP(S)_PROXY environment variables.")
    parser.add_argument("--search-window", type=float, default=45.0, help="Seconds to search on each side of midpoint.")
    parser.add_argument("--min-part-seconds", type=float, default=30.0, help="Minimum duration for each audio part.")
    parser.add_argument("--lyric-guard-seconds", type=float, default=1.2)
    parser.add_argument("--max-lyric-line-seconds", type=float, default=8.0)
    parser.add_argument("--analysis-window-seconds", type=float, default=1.2)
    parser.add_argument("--overlap-seconds", type=float, default=1.0, help="Audio overlap added to both generated parts.")
    parser.add_argument("--xfade-seconds", type=float, default=0.5, help="Final video crossfade duration. Use 0 for hard cut.")
    parser.add_argument("--transition", default="fade", help="FFmpeg xfade transition name.")
    parser.add_argument("--poll-interval", type=float, default=10.0)
    parser.add_argument("--task-timeout", type=float, default=7200.0)
    parser.add_argument("--dry-run", action="store_true", help="Only detect split and cut audio; skip RunningHub and merge.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.api_key and not args.dry_run:
        raise SystemExit("Missing --api-key or RUNNINGHUB_API_KEY.")
    if not args.image.exists():
        raise SystemExit(f"Image not found: {args.image}")
    if not args.audio.exists():
        raise SystemExit(f"Audio not found: {args.audio}")
    if args.lrc and not args.lrc.exists():
        raise SystemExit(f"LRC not found: {args.lrc}")

    run_id = uuid.uuid4().hex[:10]
    work_dir = args.work_dir or Path("outputs") / f"split_runninghub_{run_id}"
    work_dir.mkdir(parents=True, exist_ok=True)

    split_info = choose_split_point(
        args.audio,
        lrc_path=args.lrc,
        search_window=args.search_window,
        min_part_seconds=args.min_part_seconds,
        lyric_guard_seconds=args.lyric_guard_seconds,
        max_lyric_line_seconds=args.max_lyric_line_seconds,
        analysis_window_seconds=args.analysis_window_seconds,
    )
    audio_duration = float(split_info["audio_duration"])
    split_seconds = float(split_info["split_seconds"])
    overlap = min(max(args.overlap_seconds, 0.0), split_seconds, audio_duration - split_seconds)

    log(
        "Selected split: "
        f"{split_seconds:.3f}s / {audio_duration:.3f}s, "
        f"energy={split_info['energy_db']} dB, lyric_penalty={split_info['lyric_penalty']}"
    )

    part1_audio = work_dir / "part1.wav"
    part2_audio = work_dir / "part2.wav"
    part1_duration = min(audio_duration, split_seconds + overlap)
    part2_start = max(0.0, split_seconds - overlap)
    part2_duration = audio_duration - part2_start
    cut_audio_segment(args.audio, part1_audio, 0.0, part1_duration)
    cut_audio_segment(args.audio, part2_audio, part2_start, part2_duration)

    manifest: dict[str, Any] = {
        "split": split_info,
        "overlap_seconds": overlap,
        "part1_audio": str(part1_audio),
        "part2_audio": str(part2_audio),
        "part1_source_range": [0.0, round(part1_duration, 3)],
        "part2_source_range": [round(part2_start, 3), round(audio_duration, 3)],
        "output": str(args.output),
    }

    if args.dry_run:
        manifest_path = work_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"Dry run complete. Manifest: {manifest_path}")
        return 0

    base_node_info_list = load_node_info(args)
    upload_session = create_session(args.use_proxy)
    log("Uploading shared image")
    image_file_name = upload_resource(upload_session, args.api_key, args.image)
    manifest["uploaded_image"] = image_file_name

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                run_one_part,
                label="part1",
                api_key=args.api_key,
                audio_path=part1_audio,
                image_file_name=image_file_name,
                args=args,
                base_node_info_list=base_node_info_list,
                work_dir=work_dir,
            ),
            executor.submit(
                run_one_part,
                label="part2",
                api_key=args.api_key,
                audio_path=part2_audio,
                image_file_name=image_file_name,
                args=args,
                base_node_info_list=base_node_info_list,
                work_dir=work_dir,
            ),
        ]
        results = [future.result() for future in concurrent.futures.as_completed(futures)]

    results_by_label = {item["label"]: item for item in results}
    part1_video = Path(results_by_label["part1"]["video"])
    part2_video = Path(results_by_label["part2"]["video"])
    manifest["runninghub"] = results_by_label

    log("Merging generated videos with original full audio")
    merge_videos_with_original_audio(
        part1_video=part1_video,
        part2_video=part2_video,
        original_audio=args.audio,
        output_video=args.output,
        audio_duration=audio_duration,
        split_seconds=split_seconds,
        overlap_seconds=overlap,
        xfade_seconds=args.xfade_seconds,
        transition=args.transition,
    )

    manifest_path = work_dir / "manifest.json"
    manifest["final_video"] = str(args.output)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"Done: {args.output}")
    log(f"Manifest: {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
