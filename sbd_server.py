from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uvicorn
import importlib
import json
import os
import sys
import logging

# Set up logging to see errors in Render logs
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sbd_server")

# Point this to where you downloaded UKBinCollectionData
# Ensure 'uk_bin_collection' folder is in the python path
sys.path.append(os.getcwd()) 

# WRAPPED IMPORT: Prevents crash if files are missing/misplaced
try:
    from uk_bin_collection.uk_bin_collection import collect_data
    logger.info("Successfully imported UKBinCollectionData")
except ImportError as e:
    logger.error(f"CRITICAL ERROR: Could not import UKBinCollectionData. Error: {e}")
    collect_data = None

app = FastAPI()

class BinRequest(BaseModel):
    address_data: str
    module: str

@app.get("/")
def home():
    if collect_data is None:
        return {
            "status": "Error",
            "message": "UKBinCollectionData library could not be imported. Check Render logs for 'CRITICAL ERROR'."
        }
    return {"status": "Bin API is running", "chrome_check": "Remember to add the Chrome Buildpack on Render!"}

@app.get("/get_addresses")
def get_addresses(postcode: str, module: str):
    """
    Wraps the 'get_address_data' method of the scraper.
    """
    if collect_data is None:
         return {"error": "Server misconfigured: Library not found."}

    try:
        # Dynamically import the council module
        module_path = f"uk_bin_collection.councils.{module}"
        try:
            council_module = importlib.import_module(module_path)
        except ImportError:
            return {"error": f"Council module '{module}' not found."}
        
        # NOTE: This generic endpoint returns the postcode as the UPRN 
        # because mapping every council's specific address lookup logic 
        # is complex. The user may need to enter UPRN manually if 
        # the simple postcode lookup fails.
        return [{"uprn": postcode, "address": f"Address lookup for {postcode} (Check Scraper Support)"}]

    except Exception as e:
        logger.error(f"Address Error: {e}")
        return {"error": str(e)}

@app.post("/get_bins")
def get_bins(req: BinRequest):
    """
    Runs the scraper to get bin data.
    """
    if collect_data is None:
        raise HTTPException(status_code=500, detail="Server misconfigured: Library not found.")

    try:
        # We run the main collect_data function
        # This will fail if Chrome is not installed on the server
        logger.info(f"Fetching bins for {req.module}...")
        
        json_output = collect_data.run_council_scraper(req.module, req.address_data)
        
        # The library usually returns a JSON structure string or dictionary
        if isinstance(json_output, str):
            return json.loads(json_output)
        return json_output

    except Exception as e:
        logger.error(f"Bin Fetch Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    # CRITICAL FIX FOR RENDER: Use the PORT environment variable
    port = int(os.environ.get("PORT", 10000))
    logger.info(f"Starting server on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
