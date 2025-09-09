#!/usr/bin/env python3
"""
BU (Browser Use) Agent with MCHP-like behavior patterns
Simulates human-like browser usage with task clustering and realistic timing
"""

import os
import asyncio
import time
import random
import sys
from datetime import datetime
from browser_use import Agent, ChatOllama, Browser

# Configuration (matching MCHP parameters)
TASK_CLUSTER_COUNT = 5  # Number of tasks to perform in a cluster
TASK_INTERVAL = 10  # Seconds between tasks within a cluster
GROUPING_INTERVAL = 500  # Seconds between task clusters (8.3 minutes)
MIN_ACTION_DELAY = 1  # Minimum delay for actions (seconds)
MAX_ACTION_DELAY = 20  # Maximum delay for actions (seconds)

# Get model from environment variable (configured by install script)
model_name = os.getenv("OLLAMA_MODEL", "llama3:8b")
print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Initializing BU Agent with model: {model_name}")

# Initialize LLM
llm = ChatOllama(model=model_name)

# MCHP-like browser tasks (adapted from MCHP's browse_web.py and website lists)
browser_tasks = [
    # Search engine tasks (from MCHP's google_searches.txt)
    "Go to google.com and search for 'how to remove specific text from a file'",
    "Visit google.com and search for 'C# SQL query with user input'",
    "Navigate to google.com and search for 'python programming tutorials'",
    "Go to google.com and search for 'how to use vscode'",
    "Search google.com for 'what is my ip address'",
    
    # News browsing (from MCHP's website list)
    "Visit cnn.com and look at the top headlines",
    "Go to bbc.com and check world news",
    "Navigate to reuters.com and browse financial news",
    "Visit nytimes.com and check technology section",
    "Go to wsj.com and look at market news",
    
    # Social media simulation (from MCHP patterns)
    "Visit twitter.com and look at trending topics",
    "Go to reddit.com and browse the front page",
    "Navigate to linkedin.com homepage",
    "Visit instagram.com and check explore page",
    
    # YouTube-style content (from MCHP's browse_youtube.txt)
    "Go to youtube.com and search for 'cake decorating tutorials'",
    "Visit youtube.com and search for 'python 101'",
    "Navigate to youtube.com and search for 'VSCode tips and tricks'",
    "Go to youtube.com and look for 'buttercream icing recipes'",
    
    # E-commerce browsing (from MCHP's website list)
    "Visit amazon.com and search for 'laptop'",
    "Go to ebay.com and browse electronics",
    "Navigate to walmart.com and check deals",
    "Visit bestbuy.com and look at computers",
    
    # Educational sites (from MCHP's educational sites)
    "Go to wikipedia.org and search for 'artificial intelligence'",
    "Visit mit.edu and look for online courses",
    "Navigate to stanford.edu and check research",
    "Go to nasa.gov and look at space news",
    
    # Government sites (from MCHP patterns)
    "Visit cdc.gov and check health guidelines",
    "Go to irs.gov and look for tax information",
    "Navigate to usa.gov and browse services",
    
    # Tech company sites (from MCHP's website list)
    "Visit microsoft.com and check products",
    "Go to apple.com and browse new releases",
    "Navigate to google.com/about",
    "Visit github.com and browse trending repositories",
    
    # Entertainment (from MCHP patterns)
    "Go to netflix.com and browse shows",
    "Visit spotify.com and check music",
    "Navigate to twitch.tv and see live streams",
    "Go to imdb.com and check movie ratings",
    
    # Sports and weather
    "Visit espn.com and check scores",
    "Go to weather.com and check forecast",
    "Navigate to nfl.com and look at schedules",
    "Visit nba.com and check standings",
    
    # Random popular sites (simulating MCHP's browse_web.py behavior)
    "Visit stackoverflow.com and browse questions",
    "Go to quora.com and read answers",
    "Navigate to medium.com and read articles",
    "Visit pinterest.com and browse images",
]

def random_delay(min_delay=MIN_ACTION_DELAY, max_delay=MAX_ACTION_DELAY):
    """Simulate human-like random delays between actions"""
    delay = random.uniform(min_delay, max_delay)
    time.sleep(delay)
    return delay

