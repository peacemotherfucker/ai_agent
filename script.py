import argparse
import os
import subprocess
import json
import time
import logging
import yaml
import re
from pathlib import Path
from typing import Dict, List, Optional
from dataclasses import dataclass
from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_exponential
from logging.handlers import RotatingFileHandler

# Configure logging
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Configure LLM message logger
llm_logger = logging.getLogger('llm_messages')
llm_logger.setLevel(logging.INFO)

# Console handler
console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))

# File handlers
log_dir = '/app/logs'
data_dir = '/app/data'
os.makedirs(log_dir, exist_ok=True)
os.makedirs(data_dir, exist_ok=True)

# Main log file handler - using FileHandler instead of RotatingFileHandler to overwrite on startup
file_handler = logging.FileHandler(
    os.path.join(log_dir, 'ai_agent.log'),
    mode='w',
    encoding='utf-8'
)
file_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))

# LLM messages log file handler - using FileHandler to overwrite on startup
llm_file_handler = logging.FileHandler(
    os.path.join(log_dir, 'llm_messages.log'),
    mode='w',
    encoding='utf-8'
)
llm_file_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))

logger.addHandler(console_handler)
logger.addHandler(file_handler)
llm_logger.addHandler(llm_file_handler)

# Log startup message
logger.info("=== New session started ===")
llm_logger.info("=== New session started ===")

@dataclass
class Config:
    max_steps: int = 10
    timeout: int = 30
    history_size: int = 5
    model: str = None
    dangerous_commands: List[str] = None
    log_level: str = "INFO"
    
    @classmethod
    def load(cls, config_path: str = "config.yaml") -> 'Config':
        default_dangerous = ["rm", "mkfs", "dd", "fork", ">", "sudo"]
        config_data = {}
        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                config_data = yaml.safe_load(f)
        
        # Get model from environment variable with fallback
        config_data['model'] = os.getenv("OPENAI_MODEL", "gpt-4-1106-preview")
        config_data['dangerous_commands'] = config_data.get('dangerous_commands', default_dangerous)
        return cls(**config_data)

