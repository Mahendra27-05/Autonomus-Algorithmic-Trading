from growwapi import GrowwAPI
 
# Groww API Credentials (Replace with your actual credentials)
API_AUTH_TOKEN = "----" # Groww API Key
 
# Initialize Groww API
groww = GrowwAPI(API_AUTH_TOKEN)
 
# Get option chain for NIFTY with specific expiry date
option_chain_response = groww.get_option_chain(
    exchange=groww.EXCHANGE_BSE, # EXCHANGE_BSE
    underlying="SENSEX",         # SENSEX,NIFTY,BANKNIFTY,FINNIFTY,MIDCPNIFTY, all indices
    expiry_date="2026-07-23"     # Format: YYYY-MM-DD
)
print(option_chain_response)