"""Kapso WhatsApp platform adapter for Hermes Agent.

This plugin runs an aiohttp webhook server for Kapso platform webhooks and
sends outbound WhatsApp messages through Kapso's WhatsApp Cloud API proxy.
It is designed to be installed as a Hermes platform plugin, for example:

    ~/.hermes/plugins/platforms/kapso/
      plugin.yaml
      adapter.py
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import uuid
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlencode, urlsplit

try:
    from aiohttp import ClientSession, ClientTimeout, web

    AIOHTTP_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised by Hermes runtime checks
    ClientSession = None  # type: ignore[assignment]
    ClientTimeout = None  # type: ignore[assignment]
    web = None  # type: ignore[assignment]
    AIOHTTP_AVAILABLE = False

from gateway.config import Platform
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)

try:
    from gateway.platforms.base import cache_image_from_bytes as _hermes_cache_image_from_bytes
except ImportError:  # pragma: no cover - only used outside newer Hermes runtimes
    _hermes_cache_image_from_bytes = None

try:
    from gateway.platforms.base import cache_audio_from_bytes as _hermes_cache_audio_from_bytes
except ImportError:  # pragma: no cover - only used outside newer Hermes runtimes
    _hermes_cache_audio_from_bytes = None

try:
    from gateway.platforms.base import cache_document_from_bytes as _hermes_cache_document_from_bytes
except ImportError:  # pragma: no cover - only used outside newer Hermes runtimes
    _hermes_cache_document_from_bytes = None

try:
    from gateway.platforms.base import SUPPORTED_DOCUMENT_TYPES as _hermes_supported_document_types
except ImportError:  # pragma: no cover - only used outside newer Hermes runtimes
    _hermes_supported_document_types = {}

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.kapso.ai/meta/whatsapp"
DEFAULT_GRAPH_VERSION = "v24.0"
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8648
DEFAULT_WEBHOOK_PATH = "/kapso/webhook"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 20.0
DEFAULT_MAX_BODY_BYTES = 1_048_576
MAX_WHATSAPP_TEXT_LENGTH = 4096
SEEN_MESSAGE_CACHE_SIZE = 1024
KAPSO_MESSAGE_RECEIVED_EVENT = "whatsapp.message.received"

_GRAPH_VERSION_RE = re.compile(r"/v\d+\.\d+$")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_MD_BOLD_RE = re.compile(r"\*\*([^*\n]+)\*\*")
_MD_UNDERLINE_BOLD_RE = re.compile(r"__([^_\n]+)__")
_MD_HEADING_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_IMAGE_MIME_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
_AUDIO_MIME_EXTENSIONS = {
    "audio/aac": ".aac",
    "audio/amr": ".amr",
    "audio/mp4": ".m4a",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/ogg": ".ogg",
    # WhatsApp voice notes are Ogg Opus; OpenAI STT accepts .ogg uploads.
    "audio/opus": ".ogg",
    "audio/wav": ".wav",
    "audio/webm": ".webm",
    "audio/x-m4a": ".m4a",
}
_FALLBACK_DOCUMENT_TYPES = {
    ".pdf": "application/pdf",
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".log": "text/plain",
    ".json": "application/json",
    ".xml": "application/xml",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".toml": "application/toml",
    ".ini": "text/plain",
    ".cfg": "text/plain",
    ".zip": "application/zip",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".ods": "application/vnd.oasis.opendocument.spreadsheet",
    ".odp": "application/vnd.oasis.opendocument.presentation",
    ".rtf": "application/rtf",
    ".html": "text/html",
    ".htm": "text/html",
    ".tsv": "text/tab-separated-values",
    ".py": "text/plain",
    ".sh": "text/plain",
    ".ts": "text/plain",
}
_DOCUMENT_EXT_MIME = {
    **_FALLBACK_DOCUMENT_TYPES,
    **(_hermes_supported_document_types if isinstance(_hermes_supported_document_types, dict) else {}),
}
_DOCUMENT_MIME_EXTENSIONS = {mime.lower(): ext for ext, mime in _DOCUMENT_EXT_MIME.items()}
_DOCUMENT_MIME_EXTENSIONS.update(
    {
        "application/x-yaml": ".yaml",
        "application/octet-stream": "",
        "text/xml": ".xml",
    }
)
_TEXT_DOCUMENT_EXTENSIONS = {
    ".txt",
    ".md",
    ".csv",
    ".log",
    ".json",
    ".xml",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".tsv",
    ".py",
    ".sh",
    ".ts",
}
MAX_TEXT_DOCUMENT_INLINE_BYTES = 100 * 1024


class KapsoAdapter(BasePlatformAdapter):
    """Hermes platform adapter for Kapso-backed WhatsApp conversations."""

    MAX_MESSAGE_LENGTH = MAX_WHATSAPP_TEXT_LENGTH

    def __init__(self, config, **_: Any) -> None:
        platform = Platform("kapso")
        super().__init__(config=config, platform=platform)
        extra = getattr(config, "extra", {}) or {}

        self.api_key = _first_nonempty(
            os.getenv("KAPSO_API_KEY"),
            getattr(config, "api_key", None),
            extra.get("api_key"),
            extra.get("kapso_api_key"),
        )
        self.base_url = _normalize_base_url(
            _first_nonempty(
                os.getenv("KAPSO_BASE_URL"),
                extra.get("base_url"),
                extra.get("api_base_url"),
                DEFAULT_BASE_URL,
            )
        )
        self.graph_version = _strip_slashes(
            _first_nonempty(
                os.getenv("KAPSO_GRAPH_VERSION"),
                os.getenv("META_GRAPH_VERSION"),
                extra.get("graph_version"),
                DEFAULT_GRAPH_VERSION,
            )
        )
        self.default_phone_number_id = _first_nonempty(
            os.getenv("KAPSO_PHONE_NUMBER_ID"),
            os.getenv("WHATSAPP_PHONE_NUMBER_ID"),
            extra.get("phone_number_id"),
        )
        self.webhook_secret = _first_nonempty(
            os.getenv("KAPSO_WEBHOOK_SECRET"),
            extra.get("webhook_secret"),
        )
        self.verify_webhook_signatures = _coerce_bool(
            _first_defined(
                os.getenv("KAPSO_VERIFY_WEBHOOK_SIGNATURES"),
                extra.get("verify_webhook_signatures"),
            ),
            default=True,
        )
        self.host = _first_nonempty(os.getenv("KAPSO_HOST"), extra.get("host"), DEFAULT_HOST)
        self.port = _coerce_int(
            _first_nonempty(os.getenv("KAPSO_PORT"), extra.get("port")),
            DEFAULT_PORT,
        )
        self.webhook_path = _normalize_path(
            _first_nonempty(
                os.getenv("KAPSO_WEBHOOK_PATH"),
                extra.get("webhook_path"),
                DEFAULT_WEBHOOK_PATH,
            )
        )
        self.request_timeout_seconds = _coerce_float(
            _first_nonempty(
                os.getenv("KAPSO_REQUEST_TIMEOUT_SECONDS"),
                extra.get("request_timeout_seconds"),
            ),
            DEFAULT_REQUEST_TIMEOUT_SECONDS,
        )
        self.max_body_bytes = _coerce_int(
            _first_nonempty(os.getenv("KAPSO_MAX_BODY_BYTES"), extra.get("max_body_bytes")),
            DEFAULT_MAX_BODY_BYTES,
        )

        self._runner = None
        self._session: Optional[ClientSession] = None
        self._seen_message_ids: OrderedDict[str, float] = OrderedDict()

    @property
    def name(self) -> str:
        return "Kapso"

    async def connect(self) -> bool:
        """Start the Kapso webhook listener and prepare outbound HTTP."""
        if not AIOHTTP_AVAILABLE:
            self._set_fatal_error("missing_dependency", "aiohttp is not installed", retryable=False)
            logger.error("[kapso] aiohttp is required. Install with: pip install aiohttp")
            return False
        if not validate_config(self.config):
            message = (
                "KAPSO_API_KEY and KAPSO_WEBHOOK_SECRET are required "
                "unless KAPSO_VERIFY_WEBHOOK_SIGNATURES=false"
            )
            self._set_fatal_error("config_missing", message, retryable=False)
            logger.error("[kapso] %s", message)
            return False

        timeout = ClientTimeout(total=self.request_timeout_seconds)
        self._session = ClientSession(timeout=timeout)

        app = web.Application(client_max_size=self.max_body_bytes)
        app.router.add_get("/health", self._handle_health)
        app.router.add_post(self.webhook_path, self._handle_webhook)

        self._runner = web.AppRunner(app, access_log=None)
        try:
            await self._runner.setup()
            site = web.TCPSite(self._runner, self.host, self.port)
            await site.start()
        except Exception as exc:
            await self.disconnect()
            self._set_fatal_error("listen_failed", str(exc), retryable=True)
            logger.error("[kapso] failed to start webhook listener: %s", exc)
            return False

        self._mark_connected()
        logger.info("[kapso] webhook listening on http://%s:%s%s", self.host, self.port, self.webhook_path)
        return True

    async def disconnect(self) -> None:
        """Stop the webhook listener and close outbound HTTP."""
        if self._runner is not None:
            try:
                await self._runner.cleanup()
            finally:
                self._runner = None
        if self._session is not None:
            try:
                await self._session.close()
            finally:
                self._session = None
        self._mark_disconnected()

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a text message to a WhatsApp recipient via Kapso."""
        del reply_to, metadata
        if not self._session:
            return SendResult(success=False, error="Kapso adapter is not connected", retryable=True)

        resolved = self._resolve_chat_id(chat_id)
        if not resolved:
            return SendResult(
                success=False,
                error=(
                    "Kapso chat_id must be a WhatsApp recipient number, "
                    "phone_number_id:recipient, or kapso:<encoded_phone>:<encoded_recipient>"
                ),
            )
        phone_number_id, recipient = resolved
        if not phone_number_id:
            return SendResult(
                success=False,
                error="KAPSO_PHONE_NUMBER_ID is required when chat_id does not include a phone number ID",
            )

        text = _to_whatsapp_text(content or "")
        chunks = _split_text(text, MAX_WHATSAPP_TEXT_LENGTH) or [" "]
        message_ids: List[str] = []
        raw_responses: List[Any] = []
        for chunk in chunks:
            result = await _send_text_via_kapso(
                session=self._session,
                base_url=self.base_url,
                graph_version=self.graph_version,
                api_key=self.api_key,
                phone_number_id=phone_number_id,
                recipient=recipient,
                body=chunk,
            )
            raw_responses.append(result.get("raw"))
            if result.get("error"):
                return SendResult(
                    success=False,
                    error=str(result["error"]),
                    raw_response=result.get("raw"),
                    retryable=bool(result.get("retryable")),
                )
            if result.get("message_id"):
                message_ids.append(str(result["message_id"]))

        return SendResult(
            success=True,
            message_id=message_ids[-1] if message_ids else None,
            continuation_message_ids=tuple(message_ids[:-1]),
            raw_response=raw_responses,
        )

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """WhatsApp typing indicators require a concrete inbound message ID.

        Hermes' generic typing loop usually only passes thread metadata, so
        this is a best-effort no-op unless callers include a Kapso/WhatsApp
        message ID explicitly.
        """
        if not self._session:
            return
        message_id = None
        if isinstance(metadata, dict):
            message_id = (
                metadata.get("kapso_message_id")
                or metadata.get("whatsapp_message_id")
                or metadata.get("message_id")
            )
        if not message_id:
            return
        resolved = self._resolve_chat_id(chat_id)
        if not resolved:
            return
        phone_number_id, _recipient = resolved
        if not phone_number_id:
            return
        try:
            await _mark_read_typing(
                session=self._session,
                base_url=self.base_url,
                graph_version=self.graph_version,
                api_key=self.api_key,
                phone_number_id=phone_number_id,
                message_id=str(message_id),
            )
        except Exception:
            logger.debug("[kapso] typing indicator failed", exc_info=True)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        resolved = self._resolve_chat_id(chat_id)
        if not resolved:
            return {"name": chat_id, "type": "dm"}
        _phone_number_id, recipient = resolved
        return {"name": recipient, "type": "dm"}

    async def _handle_health(self, _request) -> "web.Response":
        return web.json_response({"status": "ok", "platform": "kapso"})

    async def _handle_webhook(self, request) -> "web.Response":
        if request.content_length and request.content_length > self.max_body_bytes:
            return web.Response(status=413, text="Payload too large")

        raw_body = await request.read()
        if self.verify_webhook_signatures:
            if not self.webhook_secret:
                return web.Response(status=500, text="Webhook signature verification is not configured")
            signature = request.headers.get("X-Webhook-Signature")
            if not verify_kapso_signature(raw_body, signature, self.webhook_secret):
                return web.Response(status=401, text="Invalid signature")

        event_header = request.headers.get("X-Webhook-Event")
        batch_header = request.headers.get("X-Webhook-Batch")
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return web.Response(status=400, text="Invalid JSON")

        processed = 0
        for kapso_event in _extract_kapso_events(payload, batch_header=batch_header):
            event_name = _read_str(kapso_event, "event", "type") or event_header
            if event_name and event_name != KAPSO_MESSAGE_RECEIVED_EVENT:
                continue
            message_event = self._message_event_from_kapso_event(kapso_event)
            if message_event is None:
                continue
            await self._hydrate_media_event(message_event, kapso_event)
            if self._is_seen(message_event.message_id):
                continue
            await self.handle_message(message_event)
            self._remember_seen(message_event.message_id)
            processed += 1

        logger.debug("[kapso] processed %d webhook message(s)", processed)
        return web.Response(status=200, text="OK")

    def _message_event_from_kapso_event(self, event: Dict[str, Any]) -> Optional[MessageEvent]:
        message = _record(event.get("message"))
        if message is None and _looks_like_whatsapp_message(event):
            message = event
        if message is None:
            return None

        kapso = _record(message.get("kapso")) or {}
        if _read_str(kapso, "direction") == "outbound":
            return None

        conversation = _record(event.get("conversation"))
        phone_number_id = (
            _read_str(event, "phone_number_id", "phoneNumberId")
            or _read_str(conversation, "phone_number_id", "phoneNumberId")
            or _read_str(kapso, "phoneNumberId", "phone_number_id")
            or self.default_phone_number_id
        )
        wa_id = (
            _read_str(message, "from")
            or _normalize_phone(_read_str(conversation, "phone_number", "phoneNumber"))
            or _read_str(message, "to")
        )
        if not phone_number_id or not wa_id:
            logger.warning("[kapso] skipping webhook message without phone_number_id or participant")
            return None

        message_id = _read_str(message, "id") or f"kapso-{int(time.time() * 1000)}"
        conversation_id = _read_str(conversation, "id") or _read_str(
            kapso,
            "whatsappConversationId",
            "whatsapp_conversation_id",
        )
        contact_name = _contact_name(message, conversation, wa_id)
        text = _message_text(message)
        message_type = _message_type(message)
        chat_id = encode_kapso_chat_id(phone_number_id, wa_id, conversation_id)

        if not text:
            text = _fallback_text_for_type(message)

        source = self.build_source(
            chat_id=chat_id,
            chat_name=contact_name,
            chat_type="dm",
            user_id=wa_id,
            user_name=contact_name,
            message_id=message_id,
        )

        return MessageEvent(
            text=text,
            message_type=message_type,
            source=source,
            raw_message=event,
            message_id=message_id,
            reply_to_message_id=_read_str(_record(message.get("context")), "id"),
            timestamp=_message_timestamp(message),
        )

    async def _hydrate_media_event(self, event: MessageEvent, kapso_event: Dict[str, Any]) -> None:
        """Download inbound media into Hermes' local caches for vision/STT/document tools."""
        if event.message_type not in {
            MessageType.PHOTO,
            MessageType.AUDIO,
            MessageType.VOICE,
            MessageType.DOCUMENT,
        }:
            return
        if getattr(event, "media_urls", None):
            return
        if not self._session:
            return

        message = _record(kapso_event.get("message"))
        if message is None and _looks_like_whatsapp_message(kapso_event):
            message = kapso_event
        if not message:
            return

        if event.message_type == MessageType.PHOTO:
            kind = "image"
        elif event.message_type == MessageType.DOCUMENT:
            kind = "document"
        else:
            kind = "audio"
        media = _record(message.get(kind))
        if not media:
            return

        media_url, mime_type = await self._resolve_media_download(kapso_event, message, media, kind)
        if not media_url:
            logger.info(
                "[kapso] %s message %s has no downloadable media URL yet",
                kind,
                event.message_id,
            )
            return

        ext = _media_extension(kind, media, mime_type, default=_default_media_ext(kind))
        try:
            headers = _kapso_download_headers(media_url, self.api_key)
            async with self._session.get(media_url, headers=headers) as response:
                if response.status >= 400:
                    text = await response.text()
                    logger.warning(
                        "[kapso] failed to download %s media %s: HTTP %s %s",
                        kind,
                        event.message_id,
                        response.status,
                        _compact(text),
                    )
                    return
                data = await response.read()
                content_type = response.headers.get("Content-Type", "")
        except Exception as exc:
            logger.warning("[kapso] failed to download %s media %s: %s", kind, event.message_id, exc)
            return

        if content_type:
            ext = _media_extension(kind, {"mime_type": content_type}, content_type, default=ext)
            mime_type = content_type.split(";", 1)[0].strip() or mime_type
        try:
            if kind == "image":
                cached_path = _cache_image_bytes(data, ext)
                default_mime = f"image/{ext.lstrip('.')}"
            elif kind == "audio":
                cached_path = _cache_audio_bytes(data, ext)
                default_mime = f"audio/{ext.lstrip('.')}"
            else:
                filename = _document_cache_filename(media, ext)
                cached_path = _cache_document_bytes(data, filename)
                default_mime = _DOCUMENT_EXT_MIME.get(ext, "application/octet-stream")
                _inject_text_document_content(event, data, filename, ext)
        except Exception as exc:
            logger.warning("[kapso] failed to cache %s media %s: %s", kind, event.message_id, exc)
            return

        event.media_urls = [cached_path]
        event.media_types = [mime_type or default_mime]
        logger.info("[kapso] cached inbound %s %s at %s", kind, event.message_id, cached_path)

    async def _resolve_media_download(
        self,
        kapso_event: Dict[str, Any],
        message: Dict[str, Any],
        media: Dict[str, Any],
        kind: str,
    ) -> Tuple[str, str]:
        kapso = _record(message.get("kapso")) or {}
        media_data = _record(kapso.get("mediaData")) or _record(kapso.get("media_data")) or {}
        direct_url = (
            _read_str(kapso, "downloadUrl", "download_url", "mediaUrl", "media_url")
            or _read_str(media_data, "downloadUrl", "download_url", "url", "mediaUrl", "media_url")
            or _read_str(media, "link", "url")
        )
        mime_type = _read_str(media, "mime_type", "mimeType") or _read_str(
            media_data,
            "contentType",
            "content_type",
            "mime_type",
            "mimeType",
        )
        if direct_url:
            return direct_url, mime_type or ""

        media_id = _read_str(media, "id")
        if not media_id:
            return "", mime_type or ""

        phone_number_id = (
            _read_str(kapso_event, "phone_number_id", "phoneNumberId")
            or _read_str(message, "phone_number_id", "phoneNumberId")
            or _read_str(kapso, "phoneNumberId", "phone_number_id")
            or self.default_phone_number_id
        )
        if not phone_number_id:
            logger.info("[kapso] cannot fetch %s media %s without phone_number_id", kind, media_id)
            return "", mime_type or ""

        metadata_url = _build_media_metadata_url(
            self.base_url,
            self.graph_version,
            media_id,
            phone_number_id,
        )
        try:
            async with self._session.get(
                metadata_url,
                headers={"X-API-Key": self.api_key},
            ) as response:
                raw_text = await response.text()
                if response.status >= 400:
                    logger.warning(
                        "[kapso] media metadata fetch failed for %s: HTTP %s %s",
                        media_id,
                        response.status,
                        _compact(raw_text),
                    )
                    return "", mime_type or ""
                metadata = json.loads(raw_text) if raw_text else {}
        except Exception as exc:
            logger.warning("[kapso] media metadata fetch failed for %s: %s", media_id, exc)
            return "", mime_type or ""

        record = _record(metadata) or {}
        return (
            _read_str(record, "downloadUrl", "download_url", "url") or "",
            _read_str(record, "mime_type", "mimeType") or mime_type or "",
        )

    def _resolve_chat_id(self, chat_id: str) -> Optional[Tuple[Optional[str], str]]:
        value = str(chat_id or "").strip()
        if not value:
            return None

        if value.startswith("kapso:"):
            parts = value.split(":")
            if len(parts) not in {3, 4}:
                return None
            phone_number_id = _decode_part(parts[1])
            recipient = _decode_part(parts[2])
            if not phone_number_id or not recipient:
                return None
            return phone_number_id, _normalize_phone(recipient) or recipient

        if ":" in value:
            phone_number_id, recipient = value.split(":", 1)
            phone_number_id = phone_number_id.strip()
            recipient = _normalize_phone(recipient) or recipient.strip()
            if phone_number_id and recipient:
                return phone_number_id, recipient

        recipient = _normalize_phone(value) or value
        return self.default_phone_number_id, recipient

    def _is_seen(self, message_id: Optional[str]) -> bool:
        if not message_id:
            return False
        return message_id in self._seen_message_ids

    def _remember_seen(self, message_id: Optional[str]) -> None:
        if not message_id:
            return
        self._seen_message_ids[message_id] = time.time()
        self._seen_message_ids.move_to_end(message_id)
        while len(self._seen_message_ids) > SEEN_MESSAGE_CACHE_SIZE:
            self._seen_message_ids.popitem(last=False)


