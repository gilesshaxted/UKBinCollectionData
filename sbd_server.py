from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uvicorn
import importlib
import json
import os
import sys
import logging

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sbd_server")

# --- PATH FIXER ---
# The UKBinCollectionData repo has nested folders (uk_bin_collection/uk_bin_collection/councils).
# We need to make sure the *correct* parent is in sys.path so the internal imports work.
current_dir = os.getcwd()
logger.info(f"Current Working Directory: {current_dir}")

# We look for the folder that contains 'councils'
found_councils = False
for root, dirs, files in os.walk(current_dir):
    if "councils" in dirs:
        # We found where councils lives. 
        # The library expects 'uk_bin_collection.councils', so we need the parent of 'uk_bin_collection' in path.
        # Check if we are inside the inner uk_bin_collection
        if os.path.basename(root) == "uk_bin_collection":
            parent_of_package = os.path.dirname(root)
            if parent_of_package not in sys.path:
                sys.path.insert(0, parent_of_package)
                logger.info(f"Added {parent_of_package} to sys.path to support imports.")
            found_councils = True
            break

if not found_councils:
    logger.warning("Could not automatically locate 'councils' package. Imports might fail.")
    # Fallback: Add current dir
    sys.path.append(current_dir)

# GLOBAL VARS TO STORE STATE
collect_data = None
import_error = None
file_structure = []

# Try to list files for debugging
try:
    file_structure = os.listdir(os.getcwd())
except Exception as e:
    file_structure = [f"Error listing files: {str(e)}"]

# ATTEMPT IMPORT
try:
    # We try to import the main runner. 
    # Because of the path fix above, this should work standardly now.
    from uk_bin_collection.uk_bin_collection import collect_data
    logger.info("Successfully imported UKBinCollectionData")
except ImportError as e:
    import_error = f"ImportError: {str(e)}"
    logger.error(f"CRITICAL: {import_error}")
except Exception as e:
    import_error = f"GeneralError: {str(e)}"
    logger.error(f"CRITICAL: {import_error}")

app = FastAPI()

class BinRequest(BaseModel):
    address_data: str
    module: str

@app.get("/")
def home():
    # If the library failed to load, return the EXACT error to the user
    if collect_data is None:
        return {
            "status": "Error",
            "message": "Library failed to load.",
            "debug_error": import_error,
            "current_folder": os.getcwd(),
            "files_in_folder": file_structure,
            "sys_path": sys.path
        }
    return {"status": "OK", "message": "Bin API is running and Library is loaded."}

@app.get("/get_councils")
def get_councils():
    councils = []
    errors = []
    
    try:
        # Since we fixed the path, we can try to find the module path via python
        import uk_bin_collection.councils
        package_path = os.path.dirname(uk_bin_collection.councils.__file__)
        
        for file in os.listdir(package_path):
            if file.endswith(".py") and not file.startswith("__"):
                councils.append(file[:-3])
        
        councils.sort()
        return {"councils": councils}

    except Exception as e:
        errors.append(f"Standard import listing failed: {str(e)}")
        
        # Fallback: Manual File Walk
        try:
            for root, dirs, files in os.walk(os.getcwd()):
                if "councils" in dirs:
                    councils_path = os.path.join(root, "councils")
                    for file in os.listdir(councils_path):
                         if file.endswith(".py") and not file.startswith("__"):
                            councils.append(file[:-3])
                    councils.sort()
                    return {"councils": councils, "note": "Loaded via fallback file walk"}
        except Exception as walk_e:
            errors.append(f"Fallback walk failed: {str(walk_e)}")

    return {
        "error": "Could not list councils.", 
        "details": errors, 
        "server_import_status": import_error
    }

@app.get("/get_addresses")
def get_addresses(postcode: str, module: str):
    if collect_data is None:
         return {"error": f"Server Misconfigured: {import_error}"}

    # Verify the module is importable before pretending we got an address
    # This acts as a sanity check for the user's input
    try:
        # The library uses this import path format:
        module_path = f"uk_bin_collection.councils.{module}"
        importlib.import_module(module_path)
    except ImportError as e:
        logger.error(f"Failed to import {module}: {e}")
        return {"error": f"Council '{module}' not found in 'uk_bin_collection.councils'. Check spelling (Case Sensitive!). Error: {str(e)}"}

    # NOTE: Most scrapers in this library do NOT have a standalone 'get_addresses' function.
    # They require the user to find the UPRN manually or the scraper does it internally during execution.
    # We return a dummy list to allow the UI to proceed to the 'get_bins' step, 
    # where the scraper logic will run fully.
    return [{"uprn": postcode, "address": f"Address lookup for {postcode} (Select to continue)"}]

@app.post("/get_bins")
def get_bins(req: BinRequest):
    if collect_data is None:
        raise HTTPException(status_code=500, detail=f"Server Misconfigured: {import_error}")

    try:
        logger.info(f"Running scraper for {req.module} with input {req.address_data}")
        json_output = collect_data.run_council_scraper(req.module, req.address_data)
        if isinstance(json_output, str):
            return json.loads(json_output)
        return json_output
    except Exception as e:
        logger.error(f"Scraper Error: {e}")
        # Return error as JSON so WP can display it
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
