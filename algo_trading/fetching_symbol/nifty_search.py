import pyotp
import json
from growwapi import GrowwAPI

GROWW_API_KEY = "----"       # Groww API TOTP Token
GROWW_TOTP_SECRET = "----"   # Groww 32_STRING Access Token
def find_exact_symbol():
    print("Authenticating...")
    totp = pyotp.TOTP(GROWW_TOTP_SECRET).now()
    access_token = GrowwAPI.get_access_token(api_key=GROWW_API_KEY, totp=totp)
    groww = GrowwAPI(access_token)
    
    try:
        print("\n1. Fetching Expiry Dates for NIFTY in July 2026...")
        # Get the official expiry date strings Groww expects
        expiries = groww.get_expiries(
            exchange=groww.EXCHANGE_NSE,    # For Nifty EXCHANGE_BSE
            underlying_symbol="NIFTY",      # For SENSEX,BANK NIFTY,...All Indices
            year=2026,
            month=7
        )
        print(json.dumps(expiries, indent=2))
        
        # If we successfully found expiries, fetch the contract names for the first one
        if expiries and "expiries" in expiries and len(expiries["expiries"]) > 0:
            target_expiry = expiries["expiries"][0] 
            print(f"\n2. Fetching exact Contracts for Expiry: {target_expiry}...")
            
            contracts = groww.get_contracts(
                exchange=groww.EXCHANGE_NSE,   # For Nifty EXCHANGE_BSE
                underlying_symbol="NIFTY",     # For SENSEX,BANK NIFTY,...All Indices
                expiry_date=target_expiry
            )
            print(json.dumps(contracts, indent=2))
            
    except Exception as e:
        print(f"Failed: {e}")

if __name__ == "__main__":
    find_exact_symbol()