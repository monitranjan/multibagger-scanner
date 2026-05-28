file_path = "/Users/monitranjan/.gemini/antigravity/brain/cc8fa4fb-7043-4eeb-a2ed-d38fdcc7b217/.system_generated/steps/699/content.md"

with open(file_path, "r", encoding="utf-8") as f:
    content = f.read()

# Let's search for "common-stocks" and print the exact parameters passed in the POST payload
pattern = "common-stocks"
idx = content.find(pattern)
if idx != -1:
    print(content[idx - 200:idx + 500])
else:
    print("Not found")
