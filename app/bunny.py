"""Upload slip PDFs to Bunny Storage and return their public CDN URL."""
from __future__ import annotations

import hashlib

import requests

from app.config import BunnySettings
from app.errors import PermanentError, classify_status, network_error

MAX_PDF_BYTES = 5 * 1024 * 1024


class BunnyClient:
    def __init__(self, settings: BunnySettings, session: requests.Session, timeout: int):
        self.s = settings
        self.session = session
        self.timeout = (10, timeout)

    def folder_for(self, event_type: str) -> str:
        return self.s.entry_folder if event_type == "ENTRY" else self.s.exit_folder

    def public_url(self, folder: str, filename: str) -> str:
        return f"{self.s.cdn_url}/{folder}/{filename}"

    def upload(self, pdf: bytes, folder: str, filename: str) -> str:
        if not pdf:
            raise PermanentError("generated PDF is empty")
        if len(pdf) > MAX_PDF_BYTES:
            raise PermanentError(f"generated PDF is too large ({len(pdf)} bytes)")

        url = f"https://{self.s.storage_host}/{self.s.storage_zone}/{folder}/{filename}"
        headers = {
            "AccessKey": self.s.access_key.get(),
            "Content-Type": "application/pdf",
            # Bunny rejects the upload if the stored bytes don't match
            "Checksum": hashlib.sha256(pdf).hexdigest().upper(),
        }
        try:
            response = self.session.put(url, data=pdf, headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            raise network_error("Bunny upload", exc) from None
        if response.status_code not in (200, 201):
            raise classify_status("Bunny upload", response)
        return self.public_url(folder, filename)
