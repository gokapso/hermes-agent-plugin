from __future__ import annotations

import dataclasses
import hashlib
import hmac
import importlib
import sys
import types
from enum import Enum
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def install_gateway_stubs() -> None:
    gateway = types.ModuleType("gateway")
    gateway_config = types.ModuleType("gateway.config")
    gateway_platforms = types.ModuleType("gateway.platforms")
    gateway_base = types.ModuleType("gateway.platforms.base")

    class Platform(str):
        def __new__(cls, value: str):
            return str.__new__(cls, value)

        @property
        def value(self) -> str:
            return str(self)

    @dataclasses.dataclass
    class PlatformConfig:
        enabled: bool = True
        api_key: str | None = None
        extra: dict[str, Any] = dataclasses.field(default_factory=dict)

    class MessageType(Enum):
        TEXT = "text"
        LOCATION = "location"
        PHOTO = "photo"
        VIDEO = "video"
        AUDIO = "audio"
        VOICE = "voice"
        DOCUMENT = "document"
        STICKER = "sticker"
        COMMAND = "command"

    @dataclasses.dataclass
    class MessageEvent:
        text: str
        message_type: MessageType = MessageType.TEXT
        source: Any = None
        raw_message: Any = None
        message_id: str | None = None
        reply_to_message_id: str | None = None
        timestamp: Any = None

    @dataclasses.dataclass
    class SendResult:
        success: bool
        message_id: str | None = None
        error: str | None = None
        raw_response: Any = None
        retryable: bool = False
        continuation_message_ids: tuple = ()

    @dataclasses.dataclass
    class Source:
        platform: Any
        chat_id: str
        chat_name: str | None
        chat_type: str
        user_id: str | None
        user_name: str | None
        message_id: str | None

    class BasePlatformAdapter:
        def __init__(self, config, platform):
            self.config = config
            self.platform = platform
            self.handled: list[MessageEvent] = []
            self._running = False

        def _mark_connected(self):
            self._running = True

        def _mark_disconnected(self):
            self._running = False

        def _set_fatal_error(self, code, message, *, retryable):
            self.fatal = (code, message, retryable)

        def build_source(
            self,
            chat_id,
            chat_name=None,
            chat_type="dm",
            user_id=None,
            user_name=None,
            message_id=None,
            **_,
        ):
            return Source(
                platform=self.platform,
                chat_id=chat_id,
                chat_name=chat_name,
                chat_type=chat_type,
                user_id=user_id,
                user_name=user_name,
                message_id=message_id,
            )

        async def handle_message(self, event):
            self.handled.append(event)

    gateway_config.Platform = Platform
    gateway_config.PlatformConfig = PlatformConfig
    gateway_base.BasePlatformAdapter = BasePlatformAdapter
    gateway_base.MessageEvent = MessageEvent
    gateway_base.MessageType = MessageType
    gateway_base.SendResult = SendResult

    sys.modules["gateway"] = gateway
    sys.modules["gateway.config"] = gateway_config
    sys.modules["gateway.platforms"] = gateway_platforms
    sys.modules["gateway.platforms.base"] = gateway_base


install_gateway_stubs()
adapter = importlib.import_module("adapter")