class CommandExecutor:
    def __init__(self, config: Config):
        self.config = config
        self.history: List[Dict] = []
        self.client = OpenAI(
            api_key=self._get_api_key(),
            base_url=os.getenv("OPENAI_API_BASE", "https://api.openai.com/v1")
        )
        
    def _get_api_key(self) -> str:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY environment variable is not set")
        return api_key

    def is_dangerous_command(self, command: str) -> bool:
        return any(dangerous_cmd in command.lower() 
                  for dangerous_cmd in self.config.dangerous_commands)

    def execute_command(self, command: str) -> Dict:
        if self.is_dangerous_command(command):
            logger.warning(f"Potentially dangerous command detected: {command}")
            return {
                "stdout": "",
                "stderr": "Command blocked due to security concerns",
                "returncode": -1,
                "success": False
            }

        try:
            logger.info(f"Executing command: {command}")
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=self.config.timeout
            )
            output = {
                "stdout": result.stdout,
                "stderr": result.stderr,
                "returncode": result.returncode,
                "success": result.returncode == 0
            }
            logger.debug(f"Command result: {output}")
            return output
        except subprocess.TimeoutExpired:
            logger.error(f"Command timed out after {self.config.timeout} seconds")
            return {
                "stdout": "",
                "stderr": f"Command timed out after {self.config.timeout} seconds",
                "returncode": -1,
                "success": False
            }
        except Exception as e:
            logger.error(f"Error executing command: {str(e)}")
            return {
                "stdout": "",
                "stderr": str(e),
                "returncode": -1,
                "success": False
            }

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
    def get_next_commands(self, goal: str) -> Dict:
        messages = [
            {
                "role": "system",
                "content": "You are a self driven AI agent. You are integrated directly into a Linux container with root rights. You can create scripts , write code , create interfaces etc. Your primary tool is linux command line. Generate commands to achieve a given goal. Keep commands short and expect a linux cli response. Respond ONLY with JSON: {'commands': [], 'goal_done': bool}. Only mark goal_done: true, when you think you have reached the goal. When you reach the goal, you will be terminated"
            },
            {
                "role": "user",
                "content": f"Goal: {goal}\nHistory:\n{json.dumps(self.history[-self.config.history_size:], indent=2)}"
            }
        ]

        # Log messages with proper formatting
        formatted_messages = json.dumps(messages, indent=2).replace('\\n', '\n')
        llm_logger.info("Sending messages to LLM API:\n%s", formatted_messages)
        llm_logger.info("Using model: %s", self.config.model)

        try:
            response = self.client.chat.completions.create(
                model=self.config.model,
                messages=messages,
                response_format={"type": "json_object"}
            )
            
            llm_logger.info("API Response Status - Has Choices: %s", bool(response.choices))
            
            if not response.choices:
                raise ValueError("No choices in OpenAI API response")
                
            response_message = response.choices[0].message
            if not response_message:
                raise ValueError("No message in OpenAI API response choice")
                
            response_content = response_message.content
            if not response_content:
                raise ValueError("Empty content in OpenAI API response message")

            llm_logger.info("Raw response content:\n%s", response_content)
            
            # Extract JSON from the response content
            try:
                # Look for JSON content between triple backticks
                json_match = re.search(r'```json\s*(.*?)\s*```', response_content, re.DOTALL)
                if json_match:
                    response_content = json_match.group(1).strip()
                    llm_logger.info("Extracted JSON from markdown:\n%s", response_content)
                else:
                    llm_logger.warning("No JSON block found in markdown, trying to parse entire response")
                
                parsed_response = json.loads(response_content)
                
                # Check if goal is done
                if parsed_response.get('goal_done', False):
                    logger.info("Goal completed successfully!")
                    return {"commands": [], "goal_done": True}
                    
                return parsed_response

            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse JSON response: {e}")
                return {"commands": [], "goal_done": False}

        except Exception as e:
            logger.error(f"Error getting next commands: {e}")
            return {"commands": [], "goal_done": False}

    def run(self, goal: str) -> None:
        logger.info(f"Starting execution with goal: {goal}")
        
        for step in range(1, self.config.max_steps + 1):
            logger.info(f"=== Step {step}/{self.config.max_steps} ===")
            
            try:
                response_json = self.get_next_commands(goal)
            except Exception as e:
                logger.error(f"Failed to get next commands: {str(e)}")
                break

            if response_json.get("done", False):
                logger.info("Goal achieved by LLM declaration!")
                break

            try:
                commands = response_json.get("commands", [])
            except Exception as e:
                logger.exception(f"Failed to generate commands: {str(e)}")
                continue

            if not commands:
                logger.warning("No commands generated by LLM")
                continue

            for cmd in commands:
                result = self.execute_command(cmd)
                self.history.append({"command": cmd, "result": result})
                
                if result["stdout"]:
                    logger.info(f"STDOUT: {result['stdout']}")
                if result["stderr"]:
                    logger.error(f"STDERR: {result['stderr']}")
                logger.info(f"Return code: {result['returncode']}")

        self._print_summary()

    def _print_summary(self) -> None:
        logger.info("\n=== Final Summary ===")
        for idx, entry in enumerate(self.history):
            logger.info(f"Step {idx+1}: {entry['command']}")
            logger.info(f"  Return code: {entry['result']['returncode']}")
            if entry['result']['stdout']:
                logger.info(f"  STDOUT: {entry['result']['stdout'][:200]}...")
            if entry['result']['stderr']:
                logger.info(f"  STDERR: {entry['result']['stderr'][:200]}...")

def main():
    parser = argparse.ArgumentParser(description='AI Agent for executing commands')
    parser.add_argument('--goal', required=True, help='The goal to achieve')
    parser.add_argument('--config', default='config.yaml', help='Path to config file')
    args = parser.parse_args()

    config = Config.load(args.config)
    executor = CommandExecutor(config)
    
    step = 0
    while step < config.max_steps:
        logger.info(f"Step {step + 1}/{config.max_steps}")
        
        response = executor.get_next_commands(args.goal)
        
        # Check if goal is completed
        if response.get('goal_done', False):
            logger.info("Goal has been reached! Waiting for new tasks...")
            # Instead of exiting, sleep indefinitely
            while True:
                time.sleep(60)
            
        commands = response.get('commands', [])
        if not commands:
            logger.warning("No commands received, stopping execution")
            break

        for command in commands:
            result = executor.execute_command(command)
            executor.history.append({
                "command": command,
                "result": result
            })
            
            if not result['success']:
                logger.error(f"Command failed: {command}")
                return 1

        step += 1
    
    logger.warning("Maximum steps reached without completing the goal")
    return 1

if __name__ == "__main__":
    main()