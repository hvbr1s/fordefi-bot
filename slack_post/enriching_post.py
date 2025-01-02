async def enrich_bot_post(username, query, summary, urgency, channel, ts, slack_client, current_day):

    thena_id_dan = "6740cc1209c61cc23e36595f"
    thena_id_dima = "63d61901d768b1397a450109"
    default_assignee = thena_id_dima if current_day in [5,6] else thena_id_dan
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



    return "hello!"