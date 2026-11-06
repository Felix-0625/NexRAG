from pydantic_ai import Agent

from search_agent.config import settings
from search_agent.deps import PipelineDeps, create_model
from search_agent.models import SearchResult

analyze_synthesizer = Agent(
    create_model(),
    output_type=SearchResult,
    instructions=settings.search_analyze_synthesize_prompt,
    deps_type=PipelineDeps,
)
