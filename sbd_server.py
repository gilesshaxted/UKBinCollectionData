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
    return {"status": "OK", "message": "Bin API is running (v2.9 - House Names & UPRN Fix)."}

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
        env = os.environ.copy()
        env["PYTHONPATH"] = os.getcwd() + os.pathsep + env.get("PYTHONPATH", "")
        cmd = [sys.executable, collect_data_path, module_name]

        # --- INTELLIGENT PARSING LOGIC ---
        
        # 1. Check for URL
        if input_data.lower().startswith("http"):
            logger.info(f"DETECTED MODE: URL")
            cmd.append(input_data)
        
        else:
            # We MUST provide a URL argument to satisfy the script, even if using -p/-u
            cmd.append("https://example.com") 

            # 2. Extract Postcode using Regex
            # Captures standard UK postcodes (e.g. SW1A 1AA, SN8 1RA, M1 1AA)
            pc_pattern = r'([Gg][Ii][Rr] 0[Aa]{2})|((([A-Za-z][0-9]{1,2})|(([A-Za-z][A-Ha-hJ-Yj-y][0-9]{1,2})|(([A-Za-z][0-9][A-Za-z])|([A-Za-z][A-Ha-hJ-Yj-y][0-9][A-Za-z]?))))\s?[0-9][A-Za-z]{2})'
            pc_match = re.search(pc_pattern, input_data)
            
            extracted_postcode = ""
            remaining_text = input_data
            
            if pc_match:
                extracted_postcode = pc_match.group(0).upper()
                # Remove postcode from input to see what is left (House Number/Name/UPRN)
                remaining_text = input_data.replace(extracted_postcode, "").strip()
            
            # 3. Detect UPRN in the remaining text (Look for long number sequence)
            uprn_match = re.search(r'\b\d{8,12}\b', remaining_text) # UPRNs are usually 12 digits, sometimes fewer
            
            if uprn_match:
                uprn = uprn_match.group(0)
                logger.info(f"DETECTED MODE: UPRN ({uprn})")
                cmd.append("-u")
                cmd.append(uprn)
                
                # If we also found a postcode, pass it to help validation!
                if extracted_postcode:
                    logger.info(f"Adding accompanying Postcode: {extracted_postcode}")
                    cmd.append("-p")
                    cmd.append(extracted_postcode)
                else:
                    # Fallback Dummy Postcode (Wiltshire HQ) to prevent crash if scraper demands one
                    logger.info(f"Adding Dummy Postcode (Wiltshire HQ) for validation")
                    cmd.append("-p")
                    cmd.append("BA14 8JN")
            
            else:
                # 4. Standard Address/Postcode Search
                logger.info(f"DETECTED MODE: POSTCODE SEARCH")
                
                if extracted_postcode:
                    cmd.append("-p")
                    cmd.append(extracted_postcode)
                    
                    # Anything left over is likely the House Name or Number
                    # Remove punctuation/extra spaces
                    house_identifier = remaining_text.strip(",. ")
                    if house_identifier:
                        logger.info(f"Adding House Identifier (Name/Number): {house_identifier}")
                        cmd.append("-n")
                        cmd.append(house_identifier)
                else:
                    # Fallback: User typed something that doesn't look like a postcode.
                    # Send it raw as postcode and hope.
                    logger.info(f"No regex match. Sending raw input as postcode: {input_data}")
                    cmd.append("-p")
                    cmd.append(input_data)

        # Log command
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
        
        # UX: Handle empty bins or specific errors in output
        if '"bins": []' in output:
             logger.warning("Scraper returned empty bins list.")
             # We return it anyway so the frontend can say "No bins found"
        
        try:
            return json.loads(output)
        except json.JSONDecodeError:
            import re
            json_match = re.search(r'(\{.*"bins".*\})', output, re.DOTALL)
            if json_match:
                return json.loads(json_match.group(1))
            else:
                raise Exception(f"Could not parse JSON. Output start: {output[:100]}...")

    except HTTPException as he:
        raise he
    except Exception as e:
        logger.error(f"Execution Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
