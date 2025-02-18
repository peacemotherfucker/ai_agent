from flask import Flask, render_template, request, jsonify
import logging
from script import CommandExecutor, Config
import os
import json
from threading import Thread
from queue import Queue
import time

app = Flask(__name__)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Ensure we're in the correct directory
os.chdir(os.path.dirname(os.path.abspath(__file__)))

# Create necessary directories
os.makedirs('/app/data', exist_ok=True)
os.makedirs('/app/logs', exist_ok=True)

# Global variables to store current settings and task status
current_settings = {
    "system_content": "You are a self driven AI agent. You are integrated directly into a Linux container with root rights. You can create scripts , write code , create interfaces etc. Your primary tool is linux command line. Generate commands to achieve a given goal. Keep commands short and expect a linux cli response. Respond ONLY with JSON: {'commands': [], 'goal_done': bool}. Only mark goal_done: true, when you think you have reached the goal.",
    "goal": "",
    "model": os.getenv("OPENAI_MODEL", "gpt-4-1106-preview")
}

task_status = {
    "is_running": False,
    "messages": [],
    "goal_reached": False
}

result_queue = Queue()

def run_agent(goal, system_content):
    global task_status
    task_status["is_running"] = True
    task_status["messages"] = []
    task_status["goal_reached"] = False
    
    try:
        config = Config.load()
        executor = CommandExecutor(config)
        
        # Override system content
        executor.system_content = system_content
        
        step = 0
        while step < config.max_steps and not task_status["goal_reached"]:
            response = executor.get_next_commands(goal)
            
            if response.get('goal_done', False):
                task_status["goal_reached"] = True
                task_status["messages"].append({
                    "type": "success",
                    "message": "Goal has been reached!"
                })
                break
                
            commands = response.get('commands', [])
            if not commands:
                task_status["messages"].append({
                    "type": "warning",
                    "message": "No commands received, stopping execution"
                })
                break

            for command in commands:
                result = executor.execute_command(command)
                message = {
                    "type": "command",
                    "command": command,
                    "success": result['success'],
                    "stdout": result['stdout'],
                    "stderr": result['stderr']
                }
                task_status["messages"].append(message)
                
                if not result['success']:
                    task_status["messages"].append({
                        "type": "error",
                        "message": f"Command failed: {command}"
                    })
                    return

            step += 1
            
    except Exception as e:
        task_status["messages"].append({
            "type": "error",
            "message": f"Error: {str(e)}"
        })
    finally:
        task_status["is_running"] = False

@app.route('/')
def index():
    return render_template('index.html', settings=current_settings)

@app.route('/api/settings', methods=['GET'])
def get_settings():
    return jsonify(current_settings)

@app.route('/api/settings', methods=['POST'])
def update_settings():
    data = request.json
    current_settings.update(data)
    return jsonify({"status": "success"})

@app.route('/api/start', methods=['POST'])
def start_task():
    if task_status["is_running"]:
        return jsonify({"status": "error", "message": "Task is already running"})
    
    data = request.json
    current_settings.update(data)
    
    Thread(target=run_agent, args=(current_settings["goal"], current_settings["system_content"])).start()
    return jsonify({"status": "success"})

@app.route('/api/status')
def get_status():
    return jsonify(task_status)

@app.route('/api/stop', methods=['POST'])
def stop_task():
    task_status["is_running"] = False
    return jsonify({"status": "success"})

@app.route('/api/llm_logs')
def get_llm_logs():
    try:
        with open('/app/logs/llm_messages.log', 'r') as f:
            # Read last 1000 lines (or less if file is smaller)
            lines = f.readlines()[-1000:]
            return jsonify({"logs": lines})
    except Exception as e:
        return jsonify({"logs": [f"Error reading log file: {str(e)}"]})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
