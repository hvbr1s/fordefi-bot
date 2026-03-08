from datetime import datetime, timedelta

LOOKBACK_DAYS = 2
DATADOG_REQUEST_ID_URL = "https://app.datadoghq.com/logs?query=%40http.request.xrequestid%3A{id}&agg_m=count&agg_m_source=base&agg_t=count&clustering_pattern_field_path=message&cols=host%2Cservice&messageDisplay=inline&refresh_mode=sliding&storage=hot&stream_sort=desc&viz=stream&from_ts={from_ts}&to_ts={to_ts}&live=false"
DATADOG_TRANSACTION_ID_URL = "https://app.datadoghq.com/logs?query=%40transaction_id%3A{id}&agg_m=count&agg_m_source=base&agg_t=count&clustering_pattern_field_path=message&cols=host%2Cservice&messageDisplay=inline&refresh_mode=sliding&storage=hot&stream_sort=desc&viz=stream&from_ts={from_ts}&to_ts={to_ts}&live=false"

async def enrich_bot_post(username, query, channel, ts, slack_client, transaction_ids=None, request_ids=None, organization_id=None, organization_name=None):
    processed_username = username.split('@')[0].strip()

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
        org_display = f"*{organization_name}* ({organization_id})" if organization_name else organization_id
        post += f"🏢 Organization: \n {org_display}\n"

    if transaction_ids or request_ids:
        now = datetime.now()
        to_ts = int(now.timestamp() * 1000)
        from_ts = int((now - timedelta(days=LOOKBACK_DAYS)).timestamp() * 1000)

        for tx_id in (transaction_ids or []):
            dd_tx_link = DATADOG_TRANSACTION_ID_URL.format(id=tx_id, from_ts=from_ts, to_ts=to_ts)
            post += f"🐶 DD Logs:\n <{dd_tx_link}|TxID: {tx_id}>\n"
        for req_id in (request_ids or []):
            dd_req_link = DATADOG_REQUEST_ID_URL.format(id=req_id, from_ts=from_ts, to_ts=to_ts)
            post += f"🐶 DD Logs:\n <{dd_req_link}|RequestID: {req_id}>\n"

    return post