def check_requirements() -> bool:
    """Return True when adapter runtime dependencies are importable."""
    return AIOHTTP_AVAILABLE


def validate_config(config) -> bool:
    """Validate credentials and webhook security settings."""
    extra = getattr(config, "extra", {}) or {}
    api_key = _first_nonempty(
        os.getenv("KAPSO_API_KEY"),
        getattr(config, "api_key", None),
        extra.get("api_key"),
        extra.get("kapso_api_key"),
    )
    verify = _coerce_bool(
        _first_defined(
            os.getenv("KAPSO_VERIFY_WEBHOOK_SIGNATURES"),
            extra.get("verify_webhook_signatures"),
        ),
        default=True,
    )
    secret = _first_nonempty(os.getenv("KAPSO_WEBHOOK_SECRET"), extra.get("webhook_secret"))
    return bool(api_key and (secret or not verify))


def is_connected(config) -> bool:
    """Surface Kapso as configured in Hermes status without constructing it."""
    return validate_config(config)


def _env_enablement() -> Optional[Dict[str, Any]]:
    api_key = os.getenv("KAPSO_API_KEY", "").strip()
    if not api_key:
        return None
    verify = _coerce_bool(os.getenv("KAPSO_VERIFY_WEBHOOK_SIGNATURES"), default=True)
    webhook_secret = os.getenv("KAPSO_WEBHOOK_SECRET", "").strip()
    if verify and not webhook_secret:
        return None

    seed: Dict[str, Any] = {"api_key": api_key}
    _seed_env(seed, "base_url", "KAPSO_BASE_URL")
    _seed_env(seed, "graph_version", "KAPSO_GRAPH_VERSION", "META_GRAPH_VERSION")
    _seed_env(seed, "phone_number_id", "KAPSO_PHONE_NUMBER_ID", "WHATSAPP_PHONE_NUMBER_ID")
    _seed_env(seed, "webhook_secret", "KAPSO_WEBHOOK_SECRET")
    _seed_env(seed, "host", "KAPSO_HOST")
    _seed_int_env(seed, "port", "KAPSO_PORT")
    _seed_env(seed, "webhook_path", "KAPSO_WEBHOOK_PATH")
    if os.getenv("KAPSO_VERIFY_WEBHOOK_SIGNATURES") is not None:
        seed["verify_webhook_signatures"] = verify
    home = os.getenv("KAPSO_HOME_CHANNEL", "").strip()
    if home:
        seed["home_channel"] = {
            "chat_id": home,
            "name": os.getenv("KAPSO_HOME_CHANNEL_NAME", home),
        }
    return seed


