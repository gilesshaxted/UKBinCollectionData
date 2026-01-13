import re
from datetime import datetime
from bs4 import BeautifulSoup
import requests

from uk_bin_collection.uk_bin_collection.common import *
from uk_bin_collection.uk_bin_collection.get_bin_data import AbstractGetBinDataClass


class CouncilClass(AbstractGetBinDataClass):
    """
    Concrete classes have to implement all abstract operations of the
    base class. They can also override some operations with a default
    implementation.
    """

    def parse_data(self, page: str, **kwargs) -> dict:
        """
        Extract upcoming bin collection dates and their types for the supplied postcode or UPRN.
        
        Queries the council waste collection calendar for a rolling 12-month period, parses the HTML response, and returns a dictionary with a "bins" list containing collection entries.
        
        Parameters:
            page (str): Unused parameter retained for interface compatibility.
            postcode (str, optional): Provided via kwargs["postcode"]; the postcode to query.
            uprn (str|int, optional): Provided via kwargs["uprn"]; will be converted to a 12-character zero-padded string.
        
        Returns:
            dict: A dictionary with key "bins" mapping to a list of dictionaries. Each entry contains:
                - "type": the collection type as a string.
                - "collectionDate": the collection date as a string formatted according to the module's `date_format`.
        
        Raises:
            SystemError: If an HTTP request to the council calendar endpoint does not return status code 200.
        """
        requests.packages.urllib3.disable_warnings()
        
        # Define 12 months to get from the calendar (Rolling 12 Months)
        months_to_fetch = []
        current_date = datetime.now()
        start_month = current_date.month
        start_year = current_date.year

        for i in range(12):
            # Calculate month and year for this iteration
            # (month - 1) + i gives a 0-indexed offset.
            # % 12 gives 0-11 range. + 1 converts back to 1-12.
            m = (start_month - 1 + i) % 12 + 1
            # Calculate year increment. 
            # If start_month + i > 12, we are in next year(s)
            y = start_year + ((start_month - 1 + i) // 12)
            months_to_fetch.append((m, y))

        # Get and check the postcode and UPRN values
        user_postcode = kwargs.get("postcode")
        check_postcode(user_postcode)
        user_uprn = kwargs.get("uprn")
        check_uprn(user_uprn)
        user_uprn = str(user_uprn).zfill(12)

        # Some data for the request
        cookies = {
            "ARRAffinity": "c5a9db7fe43cef907f06528c3d34a997365656f757206fbdf34193e2c3b6f737",
            "ARRAffinitySameSite": "c5a9db7fe43cef907f06528c3d34a997365656f757206fbdf34193e2c3b6f737",
        }
        headers = {
            "Accept": "*/*",
            "Accept-Language": "en-GB,en;q=0.9",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            # 'Cookie': 'ARRAffinity=c5a9db7fe43cef907f06528c3d34a997365656f757206fbdf34193e2c3b6f737; ARRAffinitySameSite=c5a9db7fe43cef907f06528c3d34a997365656f757206fbdf34193e2c3b6f737',
            "Origin": "https://ilambassadorformsprod.azurewebsites.net",
            "Pragma": "no-cache",
            "Referer": "https://ilambassadorformsprod.azurewebsites.net/wastecollectiondays/index",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/112.0.0.0 Safari/537.36 OPR/98.0.0.0",
            "X-Requested-With": "XMLHttpRequest",
            "sec-ch-ua": '"Chromium";v="112", "Not_A Brand";v="24", "Opera GX";v="98"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        }

        data_bins = {"bins": []}

        # For each of the months we defined
        for cal_month, cal_year in months_to_fetch:
            
            # Data for the calendar
            data = {
                "Month": cal_month,
                "Year": cal_year,
                "Postcode": user_postcode,
                "Uprn": user_uprn,
            }

            # Send it all as a POST
            try:
                response = requests.post(
                    "https://ilambassadorformsprod.azurewebsites.net/wastecollectiondays/wastecollectioncalendar",
                    cookies=cookies,
                    headers=headers,
                    data=data,
                    timeout=10 # Added timeout for safety
                )
            except Exception as e:
                 # If one month fails, log it but try to continue or re-raise
                 # For now, we'll re-raise to signal failure
                 raise SystemError(f"Connection failed for {cal_month}/{cal_year}: {e}")

            # If we don't get a HTTP200, throw an error
            if response.status_code != 200:
                raise SystemError(
                    f"Error retrieving data for {cal_month}/{cal_year}! Status: {response.status_code}"
                )

            soup = BeautifulSoup(response.text, features="html.parser")
            soup.prettify()
            # Find all the bits of the current calendar that contain an event
            resultscontainer = soup.find_all("div", {"class": "cal-inner"})

            for result in resultscontainer:
                event = result.find("div", {"class": "events-list"})
                if event:
                    try:
                        date_span = result.find("span", class_="day-no")
                        if not date_span or "data-cal-date" not in date_span.attrs:
                            continue

                        collectiondate = datetime.strptime(
                            date_span["data-cal-date"],
                            "%Y-%m-%dT%H:%M:%S",
                        ).strftime(date_format)
                        
                        collection_type_element = result.select_one(".rc-event-container span")
                        if not collection_type_element:
                            continue
                            
                        collection_type = collection_type_element.text.strip()

                        collection_types = collection_type.split(" and ")

                        for type in collection_types:
                            dict_data = {
                                "type": type,
                                "collectionDate": collectiondate,
                            }
                            # Check for duplicates before adding (just in case overlapping requests occur)
                            if dict_data not in data_bins["bins"]:
                                data_bins["bins"].append(dict_data)
                                
                    except Exception as e:
                        # Skip this specific entry if parsing fails, but continue with others
                        continue

        return data_bins
