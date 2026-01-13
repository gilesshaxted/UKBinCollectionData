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
import requests
from bs4 import BeautifulSoup

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sbd_server")

# --- GLOBAL CACHE ---
# Stores bin data in memory to make ICS/Calendar requests instant.
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

# --- UPRN LOOKUP HELPER ---
def lookup_uprn_public(postcode, house_identifier):
    """
    Attempts to find a UPRN for a given postcode and house name/number
    by querying a public directory (uprn.uk).
    """
    try:
        clean_pc = postcode.replace(" ", "").strip()
        url = f"https://www.uprn.uk/addresses/{clean_pc}"
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
        
        logger.info(f"UPRN LOOKUP: Querying {url} for '{house_identifier}'")
        response = requests.get(url, headers=headers, timeout=5)
        
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            # The site lists addresses in table rows or list items.
            # We look for text that contains our house identifier.
            
            # Simple fuzzy match strategy
            target = house_identifier.lower()
            
            # Find all links that might be UPRN links
            links = soup.find_all('a', href=True)
            for link in links:
                txt = link.get_text().lower()
                href = link['href']
                
                # Check if this link looks like a UPRN link (/uprn/12345)
                if "/uprn/" in href:
                    # Check if the address text contains our house name/number
                    # We check strict components to avoid "1" matching "10"
                    # But for names like "High Trees", a substring check is usually safe enough for now.
                    if target in txt:
                        # Extract UPRN from text or href
                        uprn_match = re.search(r'\d{8,12}', href)
                        if uprn_match:
                            found_uprn = uprn_match.group(0)
                            logger.info(f"UPRN LOOKUP: Found match! {txt} -> {found_uprn}")
                            return found_uprn
            
            logger.info("UPRN LOOKUP: No match found on page.")
        else:
            logger.warning(f"UPRN LOOKUP: Failed to fetch page. Status: {response.status_code}")
            
    except Exception as e:
        logger.error(f"UPRN LOOKUP ERROR: {e}")
    
    return None

@app.get("/")
def home():
    return {"status": "OK", "message": "Bin API is running (v3.3 - UPRN Auto-Lookup)."}

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
        cache_key = f"{module_name}|{input_data.lower()}"
        current_time = time.time()
        
        if cache_key in BIN_CACHE:
            cached_item = BIN_CACHE[cache_key]
            if current_time - cached_item["timestamp"] < CACHE_DURATION:
                logger.info(f"CACHE HIT: Returning saved data for {input_data}")
                return cached_item["data"]
            else:
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
            cmd.append("https://example.com") 

            # Extract Postcode
            pc_pattern = r'([Gg][Ii][Rr] 0[Aa]{2})|((([A-Za-z][0-9]{1,2})|(([A-Za-z][A-Ha-hJ-Yj-y][0-9]{1,2})|(([A-Za-z][0-9][A-Za-z])|([A-Za-z][A-Ha-hJ-Yj-y][0-9][A-Za-z]?))))\s?[0-9][A-Za-z]{2})'
            pc_match = re.search(pc_pattern, input_data)
            
            extracted_postcode = ""
            remaining_text = input_data
            
            if pc_match:
                extracted_postcode = pc_match.group(0).upper()
                remaining_text = input_data.replace(extracted_postcode, "").strip()
            
            # Detect UPRN
            uprn_match = re.search(r'\b\d{8,12}\b', remaining_text)
            
            # --- ARGUMENT ASSEMBLY ---
            if uprn_match:
                # Case A: User provided UPRN explicitly
                uprn = uprn_match.group(0)
                logger.info(f"DETECTED MODE: UPRN (Explicit: {uprn})")
                cmd.append("-u")
                cmd.append(uprn)
                
                if extracted_postcode:
                    cmd.append("-p")
                    cmd.append(extracted_postcode)
                else:
                    cmd.append("-p")
                    cmd.append("BA14 8JN")
                    used_dummy_postcode = True
            
            else:
                # Case B: Postcode Search (Address Name/Number provided)
                logger.info(f"DETECTED MODE: POSTCODE SEARCH")
                
                if extracted_postcode:
                    house_identifier = remaining_text.strip(",. ")
                    
                    # --- AUTO-LOOKUP ATTEMPT ---
                    found_uprn = None
                    if house_identifier:
                        # Try to find UPRN externally before asking scraper
                        found_uprn = lookup_uprn_public(extracted_postcode, house_identifier)
                    
                    if found_uprn:
                         # We found it! Switch to UPRN mode.
                         logger.info(f"SWITCHING TO UPRN MODE via Auto-Lookup: {found_uprn}")
                         cmd.append("-u")
                         cmd.append(found_uprn)
                         cmd.append("-p")
                         cmd.append(extracted_postcode)
                    else:
                        # Fallback to standard scraper logic
                        cmd.append("-p")
                        cmd.append(extracted_postcode)
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
                 raise HTTPException(status_code=400, detail="This Council requires you to provide the Postcode alongside the UPRN.")
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
