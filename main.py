import argparse
import base64
import binascii
import json
import mimetypes
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yt_dlp
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials as OAuthCredentials
from googleapiclient.errors import HttpError
from starlette.datastructures import URL


ProgressCallback = Optional[Callable[[Dict], None]]


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_FFMPEG_DIR = SCRIPT_DIR / "ffmpeg" / "bin"


def resolve_ffmpeg_location(custom_path: Optional[str] = None) -> Optional[str]:
    """Return directory containing ffmpeg binaries if available."""
    if custom_path:
        candidate = Path(custom_path).expanduser().resolve()
        if candidate.is_file():
            return str(candidate.parent)
        if candidate.is_dir():
            return str(candidate)
        raise FileNotFoundError(f"ไม่พบตำแหน่ง FFmpeg ที่ระบุ: {candidate}")

    if DEFAULT_FFMPEG_DIR.exists():
        return str(DEFAULT_FFMPEG_DIR.resolve())
    return None


FORMAT_PRESETS: Dict[str, Dict[str, str]] = {
    "auto": {
        "label": "Best (single file)",
        "format": "best",
        "description": "เลือกไฟล์คุณภาพดีที่สุดแบบไฟล์เดียว (ไม่ต้องใช้ FFmpeg)",
    },
    "merge_best": {
        "label": "Best video + audio (ต้องมี FFmpeg)",
        "format": "bv*+ba/best",
        "description": "เลือกวิดีโอและเสียงที่ดีที่สุดแล้วรวมไฟล์ (ต้องติดตั้ง FFmpeg)",
    },
    "video_only": {
        "label": "Video only",
        "format": "bv*",
        "description": "ดาวน์โหลดเฉพาะวิดีโอ (ไม่มีเสียง)",
    },
    "audio_only": {
        "label": "Audio only",
        "format": "ba/best",
        "description": "ดาวน์โหลดเฉพาะเสียง (เลือกคุณภาพดีที่สุด)",
    },
}


GOOGLE_DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
_GOOGLE_DRIVE_CLIENT_LOCK = threading.Lock()
_google_drive_client: Optional["GoogleDriveClient"] = None


class GoogleDriveClient:
    def __init__(
        self,
        credentials: Any,
        folder_id: Optional[str],
        share_public: bool = True,
    ) -> None:
        self._credentials = credentials
        self._folder_id = folder_id
        self._share_public = share_public
        self._service = None
        self._chunk_size = 8 * 1024 * 1024

    def _refresh_credentials(self) -> None:
        refreshable = getattr(self._credentials, "refresh_token", None)
        expired = getattr(self._credentials, "expired", False)
        if refreshable and expired:
            self._credentials.refresh(GoogleAuthRequest())

    def _ensure_service(self):
        if self._service is None:
            try:
                from googleapiclient.discovery import build
            except ImportError as exc:  # pragma: no cover - optional dependency
                raise RuntimeError(
                    "Google Drive integration requires google-api-python-client and google-auth packages"
                ) from exc
            self._refresh_credentials()
            self._service = build(
                "drive",
                "v3",
                credentials=self._credentials,
                cache_discovery=False,
            )
        return self._service

    def upload_file(self, file_path: Path, mime_type: Optional[str]) -> Dict[str, str]:
        self._refresh_credentials()
        service = self._ensure_service()
        try:
            from googleapiclient.http import MediaFileUpload
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "Google Drive integration requires google-api-python-client package"
            ) from exc

        metadata: Dict[str, Any] = {"name": file_path.name}
        if self._folder_id:
            metadata["parents"] = [self._folder_id]

        media = MediaFileUpload(
            str(file_path),
            mimetype=mime_type or "application/octet-stream",
            chunksize=self._chunk_size,
            resumable=True,
        )
        request = service.files().create(
            body=metadata,
            media_body=media,
            fields="id,name,size,webViewLink",
            supportsAllDrives=True,
        )

        response = None
        try:
            while response is None:
                _, response = request.next_chunk()
        except HttpError as exc:
            message = exc.content.decode() if isinstance(exc.content, bytes) else str(exc)
            if "storageQuotaExceeded" in message:
                raise RuntimeError(
                    "Google Drive service account has no storage quota. "
                    "Use a Shared Drive or OAuth user credentials instead."
                ) from exc
            raise

        file_id = response["id"]
        if self._share_public:
            try:
                service.permissions().create(
                    fileId=file_id,
                    body={"role": "reader", "type": "anyone"},
                    fields="id",
                    supportsAllDrives=True,
                ).execute()
            except Exception:
                pass

        download_url = f"https://drive.google.com/uc?export=download&id={file_id}"
        view_url = response.get("webViewLink") or download_url
        return {
            "file_id": file_id,
            "download_url": download_url,
            "view_url": view_url,
        }

    def delete_file(self, file_id: str) -> None:
        self._refresh_credentials()
        service = self._ensure_service()
        try:
            service.files().delete(
                fileId=file_id,
                supportsAllDrives=True,
            ).execute()
        except Exception:
            pass