def _apply_yaml_config(_yaml_cfg: dict, platform_cfg: dict) -> Optional[dict]:
    """Bridge simple `kapso:` YAML keys into PlatformConfig.extra."""
    if not isinstance(platform_cfg, dict):
        return None
    extra: Dict[str, Any] = {}
    for key in (
        "api_key",
        "kapso_api_key",
        "base_url",
        "graph_version",
        "phone_number_id",
        "webhook_secret",
        "verify_webhook_signatures",
        "host",
        "port",
        "webhook_path",
        "request_timeout_seconds",
        "max_body_bytes",
    ):
        if key in platform_cfg:
            extra[key] = platform_cfg[key]
    return extra or None


async def _standalone_send(
    pconfig,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[List[str]] = None,
    force_document: bool = False,
) -> Dict[str, Any]:
    """Out-of-process delivery for cron jobs and send_message fallbacks."""
    del thread_id, force_document
    if not AIOHTTP_AVAILABLE:
        return {"error": "Kapso standalone send: aiohttp is not installed"}
    extra = getattr(pconfig, "extra", {}) or {}
    api_key = _first_nonempty(
        os.getenv("KAPSO_API_KEY"),
        getattr(pconfig, "api_key", None),
        extra.get("api_key"),
        extra.get("kapso_api_key"),
    )
    if not api_key:
        return {"error": "Kapso standalone send: KAPSO_API_KEY is required"}

    base_url = _normalize_base_url(
        _first_nonempty(os.getenv("KAPSO_BASE_URL"), extra.get("base_url"), DEFAULT_BASE_URL)
    )
    graph_version = _strip_slashes(
        _first_nonempty(
            os.getenv("KAPSO_GRAPH_VERSION"),
            os.getenv("META_GRAPH_VERSION"),
            extra.get("graph_version"),
            DEFAULT_GRAPH_VERSION,
        )
    )
    default_phone_number_id = _first_nonempty(
        os.getenv("KAPSO_PHONE_NUMBER_ID"),
        os.getenv("WHATSAPP_PHONE_NUMBER_ID"),
        extra.get("phone_number_id"),
    )
    resolved = _resolve_chat_id_static(chat_id, default_phone_number_id)
    if not resolved:
        return {"error": "Kapso standalone send: chat_id is empty or invalid"}
    phone_number_id, recipient = resolved
    if not phone_number_id:
        return {"error": "Kapso standalone send: KAPSO_PHONE_NUMBER_ID is required"}

    text = _to_whatsapp_text(message or "")
    if media_files:
        text = f"{text}\n\n[{len(media_files)} attachment(s) generated; send media via a live gateway adapter.]".strip()
    chunks = _split_text(text, MAX_WHATSAPP_TEXT_LENGTH) or [" "]

    message_ids: List[str] = []
    timeout = ClientTimeout(total=DEFAULT_REQUEST_TIMEOUT_SECONDS)
    async with ClientSession(timeout=timeout) as session:
        for chunk in chunks:
            result = await _send_text_via_kapso(
                session=session,
                base_url=base_url,
                graph_version=graph_version,
                api_key=api_key,
                phone_number_id=phone_number_id,
                recipient=recipient,
                body=chunk,
            )
            if result.get("error"):
                return {"error": f"Kapso standalone send failed: {result['error']}"}
            if result.get("message_id"):
                message_ids.append(str(result["message_id"]))
    return {
        "success": True,
        "platform": "kapso",
        "chat_id": chat_id,
        "message_id": message_ids[-1] if message_ids else None,
    }


