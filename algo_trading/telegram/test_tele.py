import requests

TOKEN = "-----" # API Token Key
CHAT_ID = "----" # CHAT_ID provided by Telegram

def test_connection():
    print("Attempting to send message to Ivan AlgoBot...")
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    
    payload = {
        "chat_id": CHAT_ID,
        "text": "✅ *Connection Successful!*\nIvan AlgoBot is now linked to your Python environment and ready for market execution.",
        "parse_mode": "Markdown"
    }
    
    try:
        response = requests.post(url, json=payload)
        
        # Check if Telegram accepted the message
        if response.status_code == 200:
            print("Success! Check your Telegram app on your phone.")
        else:
            print(f"Failed. Telegram servers said: {response.text}")
            
    except Exception as e:
        print(f"Network error: {e}")

if __name__ == "__main__":
    test_connection()