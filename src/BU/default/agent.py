#!/usr/bin/env python3

import os
import asyncio
from browser_use import Agent, ChatOllama

# Get model from environment variable (configured by install script)
model_name = os.getenv("OLLAMA_MODEL", "llama3.1:8b")
llm = ChatOllama(model=model_name)

# Simple task - you can modify this
task = "Search for latest news about AI and summarize the top 3 articles"

# Create agent - browser_use will handle browser configuration
agent = Agent(
    task=task,
    llm=llm,
    # Browser will be auto-configured based on what's installed
)

async def main():
    print(f"Starting BU agent with model: {model_name}")
    print(f"Task: {task}")
    
    try:
        history = await agent.run(max_steps=10)
        print("Task completed successfully!")
        return history
    except Exception as e:
        print(f"Error running agent: {e}")
        return None

if __name__ == "__main__":
    asyncio.run(main())