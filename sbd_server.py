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
import time

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sbd_server")

# --- GLOBAL CACHE ---
# Stores bin data in memory to make ICS/Calendar requests instant.
# Format: { "unique_key": { "timestamp": 123456789, "data": {...} } }
BIN_CACHE = {}
CACHE_DURATION = 86400  # 24 Hours

# --- PATH FINDER ---
current_dir = os.getcwd()
collect_data_path = None
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
    return {"status": "OK", "message": "Bin API is running (v3.2 - Caching Enabled)."}

@app.get("/get_councils")
def get_councils():
    councils = []
    errors = []
    try:
        found_councils_path = None
        for root, dirs, files in os.walk(os.getcwd()):
            if "councils" in dirs:
                found_councils_path = os.path.join(root, "councils")
                py_files = [f for f in os.listdir(found_councils_path) if f.endswith(".py")]
                if len(py_files) > 0:
                    break
        
        if found_councils_path:
            for file in os.listdir(found_councils_path):
                if file.endswith(".py") and not file.startswith("__"):
                    raw_name = file[:-3]
                    formatted_name = re.sub(r'(?<!^)(?=[A-Z])', ' ', raw_name)
                    councils.append(formatted_name)
            councils.sort()
            return {"councils": councils}
        else:
            errors.append("Could not locate 'councils' folder.")
    except Exception as e:
        errors.append(f"Error listing councils: {str(e)}")
    return {"error": "Could not list councils.", "details": errors}

@app.get("/get_addresses")
def get_addresses(postcode: str, module: str):
    return [{"uprn": postcode, "address": f"Address lookup for {postcode} (Select to continue)"}]

@app.post("/get_bins")
def get_bins(req: BinRequest):
    if not collect_data_path:
        raise HTTPException(status_code=500, detail="Server misconfigured: collect_data.py not found.")

    try:
        module_name = req.module.replace(" ", "")
        input_data = req.address_data.strip()
        
        # --- CACHE CHECK ---
        # Generate a unique key for this request
        cache_key = f"{module_name}|{input_data.lower()}"
        current_time = time.time()
        
        if cache_key in BIN_CACHE:
            cached_item = BIN_CACHE[cache_key]
            if current_time - cached_item["timestamp"] < CACHE_DURATION:
                logger.info(f"CACHE HIT: Returning saved data for {input_data}")
                return cached_item["data"]
            else:
                logger.info(f"CACHE EXPIRED: Refreshing data for {input_data}")
                del BIN_CACHE[cache_key]
        
        # --- PREPARE SUBPROCESS ---
        env = os.environ.copy()
        env["PYTHONPATH"] = os.getcwd() + os.pathsep + env.get("PYTHONPATH", "")
        cmd = [sys.executable, collect_data_path, module_name]
        
        used_dummy_postcode = False 

        # --- INTELLIGENT PARSING LOGIC ---
        if input_data.lower().startswith("http"):
            logger.info(f"DETECTED MODE: URL")
            cmd.append(input_data)
        
        else:
            cmd.append("https://example.com") # URL Placeholder

            # Extract Postcode using Regex
            pc_pattern = r'([Gg][Ii][Rr] 0[Aa]{2})|((([A-Za-z][0-9]{1,2})|(([A-Za-z][A-Ha-hJ-Yj-y][0-9]{1,2})|(([A-Za-z][0-9][A-Za-z])|([A-Za-z][A-Ha-hJ-Yj-y][0-9][A-Za-z]?))))\s?[0-9][A-Za-z]{2})'
            pc_match = re.search(pc_pattern, input_data)
            
            extracted_postcode = ""
            remaining_text = input_data
            
            if pc_match:
                extracted_postcode = pc_match.group(0).upper()
                remaining_text = input_data.replace(extracted_postcode, "").strip()
            
            # Detect UPRN
            uprn_match = re.search(r'\b\d{8,12}\b', remaining_text)
            
            if uprn_match:
                uprn = uprn_match.group(0)
                logger.info(f"DETECTED MODE: UPRN ({uprn})")
                cmd.append("-u")
                cmd.append(uprn)
                
                if extracted_postcode:
                    logger.info(f"Adding accompanying Postcode: {extracted_postcode}")
                    cmd.append("-p")
                    cmd.append(extracted_postcode)
                else:
                    logger.info(f"Adding Dummy Postcode (Wiltshire HQ) for validation")
                    cmd.append("-p")
                    cmd.append("BA14 8JN")
                    used_dummy_postcode = True
            
            else:
                logger.info(f"DETECTED MODE: POSTCODE SEARCH")
                
                if extracted_postcode:
                    cmd.append("-p")
                    cmd.append(extracted_postcode)
                    
                    # Clean remaining text to be a valid House Number/Name
                    house_identifier = remaining_text.strip(",. ")
                    if house_identifier:
                        logger.info(f"Adding House Identifier (Name/Number): {house_identifier}")
                        cmd.append("-n")
                        cmd.append(house_identifier)
                else:
                    logger.info(f"No regex match. Sending raw input as postcode: {input_data}")
                    cmd.append("-p")
                    cmd.append(input_data)

        logger.info(f"Command: {cmd}")

        result = subprocess.run(cmd, capture_output=True, text=True, env=env)
        
        if result.stdout:
            logger.info(f"STDOUT: {result.stdout[:200]}...")
        if result.stderr:
            logger.error(f"STDERR: {result.stderr}")

        if result.returncode != 0:
            err_msg = result.stderr
            if "MissingSchema" in err_msg:
                 err_msg = "Scraper failed on placeholder URL. This council might require a specific URL."
            elif "not found" in err_msg.lower():
                 err_msg = "Address not found."
            raise Exception(f"Script failed: {err_msg}")

        output = result.stdout.strip()
        
        if "Exception encountered" in output or "Invalid UPRN" in output:
             raise HTTPException(status_code=400, detail="Address not found by council system. Please try searching with your UPRN (12-digit number) found on 'uprn.uk'.")

        if '"bins": []' in output:
             if used_dummy_postcode:
                 raise HTTPException(status_code=400, detail="This Council requires you to provide the Postcode alongside the UPRN. Please search again entering both, e.g. '100120992798 SN8 1RA'.")
             logger.warning("Scraper returned empty bins list.")
        
        try:
            json_data = json.loads(output)
        except json.JSONDecodeError:
            json_match = re.search(r'(\{.*"bins".*\})', output, re.DOTALL)
            if json_match:
                json_data = json.loads(json_match.group(1))
            else:
                raise Exception(f"Could not parse JSON. Output start: {output[:100]}...")

        # --- SAVE TO CACHE ---
        BIN_CACHE[cache_key] = {
            "timestamp": time.time(),
            "data": json_data
        }
        logger.info(f"Saved result to CACHE for key: {cache_key}")

        return json_data

    except HTTPException as he:
        raise he
    except Exception as e:
        logger.error(f"Execution Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
