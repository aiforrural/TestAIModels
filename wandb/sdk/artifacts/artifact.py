"""Artifact class."""
import contextlib
import datetime
import json
import multiprocessing.dummy
import os
import platform
import re
import shutil
import tempfile
import time
from collections import namedtuple
from copy import copy
from typing import (
    IO,
    TYPE_CHECKING,
    Any,
    ContextManager,
    Dict,
    List,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
    cast,
)
from urllib.parse import urlparse

import requests

import wandb
from wandb import data_types, env, util
from wandb.apis.public import ArtifactFiles, RetryingClient, Run
from wandb.data_types import WBValue
from wandb.errors.term import termerror, termlog, termwarn
from wandb.sdk.artifacts.artifact_download_logger import ArtifactDownloadLogger
from wandb.sdk.artifacts.artifact_manifest import ArtifactManifest
from wandb.sdk.artifacts.artifact_manifest_entry import ArtifactManifestEntry
from wandb.sdk.artifacts.artifact_manifests.artifact_manifest_v1 import (
    ArtifactManifestV1,
)
from wandb.sdk.artifacts.artifact_saver import get_staging_dir
from wandb.sdk.artifacts.artifact_state import ArtifactState
from wandb.sdk.artifacts.artifacts_cache import get_artifacts_cache
from wandb.sdk.artifacts.exceptions import (
    ArtifactFinalizedError,
    ArtifactNotLoggedError,
    WaitTimeoutError,
)
from wandb.sdk.artifacts.storage_layout import StorageLayout
from wandb.sdk.artifacts.storage_policies.wandb_storage_policy import WandbStoragePolicy
from wandb.sdk.data_types._dtypes import Type, TypeRegistry
from wandb.sdk.lib import filesystem, runid, telemetry
from wandb.sdk.lib.hashutil import B64MD5, b64_to_hex_id, hex_to_b64_id, md5_file_b64
from wandb.sdk.lib.paths import FilePathStr, LogicalPath, StrPath, URIStr

reset_path = util.vendor_setup()

from wandb_gql import gql  # noqa: E402

reset_path()

if TYPE_CHECKING:
    from wandb.sdk.interface.message_future import MessageFuture


