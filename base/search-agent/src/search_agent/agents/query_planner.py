from pydantic_ai import Agent

from search_agent.config import settings
from search_agent.deps import PipelineDeps, create_model

query_planner = Agent(
    create_model(),
    output_type=list[str],
    instructions=settings.search_query_planner_prompt,
    deps_type=PipelineDeps,
)
