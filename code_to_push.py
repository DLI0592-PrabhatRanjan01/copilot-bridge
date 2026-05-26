# Sample code - replace this with the code you want to test
# This file will be pushed to GitHub by the COPO system

print("Hello from Copilot Bridge!")
print("If you can see this output, the bridge is working.")

# Example: test something that requires internet
import urllib.request
try:
    response = urllib.request.urlopen("https://httpbin.org/get", timeout=5)
    print(f"HTTP Status: {response.status}")
    print("Internet access: OK")
except Exception as e:
    print(f"Error: {e}")
