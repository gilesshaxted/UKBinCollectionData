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

# Point to current directory
sys.path.append(os.getcwd()) 

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
            "files_in_folder": file_structure
        }
    return {"status": "OK", "message": "Bin API is running and Library is loaded."}

@app.get("/get_councils")
def get_councils():
    # Attempt to list councils from the file system even if the library failed to import
    councils = []
    errors = []
    
    # Strategy 1: File System Walk (Most Robust)
    try:
        # Check probable paths based on common repo structures
        current_dir = os.getcwd()
        search_paths = [
            os.path.join(current_dir, "uk_bin_collection", "uk_bin_collection", "councils"),
            os.path.join(current_dir, "uk_bin_collection", "councils"),
            os.path.join(current_dir, "councils")
        ]
        
        found_path = None
        for path in search_paths:
            if os.path.exists(path) and os.path.isdir(path):
                found_path = path
                break
        
        if found_path:
            for file in os.listdir(found_path):
                if file.endswith(".py") and not file.startswith("__"):
                    councils.append(file[:-3]) # remove .py extension
            
            if councils:
                councils.sort()
                return {"councils": councils}
            else:
                errors.append(f"Found folder {found_path} but it was empty.")
        else:
             errors.append("Could not locate 'councils' folder on disk.")

    except Exception as e:
        errors.append(f"File system error: {str(e)}")

    # Strategy 2: Python Import (Fallback if Strategy 1 failed but library is loaded)
    if collect_data is not None:
        try:
            import uk_bin_collection.councils
            package_path = os.path.dirname(uk_bin_collection.councils.__file__)
            for file in os.listdir(package_path):
                if file.endswith(".py") and not file.startswith("__"):
                    councils.append(file[:-3])
            councils.sort()
            return {"councils": councils}
        except Exception as e:
            errors.append(f"Import strategy error: {str(e)}")
            
    # If we got here, both failed
    return {
        "error": "Could not list councils.", 
        "details": errors, 
        "server_import_status": import_error
    }

@app.get("/get_addresses")
def get_addresses(postcode: str, module: str):
    if collect_data is None:
         return {"error": f"Server Misconfigured: {import_error}"}

    try:
        # Check if the specific council module exists before running
        module_path = f"uk_bin_collection.councils.{module}"
        try:
            importlib.import_module(module_path)
        except ImportError:
            return {"error": f"Council '{module}' not found. Check spelling (Case Sensitive!)."}

        return [{"uprn": postcode, "address": f"Address lookup for {postcode}"}]
    except Exception as e:
        return {"error": str(e)}

@app.post("/get_bins")
def get_bins(req: BinRequest):
    if collect_data is None:
        raise HTTPException(status_code=500, detail=f"Server Misconfigured: {import_error}")

    try:
        json_output = collect_data.run_council_scraper(req.module, req.address_data)
        if isinstance(json_output, str):
            return json.loads(json_output)
        return json_output
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
