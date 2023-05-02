"""Implementation of AzureEnvironment class."""

from .abstract import AbstractEnvironment
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobClient

from ..utils import LaunchError


class AzureEnvironment(AbstractEnvironment):
    """AzureEnvironment is a helper for accessing Azure resources."""

    def __init__(
        self,
        storage_account: str,
        storage_container: str,
    ):
        """Initialize an AzureEnvironment."""
        credentials = AzureEnvironment.get_credentials()
        url = f"https://{storage_account}.blob.core.windows.net"
        client = BlobClient(url, storage_container, "test.txt", credential=credentials)
        client.upload_blob("~/repos/wandb/README.md")

    @classmethod
    def from_config(cls, config: dict, verify: bool = True) -> "AzureEnvironment":
        """Create an AzureEnvironment from a config dict."""
        storage_account = config.get("storage_account")
        if storage_account is None:
            raise LaunchError(
                "Please specify a storage account to use under the environment.storage_account key."
            )
        storage_container = config.get("storage_container")
        if storage_container is None:
            raise LaunchError(
                "Please specify a storage container to use under the environment.storage_container key."
            )
        return cls(
            storage_account=storage_account,
            storage_container=storage_container,
        )

    @classmethod
    def get_credentials(cls):
        """Get Azure credentials."""
        credentials = DefaultAzureCredential()
        return credentials.get_token("https://storage.azure.com/.com")

    def upload_file(self, source: str, destination: str) -> None:
        """Upload a file to Azure blob storage."""

    def upload_dir(self, source: str, destination: str) -> None:
        pass

    def verify_storage_uri(self, uri: str) -> None:
        pass

    def verify(self) -> None:
        pass