def _parse_service_account_candidate(candidate: str) -> Optional[Dict[str, Any]]:
    text = candidate.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    try:
        decoded = base64.b64decode(text)
    except (ValueError, binascii.Error):
        return None
    try:
        return json.loads(decoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _load_service_account_info() -> Optional[Dict[str, Any]]:
    json_b64 = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON_BASE64")
    if json_b64:
        parsed = _parse_service_account_candidate(json_b64)
        if parsed:
            return parsed
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON_BASE64 is not valid JSON")

    for env_name in ("GOOGLE_SERVICE_ACCOUNT_JSON", "GOOGLE_SERVICE_ACCOUNT_FILE"):
        raw_value = os.getenv(env_name)
        if not raw_value:
            continue
        path = Path(raw_value).expanduser()
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"Invalid JSON in credentials file: {path}") from exc
        parsed = _parse_service_account_candidate(raw_value)
        if parsed:
            return parsed
        raise RuntimeError(f"Unable to parse Google credentials from {env_name}")
    default_path = SCRIPT_DIR / "credentials" / "service_account.json"
    if default_path.exists():
        try:
            return json.loads(default_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Invalid JSON in credentials file: {default_path}") from exc
    return None


def _load_oauth_credentials() -> Optional[OAuthCredentials]:
    json_b64 = os.getenv("GOOGLE_OAUTH_TOKEN_JSON_BASE64")
    token_info: Optional[Dict[str, Any]] = None
    if json_b64:
        parsed = _parse_service_account_candidate(json_b64)
        if parsed:
            token_info = parsed
        else:
            raise RuntimeError("GOOGLE_OAUTH_TOKEN_JSON_BASE64 is not valid JSON/base64")
    if token_info is None:
        raw_json = os.getenv("GOOGLE_OAUTH_TOKEN_JSON")
        if raw_json:
            parsed = _parse_service_account_candidate(raw_json)
            if parsed:
                token_info = parsed
            else:
                raise RuntimeError("Unable to parse GOOGLE_OAUTH_TOKEN_JSON")
    if token_info is None:
        token_file = os.getenv("GOOGLE_OAUTH_TOKEN_FILE")
        if token_file:
            token_path = Path(token_file).expanduser()
        else:
            token_path = SCRIPT_DIR / "credentials" / "token.json"
        if token_path.exists():
            try:
                token_info = json.loads(token_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"Invalid JSON in OAuth token file: {token_path}") from exc
    if token_info is None:
        return None

    try:
        credentials = OAuthCredentials.from_authorized_user_info(
            token_info,
            scopes=[GOOGLE_DRIVE_SCOPE],
        )
    except ValueError as exc:
        raise RuntimeError("OAuth token JSON is missing required fields (generate token.json via OAuth flow with client secrets)") from exc

    if credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(GoogleAuthRequest())
        except Exception as exc:  # pragma: no cover - network refresh
            raise RuntimeError("Failed to refresh Google OAuth token") from exc
    return credentials


def get_google_drive_client() -> Optional[GoogleDriveClient]:
    global _google_drive_client
    if _google_drive_client is not None:
        return _google_drive_client

    share_flag = os.getenv("GOOGLE_DRIVE_SHARE_PUBLIC", "true").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")

    credentials: Optional[Any] = _load_oauth_credentials()
    if credentials is None:
        service_account_info = _load_service_account_info()
        if service_account_info:
            try:
                from google.oauth2 import service_account as service_account_module
            except ImportError as exc:  # pragma: no cover - optional dependency
                raise RuntimeError(
                    "Google Drive integration requires google-auth for service account support"
                ) from exc
            credentials = service_account_module.Credentials.from_service_account_info(
                service_account_info,
                scopes=[GOOGLE_DRIVE_SCOPE],
            )

    if credentials is None:
        return None

    with _GOOGLE_DRIVE_CLIENT_LOCK:
        if _google_drive_client is None:
            _google_drive_client = GoogleDriveClient(
                credentials,
                folder_id,
                share_public=share_flag,
            )
    return _google_drive_client

def resolve_format_choice(format_choice: str) -> str:
    """Map preset keys to yt-dlp format strings (allow raw override)."""
    preset = FORMAT_PRESETS.get(format_choice)
    if preset:
        return preset["format"]
    return format_choice or FORMAT_PRESETS["auto"]["format"]


def download_video(
    url: str,
    output_path: str = ".",
    format_choice: str = "auto",
    ffmpeg_location: Optional[str] = None,
    progress_callback: ProgressCallback = None,
) -> Path:
    """Download a single video URL and return the resulting file path."""
    output_dir = Path(output_path or ".").expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    last_filename: Dict[str, Optional[Path]] = {"value": None}

    def _hook(data: Dict) -> None:
        filename = data.get("filename")
        if filename:
            last_filename["value"] = Path(filename)
        if progress_callback:
            progress_callback(data)

    ydl_opts = {
        "outtmpl": str(output_dir / "%(title)s.%(ext)s"),
        "progress_hooks": [_hook],
        "format": resolve_format_choice(format_choice),
        "quiet": True,
        "no_warnings": True,
    }
    resolved_ffmpeg = resolve_ffmpeg_location(ffmpeg_location)
    if resolved_ffmpeg:
        ydl_opts["ffmpeg_location"] = resolved_ffmpeg

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)

    final_path = last_filename["value"]
    if final_path is None:
        final_path = _pick_path_from_info(info)
    if final_path is None:
        raise RuntimeError("ไม่พบไฟล์ที่ดาวน์โหลดสำเร็จ")
    return _resolve_final_file(final_path)


