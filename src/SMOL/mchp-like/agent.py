#!/usr/bin/env python3
"""
SMOL Agent with MCHP-like behavior patterns
Simulates human-like computer usage with task clustering and realistic timing
"""

from smolagents import CodeAgent, LiteLLMModel, DuckDuckGoSearchTool
import time
import random
import os
import sys
from datetime import datetime

# Configuration (matching MCHP parameters)
TASK_CLUSTER_COUNT = 5  # Number of tasks to perform in a cluster
TASK_INTERVAL = 10  # Seconds between tasks within a cluster
GROUPING_INTERVAL = 500  # Seconds between task clusters (8.3 minutes)
MIN_ACTION_DELAY = 1  # Minimum delay for actions (seconds)
MAX_ACTION_DELAY = 20  # Maximum delay for actions (seconds)

# Initialize with LiteLLM - can use local models or API-based models
model_id = os.getenv("LITELLM_MODEL", "ollama/llama2")  # Default to local Ollama model
print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Initializing SMOL Agent with model: {model_id}")

try:
    model = LiteLLMModel(model_id=model_id)
    agent = CodeAgent(
        tools=[DuckDuckGoSearchTool()],
        model=model,
    )
except Exception as e:
    print(f"[ERROR] Failed to initialize agent: {e}")
    sys.exit(1)

# MCHP-like task list
tasks = [
    # Technical searches (from MCHP's google_searches.txt)
    "Search for 'how to remove specific text from a file' and summarize the methods",
    "Look up 'C# SQL query with user input' and explain SQL injection prevention",
    "Search for 'python programming tutorials' and list beginner resources",
    "Find information about 'how to use vscode' and list keyboard shortcuts",
    
    # Practical queries (from MCHP patterns)
    "Search for 'what is my ip address' and explain IP types",
    "Look up 'weather' and get current conditions",
    "Search for 'starbucks near me' (simulate location-based search)",
    "Find 'gmail' features and tips",
    "Search 'facebook' privacy settings guide",
    
    # YouTube-style content (from MCHP's browse_youtube.txt)
    "Search for 'cake decorating tutorials' and summarize techniques",
    "Look up 'buttercream icing recipes' and list ingredients",
    "Find 'python 101' tutorial videos descriptions",
    "Search for 'VSCode tips and tricks' content",
    
    # News and current events (from MCHP's website list)
    "Check CNN for breaking news headlines",
    "Look up BBC world news stories",
    "Search Reuters for financial news",
    "Find technology news from tech sites",
    "Look up 'nfl schedule' or 'nba scores'",
    "Search for 'netflix' new releases",
    
    # Educational content (from MCHP's educational sites)
    "Search MIT OpenCourseWare for free courses",
    "Look up Stanford research papers",
    "Find NASA space exploration updates",
    "Search NIST for technical publications",
    "Browse Wikipedia for random interesting articles",
    
    # E-commerce simulation (from MCHP's website list)
    "Search Amazon for trending electronics",
    "Look up eBay auction items",
    "Find Walmart deals and discounts",
    "Search for product reviews and comparisons",
    
    # Government and institutional (from MCHP patterns)
    "Search CDC health guidelines",
    "Look up FBI safety tips",
    "Find IRS tax information",
    "Search government services and resources",
    
    # Tech companies (from MCHP's website list)
    "Browse Microsoft product updates",
    "Search Apple new releases",
    "Look up Adobe Creative Cloud features",
    "Find IBM technology solutions",
    
    # Social media trends (from MCHP patterns)
    "Search Twitter/X trending topics",
    "Look up Instagram popular hashtags",
    "Find TikTok viral content descriptions",
    "Search LinkedIn professional tips",
    
    # Random browsing (simulating MCHP's browse_web.py behavior)
    "Visit and describe a random popular website",
    "Explore a random news website",
    "Browse a random educational resource",
    "Check a random technology blog",
]

def random_delay(min_delay=MIN_ACTION_DELAY, max_delay=MAX_ACTION_DELAY):
    """Simulate human-like random delays between actions"""
    delay = random.uniform(min_delay, max_delay)
    time.sleep(delay)
    return delay

def perform_task_cluster():
    """Perform a cluster of tasks (MCHP-style grouping)"""
    cluster_size = min(TASK_CLUSTER_COUNT, len(tasks))
    selected_tasks = random.sample(tasks, cluster_size)
    
    print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Starting task cluster with {cluster_size} tasks")
    
    for i, task in enumerate(selected_tasks, 1):
        try:
            print(f"\n[Task {i}/{cluster_size}] {task}")
            
            # Add random pre-task delay (simulating reading/thinking)
            pre_delay = random_delay(2, 5)
            print(f"  [Delay] Pre-task delay: {pre_delay:.1f}s")
            
            # Execute the task
            start_time = time.time()
            result = agent.run(task)
            execution_time = time.time() - start_time
            
            # Print result (truncated for readability)
            result_str = str(result)
            if len(result_str) > 200:
                result_str = result_str[:200] + "..."
            print(f"  [Result] {result_str}")
            print(f"  [Time] Task completed in {execution_time:.1f}s")
            
            # Inter-task delay within cluster
            if i < cluster_size:
                inter_delay = random.uniform(TASK_INTERVAL - 5, TASK_INTERVAL + 5)
                print(f"  [Delay] Waiting {inter_delay:.1f}s before next task")
                time.sleep(inter_delay)
                
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"  [ERROR] Task failed: {e}")
            print(f"  [Recovery] Waiting 5 seconds before continuing")
            time.sleep(5)
            continue
    
    print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Task cluster completed")

def main():
    """Main loop with MCHP-like behavior"""
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] SMOL Agent starting with MCHP behavior patterns")
    print(f"Configuration:")
    print(f"  - Tasks per cluster: {TASK_CLUSTER_COUNT}")
    print(f"  - Task interval: {TASK_INTERVAL}s")
    print(f"  - Cluster interval: {GROUPING_INTERVAL}s")
    print(f"  - Model: {model_id}")
    
    iteration = 0
    
    try:
        while True:
            iteration += 1
            print(f"\n{'='*60}")
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Starting iteration {iteration}")
            print(f"{'='*60}")
            
            # Perform a cluster of tasks
            perform_task_cluster()
            
            # Wait before next cluster (MCHP grouping interval)
            # Add some randomness to avoid patterns
            wait_variance = random.uniform(0.8, 1.2)
            wait_time = GROUPING_INTERVAL * wait_variance
            
            print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Entering idle period")
            print(f"[Delay] Waiting {wait_time:.0f}s ({wait_time/60:.1f} minutes) before next cluster")
            
            # Break the wait into smaller chunks for responsiveness
            wait_chunks = int(wait_time / 30)  # 30-second chunks
            for chunk in range(wait_chunks):
                time.sleep(30)
                remaining = wait_time - (chunk + 1) * 30
                if remaining > 30 and chunk % 4 == 3:  # Every 2 minutes
                    print(f"  [Status] {remaining:.0f}s remaining...")
            
            # Sleep remainder
            remainder = wait_time % 30
            if remainder > 0:
                time.sleep(remainder)
                
    except KeyboardInterrupt:
        print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Agent stopped by user")
        print("Graceful shutdown completed")
        sys.exit(0)
    except Exception as e:
        print(f"\n[FATAL ERROR] {e}")
        print("Agent terminated unexpectedly")
        sys.exit(1)

if __name__ == "__main__":
    main()