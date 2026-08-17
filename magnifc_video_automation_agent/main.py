from schema import AgentState


def get_user_input() -> AgentState:
    return {
        "topic": input("Video topic: ").strip(),
        "tone": input("Tone: ").strip(),
        "duration": input("Duration: ").strip(),
        "language": input("Language: ").strip(),
        "characters": [
            character.strip()
            for character in input("Characters, comma separated: ").split(",")
            if character.strip()
        ],
        "output_dir": "outputs",
    }


if __name__ == "__main__":
    print(get_user_input())
