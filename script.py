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
os.makedirs(log_dir, exist_ok=True)

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
                "content": "You are a Linux expert assistant. Generate commands to achieve a given goal. Respond ONLY with JSON: {'commands': []}."
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
                
                try:
                    parsed_response = json.loads(response_content)
                except json.JSONDecodeError as e:
                    llm_logger.error("JSON parsing error: %s\nTrying to clean the content...", str(e))
                    # Try to find anything that looks like a JSON object
                    json_match = re.search(r'(\{[^}]+\})', response_content, re.DOTALL)
                    if json_match:
                        response_content = json_match.group(1).strip()
                        llm_logger.info("Found JSON-like content:\n%s", response_content)
                        parsed_response = json.loads(response_content)
                    else:
                        raise ValueError("Could not find valid JSON in response")

                if not isinstance(parsed_response, dict):
                    raise ValueError(f"Expected dict, got {type(parsed_response)}")
                if 'commands' not in parsed_response:
                    raise ValueError(f"Missing required field 'commands' in response: {parsed_response}")
                return parsed_response
            except json.JSONDecodeError as e:
                llm_logger.error("Failed to parse JSON response: %s\nContent: %s", str(e), response_content)
                raise
            except Exception as e:
                llm_logger.error("Error processing response: %s\nFull content: %s", str(e), response_content)
                raise
        except Exception as e:
            error_msg = f"LLM API Error: {str(e)}"
            logger.error(error_msg)
            llm_logger.error(error_msg)
            if hasattr(e, '__cause__') and e.__cause__:
                llm_logger.error("Caused by: %s", str(e.__cause__))
            raise

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
    parser = argparse.ArgumentParser(description="Autonomous Linux Command Executor")
    parser.add_argument("--goal", required=True, help="The objective to achieve")
    parser.add_argument("--config", default="config.yaml", help="Path to configuration file")
    parser.add_argument("--max-steps", type=int, help="Override maximum iterations")
    args = parser.parse_args()

    try:
        config = Config.load(args.config)
        if args.max_steps:
            config.max_steps = args.max_steps

        logging.getLogger().setLevel(getattr(logging, config.log_level.upper()))
        
        executor = CommandExecutor(config)
        executor.run(args.goal)
    except Exception as e:
        logger.error(f"Fatal error: {str(e)}")
        raise

if __name__ == "__main__":
    main()