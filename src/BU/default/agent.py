#!/usr/bin/env python3

import os
import asyncio
from browser_use import Agent, ChatOllama, Browser

# Get model from environment variable (configured by install script)
model_name = os.getenv("OLLAMA_MODEL", "llama3:8b")
llm = ChatOllama(model=model_name)

# Simple task - you can modify this
task = "Visit google.com and search for 'OpenAI news'"

async def main():
    print(f"Starting BU agent with model: {model_name}")
    print(f"Task: {task}")
    
    try:
        # Create browser instance with headless mode using Chromium
        browser = Browser(
            headless=True,
            channel="chromium",  # Explicitly use Chromium
            disable_security=False,
            keep_alive=True,
            wait_between_actions=0.5,  # Small delay between actions
            minimum_wait_page_load_time=1,  # Wait for page loads
            wait_for_network_idle_page_load_time=2,
            chromium_sandbox=True,  # Enable sandbox for security
            args=["--no-sandbox", "--disable-dev-shm-usage"]  # Common Chromium args for containers
        )
        
        # Create agent
        agent = Agent(
            task=task,
            llm=llm,
            browser=browser,
        )
        
        # Run the agent
        result = await agent.run(max_steps=5)
        print("Task completed successfully!")
        return result
    except Exception as e:
        print(f"Error running agent: {e}")
        import traceback
        traceback.print_exc()
        return None

if __name__ == "__main__":
    asyncio.run(main())