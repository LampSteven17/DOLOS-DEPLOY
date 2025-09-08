#!/usr/bin/env python3

import os
import asyncio
from browser_use import Agent, ChatOllama, BrowserConfig

# Get model from environment variable (configured by install script)
model_name = os.getenv("OLLAMA_MODEL", "llama3.1:8b")
llm = ChatOllama(model=model_name)

# Configure browser to use Firefox
browser_config = BrowserConfig(
    browser_type="firefox",  # Use Firefox instead of default Chromium
    headless=True,  # Run in headless mode for servers
)

# Simple task - you can modify this
task = "Search for latest news about AI and summarize the top 3 articles"

agent = Agent(
    task=task,
    llm=llm,
    browser_config=browser_config,
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