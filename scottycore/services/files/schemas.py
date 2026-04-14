"""Files service contract — schemas only."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class FileEntry(BaseModel):
    """A single file or folder entry returned by a directory listing."""

    name: str
    path: str
    type: Literal["file", "folder"]
    size: int = Field(description="Size in bytes; 0 for folders.")
    mtime: datetime
    ext: str = Field(description="Lowercase extension without dot, e.g. 'pdf'. Empty string for folders.")


class FileListResponse(BaseModel):
    """Response envelope for a directory listing."""

    ok: bool = True
    entries: list[FileEntry]
    path: str = Field(description="Normalized relative path that was listed.")
    tenant_slug: str


class MkdirRequest(BaseModel):
    """Request body to create a new directory."""

    path: str = Field(description="Relative path of the parent directory.")
    name: str = Field(min_length=1, max_length=255, description="Name of the new directory to create.")


class RenameRequest(BaseModel):
    """Request body to rename a file or directory."""

    path: str = Field(description="Relative path of the item to rename.")
    new_name: str = Field(min_length=1, max_length=255, description="New name (not a path — rename within same parent).")


class DeleteRequest(BaseModel):
    """Request body to delete a file or empty directory."""

    path: str = Field(description="Relative path of the item to delete.")
