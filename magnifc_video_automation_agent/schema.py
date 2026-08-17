from typing import NotRequired, TypedDict


class AgentState(TypedDict):
    topic: str
    tone: str
    duration: str
    language: str
    characters: list[str]
    output_dir: NotRequired[str]
    story: NotRequired[str]