def _pick_path_from_info(info: Dict) -> Optional[Path]:
    """Derive output file from yt-dlp info dict when hooks do not report it."""
    candidates: List[Path] = []
    if not info:
        return None

    filepath = info.get("requested_downloads") or info.get("requested_formats")
    if isinstance(filepath, list):
        for item in filepath:
            path_str = (item or {}).get("filepath") or (item or {}).get("_filename")
            if path_str:
                candidates.append(Path(path_str))
    elif isinstance(filepath, dict):
        path_str = filepath.get("filepath") or filepath.get("_filename")
        if path_str:
            candidates.append(Path(path_str))

    explicit = info.get("filepath") or info.get("_filename")
    if explicit:
        candidates.append(Path(explicit))

    entries = info.get("entries") or []
    for entry in entries:
        entry_path = (
            (entry or {}).get("requested_downloads")
            or (entry or {}).get("requested_formats")
        )
        if isinstance(entry_path, list):
            for item in entry_path:
                path_str = (item or {}).get("filepath")
                if path_str:
                    candidates.append(Path(path_str))
        elif isinstance(entry_path, dict):
            path_str = entry_path.get("filepath")
            if path_str:
                candidates.append(Path(path_str))
        fallback = (entry or {}).get("filepath") or (entry or {}).get("_filename")
        if fallback:
            candidates.append(Path(fallback))

    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return Path(candidate)
    return None


def _resolve_final_file(file_path: Path) -> Path:
    """Ensure the returned path matches the final merged file from yt-dlp."""
    if file_path.exists():
        return file_path

    # yt-dlp บางครั้งจะสร้างไฟล์ชั่วคราว เช่น *.f1.mp4 แล้วค่อย rename เป็น *.mp4
    name = file_path.name
    stripped_name = re.sub(r"\.f\d+(\.\w+)$", r"\1", name)
    alt_path = file_path.with_name(stripped_name)
    if alt_path.exists():
        return alt_path

    # สุดท้ายลองค้นหาไฟล์ชื่อใกล้เคียงในโฟลเดอร์ปลายทาง
    candidates = list(file_path.parent.glob(f"{file_path.stem.split('.f')[0]}*.{file_path.suffix.lstrip('.')}"))
    for candidate in candidates:
        if candidate.exists():
            return candidate

    raise FileNotFoundError(f"ไม่พบไฟล์ที่ดาวน์โหลดสำเร็จ: {file_path}")


