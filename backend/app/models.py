from typing import Literal

from pydantic import BaseModel, Field

# Prompts are stored in SQLite, indexed into FTS and handed to the AI CLI as an argv
# element, and nothing else bounded them (SEC-014). Generous enough for a pasted stack
# trace or diff, small enough that it can't be used to inflate the database.
PROMPT_MAX_LENGTH = 32_000


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
    prompt: str = Field(max_length=PROMPT_MAX_LENGTH)


class NewSessionCommandRequest(BaseModel):
    project_path: str
    tool: Literal["claude-code", "cursor"]
    prompt: str = Field(max_length=PROMPT_MAX_LENGTH)