def load_plugin_package():
    parent_name = "hermes_plugins"
    parent = types.ModuleType(parent_name)
    parent.__path__ = []
    sys.modules[parent_name] = parent

    module_name = f"{parent_name}.kapso_test"
    spec = importlib.util.spec_from_file_location(
        module_name,
        ROOT / "__init__.py",
        submodule_search_locations=[str(ROOT)],
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def make_config(**extra):
    platform_config = sys.modules["gateway.config"].PlatformConfig
    return platform_config(extra=extra)


def test_verify_kapso_signature_accepts_sha256_prefix() -> None:
    raw = b'{"ok":true}'
    secret = "test-secret"
    digest = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()

    assert adapter.verify_kapso_signature(raw, f"sha256={digest}", secret)
    assert adapter.verify_kapso_signature(raw, digest, secret)
    assert not adapter.verify_kapso_signature(raw, "sha256=bad", secret)


async def test_connect_accepts_hermes_reconnect_keyword(monkeypatch) -> None:
    monkeypatch.setattr(adapter, "AIOHTTP_AVAILABLE", False)
    kapso = adapter.KapsoAdapter(make_config())

    connected = await kapso.connect(is_reconnect=True)

    assert connected is False
    assert kapso.fatal == ("missing_dependency", "aiohttp is not installed", False)


def test_kapso_webhook_event_becomes_message_event() -> None:
    kapso = adapter.KapsoAdapter(
        make_config(
            api_key="key",
            webhook_secret="secret",
            phone_number_id="pn-default",
        )
    )
    event = {
        "event": "whatsapp.message.received",
        "phone_number_id": "pn-123",
        "message": {
            "id": "wamid.1",
            "from": "15551234567",
            "type": "text",
            "timestamp": "1710000000",
            "text": {"body": "hello"},
            "kapso": {"direction": "inbound", "contactName": "Rafa"},
        },
        "conversation": {"id": "conv-1", "phone_number": "+1 555 123 4567"},
    }

    message_event = kapso._message_event_from_kapso_event(event)

    assert message_event is not None
    assert message_event.text == "hello"
    assert message_event.message_id == "wamid.1"
    assert message_event.source.user_id == "15551234567"
    assert message_event.source.user_name == "Rafa"
    assert message_event.source.chat_id.startswith("kapso:")


def test_extracts_batch_webhook_data() -> None:
    payload = {
        "type": "whatsapp.message.received",
        "data": [
            {"event": "whatsapp.message.received", "message": {"id": "1", "type": "text"}},
            {"message": {"id": "2", "type": "text"}},
        ]
    }

    events = adapter._extract_kapso_events(payload, batch_header="true")

    assert len(events) == 2
    assert events[0]["message"]["id"] == "1"
    assert events[1]["event"] == "whatsapp.message.received"


def test_audio_transcript_and_media_description_text() -> None:
    transcript_message = {
        "id": "wamid.audio",
        "from": "15551234567",
        "type": "audio",
        "audio": {"id": "media-1", "mime_type": "audio/ogg"},
        "kapso": {"transcript": {"text": "please help"}},
    }
    image_message = {
        "id": "wamid.image",
        "from": "15551234567",
        "type": "image",
        "image": {"id": "media-2", "mime_type": "image/jpeg"},
        "kapso": {"media_url": "https://cdn.kapso.ai/media/image.jpg"},
    }
    content_transcript_message = {
        "id": "wamid.audio-content",
        "from": "15551234567",
        "type": "audio",
        "audio": {"id": "media-3", "mime_type": "audio/opus", "voice": True},
        "kapso": {
            "content": (
                "[Audio attached] (audio.ogg) [Size: 12 KB | Type: audio/opus] "
                "URL: https://app.kapso.ai/audio.ogg\n"
                "Transcript: I just spent 15 bucks in coffee, so please add that"
            )
        },
    }

    assert adapter._message_text(transcript_message) == "[voice] please help"
    assert adapter._message_text(content_transcript_message) == (
        "[voice] I just spent 15 bucks in coffee, so please add that"
    )
    assert (
        adapter._message_text(image_message)
        == "[image] (image/jpeg) https://cdn.kapso.ai/media/image.jpg"
    )


class FakeGetResponse:
    def __init__(self, *, status=200, text="", body=b"", headers=None):
        self.status = status
        self._text = text
        self._body = body
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def text(self):
        return self._text

    async def read(self):
        return self._body


class FakeMediaSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


async def test_hydrates_image_media_into_hermes_cache(monkeypatch) -> None:
    monkeypatch.setattr(adapter, "_cache_image_bytes", lambda data, ext: f"/tmp/hermes-image{ext}")
    kapso = adapter.KapsoAdapter(
        make_config(
            api_key="key",
            webhook_secret="secret",
            phone_number_id="pn-default",
        )
    )
    kapso._session = FakeMediaSession(
        [
            FakeGetResponse(
                text=(
                    '{"download_url":"https://api.kapso.ai/meta/whatsapp/media_download?token=abc",'
                    '"mime_type":"image/png"}'
                )
            ),
            FakeGetResponse(
                body=b"\x89PNG\r\n\x1a\npayload",
                headers={"Content-Type": "image/png"},
            ),
        ]
    )
    payload = {
        "event": "whatsapp.message.received",
        "phone_number_id": "pn-123",
        "message": {
            "id": "wamid.image",
            "from": "15551234567",
            "type": "image",
            "image": {"id": "media-123", "mime_type": "image/png"},
        },
    }

    message_event = kapso._message_event_from_kapso_event(payload)
    assert message_event is not None
    await kapso._hydrate_media_event(message_event, payload)

    assert message_event.media_urls == ["/tmp/hermes-image.png"]
    assert message_event.media_types == ["image/png"]
    assert kapso._session.calls[0] == (
        "https://api.kapso.ai/meta/whatsapp/v24.0/media-123?phone_number_id=pn-123",
        {"headers": {"X-API-Key": "key"}},
    )
    assert kapso._session.calls[1][0] == "https://api.kapso.ai/meta/whatsapp/media_download?token=abc"
    assert kapso._session.calls[1][1] == {"headers": {"X-API-Key": "key"}}


async def test_hydrates_voice_media_into_hermes_audio_cache(monkeypatch) -> None:
    monkeypatch.setattr(adapter, "_cache_audio_bytes", lambda data, ext: f"/tmp/hermes-audio{ext}")
    kapso = adapter.KapsoAdapter(
        make_config(
            api_key="key",
            webhook_secret="secret",
            phone_number_id="pn-default",
        )
    )
    kapso._session = FakeMediaSession(
        [
            FakeGetResponse(
                text=(
                    '{"download_url":"https://api.kapso.ai/meta/whatsapp/media_download?token=voice",'
                    '"mime_type":"audio/opus"}'
                )
            ),
            FakeGetResponse(
                body=b"OggSvoice-payload",
                headers={"Content-Type": "audio/opus"},
            ),
        ]
    )
    payload = {
        "event": "whatsapp.message.received",
        "phone_number_id": "pn-123",
        "message": {
            "id": "wamid.voice",
            "from": "15551234567",
            "type": "audio",
            "audio": {"id": "media-voice", "mime_type": "audio/opus", "voice": True},
        },
    }

    message_event = kapso._message_event_from_kapso_event(payload)
    assert message_event is not None
    await kapso._hydrate_media_event(message_event, payload)

    assert message_event.message_type.value == "voice"
    assert message_event.media_urls == ["/tmp/hermes-audio.ogg"]
    assert message_event.media_types == ["audio/opus"]
    assert kapso._session.calls[0] == (
        "https://api.kapso.ai/meta/whatsapp/v24.0/media-voice?phone_number_id=pn-123",
        {"headers": {"X-API-Key": "key"}},
    )
    assert kapso._session.calls[1][0] == "https://api.kapso.ai/meta/whatsapp/media_download?token=voice"
    assert kapso._session.calls[1][1] == {"headers": {"X-API-Key": "key"}}


async def test_skips_voice_hydration_when_kapso_transcript_exists() -> None:
    kapso = adapter.KapsoAdapter(
        make_config(
            api_key="key",
            webhook_secret="secret",
            phone_number_id="pn-default",
        )
    )
    kapso._session = FakeMediaSession(
        [
            FakeGetResponse(
                text=(
                    '{"download_url":"https://api.kapso.ai/meta/whatsapp/media_download?token=voice",'
                    '"mime_type":"audio/opus"}'
                )
            ),
        ]
    )
    payload = {
        "event": "whatsapp.message.received",
        "phone_number_id": "pn-123",
        "message": {
            "id": "wamid.voice-transcript",
            "from": "15551234567",
            "type": "audio",
            "audio": {"id": "media-voice", "mime_type": "audio/opus", "voice": True},
            "kapso": {
                "transcript": {"text": "Reply with the word turtle"},
                "media_url": "https://api.kapso.ai/meta/whatsapp/media_download?token=voice",
            },
        },
    }

    message_event = kapso._message_event_from_kapso_event(payload)
    assert message_event is not None
    assert message_event.message_type.value == "voice"
    assert message_event.text == "[voice] Reply with the word turtle"
    await kapso._hydrate_media_event(message_event, payload)

    assert getattr(message_event, "media_urls", None) is None
    assert kapso._session.calls == []


async def test_hydrates_document_media_into_hermes_document_cache(monkeypatch) -> None:
    monkeypatch.setattr(
        adapter,
        "_cache_document_bytes",
        lambda data, filename: f"/tmp/hermes-documents/{filename}",
    )
    kapso = adapter.KapsoAdapter(
        make_config(
            api_key="key",
            webhook_secret="secret",
            phone_number_id="pn-default",
        )
    )
    kapso._session = FakeMediaSession(
        [
            FakeGetResponse(
                text=(
                    '{"download_url":"https://api.kapso.ai/meta/whatsapp/media_download?token=doc",'
                    '"mime_type":"application/pdf"}'
                )
            ),
            FakeGetResponse(
                body=b"%PDF-1.7 invoice",
                headers={"Content-Type": "application/pdf"},
            ),
        ]
    )
    payload = {
        "event": "whatsapp.message.received",
        "phone_number_id": "pn-123",
        "message": {
            "id": "wamid.document",
            "from": "15551234567",
            "type": "document",
            "document": {
                "id": "media-doc",
                "filename": "invoice.pdf",
                "mime_type": "application/pdf",
            },
        },
    }

    message_event = kapso._message_event_from_kapso_event(payload)
    assert message_event is not None
    await kapso._hydrate_media_event(message_event, payload)

    assert message_event.message_type.value == "document"
    assert message_event.media_urls == ["/tmp/hermes-documents/invoice.pdf"]
    assert message_event.media_types == ["application/pdf"]
    assert kapso._session.calls[0] == (
        "https://api.kapso.ai/meta/whatsapp/v24.0/media-doc?phone_number_id=pn-123",
        {"headers": {"X-API-Key": "key"}},
    )
    assert kapso._session.calls[1][0] == "https://api.kapso.ai/meta/whatsapp/media_download?token=doc"
    assert kapso._session.calls[1][1] == {"headers": {"X-API-Key": "key"}}


async def test_hydrates_text_document_and_injects_content(monkeypatch) -> None:
    monkeypatch.setattr(
        adapter,
        "_cache_document_bytes",
        lambda data, filename: f"/tmp/hermes-documents/{filename}",
    )
    kapso = adapter.KapsoAdapter(
        make_config(
            api_key="key",
            webhook_secret="secret",
            phone_number_id="pn-default",
        )
    )
    kapso._session = FakeMediaSession(
        [
            FakeGetResponse(
                body=b"alpha,beta\n1,2\n",
                headers={"Content-Type": "text/csv"},
            ),
        ]
    )
    payload = {
        "event": "whatsapp.message.received",
        "phone_number_id": "pn-123",
        "message": {
            "id": "wamid.csv",
            "from": "15551234567",
            "type": "document",
            "document": {
                "filename": "sample.csv",
                "mime_type": "application/octet-stream",
                "link": "https://cdn.example.com/sample.csv",
                "caption": "Please inspect this data",
            },
        },
    }

    message_event = kapso._message_event_from_kapso_event(payload)
    assert message_event is not None
    await kapso._hydrate_media_event(message_event, payload)

    assert message_event.media_urls == ["/tmp/hermes-documents/sample.csv"]
    assert message_event.media_types == ["text/csv"]
    assert message_event.text == (
        "[Content of sample.csv]:\nalpha,beta\n1,2\n\nPlease inspect this data"
    )
    assert kapso._session.calls == [
        ("https://cdn.example.com/sample.csv", {"headers": {}})
    ]


def test_resolve_chat_id_accepts_encoded_and_plain_forms() -> None:
    kapso = adapter.KapsoAdapter(
        make_config(
            api_key="key",
            webhook_secret="secret",
            phone_number_id="pn-default",
        )
    )
    encoded = adapter.encode_kapso_chat_id("pn-123", "15551234567", "conv-1")

    assert kapso._resolve_chat_id(encoded) == ("pn-123", "15551234567")
    assert kapso._resolve_chat_id("pn-123:15551234567") == ("pn-123", "15551234567")
    assert kapso._resolve_chat_id("+1 (555) 123-4567") == ("pn-default", "+15551234567")


def test_media_item_from_value_accepts_file_key() -> None:
    assert adapter._media_item_from_value({"file": "/tmp/report.pdf"})["file_path"] == "/tmp/report.pdf"


def test_extract_phone_numbers_from_cli_shapes() -> None:
    payload = {
        "data": [
            {"phone_number_id": "pn-1", "display_phone_number": "+1 555"},
            {"phoneNumberId": "pn-2", "phoneNumber": "+1 777"},
        ]
    }

    numbers = adapter._extract_phone_numbers(payload)

    assert [adapter._phone_number_id(number) for number in numbers] == ["pn-1", "pn-2"]
    assert adapter._display_phone_number(numbers[0]) == "+1 555"
    assert adapter._display_phone_number(numbers[1]) == "+1 777"


def test_webhook_url_from_funnel_base() -> None:
    assert (
        adapter._webhook_url_from_base("https://example.ts.net")
        == "https://example.ts.net/kapso/webhook"
    )
    assert (
        adapter._webhook_url_from_base("https://example.ts.net/kapso/webhook")
        == "https://example.ts.net/kapso/webhook"
    )


def test_resolve_setup_webhook_url_preserves_explicit_endpoint() -> None:
    args = types.SimpleNamespace(
        webhook_url="https://example.ts.net/custom/webhook/",
        funnel_url="",
    )

    assert (
        adapter._resolve_setup_webhook_url(args, no_prompt=True)
        == "https://example.ts.net/custom/webhook"
    )


class FakeResponse:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self, content_type=None):
        return {"messages": [{"id": "wamid.out"}]}

    async def text(self):
        return "ok"


