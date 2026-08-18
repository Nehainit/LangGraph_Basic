from typing import Callable

from langgraph.graph import StateGraph ,START,END
from schema import AgentState
from story_agent import create_story
from storyboard_agent import create_storyboard
from image_agent import create_scene_images
from narration_agent import create_narration
from soundfx_agent import create_soundfx
from subtitle_agent import create_subtitles
from video_agent import compile_video
from story_hitl_agent import review_story
from langgraph.checkpoint.memory import InMemorySaver

def build_story_graph(
    story_creator: Callable[[AgentState], dict] = create_story,
    storyboard_creator: Callable[[AgentState], dict] = create_storyboard,
    image_creator: Callable[[AgentState], dict] = create_scene_images,
    narration_creator: Callable[[AgentState], dict] = create_narration,
    soundfx_creator: Callable[[AgentState], dict] = create_soundfx,
    subtitle_creator: Callable[[AgentState], dict] = create_subtitles,
    video_creator: Callable[[AgentState], dict] = compile_video,
):

    def route_after_review(state:AgentState) -> str:
        if state.get("approved"):
            return "create_storyboard"
        if state.get("review_note"):
            return "create_story"
        return "review_story"


    graph=StateGraph(AgentState)
    graph.add_node("create_story", story_creator)
    graph.add_node("review_story",review_story)
    graph.add_node("create_storyboard", storyboard_creator)
    graph.add_node("create_scene_images", image_creator)
    graph.add_node("create_narration", narration_creator)
    graph.add_node("create_soundfx", soundfx_creator)
    graph.add_node("create_subtitles", subtitle_creator)
    graph.add_node("compile_video", video_creator)

    # adding edges
    graph.add_edge(START,"create_story")
    graph.add_edge("create_story","review_story")
    graph.add_conditional_edges("review_story",route_after_review)
    graph.add_edge("create_storyboard", "create_scene_images")
    graph.add_edge("create_scene_images", "create_narration")
    graph.add_edge("create_narration", "create_soundfx")
    graph.add_edge("create_soundfx", "create_subtitles")
    graph.add_edge("create_subtitles", "compile_video")
    graph.add_edge("compile_video", END)
    return graph.compile(checkpointer=InMemorySaver())
    
