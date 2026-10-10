from typing import Literal

from pydantic import BaseModel, Field

# Prompts are stored in SQLite, indexed into FTS and handed to the AI CLI as an argv
# element, and nothing else bounded them (SEC-014). Generous enough for a pasted stack
# trace or diff, small enough that it can't be used to inflate the database.
PROMPT_MAX_LENGTH = 32_000
# Most session ids a POST / filter may carry (E2E search results).
SEARCH_MAX_IDS = 100
# Ciphertext is longer than its plaintext (nonce, tag, base64): room for 32,000 characters at
# worst-case 4 UTF-8 bytes each. The browser enforces the 32,000-character plaintext limit.
PROMPT_MAX_LENGTH_E2E = 171_000


class MessageIn(BaseModel):
    idx: int
    role: str
    timestamp: str
    content: str


class SessionIn(BaseModel):
    id: str
    tool: Literal["claude-code", "cursor"]
    entrypoint: str = ""
    project_path: str = ""
    title: str
    created_at: str
    last_updated_at: str
    message_count: int = 0
    last_message_preview: str = ""
    status: str = "idle"
    recent_messages: list[MessageIn] = []


class SyncIndexRequest(BaseModel):
    sessions: list[SessionIn]


class JobCompleteRequest(BaseModel):
    status: str
    result_text: str = ""
    messages: list[MessageIn] = []
    is_complete: bool | None = None


class CommandRequest(BaseModel):
    prompt: str = Field(max_length=PROMPT_MAX_LENGTH_E2E)


class NewSessionCommandRequest(BaseModel):
    project_path: str
    tool: Literal["claude-code", "cursor"]
    prompt: str = Field(max_length=PROMPT_MAX_LENGTH_E2E)


# base64 of IMAGE_MAX_BYTES (5 MiB) is ~7.0M characters; pydantic rejects anything bigger
# before it is decoded.
IMAGE_B64_MAX_LENGTH = 7_200_000


class ImageUploadRequest(BaseModel):
    session_id: str
    path: str = Field(max_length=1024)
    data_b64: str = Field(max_length=IMAGE_B64_MAX_LENGTH)


class FetchImageRequest(BaseModel):
    path: str = Field(max_length=1024)


class SearchRequest(BaseModel):
    query: str = Field(max_length=PROMPT_MAX_LENGTH_E2E)


class E2EParamsRequest(BaseModel):
    salt: str
    kdf: dict
    key_check: str
    reset: bool = False
