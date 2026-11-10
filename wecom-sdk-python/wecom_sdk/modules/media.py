"""Temporary media upload and download APIs for WeCom messages."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from ..exceptions import WeComAPIError, WeComHTTPError


class MediaAPI:
    def __init__(self, client):
        self._client = client

    async def upload(self, file_path: str | Path, *, media_type: str = "file") -> dict:
        """Upload a local file as temporary media; returned media_id lasts 3 days."""
        path = Path(file_path)
        token = await self._client.access_token()
        try:
            content = await asyncio.to_thread(path.read_bytes)
        except OSError as exc:
            raise WeComHTTPError(f"Unable to read media file: {path.name}") from exc
        try:
            response = await self._client._http.post(
                f"{self._client.base_url}/media/upload",
                params={"access_token": token, "type": media_type},
                files={"media": (path.name, content, "application/octet-stream")},
            )
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise WeComHTTPError("WeCom media upload failed") from exc
        self._client._raise_api_error(data)
        return data

    async def download(self, media_id: str) -> tuple[bytes, str]:
        """Download a temporary media item, returning (content, suggested filename)."""
        token = await self._client.access_token()
        try:
            response = await self._client._http.get(
                f"{self._client.base_url}/media/get",
                params={"access_token": token, "media_id": media_id},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise WeComHTTPError("WeCom media download failed") from exc
        content_type = response.headers.get("content-type", "")
        if "json" in content_type:
            try:
                self._client._raise_api_error(response.json())
            except ValueError as exc:
                raise WeComHTTPError("Invalid WeCom media response") from exc
            raise WeComHTTPError("Unexpected JSON response for media download")
        filename = "media"
        disposition = response.headers.get("content-disposition", "")
        if "filename=" in disposition:
            filename = disposition.split("filename=", 1)[1].strip('" ')
        return response.content, filename

    async def download_to(self, media_id: str, file_path: str | Path) -> Path:
        """Download temporary media and save it at ``file_path``."""
        content, _filename = await self.download(media_id)
        destination = Path(file_path)
        await asyncio.to_thread(destination.write_bytes, content)
        return destination