class Artifact:
    """Flexible and lightweight building block for dataset and model versioning.

    Constructs an empty artifact whose contents can be populated using its `add` family
    of functions. Once the artifact has all the desired files, you can call
    `wandb.log_artifact()` to log it.

    Arguments:
        name: (str) A human-readable name for this artifact, which is how you can
            identify this artifact in the UI or reference it in `use_artifact` calls.
            Names can contain letters, numbers, underscores, hyphens, and dots. The name
            must be unique across a project.
        type: (str) The type of the artifact, which is used to organize and
            differentiate artifacts. Common types include `dataset` or `model`, but you
            can use any string containing letters, numbers, underscores, hyphens, and
            dots.
        description: (str, optional) Free text that offers a description of the
            artifact. The description is markdown rendered in the UI, so this is a good
            place to place tables, links, etc.
        metadata: (dict, optional) Structured data associated with the artifact, for
            example class distribution of a dataset. This will eventually be queryable
            and plottable in the UI. There is a hard limit of 100 total keys.

    Examples:
        Basic usage
        ```
        wandb.init()

        artifact = wandb.Artifact('mnist', type='dataset')
        artifact.add_dir('mnist/')
        wandb.log_artifact(artifact)
        ```

    Returns:
        An `Artifact` object.
    """

    TMP_DIR = tempfile.TemporaryDirectory("wandb-artifacts")
    GQL_FRAGMENT = """
      fragment ArtifactFragment on Artifact {
          id
          artifactSequence {
              project {
                  entityName
                  name
              }
              name
          }
          versionIndex
          artifactType {
              name
          }
          description
          metadata
          aliases {
              artifactCollectionName
              alias
          }
          state
          currentManifest {
              file {
                  directUrl
              }
          }
          commitHash
          fileCount
          createdAt
          updatedAt
      }
    """

    def __init__(
        self,
        name: str,
        type: str,
        description: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        incremental: bool = False,
        use_as: Optional[str] = None,
    ) -> None:
        if not re.match(r"^[a-zA-Z0-9_\-.]+$", name):
            raise ValueError(
                f"Artifact name may only contain alphanumeric characters, dashes, "
                f"underscores, and dots. Invalid name: {name}"
            )
        if type == "job" or type.startswith("wandb-"):
            raise ValueError(
                "Artifact types 'job' and 'wandb-*' are reserved for internal use. "
                "Please use a different type."
            )
        if incremental:
            termwarn("Using experimental arg `incremental`")

        # Internal.
        self._client: Optional[RetryingClient] = None
        storage_layout = (
            StorageLayout.V1 if env.get_use_v1_artifacts() else StorageLayout.V2
        )
        self._storage_policy = WandbStoragePolicy(
            config={
                "storageLayout": storage_layout,
                #  TODO: storage region
            }
        )
        self._tmp_dir: Optional[tempfile.TemporaryDirectory] = None
        self._added_objs: Dict[int, ArtifactManifestEntry] = {}
        self._added_local_paths: Dict[str, ArtifactManifestEntry] = {}
        self._save_future: Optional["MessageFuture"] = None
        self._dependent_artifacts: Set["Artifact"] = set()
        self._download_roots: Set[str] = set()
        # Properties.
        self._id: Optional[str] = None
        self._client_id: str = runid.generate_id(128)
        self._sequence_client_id: str = runid.generate_id(128)
        self._entity: Optional[str] = None
        self._project: Optional[str] = None
        self._name: str = name  # includes version after saving
        self._version: Optional[str] = None
        self._source_entity: Optional[str] = None
        self._source_project: Optional[str] = None
        self._source_name: str = name  # includes version after saving
        self._source_version: Optional[str] = None
        self._type: str = type
        self._description: Optional[str] = description
        self._metadata: dict = self._normalize_metadata(metadata)
        self._aliases: List[str] = ["latest"]
        self._saved_aliases: List[str] = []
        self._distributed_id: Optional[str] = None
        self._incremental: bool = incremental
        self._use_as: Optional[str] = use_as
        self._state: ArtifactState = ArtifactState.PENDING
        self._manifest: ArtifactManifest = ArtifactManifestV1(self._storage_policy)
        self._commit_hash: str = None
        self._file_count: Optional[int] = None
        self._created_at: Optional[str] = None
        self._updated_at: Optional[str] = None
        self._final: bool = False
        # Cache.
        get_artifacts_cache().store_client_artifact(self)

    def __repr__(self):
        return f"<Artifact {self.id or self.name}>"

    @classmethod
    def from_id(cls, artifact_id: str, client: RetryingClient) -> Optional["Artifact"]:
        artifact = get_artifacts_cache().get_artifact(artifact_id)
        if artifact is not None:
            return artifact

        query = gql(
            """
            query ArtifactByID($id: ID!) {
                artifact(id: $id) {
                    ...ArtifactFragment
                }
            }
            """
            + cls.GQL_FRAGMENT
        )
        response = client.execute(
            query,
            variable_values={"id": artifact_id},
        )
        attrs = response.get("artifact")
        if attrs is None:
            return None
        entity = attrs["artifactSequence"]["project"]["entityName"]
        project = attrs["artifactSequence"]["project"]["name"]
        name = "{}:v{}".format(attrs["artifactSequence"]["name"], attrs["versionIndex"])
        return cls.from_attrs(entity, project, name, attrs, client)

    @classmethod
    def from_name(
        cls, entity: str, project: str, name: str, client: RetryingClient
    ) -> "Artifact":
        query = gql(
            """
            query ArtifactByName(
                $entityName: String,
                $projectName: String,
                $name: String!
            ) {
                project(name: $projectName, entityName: $entityName) {
                    artifact(name: $name) {
                        ...ArtifactFragment
                    }
                }
            }
            """
            + cls.GQL_FRAGMENT
        )
        response = client.execute(
            query,
            variable_values={
                "entityName": entity,
                "projectName": project,
                "name": name,
            },
        )
        attrs = response.get("project", {}).get("artifact")
        if attrs is None:
            raise ValueError(
                f"Unable to fetch artifact with name {entity}/{project}/{name}"
            )
        return cls.from_attrs(entity, project, name, attrs, client)

    @classmethod
    def from_attrs(
        cls, entity: str, project: str, name: str, attrs, client: RetryingClient
    ) -> "Artifact":
        artifact = cls(name.split(":")[0], attrs["artifactType"]["name"])
        artifact._client = client
        artifact._id = attrs["id"]
        artifact._entity = entity
        artifact._project = project
        artifact._name = name
        version_aliases = [
            alias["alias"]
            for alias in attrs.get("aliases", [])
            if alias["artifactCollectionName"] == name.split(":")[0]
            and util.alias_is_version_index(alias["alias"])
        ]
        assert len(version_aliases) == 1
        artifact._version = version_aliases[0]
        artifact._source_entity = attrs["artifactSequence"]["project"]["entityName"]
        artifact._source_project = attrs["artifactSequence"]["project"]["name"]
        artifact._source_name = "{}:v{}".format(
            attrs["artifactSequence"]["name"], attrs["versionIndex"]
        )
        artifact._source_version = "v{}".format(attrs["versionIndex"])
        artifact._description = attrs["description"]
        artifact.metadata = cls._normalize_metadata(
            json.loads(attrs["metadata"] or "{}")
        )
        artifact._aliases = [
            alias["alias"]
            for alias in attrs.get("aliases", [])
            if alias["artifactCollectionName"] == name.split(":")[0]
            and not util.alias_is_version_index(alias["alias"])
        ]
        artifact._saved_aliases = copy(artifact._aliases)
        artifact._state = ArtifactState(attrs["state"])
        artifact._load_manifest(attrs["currentManifest"]["file"]["directUrl"])
        artifact._commit_hash = attrs["commitHash"]
        artifact._file_count = attrs["fileCount"]
        artifact._created_at = attrs["createdAt"]
        artifact._updated_at = attrs["updatedAt"]
        artifact._final = True
        # Cache.
        get_artifacts_cache().store_artifact(artifact)
        return artifact

    def new_draft(self) -> "Artifact":
        """Create a new draft artifact with the same content as this committed artifact.

        The artifact returned can be extended or modified and logged as a new version.
        """
        if self._state == ArtifactState.PENDING:
            raise ArtifactNotLoggedError(self, "new_draft")

        artifact = Artifact(self.source_name.split(":")[0], self.type)
        artifact._description = self.description
        artifact._metadata = self.metadata
        artifact._manifest = ArtifactManifest.from_manifest_json(
            self.manifest.to_manifest_json()
        )
        return artifact

    # Properties.

    @property
    def id(self) -> Optional[str]:
        """The artifact's ID."""
        if self._state == ArtifactState.PENDING:
            return None
        assert self._id is not None
        return self._id

    @property
    def entity(self) -> str:
        """The name of the entity of the secondary (portfolio) artifact collection."""
        if self._state == ArtifactState.PENDING:
            raise ArtifactNotLoggedError(self, "entity")
        assert self._entity is not None
        return self._entity

    @property
    def project(self) -> str:
        """The name of the project of the secondary (portfolio) artifact collection."""
        if self._state == ArtifactState.PENDING:
            raise ArtifactNotLoggedError(self, "project")
        assert self._project is not None
        return self._project

    @property
    def name(self) -> str:
        """The artifact name and version in its secondary (portfolio) collection.

        A string with the format {collection}:{alias}. Before the artifact is saved,
        contains only the name since the version is not yet known.
        """
        return self._name

    @property
    def qualified_name(self) -> str:
        """The artifact's qualified name."""
        return f"{self.entity}/{self.project}/{self.name}"

    @property
    def version(self) -> str:
        """The artifact's version in its secondary (portfolio) collection."""
        if self._state == ArtifactState.PENDING:
            raise ArtifactNotLoggedError(self, "version")
        assert self._version is not None
        return self._version

    @property
    def source_entity(self) -> str:
        """The name of the entity of the primary (sequence) artifact collection."""
        if self._state == ArtifactState.PENDING:
            raise ArtifactNotLoggedError(self, "source_entity")
        assert self._source_entity is not None
        return self._source_entity

    @property
    def source_project(self) -> str:
        """The name of the project of the primary (sequence) artifact collection."""
        if self._state == ArtifactState.PENDING:
            raise ArtifactNotLoggedError(self, "source_project")
        assert self._source_project is not None
        return self._source_project

    @property
    def source_name(self) -> str:
        """The artifact name and version in its primary (sequence) collection.

        A string with the format {collection}:{alias}. Before the artifact is saved,
        contains only the name since the version is not yet known.
        """
        return self._source_name

    @property
    def qualified_source_name(self) -> str:
        """The qualified source name."""
        return f"{self.source_entity}/{self.source_project}/{self.source_name}"

    @property
    def source_version(self) -> Optional[str]:
        """The artifact's version in its primary (sequence) collection.

        A string with the format "v{number}".
        """
        if self._state == ArtifactState.PENDING:
            return None
        assert self._source_version is not None
        return self._source_version

    @property
    def type(self) -> str:
        """The artifact's type."""
        return self._type

    @property
    def description(self) -> Optional[str]:
        """The artifact description.

        Returns:
            (str): Free text that offers a user-set description of the artifact.
        """
        return self._description

    @description.setter
    def description(self, description: Optional[str]) -> None:
        """Set the description of the artifact.

        The description is markdown rendered in the UI, so this is a good place to put
        links, etc.

        Arguments:
            desc: Free text that offers a description of the artifact.
        """
        self._description = description

    @property
    def metadata(self) -> dict:
        """User-defined artifact metadata.

        Returns:
            (dict): Structured data associated with the artifact.
        """
        return self._metadata

    @metadata.setter
    def metadata(self, metadata: dict) -> None:
        """User-defined artifact metadata.

        Metadata set this way will eventually be queryable and plottable in the UI; e.g.
        the class distribution of a dataset.

        Note: There is currently a limit of 100 total keys.

        Arguments:
            metadata: (dict) Structured data associated with the artifact.
        """
        self._metadata = self._normalize_metadata(metadata)

    @property
    def aliases(self) -> List[str]:
        """The aliases associated with this artifact.

        The list is mutable and calling `save()` will persist all alias changes.
        """
        return self._aliases

    @aliases.setter
    def aliases(self, aliases: List[str]) -> None:
        """Set the aliases associated with this artifact."""
        if any(char in alias for alias in aliases for char in ["/", ":"]):
            raise ValueError(
                "Aliases must not contain any of the following characters: /, :"
            )
        self._aliases = aliases

    @property
    def distributed_id(self) -> Optional[str]:
        return self._distributed_id

    @distributed_id.setter
    def distributed_id(self, distributed_id: Optional[str]) -> None:
        self._distributed_id = distributed_id

    @property
    def incremental(self) -> bool:
        return self._incremental

    @property
    def use_as(self) -> Optional[str]:
        return self._use_as

    @property
    def state(self) -> str:
        """The status of the artifact. One of: "PENDING", "COMMITTED", or "DELETED"."""
        return self._state.value

    @property
    def manifest(self) -> ArtifactManifest:
        """The artifact's manifest.

        The manifest lists all of its contents, and can't be changed once the artifact
        has been logged.
        """
        return self._manifest

    @property
    def digest(self) -> str:
        """The logical digest of the artifact.

        The digest is the checksum of the artifact's contents. If an artifact has the
        same digest as the current `latest` version, then `log_artifact` is a no-op.
        """
        return self._manifest.digest()

    @property
    def size(self) -> int:
        """The total size of the artifact in bytes.

        Returns:
            (int): The size in bytes of the artifact. Includes any references tracked by
                this artifact.
        """
        total_size: int = 0
        for entry in self._manifest.entries.values():
            if entry.size is not None:
                total_size += entry.size
        return total_size

    @property
    def commit_hash(self) -> str:
        """The hash returned when this artifact was committed.

        Returns:
            (str): The artifact's commit hash which is used in http URLs.
        """
        if self._state == ArtifactState.PENDING:
            raise ArtifactNotLoggedError(self, "commit_hash")
        assert self._commit_hash is not None
        return self._commit_hash

    @property
    def file_count(self) -> int:
        if self._state == ArtifactState.PENDING:
            raise ArtifactNotLoggedError(self, "file_count")
        assert self._file_count is not None
        return self._file_count

    @property
    def created_at(self) -> str:
        """The time at which the artifact was created."""
        if self._state == ArtifactState.PENDING:
            raise ArtifactNotLoggedError(self, "created_at")
        assert self._created_at is not None
        return self._created_at

    @property
    def updated_at(self) -> str:
        """The time at which the artifact was last updated."""
        if self._state == ArtifactState.PENDING:
            raise ArtifactNotLoggedError(self, "created_at")
        assert self._created_at is not None
        return self._updated_at or self._created_at

    # State management.

    def finalize(self) -> None:
        """Mark this artifact as final, disallowing further modifications.

        This happens automatically when calling `log_artifact`.

        Returns:
            None
        """
        self._final = True

    def _ensure_can_add(self) -> None:
        if self._final:
            raise ArtifactFinalizedError(artifact=self)

    def is_draft(self) -> bool:
        return self._state == ArtifactState.PENDING

    def _is_draft_save_started(self) -> bool:
        return self._save_future is not None

    def save(
        self,
        project: Optional[str] = None,
        settings: Optional["wandb.wandb_sdk.wandb_settings.Settings"] = None,
    ) -> None:
        """Persist any changes made to the artifact.

        If currently in a run, that run will log this artifact. If not currently in a
        run, a run of type "auto" will be created to track this artifact.

        Arguments:
            project: (str, optional) A project to use for the artifact in the case that
                a run is not already in context
            settings: (wandb.Settings, optional) A settings object to use when
                initializing an automatic run. Most commonly used in testing harness.

        Returns:
            None
        """
        if self._state != ArtifactState.PENDING:
            return self._update()

        if self._incremental:
            with telemetry.context() as tel:
                tel.feature.artifact_incremental = True

        if wandb.run is None:
            if settings is None:
                settings = wandb.Settings(silent="true")
            with wandb.init(project=project, job_type="auto", settings=settings) as run:
                # redoing this here because in this branch we know we didn't
                # have the run at the beginning of the method
                if self._incremental:
                    with telemetry.context(run=run) as tel:
                        tel.feature.artifact_incremental = True
                run.log_artifact(self)
        else:
            wandb.run.log_artifact(self)

    def _set_save_future(
        self, save_future: "MessageFuture", client: RetryingClient
    ) -> None:
        self._save_future = save_future
        self._client = client

    def wait(self, timeout: Optional[int] = None) -> "Artifact":
        """Wait for this artifact to finish logging, if needed.

        Arguments:
            timeout: (int, optional) Wait up to this long.

        Returns:
            Artifact
        """
        if self._state == ArtifactState.PENDING:
            if self._save_future is None:
                raise ArtifactNotLoggedError(self, "wait")
            result = self._save_future.get(timeout)
            if not result:
                raise WaitTimeoutError(
                    "Artifact upload wait timed out, failed to fetch Artifact response"
                )
            response = result.response.log_artifact_response
            if response.error_message:
                raise ValueError(response.error_message)
            self._populate_after_save(response.artifact_id)
        return self

    def _populate_after_save(self, artifact_id: str):
        query = gql(
            """
            query ArtifactByID($id: ID!) {
                artifact(id: $id) {
                    artifactSequence {
                        project {
                            entityName
                            name
                        }
                        name
                    }
                    versionIndex
                    aliases {
                        artifactCollectionName
                        alias
                    }
                    state
                    currentManifest {
                        file {
                            directUrl
                        }
                    }
                    commitHash
                    fileCount
                    createdAt
                    updatedAt
                }
            }
            """
        )
        response = self._client.execute(
            query,
            variable_values={"id": artifact_id},
        )
        attrs = response.get("artifact")
        if attrs is None:
            raise ValueError(f"Unable to fetch artifact with id {artifact_id}")
        self._id = artifact_id
        self._entity = attrs["artifactSequence"]["project"]["entityName"]
        self._project = attrs["artifactSequence"]["project"]["name"]
        self._name = "{}:v{}".format(
            attrs["artifactSequence"]["name"], attrs["versionIndex"]
        )
        self._version = "v{}".format(attrs["versionIndex"])
        self._source_entity = self._entity
        self._source_project = self._project
        self._source_name = self._name
        self._source_version = self._version
        self._aliases = [
            alias["alias"]
            for alias in attrs.get("aliases", [])
            if alias["artifactCollectionName"] == self._name.split(":")[0]
            and not util.alias_is_version_index(alias["alias"])
        ]
        self._state = ArtifactState(attrs["state"])
        with requests.get(attrs["currentManifest"]["file"]["directUrl"]) as request:
            request.raise_for_status()
            self._manifest = ArtifactManifest.from_manifest_json(
                json.loads(util.ensure_text(request.content))
            )
        self._commit_hash = attrs["commitHash"]
        self._file_count = attrs["fileCount"]
        self._created_at = attrs["createdAt"]
        self._updated_at = attrs["updatedAt"]

    def _update(self) -> None:
        """Persists artifact changes to the wandb backend."""
        aliases = None
        introspect_query = gql(
            """
            query ProbeServerAddAliasesInput {
               AddAliasesInputInfoType: __type(name: "AddAliasesInput") {
                   name
                   inputFields {
                       name
                   }
                }
            }
            """
        )
        response = self._client.execute(introspect_query)
        if response.get("AddAliasesInputInfoType"):  # wandb backend version >= 0.13.0
            aliases_to_add = set(self._aliases) - set(self._saved_aliases)
            aliases_to_delete = set(self._saved_aliases) - set(self._aliases)
            if len(aliases_to_add) > 0:
                add_mutation = gql(
                    """
                    mutation addAliases(
                        $artifactID: ID!,
                        $aliases: [ArtifactCollectionAliasInput!]!,
                    ) {
                        addAliases(
                            input: {artifactID: $artifactID, aliases: $aliases}
                        ) {
                            success
                        }
                    }
                    """
                )
                self._client.execute(
                    add_mutation,
                    variable_values={
                        "artifactID": self.id,
                        "aliases": [
                            {
                                "entityName": self._entity,
                                "projectName": self._project,
                                "artifactCollectionName": self._name.split(":")[0],
                                "alias": alias,
                            }
                            for alias in aliases_to_add
                        ],
                    },
                )
            if len(aliases_to_delete) > 0:
                delete_mutation = gql(
                    """
                    mutation deleteAliases(
                        $artifactID: ID!,
                        $aliases: [ArtifactCollectionAliasInput!]!,
                    ) {
                        deleteAliases(
                            input: {artifactID: $artifactID, aliases: $aliases}
                        ) {
                            success
                        }
                    }
                    """
                )
                self._client.execute(
                    delete_mutation,
                    variable_values={
                        "artifactID": self.id,
                        "aliases": [
                            {
                                "entityName": self._entity,
                                "projectName": self._project,
                                "artifactCollectionName": self._name.split(":")[0],
                                "alias": alias,
                            }
                            for alias in aliases_to_delete
                        ],
                    },
                )
            self._saved_aliases = copy(self._aliases)
        else:  # wandb backend version < 0.13.0
            aliases = [
                {
                    "artifactCollectionName": self._name.split(":")[0],
                    "alias": alias,
                }
                for alias in self._aliases
            ]

        mutation = gql(
            """
            mutation updateArtifact(
                $artifactID: ID!,
                $description: String,
                $metadata: JSONString,
                $aliases: [ArtifactAliasInput!]
            ) {
                updateArtifact(
                    input: {
                        artifactID: $artifactID,
                        description: $description,
                        metadata: $metadata,
                        aliases: $aliases
                    }
                ) {
                    artifact {
                        id
                    }
                }
            }
            """
        )
        self._client.execute(
            mutation,
            variable_values={
                "artifactID": self.id,
                "description": self.description,
                "metadata": util.json_dumps_safer(self.metadata),
                "aliases": aliases,
            },
        )

    # Adding, removing, getting entries.

    def __getitem__(self, name: str) -> Optional[data_types.WBValue]:
        """Get the WBValue object located at the artifact relative `name`.

        Arguments:
            name: (str) The artifact relative name to get

        Raises:
            ArtifactNotLoggedError: if the artifact isn't logged or the run is offline

        Examples:
            Basic usage
            ```
            artifact = wandb.Artifact('my_table', 'dataset')
            table = wandb.Table(columns=["a", "b", "c"], data=[[i, i*2, 2**i]])
            artifact["my_table"] = table

            wandb.log_artifact(artifact)
            ```

            Retrieving an object:
            ```
            artifact = wandb.use_artifact('my_table:latest')
            table = artifact["my_table"]
            ```
        """
        return self.get(name)

    def __setitem__(self, name: str, item: data_types.WBValue) -> ArtifactManifestEntry:
        """Add `item` to the artifact at path `name`.

        Arguments:
            name: (str) The path within the artifact to add the object.
            item: (wandb.WBValue) The object to add.

        Returns:
            ArtifactManifestEntry: the added manifest entry

        Raises:
            ArtifactFinalizedError: if the artifact has already been finalized.

        Examples:
            Basic usage
            ```
            artifact = wandb.Artifact('my_table', 'dataset')
            table = wandb.Table(columns=["a", "b", "c"], data=[[i, i*2, 2**i]])
            artifact["my_table"] = table

            wandb.log_artifact(artifact)
            ```

            Retrieving an object:
            ```
            artifact = wandb.use_artifact('my_table:latest')
            table = artifact["my_table"]
            ```
        """
        return self.add(item, name)

    @contextlib.contextmanager
    def new_file(
        self, name: str, mode: str = "w", encoding: Optional[str] = None
    ) -> ContextManager[IO]:
        """Open a new temporary file that will be automatically added to the artifact.

        Arguments:
            name: (str) The name of the new file being added to the artifact.
            mode: (str, optional) The mode in which to open the new file.
            encoding: (str, optional) The encoding in which to open the new file.

        Examples:
            ```
            artifact = wandb.Artifact('my_data', type='dataset')
            with artifact.new_file('hello.txt') as f:
                f.write('hello!')
            wandb.log_artifact(artifact)
            ```

        Returns:
            (file): A new file object that can be written to. Upon closing,
                the file will be automatically added to the artifact.

        Raises:
            ArtifactFinalizedError: if the artifact has already been finalized.
        """
        self._ensure_can_add()
        if self._tmp_dir is None:
            self._tmp_dir = tempfile.TemporaryDirectory()
        path = os.path.join(self._tmp_dir.name, name.lstrip("/"))
        if os.path.exists(path):
            raise ValueError(f"File with name {name!r} already exists at {path!r}")

        filesystem.mkdir_exists_ok(os.path.dirname(path))
        try:
            with util.fsync_open(path, mode, encoding) as f:
                yield f
        except UnicodeEncodeError as e:
            termerror(
                f"Failed to open the provided file (UnicodeEncodeError: {e}). Please "
                f"provide the proper encoding."
            )
            raise e

        self.add_file(path, name=name)

    def add_file(
        self,
        local_path: str,
        name: Optional[str] = None,
        is_tmp: Optional[bool] = False,
    ) -> ArtifactManifestEntry:
        """Add a local file to the artifact.

        Arguments:
            local_path: (str) The path to the file being added.
            name: (str, optional) The path within the artifact to use for the file being
                added. Defaults to the basename of the file.
            is_tmp: (bool, optional) If true, then the file is renamed deterministically
                to avoid collisions. (default: False)

        Examples:
            Add a file without an explicit name:
            ```
            # Add as `file.txt'
            artifact.add_file('path/to/file.txt')
            ```

            Add a file with an explicit name:
            ```
            # Add as 'new/path/file.txt'
            artifact.add_file('path/to/file.txt', name='new/path/file.txt')
            ```

        Raises:
            ArtifactFinalizedError: if the artifact has already been finalized.

        Returns:
            ArtifactManifestEntry: the added manifest entry

        """
        self._ensure_can_add()
        if not os.path.isfile(local_path):
            raise ValueError("Path is not a file: %s" % local_path)

        name = LogicalPath(name or os.path.basename(local_path))
        digest = md5_file_b64(local_path)

        if is_tmp:
            file_path, file_name = os.path.split(name)
            file_name_parts = file_name.split(".")
            file_name_parts[0] = b64_to_hex_id(digest)[:20]
            name = os.path.join(file_path, ".".join(file_name_parts))

        return self._add_local_file(name, local_path, digest=digest)

    def add_dir(self, local_path: str, name: Optional[str] = None) -> None:
        """Add a local directory to the artifact.

        Arguments:
            local_path: (str) The path to the directory being added.
            name: (str, optional) The path within the artifact to use for the directory
                being added. Defaults to the root of the artifact.

        Examples:
            Add a directory without an explicit name:
            ```
            # All files in `my_dir/` are added at the root of the artifact.
            artifact.add_dir('my_dir/')
            ```

            Add a directory and name it explicitly:
            ```
            # All files in `my_dir/` are added under `destination/`.
            artifact.add_dir('my_dir/', name='destination')
            ```

        Raises:
            ArtifactFinalizedError: if the artifact has already been finalized.

        Returns:
            None
        """
        self._ensure_can_add()
        if not os.path.isdir(local_path):
            raise ValueError("Path is not a directory: %s" % local_path)

        termlog(
            "Adding directory to artifact (%s)... "
            % os.path.join(".", os.path.normpath(local_path)),
            newline=False,
        )
        start_time = time.time()

        paths = []
        for dirpath, _, filenames in os.walk(local_path, followlinks=True):
            for fname in filenames:
                physical_path = os.path.join(dirpath, fname)
                logical_path = os.path.relpath(physical_path, start=local_path)
                if name is not None:
                    logical_path = os.path.join(name, logical_path)
                paths.append((logical_path, physical_path))

        def add_manifest_file(log_phy_path: Tuple[str, str]) -> None:
            logical_path, physical_path = log_phy_path
            self._add_local_file(logical_path, physical_path)

        import multiprocessing.dummy  # this uses threads

        num_threads = 8
        pool = multiprocessing.dummy.Pool(num_threads)
        pool.map(add_manifest_file, paths)
        pool.close()
        pool.join()

        termlog("Done. %.1fs" % (time.time() - start_time), prefix=False)

    def add_reference(
        self,
        uri: Union[ArtifactManifestEntry, str],
        name: Optional[StrPath] = None,
        checksum: bool = True,
        max_objects: Optional[int] = None,
    ) -> Sequence[ArtifactManifestEntry]:
        """Add a reference denoted by a URI to the artifact.

        Unlike adding files or directories, references are NOT uploaded to W&B. However,
        artifact methods such as `download()` can be used regardless of whether the
        artifact contains references or uploaded files.

        By default, W&B offers special handling for the following schemes:

        - http(s): The size and digest of the file will be inferred by the
          `Content-Length` and the `ETag` response headers returned by the server.
        - s3: The checksum and size will be pulled from the object metadata. If bucket
          versioning is enabled, then the version ID is also tracked.
        - gs: The checksum and size will be pulled from the object metadata. If bucket
          versioning is enabled, then the version ID is also tracked.
        - https, domain matching *.blob.core.windows.net (Azure): The checksum and size
          will be pulled from the blob metadata. If storage account versioning is
          enabled, then the version ID is also tracked.
        - file: The checksum and size will be pulled from the file system. This scheme
          is useful if you have an NFS share or other externally mounted volume
          containing files you wish to track but not necessarily upload.

        For any other scheme, the digest is just a hash of the URI and the size is left
        blank.

        Arguments:
            uri: (str) The URI path of the reference to add. Can be an object returned
                from Artifact.get_path to store a reference to another artifact's entry.
            name: (str) The path within the artifact to place the contents of this
                reference
            checksum: (bool, optional) Whether or not to checksum the resource(s)
                located at the reference URI. Checksumming is strongly recommended as it
                enables automatic integrity validation, however it can be disabled to
                speed up artifact creation. (default: True)
            max_objects: (int, optional) The maximum number of objects to consider when
                adding a reference that points to directory or bucket store prefix. For
                S3 and GCS, this limit is 10,000 by default but is uncapped for other
                URI schemes. (default: None)

        Raises:
            ArtifactFinalizedError: if the artifact has already been finalized.

        Returns:
            List["ArtifactManifestEntry"]: The added manifest entries.

        Examples:
        Add an HTTP link:
        ```python
        # Adds `file.txt` to the root of the artifact as a reference.
        artifact.add_reference("http://myserver.com/file.txt")
        ```

        Add an S3 prefix without an explicit name:
        ```python
        # All objects under `prefix/` will be added at the root of the artifact.
        artifact.add_reference("s3://mybucket/prefix")
        ```

        Add a GCS prefix with an explicit name:
        ```python
        # All objects under `prefix/` will be added under `path/` at the artifact root.
        artifact.add_reference("gs://mybucket/prefix", name="path")
        ```
        """
        self._ensure_can_add()
        if name is not None:
            name = LogicalPath(name)

        # This is a bit of a hack, we want to check if the uri is a of the type
        # ArtifactManifestEntry. If so, then recover the reference URL.
        if isinstance(uri, ArtifactManifestEntry):
            uri_str = uri.ref_url()
        elif isinstance(uri, str):
            uri_str = uri
        url = urlparse(str(uri_str))
        if not url.scheme:
            raise ValueError(
                "References must be URIs. To reference a local file, use file://"
            )

        manifest_entries = self._storage_policy.store_reference(
            self,
            URIStr(uri_str),
            name=name,
            checksum=checksum,
            max_objects=max_objects,
        )
        for entry in manifest_entries:
            self._manifest.add_entry(entry)

        return manifest_entries

    def add(self, obj: data_types.WBValue, name: StrPath) -> ArtifactManifestEntry:
        """Add wandb.WBValue `obj` to the artifact.

        ```
        obj = artifact.get(name)
        ```

        Arguments:
            obj: (wandb.WBValue) The object to add. Currently support one of
                Bokeh, JoinedTable, PartitionedTable, Table, Classes, ImageMask,
                BoundingBoxes2D, Audio, Image, Video, Html, Object3D
            name: (str) The path within the artifact to add the object.

        Returns:
            ArtifactManifestEntry: the added manifest entry

        Raises:
            ArtifactFinalizedError: if the artifact has already been finalized.

        Examples:
            Basic usage
            ```
            artifact = wandb.Artifact('my_table', 'dataset')
            table = wandb.Table(columns=["a", "b", "c"], data=[[i, i*2, 2**i]])
            artifact.add(table, "my_table")

            wandb.log_artifact(artifact)
            ```

            Retrieve an object:
            ```
            artifact = wandb.use_artifact('my_table:latest')
            table = artifact.get("my_table")
            ```
        """
        self._ensure_can_add()
        name = LogicalPath(name)

        # This is a "hack" to automatically rename tables added to
        # the wandb /media/tables directory to their sha-based name.
        # TODO: figure out a more appropriate convention.
        is_tmp_name = name.startswith("media/tables")

        # Validate that the object is one of the correct wandb.Media types
        # TODO: move this to checking subclass of wandb.Media once all are
        # generally supported
        allowed_types = [
            data_types.Bokeh,
            data_types.JoinedTable,
            data_types.PartitionedTable,
            data_types.Table,
            data_types.Classes,
            data_types.ImageMask,
            data_types.BoundingBoxes2D,
            data_types.Audio,
            data_types.Image,
            data_types.Video,
            data_types.Html,
            data_types.Object3D,
            data_types.Molecule,
            data_types._SavedModel,
        ]

        if not any(isinstance(obj, t) for t in allowed_types):
            raise ValueError(
                "Found object of type {}, expected one of {}.".format(
                    obj.__class__, allowed_types
                )
            )

        obj_id = id(obj)
        if obj_id in self._added_objs:
            return self._added_objs[obj_id]

        # If the object is coming from another artifact, save it as a reference
        ref_path = obj._get_artifact_entry_ref_url()
        if ref_path is not None:
            return self.add_reference(ref_path, type(obj).with_suffix(name))[0]

        val = obj.to_json(self)
        name = obj.with_suffix(name)
        entry = self._manifest.get_entry_by_path(name)
        if entry is not None:
            return entry

        def do_write(f: IO) -> None:
            import json

            # TODO: Do we need to open with utf-8 codec?
            f.write(json.dumps(val, sort_keys=True))

        if is_tmp_name:
            file_path = os.path.join(self.TMP_DIR.name, str(id(self)), name)
            folder_path, _ = os.path.split(file_path)
            if not os.path.exists(folder_path):
                os.makedirs(folder_path)
            with open(file_path, "w") as tmp_f:
                do_write(tmp_f)
        else:
            with self.new_file(name) as f:
                file_path = f.name
                do_write(f)

        # Note, we add the file from our temp directory.
        # It will be added again later on finalize, but succeed since
        # the checksum should match
        entry = self.add_file(file_path, name, is_tmp_name)
        self._added_objs[obj_id] = entry
        if obj._artifact_target is None:
            obj._set_artifact_target(self, entry.path)

        if is_tmp_name:
            if os.path.exists(file_path):
                os.remove(file_path)

        return entry

    def _add_local_file(
        self, name: StrPath, path: StrPath, digest: Optional[B64MD5] = None
    ) -> ArtifactManifestEntry:
        with tempfile.NamedTemporaryFile(dir=get_staging_dir(), delete=False) as f:
            staging_path = f.name
            shutil.copyfile(path, staging_path)
            os.chmod(staging_path, 0o400)

        entry = ArtifactManifestEntry(
            path=name,
            digest=digest or md5_file_b64(staging_path),
            size=os.path.getsize(staging_path),
            local_path=staging_path,
        )

        self._manifest.add_entry(entry)
        self._added_local_paths[os.fspath(path)] = entry
        return entry

    def get_path(self, name: StrPath) -> ArtifactManifestEntry:
        """Get the path to the file located at the artifact relative `name`.

        Arguments:
            name: (str) The artifact relative name to get

        Raises:
            ArtifactNotLoggedError: if the artifact isn't logged or the run is offline

        Examples:
            Basic usage
            ```
            # Run logging the artifact
            with wandb.init() as r:
                artifact = wandb.Artifact('my_dataset', type='dataset')
                artifact.add_file('path/to/file.txt')
                wandb.log_artifact(artifact)

            # Run using the artifact
            with wandb.init() as r:
                artifact = r.use_artifact('my_dataset:latest')
                path = artifact.get_path('file.txt')

                # Can now download 'file.txt' directly:
                path.download()
            ```
        """
        if self._state == ArtifactState.PENDING:
            raise ArtifactNotLoggedError(self, "get_path")

        name = LogicalPath(name)
        entry = self.manifest.entries.get(name) or self._get_obj_entry(name)[0]
        if entry is None:
            raise KeyError("Path not contained in artifact: %s" % name)
        entry._parent_artifact = self
        return entry

    def get(self, name: str) -> Optional[data_types.WBValue]:
        """Get the WBValue object located at the artifact relative `name`.

        Arguments:
            name: (str) The artifact relative name to get

        Raises:
            ArtifactNotLoggedError: if the artifact isn't logged or the run is offline

        Examples:
            Basic usage
            ```
            # Run logging the artifact
            with wandb.init() as r:
                artifact = wandb.Artifact('my_dataset', type='dataset')
                table = wandb.Table(columns=["a", "b", "c"], data=[[i, i*2, 2**i]])
                artifact.add(table, "my_table")
                wandb.log_artifact(artifact)

            # Run using the artifact
            with wandb.init() as r:
                artifact = r.use_artifact('my_dataset:latest')
                table = r.get('my_table')
            ```
        """
        if self._state == ArtifactState.PENDING:
            raise ArtifactNotLoggedError(self, "get")

        entry, wb_class = self._get_obj_entry(name)
        if entry is None:
            return None

        # If the entry is a reference from another artifact, then get it directly from
        # that artifact.
        if entry._is_artifact_reference():
            artifact = Artifact.from_id(
                hex_to_b64_id(util.host_from_path(entry.ref)), self.client
            )
            return artifact.get(util.uri_from_path(entry.ref))

        # Special case for wandb.Table. This is intended to be a short term
        # optimization. Since tables are likely to download many other assets in
        # artifact(s), we eagerly download the artifact using the parallelized
        # `artifact.download`. In the future, we should refactor the deserialization
        # pattern such that this special case is not needed.
        if wb_class == wandb.Table:
            self.download(recursive=True)

        # Get the ArtifactManifestEntry
        item = self.get_path(entry.path)
        item_path = item.download()

        # Load the object from the JSON blob
        result = None
        json_obj = {}
        with open(item_path) as file:
            json_obj = json.load(file)
        result = wb_class.from_json(json_obj, self)
        result._set_artifact_source(self, name)
        return result

    def get_added_local_path_name(self, local_path: str) -> Optional[str]:
        """Get the artifact relative name of a file added by a local filesystem path.

        Arguments:
            local_path: (str) The local path to resolve into an artifact relative name.

        Returns:
            str: The artifact relative name.

        Examples:
            Basic usage
            ```
            artifact = wandb.Artifact('my_dataset', type='dataset')
            artifact.add_file('path/to/file.txt', name='artifact/path/file.txt')

            # Returns `artifact/path/file.txt`:
            name = artifact.get_added_local_path_name('path/to/file.txt')
            ```
        """
        entry = self._added_local_paths.get(local_path, None)
        if entry is None:
            return None
        return entry.path

    def _get_obj_entry(self, name):
        """Return an object entry by name, handling any type suffixes.

        When objects are added with `.add(obj, name)`, the name is typically changed to
        include the suffix of the object type when serializing to JSON. So we need to be
        able to resolve a name, without tasking the user with appending .THING.json.
        This method returns an entry if it exists by a suffixed name.

        Args:
            name: (str) name used when adding
        """
        for wb_class in WBValue.type_mapping().values():
            wandb_file_name = wb_class.with_suffix(name)
            entry = self._manifest.entries.get(wandb_file_name)
            if entry is not None:
                return entry, wb_class
        return None, None

    # Downloading.

    def download(
        self, root: Optional[str] = None, recursive: bool = False
    ) -> FilePathStr:
        """Download the contents of the artifact to the specified root directory.

        NOTE: Any existing files at `root` are left untouched. Explicitly delete
        root before calling `download` if you want the contents of `root` to exactly
        match the artifact.

        Arguments:
            root: (str, optional) The directory in which to download this artifact's files.
            recursive: (bool, optional) If true, then all dependent artifacts are eagerly
                downloaded. Otherwise, the dependent artifacts are downloaded as needed.

        Returns:
            (str): The path to the downloaded contents.
        """
        if self._state == ArtifactState.PENDING:
            raise ArtifactNotLoggedError(self, "download")

        root = root or self._default_root()
        self._add_download_root(root)

        nfiles = len(self._manifest.entries)
        size = sum(e.size for e in self._manifest.entries.values())
        log = False
        if nfiles > 5000 or size > 50 * 1024 * 1024:
            log = True
            termlog(
                "Downloading large artifact {}, {:.2f}MB. {} files... ".format(
                    self.name, size / (1024 * 1024), nfiles
                ),
            )
            start_time = datetime.datetime.now()
        download_logger = ArtifactDownloadLogger(nfiles=nfiles)

        def _download_file(name: str) -> None:
            self.get_path(name).download(root)
            download_logger.notify_downloaded()

        pool = multiprocessing.dummy.Pool(32)
        pool.map(_download_file, self.manifest.entries)
        if recursive:
            pool.map(lambda artifact: artifact.download(), self._dependent_artifacts)
        pool.close()
        pool.join()

        if log:
            now = datetime.datetime.now()
            delta = abs((now - start_time).total_seconds())
            hours = int(delta // 3600)
            minutes = int((delta - hours * 3600) // 60)
            seconds = delta - hours * 3600 - minutes * 60
            termlog(
                f"Done. {hours}:{minutes}:{seconds:.1f}",
                prefix=False,
            )
        return root

    def checkout(self, root: Optional[str] = None) -> str:
        """Replace the specified root directory with the contents of the artifact.

        WARNING: This will DELETE all files in `root` that are not included in the
        artifact.

        Arguments:
            root: (str, optional) The directory to replace with this artifact's files.

        Returns:
           (str): The path to the checked out contents.
        """
        if self._state == ArtifactState.PENDING:
            raise ArtifactNotLoggedError(self, "checkout")

        root = root or self._default_root(include_version=False)

        for dirpath, _, files in os.walk(root):
            for file in files:
                full_path = os.path.join(dirpath, file)
                artifact_path = os.path.relpath(full_path, start=root)
                try:
                    self.get_path(artifact_path)
                except KeyError:
                    # File is not part of the artifact, remove it.
                    os.remove(full_path)

        return self.download(root=root)

    def verify(self, root: Optional[str] = None) -> bool:
        """Verify that the actual contents of an artifact match the manifest.

        All files in the directory are checksummed and the checksums are then
        cross-referenced against the artifact's manifest.

        NOTE: References are not verified.

        Arguments:
            root: (str, optional) The directory to verify. If None
                artifact will be downloaded to './artifacts/self.name/'

        Raises:
            (ValueError): If the verification fails.
        """
        if self._state == ArtifactState.PENDING:
            raise ArtifactNotLoggedError(self, "verify")

        root = root or self._default_root()

        for dirpath, _, files in os.walk(root):
            for file in files:
                full_path = os.path.join(dirpath, file)
                artifact_path = os.path.relpath(full_path, start=root)
                try:
                    self.get_path(artifact_path)
                except KeyError:
                    raise ValueError(
                        "Found file {} which is not a member of artifact {}".format(
                            full_path, self.name
                        )
                    )

        ref_count = 0
        for entry in self._manifest.entries.values():
            if entry.ref is None:
                if md5_file_b64(os.path.join(root, entry.path)) != entry.digest:
                    raise ValueError("Digest mismatch for file: %s" % entry.path)
            else:
                ref_count += 1
        if ref_count > 0:
            print("Warning: skipped verification of %s refs" % ref_count)

    def file(self, root=None):
        """Download a single file artifact to dir specified by the root.

        Arguments:
            root: (str, optional) The root directory in which to place the file.
                Defaults to './artifacts/self.name/'.

        Returns:
            (str): The full path of the downloaded file.
        """
        if self._state == ArtifactState.PENDING:
            raise ArtifactNotLoggedError(self, "file")

        if root is None:
            root = os.path.join(".", "artifacts", self.name)

        if len(self._manifest.entries) > 1:
            raise ValueError(
                "This artifact contains more than one file, call `.download()` to get "
                'all files or call .get_path("filename").download()'
            )

        return self.get_path(list(self._manifest.entries)[0]).download(root)

    def files(self, names=None, per_page=50):
        """Iterate over all files stored in this artifact.

        Arguments:
            names: (list of str, optional) The filename paths relative to the
                root of the artifact you wish to list.
            per_page: (int, default 50) The number of files to return per request

        Returns:
            (`ArtifactFiles`): An iterator containing `File` objects
        """
        if self._state == ArtifactState.PENDING:
            raise ArtifactNotLoggedError(self, "files")

        return ArtifactFiles(self._client, self, names, per_page)

    def _default_root(self, include_version=True) -> str:
        name = self.name if include_version else self.name.split(":")[0]
        root = os.path.join(env.get_artifact_dir(), name)
        if platform.system() == "Windows":
            head, tail = os.path.splitdrive(root)
            root = head + tail.replace(":", "-")
        return root

    def _add_download_root(self, dir_path: str) -> None:
        self._download_roots.add(os.path.abspath(dir_path))

    def _local_path_to_name(self, file_path: str) -> Optional[str]:
        """Convert a local file path to a path entry in the artifact."""
        abs_file_path = os.path.abspath(file_path)
        abs_file_parts = abs_file_path.split(os.sep)
        for i in range(len(abs_file_parts) + 1):
            if os.path.join(os.sep, *abs_file_parts[:i]) in self._download_roots:
                return os.path.join(*abs_file_parts[i:])
        return None

    # Others.

    def delete(self, delete_aliases=False) -> None:
        """Delete an artifact and its files.

        Examples:
            Delete all the "model" artifacts a run has logged:
            ```
            runs = api.runs(path="my_entity/my_project")
            for run in runs:
                for artifact in run.logged_artifacts():
                    if artifact.type == "model":
                        artifact.delete(delete_aliases=True)
            ```

        Arguments:
            delete_aliases: (bool) If true, deletes all aliases associated with the
                artifact. Otherwise, this raises an exception if the artifact has
                existing aliases.
        """
        if self._state == ArtifactState.PENDING:
            raise ArtifactNotLoggedError(self, "delete")

        mutation = gql(
            """
            mutation DeleteArtifact($artifactID: ID!, $deleteAliases: Boolean) {
                deleteArtifact(input: {
                    artifactID: $artifactID
                    deleteAliases: $deleteAliases
                }) {
                    artifact {
                        id
                    }
                }
            }
            """
        )
        self._client.execute(
            mutation,
            variable_values={
                "artifactID": self.id,
                "deleteAliases": delete_aliases,
            },
        )

    def link(self, target_path: str, aliases: Optional[List[str]] = None) -> None:
        """Link this artifact to a portfolio (a promoted collection of artifacts).

        Arguments:
            target_path: (str) The path to the portfolio. It must take the form
                {portfolio}, {project}/{portfolio} or {entity}/{project}/{portfolio}.
            aliases: (Optional[List[str]]) A list of strings which uniquely
                identifies the artifact inside the specified portfolio.

        Returns:
            None
        """
        if self._state == ArtifactState.PENDING:
            raise ArtifactNotLoggedError(self, "link")

        if ":" in target_path:
            raise ValueError(
                f"target_path {target_path} cannot contain `:` because it is not an "
                f"alias."
            )

        portfolio, project, entity = util._parse_entity_project_item(target_path)
        aliases = util._resolve_aliases(aliases)

        EmptyRunProps = namedtuple("Empty", "entity project")
        run = wandb.run if wandb.run else EmptyRunProps(entity=None, project=None)
        entity = entity or run.entity or self.entity
        project = project or run.project or self.project

        mutation = gql(
            """
            mutation LinkArtifact(
                $artifactID: ID!,
                $artifactPortfolioName: String!,
                $entityName: String!,
                $projectName: String!,
                $aliases: [ArtifactAliasInput!]
            ) {
                linkArtifact(
                    input: {
                        artifactID: $artifactID,
                        artifactPortfolioName: $artifactPortfolioName,
                        entityName: $entityName,
                        projectName: $projectName,
                        aliases: $aliases
                    }
                ) {
                    versionIndex
                }
            }
            """
        )
        self._client.execute(
            mutation,
            variable_values={
                "artifactID": self.id,
                "artifactPortfolioName": portfolio,
                "entityName": entity,
                "projectName": project,
                "aliases": [
                    {"alias": alias, "artifactCollectionName": portfolio}
                    for alias in aliases
                ],
            },
        )

    def used_by(self) -> List[Run]:
        """Get a list of the runs that have used this artifact."""
        if self._state == ArtifactState.PENDING:
            raise ArtifactNotLoggedError(self, "used_by")

        query = gql(
            """
            query ArtifactUsedBy(
                $id: ID!,
            ) {
                artifact(id: $id) {
                    usedBy {
                        edges {
                            node {
                                name
                                project {
                                    name
                                    entityName
                                }
                            }
                        }
                    }
                }
            }
            """
        )
        response = self._client.execute(
            query,
            variable_values={"id": self.id},
        )
        return [
            Run(
                self._client,
                edge["node"]["project"]["entityName"],
                edge["node"]["project"]["name"],
                edge["node"]["name"],
            )
            for edge in response.get("artifact", {}).get("usedBy", {}).get("edges", [])
        ]

    def logged_by(self) -> Optional[Run]:
        """Get the run that first logged this artifact."""
        if self._state == ArtifactState.PENDING:
            raise ArtifactNotLoggedError(self, "logged_by")

        query = gql(
            """
            query ArtifactCreatedBy(
                $id: ID!
            ) {
                artifact(id: $id) {
                    createdBy {
                        ... on Run {
                            name
                            project {
                                name
                                entityName
                            }
                        }
                    }
                }
            }
        """
        )
        response = self._client.execute(
            query,
            variable_values={"id": self.id},
        )
        creator = response.get("artifact", {}).get("createdBy", {})
        if creator.get("name") is None:
            return None
        return Run(
            self._client,
            creator["project"]["entityName"],
            creator["project"]["name"],
            creator["name"],
        )

    def json_encode(self) -> Dict[str, Any]:
        if self._state == ArtifactState.PENDING:
            raise ArtifactNotLoggedError(self, "json_encode")
        return util.artifact_to_json(self)

    @staticmethod
    def expected_type(
        entity_name: str, project_name: str, name: str, client: RetryingClient
    ) -> Optional[str]:
        """Returns the expected type for a given artifact name and project."""
        query = gql(
            """
            query ArtifactType(
                $entityName: String,
                $projectName: String,
                $name: String!
            ) {
                project(name: $projectName, entityName: $entityName) {
                    artifact(name: $name) {
                        artifactType {
                            name
                        }
                    }
                }
            }
            """
        )
        if ":" not in name:
            name += ":latest"
        response = client.execute(
            query,
            variable_values={
                "entityName": entity_name,
                "projectName": project_name,
                "name": name,
            },
        )
        return (
            ((response.get("project") or {}).get("artifact") or {}).get("artifactType")
            or {}
        ).get("name")

    @staticmethod
    def _normalize_metadata(metadata: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if metadata is None:
            return {}
        if not isinstance(metadata, dict):
            raise TypeError(f"metadata must be dict, not {type(metadata)}")
        return cast(
            Dict[str, Any], json.loads(json.dumps(util.json_friendly_val(metadata)))
        )

    def _load_manifest(self, url: str) -> None:
        with requests.get(url) as request:
            request.raise_for_status()
            self._manifest = ArtifactManifest.from_manifest_json(
                json.loads(util.ensure_text(request.content))
            )
        for entry in self._manifest.entries.values():
            if entry._is_artifact_reference():
                dep_artifact = Artifact.from_id(
                    hex_to_b64_id(util.host_from_path(entry.ref)), self.client
                )
                self._dependent_artifacts.add(dep_artifact)


class _ArtifactVersionType(Type):
    name = "artifactVersion"
    types = [Artifact]


TypeRegistry.add(_ArtifactVersionType)
