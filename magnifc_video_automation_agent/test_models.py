import os

from models import model_name_for


def test_agent_model_override():
    os.environ["ORCHESTRATOR_MODEL"] = "fast-model"
    try:
        assert model_name_for("orchestrator") == "fast-model"
    finally:
        del os.environ["ORCHESTRATOR_MODEL"]


def test_default_model():
    assert model_name_for("unknown-agent") == os.getenv("OLLAMA_MODEL", "qwen2.5:3b-instruct")


if __name__ == "__main__":
    test_agent_model_override()
    test_default_model()
