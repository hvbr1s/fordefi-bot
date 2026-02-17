import os
from dotenv import load_dotenv
from datetime import datetime, timedelta
from datadog_api_client import ApiClient, Configuration
from datadog_api_client.v2.api.logs_api import LogsApi
from datadog_api_client.v2.model.logs_list_request import LogsListRequest
from datadog_api_client.v2.model.logs_list_request_page import LogsListRequestPage
from datadog_api_client.v2.model.logs_query_filter import LogsQueryFilter

load_dotenv()

LOOKBACK_DAYS = 7


def create_config():
    config = Configuration()
    config.api_key["apiKeyAuth"] = os.environ.get("DATADOG_API_KEY")
    config.api_key["appKeyAuth"] = os.environ.get("APP_KEY")
    return config


def check_attribute(logs_api, attribute, uuid, start, end):
    request = LogsListRequest(
        filter=LogsQueryFilter(
            _from=start,
            to=end,
            query=f"{attribute}:{uuid}",
        ),
        page=LogsListRequestPage(limit=1),
    )
    response = logs_api.list_logs(body=request)
    return response.data[0] if response.data else None


def extract_org_id(log):
    if not log or not hasattr(log.attributes, "attributes"):
        return None
    attrs = log.attributes.attributes or {}
    return (
        attrs.get("organization_id")
        or attrs.get("organization", {}).get("id")
    )


def identify_uuid(uuid):
    config = create_config()
    end_time = datetime.now()
    start_time = end_time - timedelta(days=LOOKBACK_DAYS)

    start = start_time.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    end = end_time.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    attributes = {
        "@http.request.xrequestid": "request_id",
        "@transaction_id": "transaction_id",
    }

    results = {}
    org_id = None
    with ApiClient(config) as client:
        logs_api = LogsApi(client)
        for attr, label in attributes.items():
            log = check_attribute(logs_api, attr, uuid, start, end)
            results[label] = bool(log)
            if log:
                print(f"  MATCH  {attr}")
                if org_id is None:
                    org_id = extract_org_id(log)
            else:
                print(f"  MISS   {attr}")

    is_request = results["request_id"]
    is_transaction = results["transaction_id"]

    if is_request and is_transaction:
        id_type = "both"
    elif is_request:
        id_type = "request_id"
    elif is_transaction:
        id_type = "transaction_id"
    else:
        id_type = "unknown"

    return {"id_type": id_type, "organization_id": org_id}
