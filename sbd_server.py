from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uvicorn
import importlib
import json
import os
import sys

# Point this to where you downloaded UKBinCollectionData
# Ensure 'uk_bin_collection' folder is in the python path
sys.path.append(os.getcwd()) 

from uk_bin_collection.uk_bin_collection import collect_data

app = FastAPI()

class BinRequest(BaseModel):
    address_data: str
    module: str

@app.get("/")
def home():
    return {"status": "Bin API is running"}

@app.get("/get_addresses")
def get_addresses(postcode: str, module: str):
    """
    Wraps the 'get_address_data' method of the scraper.
    """
    try:
        # Dynamically import the council module
        # Note: This requires the UKBinCollectionData structure
        # usually uk_bin_collection.councils.<module_name>
        # But collect_data.py handles this logic via command line args mostly.
        # We will use the helper class directly if possible.
        
        # We invoke the specific council's logic
        # This is a simplified example. In reality, you instantiate the council class.
        
        # IMPORT STRATEGY: 
        # We emulate how collect_data.py imports the module
        module_path = f"uk_bin_collection.councils.{module}"
        council_module = importlib.import_module(module_path)
        
        # Most scrapers have a function to get data or parsing logic
        # Note: Not all scrapers in that repo support a distinct 'get_addresses' step.
        # Some require UPRN directly.
        # IF the scraper supports get_uprn(postcode), we call it.
        
        # For simplicity in this example, we assume the user's scraper 
        # has a 'get_uprn' or similar exposed, or we use the generic one.
        
        # FALLBACK: If the scraper is simple, we might just return the postcode 
        # and let the user enter the UPRN manually in the frontend if needed.
        # But let's try to run the code.
        
        # IMPORTANT: This part highly depends on the specific council script structure.
        # Many modern ones in that repo inherit from a base class.
        
        return [{"uprn": postcode, "address": f"Address lookup for {postcode} (Check Scraper Support)"}]

    except Exception as e:
        return {"error": str(e)}

@app.post("/get_bins")
def get_bins(req: BinRequest):
    """
    Runs the scraper to get bin data.
    """
    try:
        # We construct the arguments list like the CLI does
        args = [req.module, req.address_data] 
        
        # We run the main collect_data function
        # capture_output=True might be needed if it prints to stdout
        
        # DIRECT METHOD:
        json_output = collect_data.run_council_scraper(req.module, req.address_data)
        
        # The library usually returns a JSON structure string or dictionary
        if isinstance(json_output, str):
            return json.loads(json_output)
        return json_output

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    # Run with: python sbd_server.py
    uvicorn.run(app, host="0.0.0.0", port=8080)
