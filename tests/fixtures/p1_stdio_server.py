"""Test-only injected decision client. No network and no UI operations."""
import argparse
from pathlib import Path

from jev_game_agent.config import Settings
from jev_game_agent.decisions.base import DecisionAnswer
from jev_game_agent.server import create_server


class FixtureClient:
    async def decide(self, request):
        choice = next(iter(request.criteria))
        return DecisionAnswer(index=0, attempts=0, elapsed_ms=0, model="fixture-mock-no-network",
                              choice=choice, confidence=1,
                              probabilities={key: float(key == choice) for key in request.criteria})

    async def aclose(self):
        pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    args = parser.parse_args()
    create_server(Settings(api_key="mock-not-a-real-key", run_root=args.run_root),
                  client_factory=lambda settings: FixtureClient()).run(transport="stdio")
