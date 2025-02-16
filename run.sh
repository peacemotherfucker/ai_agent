#!/bin/bash

# Source the .env file if it exists
if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

# Check if OPENAI_API_KEY is set
if [ -z "$OPENAI_API_KEY" ]; then
    echo "Error: OPENAI_API_KEY environment variable is not set"
    echo "Please check your .env file or set it using: export OPENAI_API_KEY=your_api_key"
    exit 1
fi

# Create logs directory if it doesn't exist
mkdir -p logs

# Get the goal from command line arguments
if [ $# -eq 0 ]; then
    echo "Error: Please provide a goal"
    echo "Usage: ./run.sh 'your goal here'"
    exit 1
fi

# Export the goal as an environment variable
export AGENT_GOAL="$1"

# Run the container
docker-compose up --build
