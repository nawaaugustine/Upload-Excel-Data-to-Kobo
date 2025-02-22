import json
import uuid
import pandas as pd
import requests
import logging
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from tqdm import tqdm

def configure_logging(log_file='./logs/app.log', level=logging.INFO):
    """
    Configures logging for the application.
    
    Args:
        log_file (str): The path of the log file.
        level (int): The logging level.
    """
    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(levelname)s - %(message)s',
        filename=log_file,
        filemode='w'
    )

def load_config(config_file):
    """
    Loads configuration data from a JSON file.
    
    Args:
        config_file (str): The file path to the configuration JSON.
        
    Returns:
        dict: The loaded configuration data.
    
    Raises:
        Exception: If the file cannot be loaded or parsed.
    """
    try:
        with open(config_file, 'r') as f:
            config_data = json.load(f)
        return config_data
    except Exception as e:
        logging.error(f"Error loading config file {config_file}: {e}")
        raise

def send_request(endpoint, headers, payload):
    """
    Sends a POST request to the specified endpoint with retries.
    
    Args:
        endpoint (str): The API endpoint URL.
        headers (dict): HTTP headers.
        payload (dict): JSON payload to be sent.
        
    Returns:
        requests.Response: The response from the API.
    """
    session = requests.Session()
    retries = Retry(
        total=5,
        backoff_factor=0.1,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=frozenset(['POST'])
    )
    session.mount('https://', HTTPAdapter(max_retries=retries))
    response = session.post(endpoint, headers=headers, json=payload)
    return response

def safe_str(value):
    """
    Safely converts a value to a string, handling None and NaN.
    
    Args:
        value: The value to be converted.
        
    Returns:
        str: The string representation or an empty string if value is NaN.
    """
    if pd.isna(value):
        return ''
    return str(value)

def format_date(value):
    """
    Formats a date value into YYYY-MM-DD.
    
    Args:
        value: The date value.
        
    Returns:
        str: The formatted date or an empty string if conversion fails.
    """
    if pd.isna(value):
        return ''
    try:
        date = pd.to_datetime(value)
        return date.strftime('%Y-%m-%d')
    except Exception as e:
        logging.error(f"Error formatting date {value}: {e}")
        return ''

def create_repeat_group_payload(parent_record, child_df, filter_column, field_mapping):
    """
    Creates the payload for a single repeat group by filtering child records that match the parent.
    
    Args:
        parent_record (pd.Series): A row from the parent DataFrame.
        child_df (pd.DataFrame): The DataFrame containing child records.
        filter_column (str): The column in child_df to filter by parent's ID.
        field_mapping (dict): Mapping of API field names to child DataFrame column names.
        
    Returns:
        list: A list of dictionaries representing the repeat group data.
    """
    matching_children = child_df[child_df[filter_column] == parent_record['ID']]
    group_payload = []
    for child in matching_children.itertuples(index=False):
        child_entry = {}
        for api_field, df_column in field_mapping.items():
            child_entry[api_field] = safe_str(getattr(child, df_column))
        group_payload.append(child_entry)
    return group_payload

def process_repeat_groups_config_recursive(repeat_groups_config):
    """
    Recursively processes the repeat groups configuration to load the Excel data and preserve nested structure.
    
    If a configuration item has a "data_path" key, it's treated as a leaf group.
    Otherwise, it is assumed to be a container for nested group configurations.
    
    Args:
        repeat_groups_config (dict): The raw repeat groups configuration from JSON.
        
    Returns:
        dict: A processed configuration dictionary where each leaf group is loaded with data and
              each non-leaf group preserves its nested structure.
    """
    processed = {}
    for key, value in repeat_groups_config.items():
        if isinstance(value, dict):
            if "data_path" in value:
                # Leaf group: load its data from Excel.
                try:
                    child_df = pd.read_excel(value["data_path"])
                    processed[key] = {
                        "data": child_df,
                        "filter_column": value.get("filter_column", "Parent_ID"),
                        "fields": value["fields"]
                    }
                except Exception as e:
                    logging.error(f"Failed to load repeat group '{key}' data: {e}")
            else:
                # Non-leaf group: process nested groups recursively.
                nested = process_repeat_groups_config_recursive(value)
                if nested:
                    processed[key] = nested
        else:
            logging.warning(f"Skipping invalid repeat group configuration for key: {key}")
    return processed