class FakeSession:
    def __init__(self):
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse()


class FakePostResponse:
    def __init__(self, payload, *, status=200):
        self.payload = payload
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self, content_type=None):
        return self.payload

    async def text(self):
        return "ok"


class FakePostSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


class FakeContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, tb):
        return False


async def test_send_posts_text_to_kapso_proxy() -> None:
    kapso = adapter.KapsoAdapter(
        make_config(
            api_key="key",
            webhook_secret="secret",
            phone_number_id="pn-default",
        )
    )
    kapso._session = FakeSession()

    result = await kapso.send("15551234567", "Hello **world** [docs](https://example.com)")

    assert result.success is True
    assert result.message_id == "wamid.out"
    url, kwargs = kapso._session.calls[0]
    assert url == "https://api.kapso.ai/meta/whatsapp/v24.0/pn-default/messages"
    assert kwargs["headers"]["X-API-Key"] == "key"
    assert kwargs["json"]["to"] == "15551234567"
    assert kwargs["json"]["text"]["body"] == "Hello *world* docs (https://example.com)"


async def test_send_image_file_uploads_and_sends_media(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(adapter, "FormData", lambda: types.SimpleNamespace(add_field=lambda *_, **__: None))
    image = tmp_path / "chart.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\npayload")
    kapso = adapter.KapsoAdapter(
        make_config(
            api_key="key",
            webhook_secret="secret",
            phone_number_id="pn-default",
        )
    )
    kapso._session = FakePostSession(
        [
            FakePostResponse({"id": "media-123"}),
            FakePostResponse({"messages": [{"id": "wamid.media"}]}),
        ]
    )

    result = await kapso.send_image_file("15551234567", str(image), caption="Here is **the chart**")

    assert result.success is True
    assert result.message_id == "wamid.media"
    upload_url, upload_kwargs = kapso._session.calls[0]
    assert upload_url == "https://api.kapso.ai/meta/whatsapp/v24.0/pn-default/media"
    assert upload_kwargs["headers"] == {"X-API-Key": "key"}
    send_url, send_kwargs = kapso._session.calls[1]
    assert send_url == "https://api.kapso.ai/meta/whatsapp/v24.0/pn-default/messages"
    assert send_kwargs["json"] == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "15551234567",
        "type": "image",
        "image": {"id": "media-123", "caption": "Here is *the chart*"},
    }


