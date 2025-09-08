#!/usr/bin/env python3

from smolagents import CodeAgent, LiteLLMModel, DuckDuckGoSearchTool
import os

# Initialize with LiteLLM
model_id = os.getenv("LITELLM_MODEL", "ollama/llama2")
model = LiteLLMModel(model_id=model_id)

# Create agent
agent = CodeAgent(
    tools=[DuckDuckGoSearchTool()],
    model=model,
)

# Run task
task = "What is the current weather in Paris?"
result = agent.run(task)
print(result)