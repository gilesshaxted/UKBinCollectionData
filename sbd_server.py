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
    if collect_data is None:
         return {"error": f"Server Misconfigured: {import_error}"}
    
    try:
        # Locate the councils package directory dynamically
        # This assumes uk_bin_collection.councils is a package (has __init__.py)
        # If not, we might need to look in the folder structure manually
        import uk_bin_collection.councils
        package_path = os.path.dirname(uk_bin_collection.councils.__file__)
        
        councils = []
        for file in os.listdir(package_path):
            if file.endswith(".py") and not file.startswith("__"):
                councils.append(file[:-3]) # remove .py extension
        councils.sort()
        return {"councils": councils}
    except Exception as e:
        # Fallback: Try to find the path relative to current working directory
        try:
            possible_path = os.path.join(os.getcwd(), "uk_bin_collection", "councils")
            if os.path.exists(possible_path):
                councils = [f[:-3] for f in os.listdir(possible_path) if f.endswith(".py") and not f.startswith("__")]
                councils.sort()
                return {"councils": councils}
        except:
            pass
        return {"error": f"Could not list councils: {str(e)}"}

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
