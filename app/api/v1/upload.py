"""Upload API — accepts multipart file uploads and saves them to disk.

POST /api/v1/upload
  - Accepts one or more files in the ``files`` field
  - Saves to the directory configured by ``UPLOAD_DIR`` env var (default: ``data/uploads``)
  - Requires any authenticated user (viewer or above)
  - Returns ``{ok, files, count}``
"""

from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from pydantic import BaseModel

from app.core.auth import require_role
from app.core.schemas import RoleName
from app.core.config import get_settings
from app.services.auth.schemas import UserContext

router = APIRouter()

_SETTINGS = None


def _upload_dir() -> Path:
    """Return the upload directory, creating it if needed.

    Uses ``UPLOADS_BASE_DIR`` from settings (default: ``uploads``).
    """
    global _SETTINGS
    if _SETTINGS is None:
        _SETTINGS = get_settings()

    raw = getattr(_SETTINGS, "uploads_base_dir", None) or "uploads"
    path = Path(raw)
    path.mkdir(parents=True, exist_ok=True)
    return path


class UploadedFile(BaseModel):
    filename: str
    saved_as: str
    size: int


class UploadResponse(BaseModel):
    ok: bool
    files: list[UploadedFile]
    count: int


@router.post("", response_model=UploadResponse, status_code=201)
async def upload_files(
    files: list[UploadFile],
    user: UserContext = Depends(require_role(RoleName.VIEWER)),
) -> UploadResponse:
    """Accept one or more files via multipart upload and save them to disk.

    A UUID prefix is prepended to each filename to prevent collisions while
    preserving the original name for the response.
    """
    if not files:
        raise HTTPException(status_code=400, detail="No files provided")

    upload_dir = _upload_dir()
    results: list[UploadedFile] = []

    for upload in files:
        original_name = upload.filename or "file"
        # Sanitize: strip path separators an attacker might inject
        safe_name = Path(original_name).name or "file"
        saved_name = f"{uuid.uuid4().hex}_{safe_name}"
        dest = upload_dir / saved_name

        content = await upload.read()
        dest.write_bytes(content)

        results.append(UploadedFile(
            filename=safe_name,
            saved_as=saved_name,
            size=len(content),
        ))

    return UploadResponse(ok=True, files=results, count=len(results))