def interactive_setup() -> None:
    """Small `hermes gateway setup` wizard for Kapso."""
    print()
    print("Kapso WhatsApp setup")
    print("--------------------")
    print("Paste your Kapso API key first. Then run `hermes kapso setup --install-cli`")
    print("to choose a WhatsApp number and configure the webhook from the terminal.")
    print()

    try:
        from hermes_cli.config import get_env_var, set_env_var
    except ImportError:
        print("hermes_cli.config is not available. Set KAPSO_* vars manually in ~/.hermes/.env.")
        return

    def prompt(var: str, label: str, *, secret: bool = False) -> None:
        existing = get_env_var(var) if callable(get_env_var) else None
        suffix = " [keep current]" if existing else ""
        try:
            if secret:
                try:
                    value = _masked_secret_prompt(f"{label}{suffix}: ")
                except Exception:
                    value = input(f"{label}{suffix}: ").strip()
            else:
                value = input(f"{label}{suffix}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if value:
            set_env_var(var, value)

    prompt("KAPSO_API_KEY", "Kapso API key", secret=True)
    print("Done. Continue with: hermes kapso setup --install-cli")


def _setup_kapso_cli_command(parser) -> None:
    parser.add_argument(
        "action",
        nargs="?",
        choices=("guide", "setup", "status", "install-cli"),
        default="guide",
        help="What to do. Defaults to guide.",
    )
    parser.add_argument("--api-key", help="Save KAPSO_API_KEY non-interactively.")
    parser.add_argument(
        "--webhook-secret",
        help="Save KAPSO_WEBHOOK_SECRET non-interactively.",
    )
    parser.add_argument(
        "--funnel-url",
        help="Public Tailscale Funnel/base URL. /kapso/webhook is appended automatically.",
    )
    parser.add_argument(
        "--webhook-url",
        help="Full public Kapso webhook endpoint URL.",
    )
    parser.add_argument(
        "--configure-webhook",
        action="store_true",
        help="Use the Kapso CLI to create the WhatsApp webhook after setup.",
    )
    parser.add_argument(
        "--phone-number-id",
        help="Save KAPSO_PHONE_NUMBER_ID non-interactively.",
    )
    parser.add_argument(
        "--home-channel",
        help="Save KAPSO_HOME_CHANNEL non-interactively.",
    )
    parser.add_argument(
        "--allowed-users",
        help="Comma-separated WhatsApp wa_id values allowed to use the bot.",
    )
    parser.add_argument(
        "--allow-all-users",
        action="store_true",
        help="Set KAPSO_ALLOW_ALL_USERS=true for development.",
    )
    parser.add_argument(
        "--install-cli",
        action="store_true",
        help="Also install the Kapso CLI with npm install -g @kapso/cli.",
    )
    parser.add_argument(
        "--no-prompt",
        action="store_true",
        help="Do not prompt for missing values; only save supplied flags.",
    )
    parser.set_defaults(func=_kapso_cli_command)


def _kapso_cli_command(args) -> None:
    action = getattr(args, "action", "guide")
    if action == "install-cli":
        _install_kapso_cli()
        return
    if action == "status":
        _print_kapso_status()
        return
    if action == "setup":
        _run_kapso_setup_command(args)
        return
    _print_kapso_guide()


def _run_kapso_setup_command(args) -> None:
    no_prompt = bool(getattr(args, "no_prompt", False))
    _save_or_prompt_env(
        "KAPSO_API_KEY",
        "Kapso API key",
        value=getattr(args, "api_key", None),
        secret=True,
        no_prompt=no_prompt,
    )
    if getattr(args, "install_cli", False):
        _install_kapso_cli()

    use_cli_setup = bool(getattr(args, "configure_webhook", False))
    if not no_prompt and _get_env_value("KAPSO_API_KEY"):
        if not shutil.which("kapso") and _yes_no_prompt(
            "Install the Kapso CLI now?",
            default=bool(getattr(args, "install_cli", False)),
        ):
            _install_kapso_cli()
        if shutil.which("kapso"):
            use_cli_setup = _yes_no_prompt(
                "Use the Kapso CLI to choose a WhatsApp number and create the webhook?",
                default=True,
            )

    if use_cli_setup:
        if not _run_kapso_cli_setup_flow(args):
            _run_manual_kapso_setup_prompts(args, no_prompt=no_prompt)
    else:
        _run_manual_kapso_setup_prompts(args, no_prompt=no_prompt)

    allowed_users = getattr(args, "allowed_users", None)
    if allowed_users:
        cleaned = ",".join(
            part.strip() for part in allowed_users.split(",") if part.strip()
        )
        if cleaned:
            _save_env_value("KAPSO_ALLOWED_USERS", cleaned)
            _save_env_value("KAPSO_ALLOW_ALL_USERS", "false")
            print("Saved KAPSO_ALLOWED_USERS")
    if getattr(args, "allow_all_users", False):
        _save_env_value("KAPSO_ALLOW_ALL_USERS", "true")
        print("Saved KAPSO_ALLOW_ALL_USERS=true")
    _print_kapso_status()
    _print_webhook_instructions()


def _run_manual_kapso_setup_prompts(args, *, no_prompt: bool) -> None:
    _save_or_prompt_env(
        "KAPSO_WEBHOOK_SECRET",
        "Kapso webhook secret",
        value=getattr(args, "webhook_secret", None),
        secret=True,
        no_prompt=no_prompt,
    )
    _save_or_prompt_env(
        "KAPSO_PHONE_NUMBER_ID",
        "Default WhatsApp phone_number_id",
        value=getattr(args, "phone_number_id", None),
        no_prompt=no_prompt,
    )
    _save_or_prompt_env(
        "KAPSO_HOME_CHANNEL",
        "Default WhatsApp recipient for cron delivery",
        value=getattr(args, "home_channel", None),
        no_prompt=no_prompt,
    )


def _save_or_prompt_env(
    name: str,
    label: str,
    *,
    value: Optional[str] = None,
    secret: bool = False,
    no_prompt: bool = False,
) -> None:
    current = _get_env_value(name)
    if value:
        _save_env_value(name, value)
        print(f"Saved {name}")
        return
    if no_prompt:
        return
    suffix = " [keep current]" if current else ""
    try:
        if secret:
            try:
                value = _masked_secret_prompt(f"{label}{suffix}: ").strip()
            except Exception:
                value = input(f"{label}{suffix}: ").strip()
        else:
            value = input(f"{label}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return
    if value:
        _save_env_value(name, value)
        print(f"Saved {name}")


def _install_kapso_cli() -> bool:
    if shutil.which("kapso"):
        print("Kapso CLI is already installed:")
        _run_command(["kapso", "--version"])
        return True
    npm = shutil.which("npm")
    if not npm:
        print("npm was not found. Install Node.js/npm, then run:")
        print("  npm install -g @kapso/cli")
        return False
    print("Installing Kapso CLI with npm install -g @kapso/cli ...")
    result = subprocess.run([npm, "install", "-g", "@kapso/cli"], check=False)
    if result.returncode != 0:
        print("Kapso CLI install failed. You can retry manually:")
        print("  npm install -g @kapso/cli")
        return False
    print("Kapso CLI installed.")
    _run_command(["kapso", "--version"])
    return True


def _run_kapso_cli_setup_flow(args) -> bool:
    if not shutil.which("kapso"):
        print("Kapso CLI is not installed. Run: hermes kapso setup --install-cli")
        return False

    no_prompt = bool(getattr(args, "no_prompt", False))
    phone_number_id = (getattr(args, "phone_number_id", None) or "").strip()
    if not phone_number_id:
        numbers = _load_kapso_phone_numbers()
        phone_number_id = _select_kapso_phone_number(numbers, no_prompt=no_prompt)
    if not phone_number_id:
        print("Could not choose a WhatsApp phone_number_id from the Kapso CLI.")
        return False
    _save_env_value("KAPSO_PHONE_NUMBER_ID", phone_number_id)
    print("Saved KAPSO_PHONE_NUMBER_ID")

    home_channel = (getattr(args, "home_channel", None) or "").strip()
    if not home_channel and not no_prompt:
        current = _get_env_value("KAPSO_HOME_CHANNEL")
        suffix = f" [{current}]" if current else ""
        home_channel = input(
            f"Your WhatsApp number/wa_id for testing and allowlist{suffix}: "
        ).strip() or current
    if home_channel:
        normalized_home = _normalize_phone(home_channel) or home_channel
        _save_env_value("KAPSO_HOME_CHANNEL", normalized_home)
        print("Saved KAPSO_HOME_CHANNEL")
        if not getattr(args, "allowed_users", None) and not _get_env_value("KAPSO_ALLOWED_USERS"):
            _save_env_value("KAPSO_ALLOWED_USERS", normalized_home)
            _save_env_value("KAPSO_ALLOW_ALL_USERS", "false")
            print("Saved KAPSO_ALLOWED_USERS")

    webhook_url = _resolve_setup_webhook_url(args, no_prompt=no_prompt)
    if not webhook_url:
        print("Skipped webhook creation because no public Funnel/webhook URL was provided.")
        return True

    webhook_secret = (getattr(args, "webhook_secret", None) or "").strip()
    if not webhook_secret:
        webhook_secret = _get_env_value("KAPSO_WEBHOOK_SECRET")
    if not webhook_secret:
        webhook_secret = secrets.token_urlsafe(32)
        print("Generated KAPSO_WEBHOOK_SECRET")
    _save_env_value("KAPSO_WEBHOOK_SECRET", webhook_secret)
    _save_env_value("KAPSO_WEBHOOK_URL", webhook_url)

    if _create_kapso_webhook(phone_number_id, webhook_url, webhook_secret):
        print("Kapso webhook created.")
    else:
        print("Kapso webhook creation failed. Use the printed settings below as a fallback.")
    return True


def _load_kapso_phone_numbers() -> List[Dict[str, Any]]:
    result = _run_command_capture(["kapso", "whatsapp", "numbers", "list", "--output", "json"])
    if result.returncode != 0:
        print("Could not list Kapso WhatsApp numbers.")
        if result.stderr.strip():
            print(result.stderr.strip())
        print("Try `kapso login` or `kapso status`, then rerun `hermes kapso setup`.")
        return []
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        print("Kapso CLI returned non-JSON phone number output.")
        return []
    return _extract_phone_numbers(payload)


def _extract_phone_numbers(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = (
            payload.get("data")
            or payload.get("phone_numbers")
            or payload.get("phoneNumbers")
            or payload.get("items")
            or []
        )
    else:
        items = []
    return [item for item in items if isinstance(item, dict)]


def _select_kapso_phone_number(numbers: List[Dict[str, Any]], *, no_prompt: bool) -> str:
    if not numbers:
        return ""
    if len(numbers) == 1:
        phone_number_id = _phone_number_id(numbers[0])
        if phone_number_id:
            print(f"Using WhatsApp number: {_format_phone_number_option(numbers[0])}")
        return phone_number_id
    print()
    print("Connected WhatsApp numbers")
    print("--------------------------")
    for index, number in enumerate(numbers, start=1):
        print(f"{index}. {_format_phone_number_option(number)}")
    if no_prompt:
        return ""
    while True:
        choice = input("Choose a WhatsApp number: ").strip()
        if not choice:
            return ""
        try:
            index = int(choice)
        except ValueError:
            print("Enter the number from the list.")
            continue
        if 1 <= index <= len(numbers):
            return _phone_number_id(numbers[index - 1])
        print("Choice out of range.")


def _phone_number_id(number: Dict[str, Any]) -> str:
    return _read_str(number, "phone_number_id", "phoneNumberId", "id", "meta_phone_number_id") or ""


def _display_phone_number(number: Dict[str, Any]) -> str:
    return (
        _read_str(number, "display_phone_number", "displayPhoneNumber", "phone_number", "phoneNumber")
        or _phone_number_id(number)
    )


def _format_phone_number_option(number: Dict[str, Any]) -> str:
    phone_number_id = _phone_number_id(number)
    label = _read_str(number, "name", "label", "customer_name", "customerName")
    display = _display_phone_number(number)
    bits = [display]
    if label and label != display:
        bits.append(label)
    if phone_number_id and phone_number_id != display:
        bits.append(f"id={phone_number_id}")
    return " | ".join(bits)


def _resolve_setup_webhook_url(args, *, no_prompt: bool) -> str:
    webhook_url = (getattr(args, "webhook_url", None) or "").strip()
    funnel_url = (getattr(args, "funnel_url", None) or "").strip()
    if webhook_url:
        return webhook_url.rstrip("/")
    if not webhook_url and funnel_url:
        webhook_url = _webhook_url_from_base(funnel_url)
    if not webhook_url and not no_prompt:
        current_hint = _webhook_url_from_base("https://<your-funnel-host>")
        value = input(f"Public Funnel/webhook URL [{current_hint}]: ").strip()
        if value:
            webhook_url = _webhook_url_from_base(value)
    return webhook_url.rstrip("/") if webhook_url else ""


def _webhook_url_from_base(value: str) -> str:
    url = str(value or "").strip().rstrip("/")
    if not url:
        return ""
    if url.endswith(DEFAULT_WEBHOOK_PATH):
        return url
    return f"{url}{DEFAULT_WEBHOOK_PATH}"


def _create_kapso_webhook(phone_number_id: str, webhook_url: str, webhook_secret: str) -> bool:
    argv = [
        "kapso",
        "whatsapp",
        "webhooks",
        "new",
        "--phone-number-id",
        phone_number_id,
        "--url",
        webhook_url,
        "--event",
        KAPSO_MESSAGE_RECEIVED_EVENT,
        "--payload-version",
        "v2",
        "--kind",
        "kapso",
        "--secret-key",
        webhook_secret,
        "--active",
        "--output",
        "json",
    ]
    result = _run_command_capture(argv)
    if result.returncode != 0:
        if result.stderr.strip():
            print(result.stderr.strip())
        elif result.stdout.strip():
            print(result.stdout.strip())
        return False
    try:
        payload = json.loads(result.stdout or "{}")
        webhook_id = _read_str(_record(payload.get("data")) or payload, "id")
        if webhook_id:
            print(f"Webhook ID: {webhook_id}")
    except json.JSONDecodeError:
        pass
    return True


def _yes_no_prompt(label: str, *, default: bool) -> bool:
    suffix = "Y/n" if default else "y/N"
    try:
        value = input(f"{label} [{suffix}]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    if not value:
        return default
    return value in {"y", "yes", "1", "true"}


def _run_command_capture(argv: List[str]) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    api_key = _get_env_value("KAPSO_API_KEY")
    if api_key:
        env["KAPSO_API_KEY"] = api_key
    return subprocess.run(argv, check=False, capture_output=True, text=True, env=env)


def _print_kapso_status() -> None:
    print()
    print("Kapso Hermes plugin status")
    print("--------------------------")
    for name in (
        "KAPSO_API_KEY",
        "KAPSO_WEBHOOK_SECRET",
        "KAPSO_PHONE_NUMBER_ID",
        "KAPSO_HOME_CHANNEL",
        "KAPSO_WEBHOOK_URL",
        "KAPSO_ALLOWED_USERS",
        "KAPSO_ALLOW_ALL_USERS",
    ):
        value = _get_env_value(name)
        if name in {"KAPSO_API_KEY", "KAPSO_WEBHOOK_SECRET"}:
            display = "set" if value else "missing"
        else:
            display = value or "missing"
        print(f"{name}: {display}")
    if shutil.which("kapso"):
        print("Kapso CLI: installed")
    else:
        print("Kapso CLI: missing (run `hermes kapso install-cli`)")


def _print_kapso_guide() -> None:
    print(
        """
Kapso Hermes setup
------------------
1. Install and enable the plugin:
   hermes plugins install gokapso/hermes-agent-plugin --enable

2. Configure credentials and optionally install the Kapso CLI:
   hermes kapso setup --install-cli

   To let the setup command choose a connected number and create the webhook:
   hermes kapso setup --install-cli --funnel-url https://<your-funnel-host>

   Non-interactive production setup:
   hermes kapso setup --phone-number-id <id> --home-channel 15551234567 \\
     --allowed-users 15551234567 --funnel-url https://<host> \\
     --configure-webhook --no-prompt

   For local testing only:
   hermes kapso setup --allow-all-users --no-prompt

3. Kapso webhook settings:
   endpoint: https://<your-public-host>/kapso/webhook
   events: whatsapp.message.received
   payload version: v2
   secret: same value as KAPSO_WEBHOOK_SECRET

4. Restart Hermes gateway:
   hermes gateway restart

Helpful checks:
   hermes kapso status
   kapso status
   kapso whatsapp numbers list --output json
""".strip()
    )


def _print_webhook_instructions() -> None:
    endpoint = _get_env_value("KAPSO_WEBHOOK_URL") or "https://<your-public-host>/kapso/webhook"
    print()
    print("Webhook settings")
    print("----------------")
    print(f"Endpoint URL: {endpoint}")
    print("Events: whatsapp.message.received")
    print("Payload version: v2")
    print("Secret: same value as KAPSO_WEBHOOK_SECRET")
    print()
    print("Restart Hermes gateway after changing env vars:")
    print("  hermes gateway restart")


def _get_env_value(name: str) -> str:
    try:
        from hermes_cli.config import get_env_value

        return str(get_env_value(name) or "")
    except Exception:
        return os.getenv(name, "")


def _save_env_value(name: str, value: str) -> None:
    try:
        from hermes_cli.config import save_env_value

        save_env_value(name, value)
    except Exception:
        os.environ[name] = value


def _masked_secret_prompt(prompt: str) -> str:
    from hermes_cli.secret_prompt import masked_secret_prompt

    value = masked_secret_prompt(prompt)
    # Hermes' raw-mode prompt writes LF while the terminal is still raw, which
    # can move down without returning to column 0 on some terminals.
    sys.stdout.write("\r")
    sys.stdout.flush()
    return value


def _run_command(argv: List[str]) -> int:
    try:
        result = subprocess.run(argv, check=False)
        return int(result.returncode)
    except FileNotFoundError:
        return 127


def register(ctx) -> None:
    """Plugin entry point called by Hermes."""
    ctx.register_platform(
        name="kapso",
        label="Kapso WhatsApp",
        adapter_factory=lambda cfg: KapsoAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["KAPSO_API_KEY"],
        install_hint="pip install aiohttp",
        setup_fn=interactive_setup,
        env_enablement_fn=_env_enablement,
        apply_yaml_config_fn=_apply_yaml_config,
        cron_deliver_env_var="KAPSO_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        allowed_users_env="KAPSO_ALLOWED_USERS",
        allow_all_env="KAPSO_ALLOW_ALL_USERS",
        max_message_length=MAX_WHATSAPP_TEXT_LENGTH,
        emoji="K",
        pii_safe=False,
        allow_update_command=True,
        platform_hint=(
            "You are chatting through Kapso on WhatsApp. WhatsApp supports "
            "plain text plus lightweight formatting such as *bold*, _italic_, "
            "~strikethrough~, inline code, and bare URLs. Keep responses concise; "
            "text messages are capped at 4096 characters and are split when needed. "
            "Outside WhatsApp's active conversation window, proactive messages may "
            "require approved templates."
        ),
    )
    ctx.register_cli_command(
        name="kapso",
        help="Configure the Kapso WhatsApp Hermes plugin",
        description=(
            "Configure Kapso credentials, install/check the Kapso CLI, and "
            "print webhook settings for the Hermes platform adapter."
        ),
        setup_fn=_setup_kapso_cli_command,
    )


def verify_kapso_signature(raw_body: bytes | str, signature_header: Optional[str], webhook_secret: str) -> bool:
    """Verify Kapso's X-Webhook-Signature HMAC-SHA256 header."""
    if not signature_header or not webhook_secret:
        return False
    signature = signature_header.strip()
    if signature.startswith("sha256="):
        signature = signature.split("=", 1)[1]
    if not signature:
        return False
    body = raw_body.encode("utf-8") if isinstance(raw_body, str) else raw_body
    expected = hmac.new(webhook_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    try:
        received_bytes = bytes.fromhex(signature)
        expected_bytes = bytes.fromhex(expected)
    except ValueError:
        return False
    return len(received_bytes) == len(expected_bytes) and hmac.compare_digest(received_bytes, expected_bytes)


def encode_kapso_chat_id(phone_number_id: str, wa_id: str, conversation_id: Optional[str] = None) -> str:
    parts = ["kapso", _encode_part(phone_number_id), _encode_part(wa_id)]
    if conversation_id:
        parts.append(_encode_part(conversation_id))
    return ":".join(parts)


def _resolve_chat_id_static(
    chat_id: str,
    default_phone_number_id: Optional[str],
) -> Optional[Tuple[Optional[str], str]]:
    value = str(chat_id or "").strip()
    if not value:
        return None
    if value.startswith("kapso:"):
        parts = value.split(":")
        if len(parts) not in {3, 4}:
            return None
        phone_number_id = _decode_part(parts[1])
        recipient = _decode_part(parts[2])
        if not phone_number_id or not recipient:
            return None
        return phone_number_id, _normalize_phone(recipient) or recipient
    if ":" in value:
        phone_number_id, recipient = value.split(":", 1)
        phone_number_id = phone_number_id.strip()
        recipient = _normalize_phone(recipient) or recipient.strip()
        if phone_number_id and recipient:
            return phone_number_id, recipient
    recipient = _normalize_phone(value) or value
    return default_phone_number_id, recipient


async def _send_text_via_kapso(
    *,
    session,
    base_url: str,
    graph_version: str,
    api_key: str,
    phone_number_id: str,
    recipient: str,
    body: str,
) -> Dict[str, Any]:
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient,
        "type": "text",
        "text": {"body": body, "preview_url": True},
    }
    url = _messages_url(base_url, graph_version, phone_number_id)
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
    try:
        async with session.post(url, json=payload, headers=headers) as response:
            raw: Any
            try:
                raw = await response.json(content_type=None)
            except Exception:
                raw = await response.text()
            if response.status >= 300:
                return {
                    "error": f"Kapso API HTTP {response.status}: {_compact(raw)}",
                    "raw": raw,
                    "retryable": response.status >= 500,
                }
            return {"success": True, "message_id": _message_id_from_response(raw), "raw": raw}
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return {"error": str(exc), "retryable": True}


async def _mark_read_typing(
    *,
    session,
    base_url: str,
    graph_version: str,
    api_key: str,
    phone_number_id: str,
    message_id: str,
) -> None:
    payload = {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": message_id,
        "typing_indicator": {"type": "text"},
    }
    async with session.post(
        _messages_url(base_url, graph_version, phone_number_id),
        json=payload,
        headers={"X-API-Key": api_key, "Content-Type": "application/json"},
    ):
        pass


def _messages_url(base_url: str, graph_version: str, phone_number_id: str) -> str:
    base = _normalize_base_url(base_url)
    if not _GRAPH_VERSION_RE.search(base):
        base = f"{base}/{_strip_slashes(graph_version)}"
    return f"{base}/{quote(str(phone_number_id), safe='')}/messages"


def _build_media_metadata_url(
    base_url: str,
    graph_version: str,
    media_id: str,
    phone_number_id: str,
) -> str:
    base = _normalize_base_url(base_url)
    if not _GRAPH_VERSION_RE.search(base):
        base = f"{base}/{_strip_slashes(graph_version)}"
    query = urlencode({"phone_number_id": phone_number_id})
    return f"{base}/{quote(str(media_id), safe='')}?{query}"


def _kapso_download_headers(url: str, api_key: str) -> Dict[str, str]:
    if not api_key:
        return {}
    try:
        host = urlsplit(url).hostname or ""
    except Exception:
        host = ""
    if host.lower().endswith("kapso.ai"):
        return {"X-API-Key": api_key}
    return {}


def _media_extension(
    kind: str,
    media: Dict[str, Any],
    mime_type: Optional[str],
    *,
    default: str = ".jpg",
) -> str:
    raw_mime = (mime_type or _read_str(media, "mime_type", "mimeType") or "").split(";", 1)[0]
    if kind == "audio":
        mime_map = _AUDIO_MIME_EXTENSIONS
    elif kind == "document":
        mime_map = _DOCUMENT_MIME_EXTENSIONS
    else:
        mime_map = _IMAGE_MIME_EXTENSIONS
    ext = mime_map.get(raw_mime.strip().lower())
    if ext:
        return ext
    filename = _read_str(media, "filename", "file_name", "name")
    if filename:
        suffix = Path(filename).suffix.lower()
        allowed = set(mime_map.values())
        if suffix in allowed:
            return suffix
    link = _read_str(media, "link", "url")
    if link:
        suffix = Path(urlsplit(link).path).suffix.lower()
        allowed = set(mime_map.values())
        if kind == "image" and suffix == ".jpeg":
            return ".jpg"
        if suffix in allowed:
            return suffix
    return default


def _default_media_ext(kind: str) -> str:
    if kind == "audio":
        return ".ogg"
    if kind == "document":
        return ".bin"
    return ".jpg"


def _document_cache_filename(media: Dict[str, Any], ext: str) -> str:
    raw_name = _read_str(media, "filename", "file_name", "name")
    safe_name = Path(raw_name).name if raw_name else "document"
    safe_name = safe_name.replace("\x00", "").strip()
    if not safe_name or safe_name in {".", ".."}:
        safe_name = "document"
    if not Path(safe_name).suffix and ext:
        safe_name = f"{safe_name}{ext}"
    return safe_name


def _cache_image_bytes(data: bytes, ext: str) -> str:
    if _hermes_cache_image_from_bytes is not None:
        return _hermes_cache_image_from_bytes(data, ext=ext)
    cache_dir = Path(os.path.expanduser("~/.hermes/cache/images"))
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"img_{uuid.uuid4().hex[:12]}{ext}"
    path.write_bytes(data)
    return str(path)


def _cache_audio_bytes(data: bytes, ext: str) -> str:
    if _hermes_cache_audio_from_bytes is not None:
        return _hermes_cache_audio_from_bytes(data, ext=ext)
    cache_dir = Path(os.path.expanduser("~/.hermes/cache/audio"))
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"audio_{uuid.uuid4().hex[:12]}{ext}"
    path.write_bytes(data)
    return str(path)


def _cache_document_bytes(data: bytes, filename: str) -> str:
    if _hermes_cache_document_from_bytes is not None:
        return _hermes_cache_document_from_bytes(data, filename)
    cache_dir = Path(os.path.expanduser("~/.hermes/cache/documents"))
    cache_dir.mkdir(parents=True, exist_ok=True)
    safe_name = Path(filename).name if filename else "document"
    safe_name = safe_name.replace("\x00", "").strip()
    if not safe_name or safe_name in {".", ".."}:
        safe_name = "document"
    path = cache_dir / f"doc_{uuid.uuid4().hex[:12]}_{safe_name}"
    if not path.resolve().is_relative_to(cache_dir.resolve()):
        raise ValueError(f"Path traversal rejected: {filename!r}")
    path.write_bytes(data)
    return str(path)


def _inject_text_document_content(event: MessageEvent, data: bytes, filename: str, ext: str) -> None:
    if ext not in _TEXT_DOCUMENT_EXTENSIONS:
        return
    if len(data) > MAX_TEXT_DOCUMENT_INLINE_BYTES:
        return
    try:
        text_content = data.decode("utf-8").rstrip()
    except UnicodeDecodeError:
        logger.warning("[kapso] could not decode text document %s as UTF-8", filename, exc_info=True)
        return
    display_name = re.sub(r"[^\w.\- ]", "_", Path(filename).name or "document")
    injection = f"[Content of {display_name}]:\n{text_content}"
    current = str(getattr(event, "text", "") or "")
    event.text = f"{injection}\n\n{current}" if current else injection


def _extract_kapso_events(payload: Any, *, batch_header: Optional[str] = None) -> List[Dict[str, Any]]:
    record = _record(payload)
    if record is None:
        return []
    top_event = _read_str(record, "event", "type")
    data = record.get("data")
    if isinstance(data, list):
        events: List[Dict[str, Any]] = []
        for item in (_record(x) for x in data):
            if item is None:
                continue
            if top_event and not _read_str(item, "event", "type"):
                item = {**item, "event": top_event}
            events.append(item)
        return events
    if str(batch_header or "").lower() == "true":
        return []
    return [record]


def _message_text(message: Dict[str, Any]) -> str:
    text = _record(message.get("text"))
    if text and _read_str(text, "body"):
        return _read_str(text, "body") or ""
    kapso = _record(message.get("kapso")) or {}
    transcript = _read_str(_record(kapso.get("transcript")), "text")
    if transcript:
        return f"[voice] {transcript}"
    for media_key in ("image", "video", "document"):
        media = _record(message.get(media_key))
        if media and _read_str(media, "caption"):
            return _read_str(media, "caption") or ""
        if media:
            return _media_description(media_key, media, kapso)
    audio = _record(message.get("audio"))
    if audio:
        return _media_description("audio", audio, kapso)
    location = _record(message.get("location"))
    if location:
        parts = [_read_str(location, "name"), _read_str(location, "address")]
        return "\n".join(part for part in parts if part)
    reaction = _record(message.get("reaction"))
    if reaction and _read_str(reaction, "emoji"):
        return _read_str(reaction, "emoji") or ""
    reply = _interactive_reply(message)
    if reply and _read_str(reply, "title"):
        return _read_str(reply, "title") or ""
    for key in ("orderText", "order_text", "content"):
        value = _read_str(kapso, key)
        if value:
            return value
    return ""


def _message_type(message: Dict[str, Any]) -> MessageType:
    msg_type = (_read_str(message, "type") or "text").lower()
    if msg_type == "audio":
        audio = _record(message.get("audio")) or {}
        if _coerce_bool(_first_defined(audio.get("voice"), audio.get("is_voice")), default=False):
            return MessageType.VOICE
        return MessageType.AUDIO
    return {
        "image": MessageType.PHOTO,
        "video": MessageType.VIDEO,
        "voice": MessageType.VOICE,
        "document": MessageType.DOCUMENT,
        "sticker": MessageType.STICKER,
        "location": MessageType.LOCATION,
    }.get(msg_type, MessageType.TEXT)


def _fallback_text_for_type(message: Dict[str, Any]) -> str:
    msg_type = (_read_str(message, "type") or "message").lower()
    if msg_type == "document":
        document = _record(message.get("document")) or {}
        filename = _read_str(document, "filename")
        return f"[document: {filename}]" if filename else "[document]"
    if msg_type in {"image", "video", "audio", "voice", "sticker", "location", "reaction"}:
        return f"[{msg_type}]"
    return ""


def _media_description(kind: str, media: Dict[str, Any], kapso: Dict[str, Any]) -> str:
    parts = [f"[{kind}]"]
    label = _read_str(media, "filename", "caption")
    mime_type = _read_str(media, "mime_type", "mimeType")
    if label:
        parts.append(label)
    if mime_type:
        parts.append(f"({mime_type})")
    media_data = _record(kapso.get("mediaData")) or _record(kapso.get("media_data")) or {}
    url = (
        _read_str(kapso, "mediaUrl", "media_url")
        or _read_str(media_data, "url", "mediaUrl", "media_url")
    )
    if url:
        parts.append(url)
    return " ".join(parts)


def _interactive_reply(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    interactive = _record(message.get("interactive"))
    if not interactive:
        return None
    for key in ("buttonReply", "button_reply", "listReply", "list_reply"):
        reply = _record(interactive.get(key))
        if reply:
            return reply
    return None


def _contact_name(message: Dict[str, Any], conversation: Optional[Dict[str, Any]], wa_id: str) -> str:
    kapso = _record(message.get("kapso")) or {}
    conversation_kapso = _record(conversation.get("kapso")) if conversation else None
    return (
        _read_str(kapso, "contactName", "contact_name")
        or _read_str(conversation_kapso, "contactName", "contact_name")
        or _read_str(conversation, "contactName", "contact_name")
        or wa_id
    )


def _message_timestamp(message: Dict[str, Any]) -> datetime:
    raw = _read_str(message, "timestamp")
    if not raw:
        return datetime.now()
    try:
        value = float(raw)
        if value > 10_000_000_000:
            value = value / 1000
        return datetime.fromtimestamp(value)
    except (TypeError, ValueError, OSError):
        return datetime.now()


def _looks_like_whatsapp_message(record: Dict[str, Any]) -> bool:
    return bool(_read_str(record, "id") and _read_str(record, "type"))


def _message_id_from_response(raw: Any) -> Optional[str]:
    record = _record(raw)
    if not record:
        return None
    messages = record.get("messages")
    if isinstance(messages, list) and messages:
        first = _record(messages[0])
        if first:
            return _read_str(first, "id")
    return _read_str(record, "id", "message_id", "messageId")


def _to_whatsapp_text(content: str) -> str:
    text = content or ""
    text = _MD_LINK_RE.sub(lambda match: f"{match.group(1)} ({match.group(2)})", text)
    text = _MD_BOLD_RE.sub(r"*\1*", text)
    text = _MD_UNDERLINE_BOLD_RE.sub(r"*\1*", text)
    text = _MD_HEADING_RE.sub("", text)
    return text.strip()


def _split_text(text: str, limit: int) -> List[str]:
    if len(text) <= limit:
        return [text] if text else []
    chunks: List[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        cut = remaining.rfind("\n\n", 0, limit)
        if cut < int(limit * 0.5):
            cut = remaining.rfind("\n", 0, limit)
        if cut < int(limit * 0.5):
            cut = remaining.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    return chunks


def _record(value: Any) -> Optional[Dict[str, Any]]:
    return value if isinstance(value, dict) else None


def _read_str(record: Optional[Dict[str, Any]], *keys: str) -> Optional[str]:
    if not isinstance(record, dict):
        return None
    for key in keys:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)):
            return str(value)
    return None


def _normalize_phone(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    cleaned = re.sub(r"[\s().-]+", "", value.strip())
    return cleaned or None


def _encode_part(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_part(value: str) -> str:
    try:
        padded = value + ("=" * (-len(value) % 4))
        return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except Exception:
        return ""


def _first_nonempty(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _first_defined(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _coerce_bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_base_url(value: str) -> str:
    return str(value or DEFAULT_BASE_URL).rstrip("/")


def _strip_slashes(value: str) -> str:
    return str(value or "").strip().strip("/")


def _normalize_path(value: str) -> str:
    path = str(value or DEFAULT_WEBHOOK_PATH).strip()
    return path if path.startswith("/") else f"/{path}"


def _seed_env(seed: Dict[str, Any], key: str, *env_names: str) -> None:
    for env_name in env_names:
        value = os.getenv(env_name, "").strip()
        if value:
            seed[key] = value
            return


def _seed_int_env(seed: Dict[str, Any], key: str, env_name: str) -> None:
    value = os.getenv(env_name, "").strip()
    if not value:
        return
    try:
        seed[key] = int(value)
    except ValueError:
        pass


def _compact(value: Any) -> str:
    if isinstance(value, str):
        return value[:300]
    try:
        return json.dumps(value, ensure_ascii=True)[:300]
    except Exception:
        return repr(value)[:300]