async def perform_browser_task(browser, task):
    """Execute a single browser task"""
    try:
        # Create agent for this task
        agent = Agent(
            task=task,
            llm=llm,
            browser=browser,
        )
        
        # Run the task with limited steps
        result = await agent.run(max_steps=5)
        return result
    except asyncio.TimeoutError:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [ERROR] Task timed out")
        return None
    except ConnectionError as e:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [ERROR] Browser connection error: {e}")
        return None
    except Exception as e:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [ERROR] Task failed: {e}")
        return None

async def perform_task_cluster(browser):
    """Perform a cluster of browser tasks (MCHP-style grouping)"""
    cluster_size = min(TASK_CLUSTER_COUNT, len(browser_tasks))
    selected_tasks = random.sample(browser_tasks, cluster_size)
    
    print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Starting task cluster with {cluster_size} tasks")
    
    for i, task in enumerate(selected_tasks, 1):
        try:
            print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [Task {i}/{cluster_size}] {task}")
            
            # Add random pre-task delay (simulating reading/thinking)
            pre_delay = random_delay(2, 5)
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [Delay] Pre-task delay: {pre_delay:.1f}s")
            
            # Execute the browser task
            start_time = time.time()
            result = await perform_browser_task(browser, task)
            execution_time = time.time() - start_time
            
            if result:
                # Print result summary
                result_str = str(result)
                if len(result_str) > 200:
                    result_str = result_str[:200] + "..."
                print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [Result] Task completed")
            else:
                print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [Result] Task failed or timed out")
                
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [Time] Task took {execution_time:.1f}s")
            
            # Inter-task delay within cluster
            if i < cluster_size:
                inter_delay = random.uniform(TASK_INTERVAL - 5, TASK_INTERVAL + 5)
                print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [Delay] Waiting {inter_delay:.1f}s before next task")
                time.sleep(inter_delay)
                
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [ERROR] Unexpected error: {e}")
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [Recovery] Waiting 5 seconds before continuing")
            time.sleep(5)
            continue
    
    print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Task cluster completed")

async def main():
    """Main loop with MCHP-like browser behavior"""
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] BU Agent starting with MCHP behavior patterns")
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Configuration:")
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]   - Tasks per cluster: {TASK_CLUSTER_COUNT}")
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]   - Task interval: {TASK_INTERVAL}s")
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]   - Cluster interval: {GROUPING_INTERVAL}s")
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]   - Model: {model_name}")
    
    iteration = 0
    
    try:
        # Create browser instance with headless mode using async context manager
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Initializing browser...")
        async with Browser(headless=True) as browser:
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Browser connected successfully")
            
            while True:
                iteration += 1
                print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {'='*60}")
                print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Starting iteration {iteration}")
                print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {'='*60}")
                
                # Perform a cluster of browser tasks
                await perform_task_cluster(browser)
                
                # Wait before next cluster (MCHP grouping interval)
                # Add some randomness to avoid patterns
                wait_variance = random.uniform(0.8, 1.2)
                wait_time = GROUPING_INTERVAL * wait_variance
                
                print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Entering idle period")
                print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [Delay] Waiting {wait_time:.0f}s ({wait_time/60:.1f} minutes) before next cluster")
                
                # Break the wait into smaller chunks for responsiveness
                wait_chunks = int(wait_time / 30)  # 30-second chunks
                for chunk in range(wait_chunks):
                    await asyncio.sleep(30)
                    remaining = wait_time - (chunk + 1) * 30
                    if remaining > 30 and chunk % 4 == 3:  # Every 2 minutes
                        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [Status] {remaining:.0f}s remaining...")
                
                # Sleep remainder
                remainder = wait_time % 30
                if remainder > 0:
                    await asyncio.sleep(remainder)
                
    except KeyboardInterrupt:
        print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Agent stopped by user")
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Graceful shutdown completed")
        sys.exit(0)
    except Exception as e:
        print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [FATAL ERROR] {e}")
        import traceback
        traceback.print_exc()
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Agent terminated unexpectedly")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())