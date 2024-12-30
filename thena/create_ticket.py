import os
import requests

async def thena(username, query, summary, urgency, channel, ts):

    message_link = f"https://slack.com/archives/{channel}/{ts.replace('.', '')}"
    url = "https://bolt.thena.ai/rest/v2/requests"
    if urgency.lower() == "low":
        severity = "🟢 Low"
    elif urgency.lower() == "medium":
        severity = "🟠 Medium"
    elif urgency.lower() == "high":
        severity = "🔴 High"

    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "Authorization": f"Bearer {os.getenv('THENA_AUTH_TOKEN')}"
    }

    payload = {
        "request": {
            "status": "OPEN",
            "properties": {
                "system": {
                    "title": summary,
                    "description": f"{severity}-urgency request from {username}:\n\n👨‍💻💬 _{query.strip()}_\n\n🔗 Link to Slack thread: {message_link}",
                    "sentiment": "Neutral", 
                    "urgency": urgency
                }},
            "assignment": {
                "to_user_id": "6740cc1209c61cc23e36595f"
            },
            "created_for": {
                "user_email": "customer@fordefi.com"
            },
            "private": False
        }
    }

    response = requests.post(url, headers=headers, json=payload)
    print(f"Thena API response status: {response.status_code}")
    print(f"Thena API response content: {response.text}") 
    return response
