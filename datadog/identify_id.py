import os
import re
from dotenv import load_dotenv
from datetime import datetime, timedelta
from datadog_api_client import ApiClient, Configuration
from datadog_api_client.exceptions import ApiException
from datadog_api_client.v2.api.logs_api import LogsApi
from datadog_api_client.v2.model.logs_list_request import LogsListRequest
from datadog_api_client.v2.model.logs_list_request_page import LogsListRequestPage
from datadog_api_client.v2.model.logs_query_filter import LogsQueryFilter
from datadog_api_client.v2.model.logs_sort import LogsSort

load_dotenv()

LOOKBACK_DAYS = 7


def list_logs_with_retry(logs_api, request):
    try:
        return logs_api.list_logs(body=request)
    except ApiException as e:
        if getattr(e, "status", None) != 408:
            raise
        print(f"  RETRY  Datadog 408 timeout, retrying once")
        return logs_api.list_logs(body=request)

UUID_FULL_PATTERN = re.compile(
    r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
)


def prepare_uuid_query(uuid: str) -> str:
    """If UUID is truncated, strip trailing dots/ellipsis and append wildcard for Datadog search."""
    clean = uuid.rstrip('.').rstrip('…').strip()
    if UUID_FULL_PATTERN.match(clean):
        return clean
    return f"{clean}*"


def create_config():
    config = Configuration()
    config.api_key["apiKeyAuth"] = os.environ.get("DATADOG_API_KEY")
    config.api_key["appKeyAuth"] = os.environ.get("APP_KEY")
    return config


def check_attribute(logs_api, attribute, uuid, start, end):
    query_uuid = prepare_uuid_query(uuid)
    request = LogsListRequest(
        filter=LogsQueryFilter(
            _from=start,
            to=end,
            query=f"{attribute}:{query_uuid}",
        ),
        page=LogsListRequestPage(limit=1),
    )
    response = list_logs_with_retry(logs_api, request)
    return response.data[0] if response.data else None


def extract_full_uuid(log, attribute):
    """Extract the full UUID value from a matched log entry."""
    if not log or not hasattr(log.attributes, "attributes"):
        return None
    attrs = log.attributes.attributes or {}
    # Strip the leading '@' from attribute name for dict lookup
    key = attribute.lstrip('@')
    # Handle nested keys like "http.request.xrequestid"
    parts = key.split('.')
    value = attrs
    for part in parts:
        if isinstance(value, dict):
            value = value.get(part)
        else:
            return None
    return value if isinstance(value, str) else None


def extract_org_id(log):
    if not log or not hasattr(log.attributes, "attributes"):
        return None
    attrs = log.attributes.attributes or {}
    return (
        attrs.get("organization_id")
        or attrs.get("organization", {}).get("id")
    )


def find_first_log(logs_api, query, start, end):
    request = LogsListRequest(
        filter=LogsQueryFilter(_from=start, to=end, query=query),
        sort=LogsSort.TIMESTAMP_ASCENDING,
        page=LogsListRequestPage(limit=1),
    )
    response = list_logs_with_retry(logs_api, request)
    return response.data[0] if response.data else None


def resolve_payload_log_id(logs_api, id_type, resolved_uuid, start, end):
    if id_type in ("request_id", "both"):
        log = find_first_log(
            logs_api,
            f"@http.request.xrequestid:{resolved_uuid} service:bff",
            start,
            end,
        )
        return log.id if log else None
    if id_type == "transaction_id":
        org_log = find_first_log(
            logs_api,
            f"@transaction_id:{resolved_uuid} service:organization",
            start,
            end,
        )
        if not org_log:
            return None
        xreq = extract_full_uuid(org_log, "@http.request.xrequestid")
        if not xreq:
            return None
        bff_log = find_first_log(
            logs_api,
            f"@http.request.xrequestid:{xreq} service:bff",
            start,
            end,
        )
        return bff_log.id if bff_log else None
    return None


def resolve_org_name(logs_api, org_id, start, end):
    request = LogsListRequest(
        filter=LogsQueryFilter(
            _from=start,
            to=end,
            query=f"@organization.id:{org_id} @organization.name:*",
        ),
        page=LogsListRequestPage(limit=1),
    )
    response = list_logs_with_retry(logs_api, request)
    if response.data:
        attrs = response.data[0].attributes.attributes or {}
        return attrs.get("organization", {}).get("name")
    return None


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
    resolved_uuid = None
    org_id = None
    org_name = None
    with ApiClient(config) as client:
        logs_api = LogsApi(client)
        for attr, label in attributes.items():
            log = check_attribute(logs_api, attr, uuid, start, end)
            results[label] = bool(log)
            if log:
                print(f"  MATCH  {attr}")
                if resolved_uuid is None:
                    resolved_uuid = extract_full_uuid(log, attr)
                if org_id is None:
                    org_id = extract_org_id(log)
            else:
                print(f"  MISS   {attr}")

        if org_id:
            org_name = resolve_org_name(logs_api, org_id, start, end)

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

        payload_log_id = None
        if resolved_uuid and id_type != "unknown":
            payload_log_id = resolve_payload_log_id(logs_api, id_type, resolved_uuid, start, end)

    return {
        "id_type": id_type,
        "resolved_uuid": resolved_uuid,
        "organization_id": org_id,
        "organization_name": org_name,
        "payload_log_id": payload_log_id,
    }