class DownloadManager:
    """In-memory job manager for API downloads."""

    def __init__(self) -> None:
        self._jobs: Dict[str, Dict] = {}
        self._lock = threading.Lock()

    def create_job(
        self,
        url: str,
        output_path: Optional[str],
        format_choice: str,
        ffmpeg_location: Optional[str] = None,
    ) -> Dict:
        job_id = uuid.uuid4().hex
        job = {
            "id": job_id,
            "url": url,
            "output_path": str(Path(output_path or ".").expanduser()),
            "format": format_choice,
            "ffmpeg_location": ffmpeg_location,
            "status": "queued",
            "output_file": None,
            "download_url": None,
            "remote_file_id": None,
            "remote_file_url": None,
            "remote_file_view_url": None,
            "error": None,
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        with self._lock:
            self._jobs[job_id] = job
        return job.copy()

    def get_job(self, job_id: str) -> Optional[Dict]:
        with self._lock:
            job = self._jobs.get(job_id)
            return job.copy() if job else None

    def list_jobs(self) -> List[Dict]:
        with self._lock:
            return [job.copy() for job in self._jobs.values()]

    def _update_job(self, job_id: str, **updates) -> Dict:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise KeyError(job_id)
            job.update(updates)
            job["updated_at"] = time.time()
            return job.copy()

    def delete_remote_file(self, job_id: str) -> None:
        job = self.get_job(job_id)
        if not job:
            return
        remote_id = job.get("remote_file_id")
        if not remote_id:
            return
        try:
            client = get_google_drive_client()
        except Exception:
            return
        if not client:
            return
        try:
            client.delete_file(remote_id)
        except Exception:
            pass
        self._update_job(
            job_id,
            remote_file_id=None,
            remote_file_url=None,
            remote_file_view_url=None,
        )

    def mark_delivering(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job["status"] = "delivering"
            job["updated_at"] = time.time()

    def mark_downloaded(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job["status"] = "downloaded"
            job["updated_at"] = time.time()

    def mark_file_consumed(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job["status"] = "delivered"
            job["output_file"] = None
            job["download_url"] = None
            job["remote_file_id"] = None
            job["remote_file_url"] = None
            job["remote_file_view_url"] = None
            job["updated_at"] = time.time()

    def process_job(self, job_id: str) -> None:
        job = self.get_job(job_id)
        if not job:
            return

        self._update_job(job_id, status="downloading", error=None)
        try:
            output_file = download_video(
                job["url"],
                job["output_path"],
                format_choice=job.get("format", "auto"),
                ffmpeg_location=job.get("ffmpeg_location"),
            )
            mime_type, _ = mimetypes.guess_type(str(output_file))
            remote_details: Optional[Dict[str, str]] = None
            remote_error: Optional[str] = None
            drive_client: Optional[GoogleDriveClient] = None
            try:
                drive_client = get_google_drive_client()
            except Exception as exc:  # noqa: BLE001
                remote_error = str(exc)
            if drive_client:
                try:
                    remote_details = drive_client.upload_file(
                        Path(output_file),
                        mime_type,
                    )
                except Exception as exc:  # noqa: BLE001
                    remote_error = str(exc)

            updates: Dict[str, Any] = {
                "status": "completed",
                "output_file": str(output_file),
                "download_url": f"/download/{job_id}/file",
            }
            if remote_details:
                updates.update(
                    remote_file_id=remote_details["file_id"],
                    remote_file_url=remote_details["download_url"],
                    remote_file_view_url=remote_details["view_url"],
                )
            if remote_error:
                updates["error"] = f"Google Drive upload failed: {remote_error}"
            self._update_job(job_id, **updates)
        except Exception as exc:  # noqa: BLE001
            self._update_job(
                job_id,
                status="failed",
                error=str(exc),
                download_url=None,
                remote_file_id=None,
                remote_file_url=None,
                remote_file_view_url=None,
            )


def create_ui() -> None:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title("Bilibili Video Downloader")
    root.geometry("440x360")

    url_label = tk.Label(root, text="Bilibili URL:")
    url_label.pack(pady=5)
    url_entry = tk.Entry(root, width=52)
    url_entry.pack(pady=5)

    output_label = tk.Label(root, text="Output Directory:")
    output_label.pack(pady=5)
    output_frame = tk.Frame(root)
    output_frame.pack(pady=5)
    output_entry = tk.Entry(output_frame, width=40)
    output_entry.pack(side=tk.LEFT)
    output_entry.insert(0, ".")

    def browse_output() -> None:
        folder = filedialog.askdirectory()
        if folder:
            output_entry.delete(0, tk.END)
            output_entry.insert(0, folder)

    browse_button = tk.Button(output_frame, text="Browse", command=browse_output)
    browse_button.pack(side=tk.LEFT, padx=5)

    format_label = tk.Label(root, text="Download Format:")
    format_label.pack(pady=5)

    format_options = [
        (code, info["label"]) for code, info in FORMAT_PRESETS.items()
    ]
    label_to_code = {label: code for code, label in format_options}
    format_var = tk.StringVar(value=format_options[0][1])

    format_combo = ttk.Combobox(
        root,
        textvariable=format_var,
        values=[label for _, label in format_options],
        state="readonly",
        width=40,
    )
    format_combo.pack(pady=5)

    progress = ttk.Progressbar(
        root,
        orient="horizontal",
        length=320,
        mode="determinate",
    )
    progress.pack(pady=10)
    progress["maximum"] = 100
    progress["value"] = 0

    status_label = tk.Label(root, text="Status: Ready")
    status_label.pack(pady=5)

    def set_status(text: str, value: Optional[float] = None) -> None:
        status_label.config(text=text)
        if value is not None:
            progress["value"] = max(0, min(100, value))

    def reset_ui() -> None:
        download_button.config(state=tk.NORMAL)

    def start_download() -> None:
        url = url_entry.get().strip()
        output = output_entry.get().strip() or "."
        format_label_value = format_combo.get()
        format_choice = label_to_code.get(format_label_value, "auto")
        if not url:
            messagebox.showerror("Error", "กรุณาใส่ URL ของวิดีโอ")
            return

        download_button.config(state=tk.DISABLED)
        progress["value"] = 0
        set_status("Status: Preparing...", 0)

        def run_download() -> None:
            def ui_hook(data: Dict) -> None:
                def update_ui() -> None:
                    status = data.get("status")
                    if status == "downloading":
                        percent_str = data.get("_percent_str", "").strip()
                        try:
                            percent = float(percent_str.replace("%", ""))
                        except ValueError:
                            percent = progress["value"]
                        basic_name = Path(data.get("filename", "")).name
                        label = f"Status: Downloading {basic_name} {percent_str}".strip()
                        set_status(label, percent)
                    elif status == "finished":
                        set_status("Status: Processing...", 100)

                root.after(0, update_ui)

            try:
                file_path = download_video(
                    url,
                    output,
                    format_choice=format_choice,
                    progress_callback=ui_hook,
                )
            except Exception as exc:  # noqa: BLE001
                root.after(
                    0,
                    lambda: messagebox.showerror("Error", str(exc)),
                )
                root.after(0, lambda: set_status("Status: Error", 0))
            else:
                root.after(
                    0,
                    lambda: messagebox.showinfo(
                        "สำเร็จ",
                        f"ดาวน์โหลดเสร็จแล้ว:\n{file_path}",
                    ),
                )
                root.after(0, lambda: set_status("Status: Download completed!", 100))
            finally:
                root.after(0, reset_ui)

        threading.Thread(target=run_download, daemon=True).start()

    download_button = tk.Button(root, text="Download", command=start_download)
    download_button.pack(pady=10)

    root.mainloop()


def create_api_app(manager: Optional[DownloadManager] = None):
    from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request, Response
    from fastapi.responses import FileResponse
    from pydantic import BaseModel, Field
    from starlette.background import BackgroundTask

    manager = manager or DownloadManager()
    valid_formats = list(FORMAT_PRESETS.keys())
    app = FastAPI(
        title="Bilibili Downloader API",
        version="1.0.0",
        description="REST API สำหรับดาวน์โหลดวิดีโอจาก Bilibili",
    )

    def _cleanup_after_delivery(job_id: str, output_path: str) -> None:
        path = Path(output_path)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
        manager.delete_remote_file(job_id)
        manager.mark_file_consumed(job_id)

    def _mark_downloaded(job_id: str) -> None:
        manager.mark_downloaded(job_id)

    class DownloadRequest(BaseModel):
        url: str = Field(..., description="ลิงก์วิดีโอ Bilibili")
        output_path: Optional[str] = Field(
            default=".",
            description="โฟลเดอร์สำหรับบันทึกวิดีโอ",
        )
        format: str = Field(
            default="auto",
            description=f"โหมดการดาวน์โหลด (เลือกจาก: {', '.join(valid_formats)})",
        )
        ffmpeg_location: Optional[str] = Field(
            default=None,
            description="หากต้องการระบุตำแหน่ง FFmpeg เอง (โฟลเดอร์หรือไฟล์ ffmpeg.exe)",
        )


    def _first_forwarded(request: Request, header_name: str) -> Optional[str]:
        value = request.headers.get(header_name)
        if not value:
            return None
        return value.split(",")[0].strip()

    def _absolute_url(request: Request, path: str) -> str:
        """
        Build absolute URL that respects reverse proxy headers (e.g. ngrok, Render).
        """
        base = URL(str(request.base_url))

        forwarded_proto = _first_forwarded(request, "x-forwarded-proto")
        if forwarded_proto:
            base = base.replace(scheme=forwarded_proto)

        forwarded_host = _first_forwarded(request, "x-forwarded-host")
        if forwarded_host:
            base = base.replace(netloc=forwarded_host)
        else:
            forwarded_port = _first_forwarded(request, "x-forwarded-port")
            if forwarded_port and base.hostname:
                base = base.replace(netloc=f"{base.hostname}:{forwarded_port}")

        path_str = str(path)
        if path_str.startswith(("http://", "https://")):
            return path_str

        # Ensure target URL behaves as relative path joined to the computed base.
        target = URL(path_str if path_str.startswith("/") else f"/{path_str}")
        joined = base.replace(
            path=target.path,
            query=target.query or None,
            fragment=target.fragment or None,
        )
        return str(joined)

    def _format_job(job: Dict, request: Request) -> Dict:
        """Attach useful URLs to job payloads for API consumers."""
        payload = job.copy()
        payload["download_url"] = (
            _absolute_url(request, app.url_path_for("download_file", job_id=payload["id"]))
            if payload.get("download_url")
            else None
        )
        payload["status_url"] = _absolute_url(
            request, app.url_path_for("get_status", job_id=payload["id"])
        )
        payload["job_id"] = payload["id"]
        return payload

    @app.get("/health")
    def health() -> Dict[str, str]:
        return {"status": "ok"}

    @app.post("/download")
    def enqueue_download(
        payload: DownloadRequest,
        background_tasks: BackgroundTasks,
        request: Request,
    ) -> Dict[str, object]:
        if payload.format not in valid_formats:
            raise HTTPException(
                status_code=400,
                detail=f"รูปแบบการดาวน์โหลดไม่ถูกต้อง (รองรับ: {', '.join(valid_formats)})",
            )
        resolved_ffmpeg = None
        if payload.ffmpeg_location:
            try:
                resolved_ffmpeg = resolve_ffmpeg_location(payload.ffmpeg_location)
            except FileNotFoundError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        job = manager.create_job(
            payload.url,
            payload.output_path,
            payload.format,
            resolved_ffmpeg or payload.ffmpeg_location,
        )
        background_tasks.add_task(manager.process_job, job["id"])
        formatted = _format_job(job, request)
        return {
            "job_id": formatted["job_id"],
            "status": formatted["status"],
            "format": formatted["format"],
            "ffmpeg_location": formatted["ffmpeg_location"],
            "download_url": formatted["download_url"],
            "status_url": formatted["status_url"],
        }

    @app.get("/status/{job_id}")
    def get_status(job_id: str, request: Request) -> Dict:
        job = manager.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="ไม่พบงานดาวน์โหลดนี้")
        return _format_job(job, request)

    @app.get("/jobs")
    def list_jobs(request: Request) -> Dict[str, List[Dict]]:
        return {"jobs": [_format_job(job, request) for job in manager.list_jobs()]}

    @app.get("/download/{job_id}/file", response_model=None)
    def download_file(
        job_id: str,
        as_base64: bool = Query(False),
        auto_delete: bool = Query(True),
    ):
        job = manager.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="ไม่พบงานดาวน์โหลดนี้")
        status = job.get("status")
        allowed_statuses = {"completed"}
        if not auto_delete:
            allowed_statuses.add("downloaded")
        if status == "delivered":
            raise HTTPException(status_code=410, detail="ไฟล์นี้ถูกดาวน์โหลดและลบไปแล้ว")
        if status == "delivering":
            raise HTTPException(status_code=409, detail="ไฟล์กำลังถูกส่งให้คำขออื่น")
        if status not in allowed_statuses:
            raise HTTPException(
                status_code=409,
                detail=f"งานยังไม่เสร็จสมบูรณ์ (status: {status})",
            )
        output_file = job.get("output_file")
        if not output_file:
            raise HTTPException(status_code=404, detail="ไม่พบไฟล์ที่บันทึก")
        file_path = Path(output_file)
        if not file_path.exists():
            raise HTTPException(status_code=404, detail="ไฟล์ถูกลบหรือย้ายไปแล้ว")

        manager.mark_delivering(job_id)

        if as_base64:
            file_size = file_path.stat().st_size
            data = file_path.read_bytes()
            encoded = base64.b64encode(data).decode("ascii")
            mime_type, _ = mimetypes.guess_type(str(file_path))
            if auto_delete:
                _cleanup_after_delivery(job_id, str(file_path))
            else:
                manager.mark_downloaded(job_id)
            return {
                "filename": file_path.name,
                "size": file_size,
                "mime_type": mime_type or "application/octet-stream",
                "data": encoded,
            }

        mime_type, _ = mimetypes.guess_type(str(file_path))
        if auto_delete:
            cleanup_task = BackgroundTask(_cleanup_after_delivery, job_id, str(file_path))
        else:
            cleanup_task = BackgroundTask(_mark_downloaded, job_id)
        return FileResponse(
            path=file_path,
            filename=file_path.name,
            media_type=mime_type or "application/octet-stream",
            background=cleanup_task,
        )

    @app.delete("/cleanup/{job_id}")
    def manual_cleanup(job_id: str) -> Dict[str, object]:
        job = manager.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="ไม่พบงาน")
        output_file = job.get("output_file")
        if output_file:
            file_path = Path(output_file)
            if file_path.exists():
                _cleanup_after_delivery(job_id, str(file_path))
                return {"message": "ลบไฟล์สำเร็จ", "file": output_file}
        manager.delete_remote_file(job_id)
        if job.get("status") in {"completed", "delivering", "downloaded"}:
            manager.mark_file_consumed(job_id)
        return {"message": "ไฟล์ไม่พบหรือถูกลบไปแล้ว"}

    return app


def run_api_server(host: str, port: int) -> None:
    import uvicorn

    app = create_api_app()

    env_port = os.getenv("PORT")
    try:
        resolved_port = int(env_port) if env_port else port
    except (TypeError, ValueError):
        resolved_port = port

    uvicorn.run(app, host=host, port=resolved_port)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="ดาวน์โหลดวิดีโอจาก Bilibili ผ่าน CLI / GUI / REST API",
    )
    parser.add_argument(
        "url",
        nargs="?",
        help="ลิงก์วิดีโอ Bilibili (กรณีต้องการโหมด CLI)",
    )
    parser.add_argument(
        "--output",
        "-o",
        default=".",
        help="โฟลเดอร์สำหรับบันทึกวิดีโอในโหมด CLI (ค่าเริ่มต้นคือโฟลเดอร์ปัจจุบัน)",
    )
    parser.add_argument(
        "--format",
        choices=list(FORMAT_PRESETS.keys()),
        default="auto",
        help="เลือกรูปแบบการดาวน์โหลด (auto, merge_best, video_only, audio_only)",
    )
    parser.add_argument(
        "--ffmpeg-location",
        help="ระบุโฟลเดอร์หรือไฟล์ ffmpeg.exe (ถ้าไม่ได้ใช้ FFmpeg ที่มากับโปรเจ็กต์)",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="เปิดใช้งาน REST API server (n8n สามารถเรียกใช้งานได้)",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Host สำหรับ REST API server (ค่าเริ่มต้น 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port สำหรับ REST API server (ค่าเริ่มต้น 8000)",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.serve:
        run_api_server(args.host, args.port)
        return

    if args.url:
        file_path = download_video(
            args.url,
            args.output,
            format_choice=args.format,
            ffmpeg_location=args.ffmpeg_location,
        )
        print(f"Download completed: {file_path}")
        return

    create_ui()


if __name__ == "__main__":
    main()