def build_repeat_groups_payload_recursive(parent_record, groups_config):
    """
    Recursively builds the payload for repeat groups, preserving any nested structure.
    
    Args:
        parent_record (pd.Series): The parent record.
        groups_config (dict): Processed repeat groups configuration with unlimited nesting.
        
    Returns:
        dict: The dictionary to be merged into the final submission payload.
    """
    payload = {}
    for key, value in groups_config.items():
        if isinstance(value, dict) and "data" in value:
            # Leaf group node.
            child_df = value["data"]
            filter_column = value.get("filter_column", "Parent_ID")
            fields_mapping = value["fields"]
            payload[key] = create_repeat_group_payload(parent_record, child_df, filter_column, fields_mapping)
        elif isinstance(value, dict):
            # Non-leaf node: recursively process nested groups.
            payload[key] = build_repeat_groups_payload_recursive(parent_record, value)
        else:
            logging.warning(f"Invalid configuration for group '{key}', skipping.")
    return payload

def create_payload(parent_record, groups_config, project_uuid, formhub_uuid, form_version):
    """
    Creates the complete payload for API submission including repeat groups.
    
    The payload preserves the nested group structure as defined in the configuration.
    
    Args:
        parent_record (pd.Series): The parent record.
        groups_config (dict): Processed repeat groups configuration with unlimited nesting.
        project_uuid (str): The project UUID.
        formhub_uuid (str): The formhub UUID.
        form_version (str): The form version.
        
    Returns:
        dict: The complete payload to be submitted.
    """
    groups_payload = build_repeat_groups_payload_recursive(parent_record, groups_config)
    payload = {
        "id": project_uuid,
        "submission": {
            "formhub": {"uuid": formhub_uuid},
            "ID": safe_str(parent_record["ID"]),
            "Family_ID": safe_str(parent_record["Family_ID"]),
            **groups_payload,
            "__version__": form_version,
            "meta": {"instanceID": f"uuid:{safe_str(uuid.uuid4())}"}
        }
    }
    return payload

def main():
    """
    Main function: loads configuration and data, processes the repeat group configuration recursively,
    builds payloads, and sends API requests.
    
    Supports unlimited (arbitrarily deep) nesting of repeat groups.
    """
    configure_logging()
    config_data = load_config('./config/config.json')

    # Load parent data from Excel.
    try:
        parent_df = pd.read_excel(config_data['parent_data_path'])
    except Exception as e:
        logging.error(f"Failed to load parent data: {e}")
        return

    # Process repeat groups from configuration.
    if "repeat_groups" in config_data:
        repeat_groups = process_repeat_groups_config_recursive(config_data["repeat_groups"])
    else:
        # Fallback: single repeat group from 'child_data_path' with default mapping.
        try:
            child_df = pd.read_excel(config_data['child_data_path'])
            repeat_groups = {
                "repeat_group": {
                    "data": child_df,
                    "filter_column": "Parent_ID",
                    "fields": {"Name": "Name", "Age": "Age", "Gender": "Gender"}
                }
            }
        except Exception as e:
            logging.error(f"Failed to load child data: {e}")
            return

    # Read mandatory configuration values.
    endpoint     = config_data.get('api_endpoint', 'https://kobocat.unhcr.org/api/v1/submissions')
    project_uuid = config_data['project_uuid']
    formhub_uuid = config_data['formhub_uuid']
    form_version = config_data['formhub_version']
    api_token    = config_data['api_token_UNHCR_PROD']

    failed_logs = []

    # Process each parent record and send payloads via API.
    for idx, parent_record in tqdm(parent_df.iterrows(), total=parent_df.shape[0], desc="Submitting records"):
        headers = {
            'Authorization': f"Token {api_token}",
            'Content-Type': 'application/json'
        }
        payload = create_payload(parent_record, repeat_groups, project_uuid, formhub_uuid, form_version)
        
        try:
            response = send_request(endpoint, headers, payload)
            response.raise_for_status()
        except requests.RequestException as e:
            logging.error(f"Submission {idx} failed: {safe_str(e)}")
            failed_logs.append((idx, None, safe_str(e)))
            continue

        if response.status_code != 201:
            logging.error(f"Submission {idx}: {response.status_code} {response.text}")
            failed_logs.append((idx, response.status_code, response.text))
        else:
            logging.info(f"Submission {idx}: Success")

    # Save failed logs to Excel if any submissions failed.
    if failed_logs:
        try:
            failed_df = pd.DataFrame(failed_logs, columns=['Row', 'Status_Code', 'Response'])
            failed_df.to_excel('./logs/failed_logs.xlsx', index=False)
        except Exception as e:
            logging.error(f"Failed to write failed logs to Excel: {e}")

if __name__ == '__main__':
    main()
