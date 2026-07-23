import pyotp
import json
from growwapi import GrowwAPI

GROWW_API_KEY = "----"       # Groww API TOTP Token
GROWW_TOTP_SECRET = "----"   # Groww 32_STRING Access Token

totp = pyotp.TOTP(GROWW_TOTP_SECRET).now()

access_token = GrowwAPI.get_access_token(
    api_key=GROWW_API_KEY,
    totp=totp
)

groww = GrowwAPI(access_token)

expiries = groww.get_expiries(
    exchange=groww.EXCHANGE_BSE,  # For Nifty EXCHANGE_NSE
    underlying_symbol="SENSEX",   # For NIFTY,BANK NIFTY,...All Indices
    year=2026,                    # Format : yyyy
    month=7                       # Format : Month
)

expiry = expiries["expiries"][0]

option_chain = groww.get_option_chain(
    exchange=groww.EXCHANGE_BSE,   # For Nifty EXCHANGE_NSE
    underlying="SENSEX",           # For NIFTY,BANK NIFTY,...All Indices
    expiry_date=expiry
)

print("TYPE:", type(option_chain))
print("KEYS:", option_chain.keys() if isinstance(option_chain, dict) else "NOT_DICT")

print(json.dumps(option_chain, indent=2))