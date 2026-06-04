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

    assert adapter._message_text(transcript_message) == "[voice] please help"
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


def test_register_supplies_platform_hooks() -> None:
    class Ctx:
        kwargs = None
        cli_kwargs = None

        def register_platform(self, **kwargs):
            self.kwargs = kwargs

        def register_cli_command(self, **kwargs):
            self.cli_kwargs = kwargs

    ctx = Ctx()
    adapter.register(ctx)

    assert ctx.kwargs["name"] == "kapso"
    assert ctx.kwargs["required_env"] == ["KAPSO_API_KEY"]
    assert callable(ctx.kwargs["env_enablement_fn"])
    assert callable(ctx.kwargs["standalone_sender_fn"])
    assert ctx.kwargs["cron_deliver_env_var"] == "KAPSO_HOME_CHANNEL"
    assert ctx.kwargs["max_message_length"] == adapter.MAX_WHATSAPP_TEXT_LENGTH
    assert ctx.cli_kwargs["name"] == "kapso"
    assert callable(ctx.cli_kwargs["setup_fn"])
