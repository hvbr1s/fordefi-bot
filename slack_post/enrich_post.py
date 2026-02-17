DATADOG_REQUEST_ID_URL = "https://app.datadoghq.com/logs?query=%40http.request.xrequestid%3A{id}&agg_m=count&agg_m_source=base&agg_t=count&clustering_pattern_field_path=message&cols=host%2Cservice&messageDisplay=inline&refresh_mode=sliding&storage=hot&stream_sort=desc&viz=stream&live=true"
DATADOG_TRANSACTION_ID_URL = "https://app.datadoghq.com/logs?query=%40transaction_id%3A{id}&agg_m=count&agg_m_source=base&agg_t=count&clustering_pattern_field_path=message&cols=host%2Cservice&messageDisplay=inline&refresh_mode=sliding&storage=hot&stream_sort=desc&viz=stream&live=true"

async def enrich_bot_post(username, query, channel, ts, slack_client, current_day, transaction_ids=None, request_ids=None, organization_id=None):
    # dan = "<@U082GSCDFG9>"
    # dima = "<@U02PP7JRTFS>"
    # default_assignee = dima if current_day in [5,6] else dan
    # print(f"Assigning the ticket to {default_assignee}")
    processed_username = username.split('@')[0].strip()

    # Get slack channel name
    response = slack_client.conversations_info(channel=channel)
    channel_info = response["channel"]
    channel_name = channel_info["name"]
    channel_parts = channel_name.split('-')
    slack_friendly_channel_name = '-'.join(channel_parts[1:]) if len(channel_parts) > 1 else channel_name

    message_link = f"https://arnac.slack.com/archives/{channel}/p{ts.replace('.', '')}"
    post = f"""
👨‍💻💬 *{processed_username.title()}* *({slack_friendly_channel_name.title()})*: _{query.strip().replace('\n', ' ')}_\n
🔗 Link to Slack thread: {message_link}\n
"""

    if organization_id:
        post += f"🏢 Organization ID: {organization_id}\n"

    for tx_id in (transaction_ids or []):
        dd_tx_link = DATADOG_TRANSACTION_ID_URL.format(id=tx_id)
        post += f"🐶 DD Logs:\n <{dd_tx_link}|TxID: {tx_id}>\n"
    for req_id in (request_ids or []):
        dd_req_link = DATADOG_REQUEST_ID_URL.format(id=req_id)
        post += f"🐶 DD Logs:\n <{dd_req_link}|RequestID: {req_id}>\n"

    return post

## ThenaCS user ID <@U04LKS6KL7R>