async def test_send_image_file_keeps_image_kind_without_extension(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(adapter, "FormData", lambda: types.SimpleNamespace(add_field=lambda *_, **__: None))
    image = tmp_path / "chart"
    image.write_bytes(b"\x89PNG\r\n\x1a\npayload")
    kapso = adapter.KapsoAdapter(
        make_config(
            api_key="key",
            webhook_secret="secret",
            phone_number_id="pn-default",
        )
    )
    kapso._session = FakePostSession(
        [
            FakePostResponse({"id": "media-123"}),
            FakePostResponse({"messages": [{"id": "wamid.media"}]}),
        ]
    )

    result = await kapso.send_image_file("15551234567", str(image))

    assert result.success is True
    assert kapso._session.calls[1][1]["json"]["type"] == "image"
    assert kapso._session.calls[1][1]["json"]["image"] == {"id": "media-123"}


async def test_send_image_file_accepts_hermes_image_path_kwarg(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(adapter, "FormData", lambda: types.SimpleNamespace(add_field=lambda *_, **__: None))
    image = tmp_path / "chart.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\npayload")
    kapso = adapter.KapsoAdapter(
        make_config(
            api_key="key",
            webhook_secret="secret",
            phone_number_id="pn-default",
        )
    )
    kapso._session = FakePostSession(
        [
            FakePostResponse({"id": "media-123"}),
            FakePostResponse({"messages": [{"id": "wamid.media"}]}),
        ]
    )

    result = await kapso.send_image_file("15551234567", image_path=str(image))

    assert result.success is True
    assert kapso._session.calls[1][1]["json"]["type"] == "image"


async def test_send_audio_file_uploads_and_sends_audio(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(adapter, "FormData", lambda: types.SimpleNamespace(add_field=lambda *_, **__: None))
    audio = tmp_path / "reply.mp3"
    audio.write_bytes(b"ID3payload")
    kapso = adapter.KapsoAdapter(
        make_config(
            api_key="key",
            webhook_secret="secret",
            phone_number_id="pn-default",
        )
    )
    kapso._session = FakePostSession(
        [
            FakePostResponse({"id": "media-audio"}),
            FakePostResponse({"messages": [{"id": "wamid.audio"}]}),
        ]
    )

    result = await kapso.send_audio_file("15551234567", str(audio), caption="ignored")

    assert result.success is True
    assert kapso._session.calls[1][1]["json"] == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "15551234567",
        "type": "audio",
        "audio": {"id": "media-audio"},
    }


async def test_metadata_video_and_sticker_use_native_media_kinds(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(adapter, "FormData", lambda: types.SimpleNamespace(add_field=lambda *_, **__: None))
    video = tmp_path / "clip.mp4"
    sticker = tmp_path / "sticker.webp"
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    sticker.write_bytes(b"RIFFxxxxWEBP")
    kapso = adapter.KapsoAdapter(
        make_config(
            api_key="key",
            webhook_secret="secret",
            phone_number_id="pn-default",
        )
    )
    kapso._session = FakePostSession(
        [
            FakePostResponse({"id": "media-video"}),
            FakePostResponse({"messages": [{"id": "wamid.video"}]}),
            FakePostResponse({"id": "media-sticker"}),
            FakePostResponse({"messages": [{"id": "wamid.sticker"}]}),
        ]
    )

    result = await kapso.send(
        "15551234567",
        "",
        metadata={
            "media_files": [
                {"file_path": str(video), "caption": "Watch **this**"},
                {"file_path": str(sticker), "kind": "sticker", "caption": "ignored"},
            ]
        },
    )

    assert result.success is True
    assert kapso._session.calls[1][1]["json"] == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "15551234567",
        "type": "video",
        "video": {"id": "media-video", "caption": "Watch *this*"},
    }
    assert kapso._session.calls[3][1]["json"] == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "15551234567",
        "type": "sticker",
        "sticker": {"id": "media-sticker"},
    }


