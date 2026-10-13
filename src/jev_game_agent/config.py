import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    api_key: str | None = field(default=None, repr=False)
    model: str = "jev-latest"
    connect_timeout_seconds: float = 5.0
    request_budget_seconds: float = 12.0
    run_root: Path = field(default_factory=lambda: Path(__file__).resolve().parents[2] / "run")
    knowledge_root: Path = field(default_factory=lambda: Path(__file__).resolve().parents[2] / "knowledge")
    context_character_budget: int = 12000
    recent_verified_results: int = 6

    @classmethod
    def from_environment(cls) -> "Settings":
        return cls(
            api_key=os.environ.get("TYPESAFE_API_KEY") or None,
            model=os.environ.get("JEV_MODEL") or "jev-latest",
        )
