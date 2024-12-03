import os
import json
import requests

access_token = os.getenv("FORDEFI_API_TOKEN")
path = "/api/v1/vaults"

def ping(path, access_token):
            
    try:
        resp_tx = requests.get(
            f"https://api.fordefi.com{path}",
            headers={
                "Accept": "*/*",
                "Authorization": f"Bearer {access_token}"
            },
            params={

                "sort_by": "name_asc"
            },
        )
        resp_tx.raise_for_status()
        return resp_tx
    except requests.exceptions.HTTPError as e:
        error_message = f"HTTP error occurred: {str(e)}"
        if resp_tx.text:
            try:
                error_detail = resp_tx.json()
                error_message += f"\nError details: {error_detail}"
            except json.JSONDecodeError:
                error_message += f"\nRaw response: {resp_tx.text}"
        raise RuntimeError(error_message)
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Network error occurred: {str(e)}")

def main():
    if not access_token:
        print("Error: FORDEFI_API_TOKEN environment variable is not set")
        return
        
    try:
        response = ping(path, access_token)
        print(json.dumps(response.json(), indent=2))
        data = response.json()

        # Save data to a JSON file
        # with open('response_data.json', 'w') as json_file:
        #     json.dump(data, json_file, indent=2)
        # print("Data has been saved to 'response_data.json'")

        for vault in data['vaults']:
            print(f"Name: {(vault['name']).upper()}\nVault ID: {(vault['id'])}\nType: {(vault['type']).capitalize()}")
            if vault.get('address'):
                print(f"Address: {vault['address']}")
            elif vault.get('main_address'):
                print(f"Main Address: {vault['main_address']}")
            elif vault.get('main_address'):
                print(f"Main Address: {vault['main_address']}")
            elif vault.get('derivation_info', {}).get('master_public_key', {}).get('xpub'):
                print(f"Xpub: {vault['derivation_info']['master_public_key']['xpub']}")
            else:
                print("")
    except Exception as e:
            print(f"Error: {str(e)}")

if __name__ == "__main__":
    main()


# http://localhost:8000/api/v1/addressbook/contacts
