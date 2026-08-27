import re
def generate_smart_title_from_transcript(transcript: str) -> str:
    if not transcript:
        return ""
    # Remove multiple spaces and newlines
    text = re.sub(r'\s+', ' ', transcript).strip()
    # Take first ~8 words
    words = text.split()
    if len(words) > 10:
        words = words[:10]
    
    title = " ".join(words).title()
    # Clean up weird punctuation at the end
    title = re.sub(r'[^A-Za-z0-9]+$', '', title)
    return title

print(generate_smart_title_from_transcript("sidemen does what is the question like smothing new and smart and this is extra text that we dont want"))
