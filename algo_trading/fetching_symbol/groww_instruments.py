from growwapi import GrowwAPI
GROWW_TOTP_SECRET = "----"   # 32_String Access Token
groww = GrowwAPI(GROWW_TOTP_SECRET)
print(dir(groww))
import inspect
print(inspect.signature(GrowwAPI.place_order))