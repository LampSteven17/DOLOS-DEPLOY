#!/usr/bin/env python3

import os
import asyncio
from browser_use import Agent, ChatOllama, Browser

# Get model from environment variable (configured by install script)
model_name = os.getenv("OLLAMA_MODEL", "llama3.1:8b")
llm = ChatOllama(model=model_name)

# Simple task - you can modify this
task = "Visit google.com and search for 'OpenAI news'"

async def main():
    print(f"Starting BU agent with model: {model_name}")
    print(f"Task: {task}")
    
    try:
        # Create browser instance with headless mode
        browser = Browser(headless=True)
        
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