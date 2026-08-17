import os


AGENT_MODEL_ENV = {
    "orchestrator": "ORCHESTRATOR_MODEL",
    "story": "STORY_MODEL",
    "scene": "SCENE_MODEL",
    "qa": "QA_MODEL",
}


def model_name_for(agent_name: str) -> str:
    env_name = AGENT_MODEL_ENV.get(agent_name, "OLLAMA_MODEL")
    return os.getenv(env_name) or os.getenv("OLLAMA_MODEL", "qwen2.5:3b-instruct")


def load_model(agent_name: str):
    from langchain_ollama import ChatOllama

    return ChatOllama(model=model_name_for(agent_name), temperature=0.3, format="json")
