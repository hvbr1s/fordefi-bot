async def enrich_bot_post(username, query, summary, urgency, channel, ts, slack_client, current_day):

    dan = "<@U082GSCDFG9>"
    dima = "<@U02PP7JRTFS>"
    default_assignee = dima if current_day in [5,6] else dan
    print(f"Assigning the ticket to {default_assignee}")

    processed_username = username.split('@')[0].strip()

    # Get slack channel name
    response = slack_client.conversations_info(channel=channel)
    channel_info = response["channel"]
    channel_name = channel_info["name"]
    channel_parts = channel_name.split('-')
    slack_friendly_channel_name = '-'.join(channel_parts[1:]) if len(channel_parts) > 1 else channel_name

    message_link = f"https://arnac.slack.com/archives/{channel}/{ts.replace('.', '')}"
    if urgency.lower() == "low":
        severity = "🟢 Low"
    elif urgency.lower() == "medium":
        severity = "🟠 Medium"
    elif urgency.lower() == "high":
        severity = "🔴 High"

    post = f"""
👨‍💻💬 _{query.strip()}_\n
*{severity}-urgency request from {processed_username} ({slack_friendly_channel_name.title()})*\n\
🔗 Link to Slack thread: {message_link}\n
cc: {default_assignee}
"""
    
    return post

## ThenaCS user ID <@U04LKS6KL7R>