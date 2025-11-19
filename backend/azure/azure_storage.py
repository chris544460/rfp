from azure.storage.blob import BlobServiceClient, BlobType
from azure.identity import ClientSecretCredential
from dotenv import load_dotenv
import concurrent.futures
from io import BytesIO
import datetime as dt
import pandas as pd
import numpy as np
import time
import json
import os
from azure.storage.blob.aio import BlobServiceClient as AsyncBlobServiceClient

def check_and_set_https_proxy():
    """
    this function is mostly needed for local development.
    Checks if the HTTPS_PROXY environment variable is set and sets it to "http://httpproxy:9443" if not.
    Checks if the NO_PROXY environment variable is set and sets it to ".bfm.com,.blackrock.com" if not.
    """
    if "HTTPS_PROXY" not in os.environ:
        os.environ["HTTPS_PROXY"] = "http://httpproxy:9443"
    if "NO_PROXY" not in os.environ:
        os.environ["NO_PROXY"] = ".bfm.com,.blackrock.com"

class AzureStorage(object):
    def __init__(self) -> None:
        # go up one folder
        account_url = "https://rfptool.blob.core.windows.net"

        load_dotenv()

        self.credential = ClientSecretCredential(
            tenant_id = os.environ['AZURE_TENANT_ID'],
            client_id = os.environ['AZURE_CLIENT_ID'],
            client_secret = os.environ['AZURE_CLIENT_SECRET'],
            connection_verify=False) # Disables SSL verification

        self.service_client = BlobServiceClient(account_url, credential=self.credential)

        check_and_set_https_proxy() # Mostly needed for local development

        self.storage_url = account_url

    def upload_files(self, files, container):
        """
        Uploads multiple files to the specified container in Azure Blob Storage.

        Args:
            files (list): A list of file objects to be uploaded.
            container (str): The name of the container in Azure Blob Storage.

        Returns:
            list: A list of files in the container after the upload operation.
        """

        container_client = self.service_client.get_container_client(container=container)
        for i, file in enumerate(files):
            # iterate through all files
            try:
                if hasattr(file, 'getvalue'):
                    container_client.upload_blob(name=file.name, data=file.getvalue(), overwrite=True, blob_type='BlockBlob')
                else:
                    container_client.upload_blob(name=file.name, data=file, overwrite=True, blob_type='BlockBlob')
            except:
                print(f'Error uploading file {file.name}')

        return self.list_files(container)

    def list_files(self, container: str) -> list[str]:
        """
        Retrieves a list of file names from the specified container.

        Args:
            container (str): The name of the container.

        Returns:
            list[str]: A list of file names.
        """
        names = []
        container_client = self.service_client.get_container_client(container=container)

        blob_list = container_client.list_blobs()
        for file in blob_list:
            names.append(file.name)

        return names

    async def blob_append(self, new_data: str, blob_name: str, container_name: str) -> None:
        """
        Appends a new dictionary to a JSON list stored in a blob within the specified container asynchronously.

        Args:
            new_data (str): The string to append to the JSON list.
            container_name (str): The name of the container.
            blob_name (str): The name of the blob.

        Returns:
            None
        """
        # Use the async BlobServiceClient
        async_service_client = AsyncBlobServiceClient(self.storage_url, credential=self.credential)
        async with async_service_client:
            blob_client = async_service_client.get_blob_client(container=container_name, blob=blob_name)
            await blob_client.append_block(f'{new_data}\n')

    def create_append_blob(self, blob_name: str, container_name: str) -> None:
        if blob_name not in self.list_files(container_name):
            # Create the blob as an Append Blob
            blob_client = self.service_client.get_blob_client(container=container_name, blob=blob_name)
            blob_client.create_append_blob()
            blob_client.append_block("{}\n") # Initialize with an empty JSON object

    def create_container(self, process_id: str, name: str) -> str:
        """
        Creates a container in Azure Blob Storage.

        Args:
            process_id (str): The process ID.
            name (str): The name of the container.

        Returns:
            str: The complete name of the created container.

        Raises:
            Exception: If a container with the same name already exists.
        """
        complete_name = process_id + '-' + name
        try:
            container_client = self.service_client_dynamic.create_container(complete_name)
            return complete_name
        except:
            print(container_client)
            print('Container with this name already exists')

    def delete_container(self, container: str) -> None:
        """
        Deletes the specified container from the Azure Blob Storage.

        Args:
            container (str): The name of the container to delete.

        Raises:
            Exception: If an error occurs while deleting the container.

        Returns:
            None
        """
        container_client = self.service_client_dynamic.get_container_client(container=container)
        try:
            container_client.delete_container()
        except:
            print('container does not exist')

    def delete_blob(self, container_name: str, blob_name: str) -> None:
        container_client = self.service_client.get_container_client(container_name)
        blob_client = container_client.get_blob_client(blob_name)
        try:
            blob_client.delete_blob()
        except:
            print('blob does not exist')

    def get_containers(self):
        containers = self.service_client.list_containers()
        print("Containers in your account:")
        for container in containers:
            print(container['name'])
        return containers

    def retrieve_log_file(self, container_name: str, blob_name:str):
        container_client = self.service_client.get_container_client(container_name)
        blob_client = container_client.get_blob_client(blob_name)

        json_data = BytesIO()
        blob_client.download_blob().readinto(json_data)
        json_data.seek(0)

        return json_data

    def empty_container(self, container_name: str):
        """
        Empties the specified container by deleting all blobs within it.

        Args:
            container_name (str): The name of the container to empty.

        Returns:
            None
        """
        container_client = self.service_client.get_container_client(container_name)
        blob_list = container_client.list_blobs()

        for blob in blob_list:
            blob_client = container_client.get_blob_client(blob.name)
            blob_client.delete_blob()

        print(f"Container '{container_name}' has been emptied.")

    def get_blob_as_json(self, container_name, blob_name) -> dict:
        # Get the blob client
        blob_client = self.service_client.get_blob_client(container=container_name, blob=blob_name)

        # Download the blob content as bytes and parse it as JSON
        try:
            blob_content = blob_client.download_blob().readall()
        # If file not found return {}
        except:
            return {}

        return json.loads(blob_content.decode('utf-8'))

    def read_append_blob(self, container_name: str, blob_name: str) -> list[dict]:
        # Get the blob client
        blob_client = self.service_client.get_blob_client(container=container_name, blob=blob_name)

        # Download the blob content as bytes and parse it as JSON
        try:
            blob_content = blob_client.download_blob().readall()
        # If file not found return {}
        except:
            return []

        if blob_content.decode('utf-8').startswith(','):
            content = blob_content.decode('utf-8')[1:]
        else:
            content = blob_content.decode('utf-8')

        return json.loads(f'[{content}]')

    def download_blob(self, container_name: str, blob_name: str):
        """
        Downloads a blob from the specified container.

        Args:
            container_name (str): The name of the container.
            blob_name (str): The name of the blob to download.

        Returns:
            bytes: The content of the downloaded blob.
        """
        container_client = self.service_client.get_container_client(container_name)
        blob_client = container_client.get_blob_client(blob_name)

        file = BytesIO()
        file.name = blob_name
        blob_client.download_blob().readinto(file)
        file.seek(0)

        return file

    def download_folder(self, container_name: str, folder_path: str, download_into: str):
        def save_blob_locally(file):
            os.makedirs(os.path.dirname(file["localPath"]), exist_ok=True)
            blob_client = container_client.get_blob_client(file["blobName"])
            with open(file["localPath"], "wb") as download_file:
                blob_client.download_blob().readinto(download_file)

        container_client = self.service_client.get_container_client(container=container_name)

        folder_files = []

        blob_list = container_client.list_blobs()
        for blob in blob_list:
            if blob.name.startswith(folder_path):
                folder_files.append({"localPath": os.path.join(download_into, blob.name), "blobName": blob.name})

        # Download files in parallel
        with concurrent.futures.ThreadPoolExecutor() as executor:
            executor.map(save_blob_locally, folder_files)

    def create_container(self, container_name: str):
        container_client = self.service_client.create_container(container_name)
        print(f"Container {container_name} created successfully.")
        return container_client

    def upload_json(self, object: dict, blob_path: str, container_name: str):
        file = BytesIO()
        file.name = blob_path
        file.write(json.dumps(convert_ndarray_to_list(object), indent=2).encode('utf-8'))
        file.seek(0)

        container_client = self.service_client.get_container_client(container=container_name)

        if hasattr(file, 'getvalue'):
            container_client.upload_blob(name=file.name, data=file.getvalue(), overwrite=True, blob_type='BlockBlob')
        else:
            container_client.upload_blob(name=file.name, data=file, overwrite=True, blob_type='BlockBlob')

    def convert_ndarray_to_list(obj):
        """Recursively convert numpy.ndarray objects to lists."""
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        elif isinstance(obj, dict):
            return {key: convert_ndarray_to_list(value) for key, value in obj.items()}
        elif isinstance(obj, list):
            return [convert_ndarray_to_list(item) for item in obj]
        elif isinstance(obj, pd.DataFrame):
            return obj.to_dict(orient='records')
        else:
            return obj
