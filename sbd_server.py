from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional
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
    os_api_key: Optional[str] = None

class AddressRequest(BaseModel):
    postcode: str
    module: str
    os_api_key: Optional[str] = None

# --- ADDRESS LOOKUP ENGINE ---
def fetch_public_addresses(postcode):
    """
    Scrapes a public directory (uprn.uk) to return a list of 
    address-to-UPRN mappings for a given postcode.
    Fallback method if no OS API Key is provided.
    """
    results = []
    try:
        clean_pc = postcode.replace(" ", "").strip()
        url = f"https://www.uprn.uk/addresses/{clean_pc}"
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
        
        logger.info(f"PUBLIC LOOKUP: Fetching address list for {clean_pc}")
        response = requests.get(url, headers=headers, timeout=5)
        
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            # Find all links that might be UPRN links
            links = soup.find_all('a', href=True)
            for link in links:
                href = link['href']
                # Check for UPRN link pattern
                if "/uprn/" in href:
                    uprn_match = re.search(r'\d{8,12}', href)
                    if uprn_match:
                        uprn = uprn_match.group(0)
                        # Clean up address text
                        address_text = link.get_text().strip()
                        address_text = re.sub(r'\s+', ' ', address_text) # Remove extra whitespace
                        
                        results.append({
                            "uprn": uprn,
                            "address": address_text
                        })
            logger.info(f"PUBLIC LOOKUP: Found {len(results)} addresses.")
        else:
            logger.warning(f"PUBLIC LOOKUP: Failed to fetch page. Status: {response.status_code}")
            
    except Exception as e:
        logger.error(f"PUBLIC LOOKUP ERROR: {e}")
        
    return results

