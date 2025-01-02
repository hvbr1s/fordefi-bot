import os
import requests

async def thena(username, query, summary, urgency, channel, ts, slack_client, current_day):

    dan = "6740cc1209c61cc23e36595f"
    dima = "63d61901d768b1397a450109"
    default_assignee = dima if current_day in [5,6] else dan
    print(f"Assigning the ticket to {default_assignee}")

    processed_username = username.split('@')[0].strip()
    thena_api_friendly_username = processed_username.replace(" ", "")
    #telegram_usertag =  username.split('@')[1].strip()

    # Get slack channel name
    response = slack_client.conversations_info(channel=channel)
    channel_info = response["channel"]
    channel_name = channel_info["name"]
    channel_parts = channel_name.split('-')
    thena_api_friendly_channel_name = '-'.join(channel_parts[1:]) if len(channel_parts) > 1 else channel_name

    message_link = f"https://arnac.slack.com/archives/{channel}/{ts.replace('.', '')}"
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
                    "title": f"{severity}-urgency request from {processed_username} ({thena_api_friendly_channel_name.title()}): {summary}",
                    "description": f"👨‍💻💬 _{query.strip()}_\n\n🔗 Link to Slack thread: {message_link}",
                    "sentiment": "Neutral", 
                    "urgency": urgency
                }},
            "assignment": {
                "to_user_id": default_assignee
            },
            "created_for": {
                "user_email": f"{thena_api_friendly_username}@{thena_api_friendly_channel_name}.com"
            },
            "private": False
        }
    }

    response = requests.post(url, headers=headers, json=payload)
    print(f"Thena API response status: {response.status_code}")
    print(f"Thena API response content: {response.text}") 
    return response
