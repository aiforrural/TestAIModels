"""WB artifact storage handler."""
import os
from typing import TYPE_CHECKING, Optional, Sequence, Union
from urllib.parse import urlparse

import wandb
from wandb import util
from wandb.apis import PublicApi
from wandb.sdk.artifacts.artifact_manifest_entry import ArtifactManifestEntry
from wandb.sdk.artifacts.artifacts_cache import get_artifacts_cache
from wandb.sdk.artifacts.storage_handler import StorageHandler
from wandb.sdk.lib.hashutil import b64_to_hex_id, hex_to_b64_id
from wandb.sdk.lib.paths import FilePathStr, StrPath, URIStr

if TYPE_CHECKING:
    from urllib.parse import ParseResult

    from wandb.sdk.artifacts.artifact import Artifact


class WBArtifactHandler(StorageHandler):
    """Handles loading and storing Artifact reference-type files."""

    _client: Optional[PublicApi]

    def __init__(self) -> None:
        self._scheme = "wandb-artifact"
        self._cache = get_artifacts_cache()
        self._client = None

    def can_handle(self, parsed_url: "ParseResult") -> bool:
        return parsed_url.scheme == self._scheme

    @property
    def client(self) -> PublicApi:
        if self._client is None:
            self._client = PublicApi()
        return self._client

    def load_path(
        self,
        manifest_entry: ArtifactManifestEntry,
        local: bool = False,
    ) -> Union[URIStr, FilePathStr]:
        """Load the file in the specified artifact given its corresponding entry.

        Download the referenced artifact; create and return a new symlink to the caller.

        Arguments:
            manifest_entry (ArtifactManifestEntry): The index entry to load

        Returns:
            (os.PathLike): A path to the file represented by `index_entry`
        """
        # We don't check for cache hits here. Since we have 0 for size (since this
        # is a cross-artifact reference which and we've made the choice to store 0
        # in the size field), we can't confirm if the file is complete. So we just
        # rely on the dep_artifact entry's download() method to do its own cache
        # check.

        # Parse the reference path and download the artifact if needed
        artifact_id = util.host_from_path(manifest_entry.ref)
        artifact_file_path = util.uri_from_path(manifest_entry.ref)

        dep_artifact = wandb.Artifact.from_id(hex_to_b64_id(artifact_id), self.client)
        link_target_path: FilePathStr
        if local:
            link_target_path = dep_artifact.get_path(artifact_file_path).download()
        else:
            link_target_path = dep_artifact.get_path(artifact_file_path).ref_target()

        return link_target_path

    def store_path(
        self,
        artifact: "Artifact",
        path: Union[URIStr, FilePathStr],
        name: Optional[StrPath] = None,
        checksum: bool = True,
        max_objects: Optional[int] = None,
    ) -> Sequence[ArtifactManifestEntry]:
        """Store the file or directory at the given path into the specified artifact.

        Recursively resolves the reference until the result is a concrete asset.

        Arguments:
            artifact: The artifact doing the storing path (str): The path to store name
            (str): If specified, the logical name that should map to `path`

        Returns:
            (list[ArtifactManifestEntry]): A list of manifest entries to store within
            the artifact
        """
        # Recursively resolve the reference until a concrete asset is found
        # TODO: Consider resolving server-side for performance improvements.
        while path is not None and urlparse(path).scheme == self._scheme:
            artifact_id = util.host_from_path(path)
            artifact_file_path = util.uri_from_path(path)
            target_artifact = wandb.Artifact.from_id(
                hex_to_b64_id(artifact_id), self.client
            )

            # this should only have an effect if the user added the reference by url
            # string directly (in other words they did not already load the artifact into ram.)
            target_artifact._load_manifest()

            entry = target_artifact._manifest.get_entry_by_path(artifact_file_path)
            path = entry.ref

        # Create the path reference
        path = URIStr(
            "{}://{}/{}".format(
                self._scheme,
                b64_to_hex_id(target_artifact.id),
                artifact_file_path,
            )
        )

        # Return the new entry
        return [
            ArtifactManifestEntry(
                path=name or os.path.basename(path),
                ref=path,
                size=0,
                digest=entry.digest,
            )
        ]