def fetch_os_places_addresses(postcode, api_key):
    """
    Queries the Ordnance Survey Places API for addresses.
    Requires a valid API Key.
    """
    results = []
    try:
        # OS Places API Endpoint for Postcode search
        url = "https://api.os.uk/search/places/v1/postcode"
        params = {
            "postcode": postcode,
            "key": api_key,
            "dataset": "DPA,LPI" # Query both AddressBase Premium and Local Property Identifier
        }
        
        logger.info(f"OS API LOOKUP: Querying OS Places API for {postcode}")
        response = requests.get(url, params=params, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            if "results" in data:
                for item in data["results"]:
                    # The API returns a wrapper object, usually 'DPA' or 'LPI'
                    address_data = item.get("DPA") or item.get("LPI")
                    if address_data:
                        uprn = address_data.get("UPRN")
                        address = address_data.get("ADDRESS")
                        if uprn and address:
                            results.append({
                                "uprn": uprn,
                                "address": address
                            })
                logger.info(f"OS API LOOKUP: Found {len(results)} addresses.")
            else:
                logger.info("OS API LOOKUP: No results found in response.")
        elif response.status_code == 401:
             logger.error("OS API LOOKUP: Invalid API Key.")
             return [{"uprn": "error", "address": "Error: Invalid OS API Key provided."}]
        else:
            logger.warning(f"OS API LOOKUP: Request failed. Status: {response.status_code} - {response.text}")
            
    except Exception as e:
        logger.error(f"OS API LOOKUP ERROR: {e}")
        return [{"uprn": "error", "address": f"OS API Error: {str(e)}"}]
        
    return results

def lookup_uprn_public(postcode, house_identifier):
    """
    Attempts to find a single UPRN by filtering the list from fetch_public_addresses
    against a house identifier (number or name).
    """
    # Note: This function uses the public scraper implicitly for auto-matching 
    # when no list selection has occurred yet.
    addresses = fetch_public_addresses(postcode)
    target = house_identifier.lower()
    
    for item in addresses:
        addr_text = item["address"].lower()
        # Check if house identifier matches start of string or is contained clearly
        if target in addr_text:
            logger.info(f"UPRN AUTO-MATCH: '{house_identifier}' matched '{item['address']}' -> {item['uprn']}")
            return item["uprn"]
            
    return None

def lookup_uprn_os(postcode, house_identifier, api_key):
    """
    Attempts to find a single UPRN using the OS Places API.
    """
    addresses = fetch_os_places_addresses(postcode, api_key)
    target = house_identifier.lower()
    
    # Check for error response first
    if addresses and "error" in addresses[0].get("uprn", ""):
        return None

    for item in addresses:
        addr_text = item["address"].lower()
        if target in addr_text:
            logger.info(f"OS UPRN MATCH: '{house_identifier}' matched '{item['address']}' -> {item['uprn']}")
            return item["uprn"]
            
    return None

# --- STANDARD API HANDLER ---
def get_standard_api_bins(base_url, uprn):
    """
    Queries an API that conforms to the UK Waste Service Standards.
    See: https://communitiesuk.github.io/waste-service-standards/apis/waste_services.html
    """
    bins = []
    
    # Clean URL
    base_url = base_url.strip().rstrip("/")
    if not base_url.startswith("http"):
        base_url = "https://" + base_url
    
    # 1. Get Services
    services_url = f"{base_url}/services"
    params = {"uprn": uprn}
    
    logger.info(f"STANDARD API: Fetching services from {services_url} for UPRN {uprn}")
    
    try:
        resp = requests.get(services_url, params=params, timeout=15)
        if resp.status_code == 404:
             raise Exception(f"Endpoint not found: {services_url}")
        resp.raise_for_status()
        
        try:
            services_data = resp.json()
        except:
             raise Exception("API returned non-JSON response")

        # 2. Iterate through services
        for service in services_data:
            next_colls = service.get("next_collections", [])
            
            if not next_colls:
                service_id_url = service.get("@id") or service.get("id")
                
                # Construct detail URL
                if str(service_id_url).startswith("http"):
                    detail_url = service_id_url
                else:
                    if "/" in str(service_id_url):
                         detail_url = str(service_id_url)
                         if not detail_url.startswith("http"):
                             if detail_url.startswith("/"):
                                 detail_url = f"{base_url}{detail_url}"
                             else:
                                 detail_url = f"{base_url}/{detail_url}"
                    else:
                        detail_url = f"{base_url}/services/{service_id_url}"
                
                logger.info(f"STANDARD API: Fetching details from {detail_url}")
                try:
                    detail_resp = requests.get(detail_url, params={"uprn": uprn}, timeout=10)
                    if detail_resp.status_code == 200:
                        service = detail_resp.json()
                        next_colls = service.get("next_collections", [])
                except Exception as e:
                    logger.warning(f"Failed to fetch details for service {service.get('name')}: {e}")
            
            # 3. Process Collections
            bin_type = service.get("name", "Unknown Bin")
            
            for coll in next_colls:
                date_str = coll.get("start_date")
                if date_str:
                    try:
                        if "T" in date_str:
                            date_only = date_str.split("T")[0]
                            y, m, d = date_only.split("-")
                            formatted_date = f"{d}/{m}/{y}"
                        else:
                            formatted_date = date_str
                            
                        bins.append({
                            "bin": bin_type,
                            "date": formatted_date
                        })
                    except Exception as e:
                        logger.warning(f"STANDARD API: Date parse error for {date_str}: {e}")

        return {"bins": bins}

    except Exception as e:
        logger.error(f"STANDARD API ERROR: {e}")
        raise HTTPException(status_code=500, detail=f"Standard API connection failed: {str(e)}")


@app.get("/")
def home():
    return {"status": "OK", "message": "Bin API is running (v3.8 - OS API Integration)."}

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

@app.post("/get_addresses")
def get_addresses(req: AddressRequest):
    """
    Returns a list of addresses and their UPRNs for a given postcode.
    Used to populate dropdowns in UI.
    Supports OS Places API if key is provided, otherwise falls back to public scraper.
    """
    postcode = req.postcode
    os_key = req.os_api_key
    
    if os_key and len(os_key) > 5:
        # User provided an OS API Key - Use official source
        addresses = fetch_os_places_addresses(postcode, os_key)
    else:
        # No Key - Use Public Scraper
        addresses = fetch_public_addresses(postcode)
    
    if addresses:
        # Sort officially or alphabetically for better UI
        # OS API often returns mixed case, let's normalize if needed, but keeping raw is usually safer
        return addresses
    else:
        return [{"uprn": "error", "address": f"No addresses found for {postcode}. Please check format."}]

@app.post("/get_bins")
def get_bins(req: BinRequest):
    if not collect_data_path:
        raise HTTPException(status_code=500, detail="Server misconfigured: collect_data.py not found.")

    try:
        module_name = req.module.replace(" ", "")
        input_data = req.address_data.strip()
        os_key = req.os_api_key
        
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
        detected_url = None
        extracted_uprn = None
        extracted_postcode = ""
        remaining_text = input_data
        
        # 1. Extract URL if present
        url_match = re.search(r'https?://[^\s]+', input_data)
        if url_match:
             detected_url = url_match.group(0)
             remaining_text = input_data.replace(detected_url, "").strip()
        elif input_data.lower().startswith("http"):
             detected_url = input_data.split(" ")[0]
             remaining_text = input_data.replace(detected_url, "").strip()

        # 2. Extract Postcode
        pc_pattern = r'([Gg][Ii][Rr] 0[Aa]{2})|((([A-Za-z][0-9]{1,2})|(([A-Za-z][A-Ha-hJ-Yj-y][0-9]{1,2})|(([A-Za-z][0-9][A-Za-z])|([A-Za-z][A-Ha-hJ-Yj-y][0-9][A-Za-z]?))))\s?[0-9][A-Za-z]{2})'
        pc_match = re.search(pc_pattern, remaining_text)
        if pc_match:
            extracted_postcode = pc_match.group(0).upper()
            remaining_text = remaining_text.replace(extracted_postcode, "").strip()

        # 3. Detect UPRN (Standalone 8-12 digits)
        uprn_match = re.search(r'\b\d{8,12}\b', remaining_text)
        if uprn_match:
            extracted_uprn = uprn_match.group(0)

        # --- BRANCH: STANDARD API ---
        if module_name.lower() == "standard_waste_api":
            if not detected_url or not extracted_uprn:
                 raise HTTPException(status_code=400, detail="Standard API requires both a URL and a UPRN in the input.")
            
            logger.info("Executing Native Standard API Handler")
            json_data = get_standard_api_bins(detected_url, extracted_uprn)
            
            BIN_CACHE[cache_key] = {"timestamp": time.time(), "data": json_data}
            return json_data

        # --- CONFIGURATION OVERRIDES ---
        skip_url_fetch = False
        
        # Special Handling for Wiltshire Council
        if module_name.lower() == "wiltshirecouncil":
            # Wiltshire often needs -s flag and specific Azure URL
            skip_url_fetch = True
            if not detected_url:
                detected_url = "https://ilambassadorformsprod.azurewebsites.net/wastecollectiondays/index"
                logger.info("Wiltshire: Using default Azure URL")

        # --- BRANCH: SUBPROCESS (SCRAPER) ---
        if detected_url:
            logger.info(f"DETECTED MODE: URL")
            cmd.append(detected_url)
        else:
            cmd.append("https://example.com") 
        
        # Apply Skip Flag if needed
        if skip_url_fetch:
            cmd.append("-s")

        if extracted_uprn:
            # Case A: User provided UPRN (explicitly or via dropdown selection)
            logger.info(f"DETECTED MODE: UPRN (Explicit: {extracted_uprn})")
            cmd.append("-u")
            cmd.append(extracted_uprn)
            
            if extracted_postcode:
                cmd.append("-p")
                cmd.append(extracted_postcode)
            else:
                # If Wiltshire, we MUST have postcode.
                if module_name.lower() == "wiltshirecouncil":
                     raise HTTPException(status_code=400, detail="Wiltshire Council requires both UPRN and Postcode.")
                cmd.append("-p")
                cmd.append("BA14 8JN") # Dummy postcode
                used_dummy_postcode = True
        
        else:
            # Case B: Postcode Search (Address Name/Number provided)
            logger.info(f"DETECTED MODE: POSTCODE SEARCH")
            
            if extracted_postcode:
                house_identifier = remaining_text.strip(",. ")
                
                # --- AUTO-LOOKUP ATTEMPT ---
                found_uprn = None
                if house_identifier:
                    # Priority: Use OS API if key is available
                    if os_key and len(os_key) > 5:
                        logger.info("Using OS Places API for internal lookup")
                        found_uprn = lookup_uprn_os(extracted_postcode, house_identifier, os_key)
                    else:
                        logger.info("Using Public Scraper for internal lookup")
                        found_uprn = lookup_uprn_public(extracted_postcode, house_identifier)
                
                if found_uprn:
                     logger.info(f"SWITCHING TO UPRN MODE via Auto-Lookup: {found_uprn}")
                     cmd.append("-u")
                     cmd.append(found_uprn)
                     cmd.append("-p")
                     cmd.append(extracted_postcode)
                else:
                    # Fallback to standard scraper logic if auto-lookup fails
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
