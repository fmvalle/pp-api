"""Persistência binária de uploads de lista de presença (disco local ou GCS)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from app.core.config import settings

logger = logging.getLogger(__name__)


def _local_dir() -> Path:
    p = Path(settings.attendance_sheet_storage_path)
    if not p.is_absolute():
        p = Path.cwd() / p
    p.mkdir(parents=True, exist_ok=True)
    return p


def store_attendance_sheet_bytes(storage_key: str, data: bytes) -> None:
    if settings.attendance_sheet_storage_backend == "gcs":
        _gcs_upload(storage_key, data)
        return
    dest = _local_dir() / storage_key
    dest.write_bytes(data)


def load_attendance_sheet_bytes(storage_key: str) -> bytes:
    if settings.attendance_sheet_storage_backend == "gcs":
        return _gcs_download(storage_key)
    dest = _local_dir() / storage_key
    if not dest.is_file():
        logger.warning("attendance_sheet missing on disk key=%s path=%s", storage_key, dest)
        raise FileNotFoundError(storage_key)
    return dest.read_bytes()


def _gcs_upload(storage_key: str, data: bytes) -> None:
    bucket_name, blob_name = _gcs_blob_parts(storage_key)
    client = _gcs_client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_name)
    blob.upload_from_string(data)


def _gcs_download(storage_key: str) -> bytes:
    bucket_name, blob_name = _gcs_blob_parts(storage_key)
    client = _gcs_client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_name)
    if not blob.exists():
        logger.warning("attendance_sheet missing in gcs bucket=%s blob=%s", bucket_name, blob_name)
        raise FileNotFoundError(storage_key)
    return blob.download_as_bytes()


def _gcs_blob_parts(storage_key: str) -> tuple[str, str]:
    bucket = (settings.gcs_attendance_sheets_bucket or "").strip()
    if not bucket:
        raise RuntimeError("gcs_attendance_sheets_bucket não configurado")
    prefix = settings.gcs_attendance_sheets_prefix.strip().strip("/")
    blob_name = f"{prefix}/{storage_key}" if prefix else storage_key
    return bucket, blob_name


def _gcs_client():  # pragma: no cover - exercised in integration / manual
    from google.cloud import storage as gcs_storage
    from google.oauth2 import service_account

    raw = settings.gcs_credentials_json
    if raw and raw.strip():
        info = json.loads(raw)
        creds = service_account.Credentials.from_service_account_info(info)
        project = info.get("project_id")
        return gcs_storage.Client(credentials=creds, project=project)
    return gcs_storage.Client()