async def test_send_document_accepts_hermes_file_name_kwarg(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(adapter, "FormData", lambda: types.SimpleNamespace(add_field=lambda *_, **__: None))
    document = tmp_path / "report.pdf"
    document.write_bytes(b"%PDF-1.4")
    kapso = adapter.KapsoAdapter(
        make_config(
            api_key="key",
            webhook_secret="secret",
            phone_number_id="pn-default",
        )
    )
    kapso._session = FakePostSession(
        [
            FakePostResponse({"id": "media-doc"}),
            FakePostResponse({"messages": [{"id": "wamid.doc"}]}),
        ]
    )

    result = await kapso.send_document("15551234567", str(document), file_name="renamed.pdf")

    assert result.success is True
    assert kapso._session.calls[1][1]["json"]["document"] == {
        "id": "media-doc",
        "filename": "renamed.pdf",
    }


async def test_send_uses_metadata_media_files(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(adapter, "FormData", lambda: types.SimpleNamespace(add_field=lambda *_, **__: None))
    document = tmp_path / "report.pdf"
    document.write_bytes(b"%PDF-1.4")
    kapso = adapter.KapsoAdapter(
        make_config(
            api_key="key",
            webhook_secret="secret",
            phone_number_id="pn-default",
        )
    )
    kapso._session = FakePostSession(
        [
            FakePostResponse({"messages": [{"id": "wamid.text"}]}),
            FakePostResponse({"id": "media-doc"}),
            FakePostResponse({"messages": [{"id": "wamid.doc"}]}),
        ]
    )

    result = await kapso.send(
        "15551234567",
        "Done",
        metadata={"media_files": [str(document)]},
    )

    assert result.success is True
    assert result.message_id == "wamid.doc"
    assert result.continuation_message_ids == ("wamid.text",)
    assert [call[0] for call in kapso._session.calls] == [
        "https://api.kapso.ai/meta/whatsapp/v24.0/pn-default/messages",
        "https://api.kapso.ai/meta/whatsapp/v24.0/pn-default/media",
        "https://api.kapso.ai/meta/whatsapp/v24.0/pn-default/messages",
    ]
    assert kapso._session.calls[2][1]["json"] == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "15551234567",
        "type": "document",
        "document": {"id": "media-doc", "filename": "report.pdf"},
    }


async def test_send_uses_single_attachment_dict(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(adapter, "FormData", lambda: types.SimpleNamespace(add_field=lambda *_, **__: None))
    document = tmp_path / "report.pdf"
    document.write_bytes(b"%PDF-1.4")
    kapso = adapter.KapsoAdapter(
        make_config(
            api_key="key",
            webhook_secret="secret",
            phone_number_id="pn-default",
        )
    )
    kapso._session = FakePostSession(
        [
            FakePostResponse({"id": "media-doc"}),
            FakePostResponse({"messages": [{"id": "wamid.doc"}]}),
        ]
    )

    result = await kapso.send(
        "15551234567",
        "",
        metadata={"attachments": {"file_path": str(document)}},
    )

    assert result.success is True
    assert result.message_id == "wamid.doc"
    assert [call[0] for call in kapso._session.calls] == [
        "https://api.kapso.ai/meta/whatsapp/v24.0/pn-default/media",
        "https://api.kapso.ai/meta/whatsapp/v24.0/pn-default/messages",
    ]


async def test_standalone_send_uploads_media_files(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(adapter, "AIOHTTP_AVAILABLE", True)
    monkeypatch.setattr(adapter, "ClientTimeout", lambda total=None: None)
    monkeypatch.setattr(adapter, "FormData", lambda: types.SimpleNamespace(add_field=lambda *_, **__: None))
    document = tmp_path / "report.pdf"
    document.write_bytes(b"%PDF-1.4")
    fake_session = FakePostSession(
        [
            FakePostResponse({"messages": [{"id": "wamid.text"}]}),
            FakePostResponse({"id": "media-doc"}),
            FakePostResponse({"messages": [{"id": "wamid.doc"}]}),
        ]
    )
    monkeypatch.setattr(adapter, "ClientSession", lambda timeout=None: FakeContext(fake_session))

    result = await adapter._standalone_send(
        make_config(api_key="key", phone_number_id="pn-default"),
        "15551234567",
        "Done",
        media_files=[str(document)],
    )

    assert result["success"] is True
    assert result["message_id"] == "wamid.doc"
    assert [call[0] for call in fake_session.calls] == [
        "https://api.kapso.ai/meta/whatsapp/v24.0/pn-default/messages",
        "https://api.kapso.ai/meta/whatsapp/v24.0/pn-default/media",
        "https://api.kapso.ai/meta/whatsapp/v24.0/pn-default/messages",
    ]
    assert fake_session.calls[2][1]["json"] == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "15551234567",
        "type": "document",
        "document": {"id": "media-doc", "filename": "report.pdf"},
    }


async def test_standalone_send_accepts_single_media_file_string(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(adapter, "AIOHTTP_AVAILABLE", True)
    monkeypatch.setattr(adapter, "ClientTimeout", lambda total=None: None)
    monkeypatch.setattr(adapter, "FormData", lambda: types.SimpleNamespace(add_field=lambda *_, **__: None))
    document = tmp_path / "report.pdf"
    document.write_bytes(b"%PDF-1.4")
    fake_session = FakePostSession(
        [
            FakePostResponse({"id": "media-doc"}),
            FakePostResponse({"messages": [{"id": "wamid.doc"}]}),
        ]
    )
    monkeypatch.setattr(adapter, "ClientSession", lambda timeout=None: FakeContext(fake_session))

    result = await adapter._standalone_send(
        make_config(api_key="key", phone_number_id="pn-default"),
        "15551234567",
        "",
        media_files=str(document),
    )

    assert result["success"] is True
    assert result["message_id"] == "wamid.doc"
    assert [call[0] for call in fake_session.calls] == [
        "https://api.kapso.ai/meta/whatsapp/v24.0/pn-default/media",
        "https://api.kapso.ai/meta/whatsapp/v24.0/pn-default/messages",
    ]


def test_register_supplies_platform_hooks() -> None:
    class Ctx:
        kwargs = None
        cli_kwargs = None
        skill_args = None
        tool_kwargs = None

        def register_platform(self, **kwargs):
            self.kwargs = kwargs

        def register_cli_command(self, **kwargs):
            self.cli_kwargs = kwargs

        def register_skill(self, *args, **kwargs):
            self.skill_args = (args, kwargs)

        def register_tool(self, **kwargs):
            self.tool_kwargs = kwargs

    ctx = Ctx()
    plugin = load_plugin_package()
    plugin.register(ctx)

    assert ctx.kwargs["name"] == "kapso"
    assert ctx.kwargs["required_env"] == ["KAPSO_API_KEY"]
    assert callable(ctx.kwargs["env_enablement_fn"])
    assert callable(ctx.kwargs["standalone_sender_fn"])
    assert ctx.kwargs["cron_deliver_env_var"] == "KAPSO_HOME_CHANNEL"
    assert ctx.kwargs["max_message_length"] == adapter.MAX_WHATSAPP_TEXT_LENGTH
    assert ctx.cli_kwargs["name"] == "kapso"
    assert callable(ctx.cli_kwargs["setup_fn"])
    assert ctx.tool_kwargs["name"] == "xquik_search"
    assert ctx.tool_kwargs["requires_env"] == ["XQUIK_API_KEY"]
    assert callable(ctx.tool_kwargs["handler"])
    assert ctx.skill_args[0][0] == "xquik-social-research"
    assert ctx.skill_args[0][1].is_file()
