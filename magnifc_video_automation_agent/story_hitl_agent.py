from schema import AgentState
from langgraph.types import interrupt

def review_story(state: AgentState):
    decision = interrupt(
        {
            "story": state["story"],
            "actions": ["approve", "edit", "regenerate"],
        }
    )

    action = decision.get("action", "approve")

    if action == "edit":
        return {
            "story": decision["story"],
            "approved": False,
            "review_note": "",
        }

    if action == "regenerate":
        return {
            "approved": False,
            "review_note": decision.get("note", "Regenerate the story."),
        }

    return {"approved": True}
