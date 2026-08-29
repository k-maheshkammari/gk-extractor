import os
from dotenv import load_dotenv
from google import genai

load_dotenv()

keys_to_test = {}
for i in range(1, 16):
    k_val = os.getenv(f"GEMINI_API_KEY_{i}")
    if k_val:
        keys_to_test[f"Key {i}"] = k_val

if os.getenv("GEMINI_API_KEY") and os.getenv("GEMINI_API_KEY") not in keys_to_test.values():
    keys_to_test["Default Key"] = os.getenv("GEMINI_API_KEY")

print(f"\n🔍 Testing All {len(keys_to_test)} Gemini API Keys...\n" + "="*50)

for name, key in keys_to_test.items():
    try:
        client = genai.Client(api_key=key)
        res = client.models.generate_content(
            model="gemini-3.6-flash", 
            contents="Say 'OK'"
        )
        print(f"✅ {name} (...{key[-6:]}): ACTIVE & READY")
    except Exception as err:
        err_msg = str(err)
        if "429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg:
            print(f"❌ {name} (...{key[-6:]}): QUOTA EXHAUSTED")
        else:
            print(f"⚠️  {name} (...{key[-6:]}): {err_msg[:60]}...")

print("="*50 + "\n")