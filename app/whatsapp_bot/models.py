"""Wire formats exchanged between the bridge, the api and the worker."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# WhatsApp's own ceiling for a text message body.
MAX_TEXT_LENGTH = 65536

MessageType = Literal[
    "text",
    "image",
    "video",
    "audio",
    "document",
    "sticker",
    "location",
    "contact",
    "reaction",
    "other",
]


class InboundMessage(BaseModel):
    """A message received by the bridge, exactly as posted to ``/webhook``.

    ``from`` is a Python keyword, hence the alias; both ``from`` and
    ``from_jid`` are accepted when building the model.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: str = Field(min_length=1, max_length=256)
    from_jid: str = Field(alias="from", min_length=1, max_length=256)
    chat_jid: str = Field(min_length=1, max_length=256)
    is_group: bool = False
    timestamp: int = 0
    type: MessageType = "text"
    text: str = Field(default="", max_length=MAX_TEXT_LENGTH)
    quoted_id: str | None = Field(default=None, max_length=256)

    # --- attachment, set by the bridge for documents it downloaded ---
    filename: str | None = Field(default=None, max_length=512)
    mimetype: str | None = Field(default=None, max_length=256)
    media_size: int | None = None
    #: Absolute path on the shared media volume. None when nothing was
    #: downloaded — either no attachment, or the download failed.
    media_path: str | None = Field(default=None, max_length=1024)

    @property
    def is_private(self) -> bool:
        return not self.is_group

    @property
    def has_file(self) -> bool:
        """True when an attachment is actually readable on disk."""
        return bool(self.media_path)

    def filename_suffix(self) -> str:
        """Lowercase extension of the attachment, ``''`` when there is none."""
        if not self.filename or "." not in self.filename:
            return ""
        return f".{self.filename.rsplit('.', 1)[1].lower()}"

    @property
    def body(self) -> str:
        """Trimmed text — what handlers almost always want to match on."""
        return self.text.strip()

    def command(self) -> str | None:
        """Return the leading ``!command`` token in lowercase, if any."""
        body = self.body
        if not body.startswith("!"):
            return None
        return body.split(maxsplit=1)[0].lower()


class Outbound(BaseModel):
    """A message a handler wants to send. The only accepted return value.

    Either a text message, or a document with ``text`` as its optional
    caption — never neither.
    """

    model_config = ConfigDict(extra="forbid")

    jid: str = Field(min_length=1, max_length=256)
    text: str = Field(default="", max_length=MAX_TEXT_LENGTH)
    quoted_id: str | None = Field(default=None, max_length=256)
    #: Path on the shared media volume of a file to attach.
    document_path: str | None = Field(default=None, max_length=1024)
    #: Name the recipient sees. Defaults to the basename of ``document_path``.
    filename: str | None = Field(default=None, max_length=512)
    # Filled in by the pipeline runner; handlers do not need to set it.
    handler: str | None = None

    @model_validator(mode="after")
    def _needs_content(self) -> Outbound:
        if not self.text and not self.document_path:
            raise ValueError("an outbound message needs text, a document, or both")
        return self

    @property
    def is_document(self) -> bool:
        return bool(self.document_path)

    def reply_to(self, msg: InboundMessage) -> Outbound:
        """Return a copy quoting ``msg`` — sugar for handlers."""
        return self.model_copy(update={"quoted_id": msg.id})


class SendStatusUpdate(BaseModel):
    """Callback posted by the bridge once an outbound message is settled."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1, max_length=256)
    status: Literal["sent", "failed"]
    error: str | None = Field(default=None, max_length=2000)
    wa_message_id: str | None = Field(default=None, max_length=256)


class WebhookAck(BaseModel):
    """Response body of ``POST /webhook``."""

    status: Literal["queued", "duplicate"]
    id: str
    job_id: str | None = None
