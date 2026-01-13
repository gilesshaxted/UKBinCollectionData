from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uvicorn
import importlib
import json
import os
import sys
import logging
import subprocess
import re

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sbd_server")

# --- PATH FINDER ---
# We need to find the location of 'collect_data.py' to run it as a subprocess.
current_dir = os.getcwd()
collect_data_path = None

# Search for collect_data.py
for root, dirs, files in os.walk(current_dir):
    if "collect_data.py" in files:
        collect_data_path = os.path.join(root, "collect_data.py")
        break

if collect_data_path:
    logger.info(f"Found collect_data.py at: {collect_data_path}")
else:
    logger.error("CRITICAL: Could not find collect_data.py")

app = FastAPI()

class BinRequest(BaseModel):
    address_data: str
    module: str

@app.get("/")
def home():
    return {
        "status": "OK", 
        "message": "Bin API is running (v2.5 - Verbose Logging).", 
        "script_path": collect_data_path,
        "cwd": os.getcwd()
    }

@app.get("/get_councils")
def get_councils():
    councils = []
    errors = []
    
    # Strategy: File System Walk
    try:
        # Search for a folder named 'councils'
        found_councils_path = None
        for root, dirs, files in os.walk(os.getcwd()):
            if "councils" in dirs:
                found_councils_path = os.path.join(root, "councils")
                # Check if it looks like the right folder (contains .py files)
                py_files = [f for f in os.listdir(found_councils_path) if f.endswith(".py")]
                if len(py_files) > 0:
                    break
        
        if found_councils_path:
            for file in os.listdir(found_councils_path):
                if file.endswith(".py") and not file.startswith("__"):
                    raw_name = file[:-3] # remove .py extension
                    # Add space before capitals (e.g., WiltshireCouncil -> Wiltshire Council)
                    formatted_name = re.sub(r'(?<!^)(?=[A-Z])', ' ', raw_name)
                    councils.append(formatted_name)
            councils.sort()
            logger.info(f"Returning {len(councils)} councils.")
            return {"councils": councils}
        else:
            errors.append("Could not locate 'councils' folder.")
            
    except Exception as e:
        errors.append(f"Error listing councils: {str(e)}")

    return {"error": "Could not list councils.", "details": errors}

@app.get("/get_addresses")
def get_addresses(postcode: str, module: str):
    # Sanity check: clean module name (remove spaces for file check)
    clean_module = module.replace(" ", "")
    
    return [{"uprn": postcode, "address": f"Address lookup for {postcode} (Select to continue)"}]

@app.post("/get_bins")
def get_bins(req: BinRequest):
    if not collect_data_path:
        raise HTTPException(status_code=500, detail="Server misconfigured: collect_data.py not found.")

    try:
        # Convert "Wiltshire Council" back to "WiltshireCouncil"
        module_name = req.module.replace(" ", "")
        
        input_data = req.address_data.strip()
        
        # Prepare Environment (ensure PYTHONPATH includes current dir)
        env = os.environ.copy()
        env["PYTHONPATH"] = os.getcwd() + os.pathsep + env.get("PYTHONPATH", "")

        # COMMAND CONSTRUCTION
        # python collect_data.py <module> <URL> [ARGS]
        cmd = [sys.executable, collect_data_path, module_name]

        # Intelligent Argument Construction
        if input_data.lower().startswith("http"):
            # It's a URL - pass it as the mandatory second argument
            logger.info(f"DETECTED MODE: URL")
            logger.info(f"Subprocess: Running {module_name} with URL '{input_data}'")
            cmd.append(input_data)
        else:
            # It is NOT a URL (Postcode or UPRN).
            # The script requires a URL argument anyway. We pass a dummy URL.
            cmd.append("https://example.com") 
            
            # Check if strictly digits
            if input_data.isdigit():
                # It's a UPRN
                logger.info(f"DETECTED MODE: UPRN")
                logger.info(f"Subprocess: Running {module_name} with UPRN '{input_data}'")
                cmd.append("-u")
                cmd.append(input_data)
            else:
                # Assume Postcode - Apply UK Postcode Formatting logic
                logger.info(f"DETECTED MODE: POSTCODE")
                cmd.append("-p")
                
                # Normalize: Uppercase and remove spaces
                pc_clean = input_data.replace(" ", "").upper()
                
                # Re-insert space (UK postcodes are generally: Outcode + Space + Incode (3 chars))
                # If length is > 4, we assume the last 3 are the Incode.
                if len(pc_clean) >= 5 and len(pc_clean) <= 7:
                    pc_formatted = pc_clean[:-3] + " " + pc_clean[-3:]
                    logger.info(f"Subprocess: Running {module_name} with formatted Postcode '{pc_formatted}'")
                    cmd.append(pc_formatted)
                else:
                    # Fallback for short/weird postcodes
                    logger.info(f"Subprocess: Running {module_name} with raw Postcode '{input_data}'")
                    cmd.append(input_data)
        
        # Run the command and capture output
        result = subprocess.run(cmd, capture_output=True, text=True, env=env)
        
        # LOGGING OUTPUTS FOR DEBUGGING
        if result.stdout:
            logger.info(f"STDOUT: {result.stdout[:200]}...") 
        if result.stderr:
            logger.error(f"STDERR: {result.stderr}")

        if result.returncode != 0:
            # Check for common errors to give better feedback
            err_msg = result.stderr
            if "MissingSchema" in err_msg:
                 err_msg = "This council might require a valid URL but a Postcode was provided. The scraper attempted to load a placeholder URL and failed."
            elif "not found" in err_msg.lower():
                 err_msg = "Address or Postcode not found by the council's system."
            
            raise Exception(f"Script failed: {err_msg}")

        # PARSE JSON
        output = result.stdout.strip()
        
        try:
            return json.loads(output)
        except json.JSONDecodeError:
            import re
            json_match = re.search(r'(\{.*"bins".*\})', output, re.DOTALL)
            if json_match:
                return json.loads(json_match.group(1))
            else:
                # If we have output but no JSON, it might be an error printed to stdout
                raise Exception(f"Could not parse JSON from output. Raw output start: {output[:100]}...")

    except HTTPException as he:
        raise he
    except Exception as e:
        logger.error(f"Execution Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
