from __future__ import annotations

import builtins
import uuid
from typing import Any

from ...errors.errors import HttpError, PlaneError
from ...models.work_items import (
    CreateWorkItemDependency,
    WorkItemDependencyResponse,
    WorkItemWithRelationType,
)
from ..base_resource import BaseResource


class WorkItemDependencies(BaseResource):
    """API client for managing work item dependency relations.

    Covers the six built-in dependency directions:
    blocking / blocked_by / start_before / start_after / finish_before / finish_after.

    Community Edition has no ``dependencies/`` route; it serves ``relations/`` with a
    different payload and response shape. ``list`` and ``create`` try ``dependencies/``
    first and fall back to ``relations/`` only when the server answers 404 for it.
    Authentication errors, server errors and timeouts are never retried elsewhere. A 404
    on a write means the request was not applied, so the fallback cannot double-write.
    ``remove`` is unchanged and not adapted for CE; CE removal support is unverified.
    """

    def __init__(self, config: Any) -> None:
        super().__init__(config, "/workspaces/")

    def list(
        self, workspace_slug: str, project_id: str, work_item_id: str
    ) -> WorkItemDependencyResponse:
        """List all dependency relations for a work item grouped by direction.

        Args:
            workspace_slug: The workspace slug identifier
            project_id: UUID of the project
            work_item_id: UUID of the work item
        """
        base = f"{workspace_slug}/projects/{project_id}/work-items/{work_item_id}"
        try:
            response = self._get(f"{base}/dependencies/")
        except HttpError as error:
            if error.status_code != 404:
                raise
            response = self._ce_groups(
                self._fallback(error, lambda: self._get(f"{base}/relations/"))
            )
        return WorkItemDependencyResponse.model_validate(response)

    def create(
        self,
        workspace_slug: str,
        project_id: str,
        work_item_id: str,
        data: CreateWorkItemDependency,
    ) -> builtins.list[WorkItemWithRelationType]:
        """Create one or more dependency relations for a work item.

        Args:
            workspace_slug: The workspace slug identifier
            project_id: UUID of the project
            work_item_id: UUID of the work item
            data: Dependency creation payload
        """
        base = f"{workspace_slug}/projects/{project_id}/work-items/{work_item_id}"
        try:
            response = self._post(f"{base}/dependencies/", data.model_dump(exclude_none=True))
        except HttpError as error:
            if error.status_code != 404:
                raise
            response = self._create_via_relations(base, data, error)
        return [WorkItemWithRelationType.model_validate(item) for item in response]

    @staticmethod
    def _fallback(original: HttpError, call: Any) -> Any:
        """Run a CE ``relations/`` call; a 404 there too means the original error stands."""
        try:
            return call()
        except HttpError as error:
            if error.status_code == 404:
                raise original from error
            raise

    @staticmethod
    def _canonical_id(value: str) -> str:
        """CE stores and returns lowercase hyphenated UUIDs whatever spelling it was sent."""
        try:
            return str(uuid.UUID(value))
        except ValueError:
            return value

    @staticmethod
    def _ce_groups(
        groups: dict[str, builtins.list[dict[str, Any]]],
    ) -> dict[str, builtins.list[dict[str, Any]]]:
        """Turn CE ``{project_id, issue_id}`` references into work item references.

        ``issue_id`` is CE's wire key for the related work item's id.
        """
        return {
            group: [
                {"id": ref["issue_id"], "project_id": ref["project_id"], "relation_type": group}
                for ref in refs
            ]
            for group, refs in groups.items()
        }

    def _create_via_relations(
        self, base: str, data: CreateWorkItemDependency, original: HttpError
    ) -> builtins.list[dict[str, Any]]:
        """Create on CE ``relations/``, posting only targets not already related.

        Reading first makes a retry after an uncertain outcome (for example a timeout)
        safe: relations that landed are returned, not created a second time.
        """
        groups = self._ce_groups(self._fallback(original, lambda: self._get(f"{base}/relations/")))
        existing = groups.get(data.relation_type, [])
        present = {ref["id"] for ref in existing}
        wanted = builtins.list(dict.fromkeys(self._canonical_id(t) for t in data.work_item_ids))
        missing = [target for target in wanted if target not in present]
        created: builtins.list[dict[str, Any]] = []
        if missing:
            # `issues` is the CE wire key for the target work item ids.
            payload = {"relation_type": data.relation_type, "issues": missing}
            response = self._fallback(original, lambda: self._post(f"{base}/relations/", payload))
            # Report the same minimal reference the read gives, from this work item's side,
            # so a first call and a retry return identical items.
            created = [
                {
                    "id": item["id"],
                    "project_id": item["project_id"],
                    "relation_type": data.relation_type,
                }
                for item in response
            ]
            # CE silently skips ids it does not know, so a short answer is a failed write.
            landed = {c["id"] for c in created}
            absent = [target for target in missing if target not in landed]
            if absent:
                raise PlaneError(
                    f"{data.relation_type} relation not created for work items: {absent}"
                )
        return [ref for ref in existing if ref["id"] in set(wanted)] + created

    def remove(
        self,
        workspace_slug: str,
        project_id: str,
        work_item_id: str,
        related_work_item_id: str,
    ) -> None:
        """Remove a dependency relation between this work item and a target.

        Args:
            workspace_slug: The workspace slug identifier
            project_id: UUID of the project
            work_item_id: UUID of the work item
            related_work_item_id: UUID of the related work item to remove the dependency with
        """
        self._delete(
            f"{workspace_slug}/projects/{project_id}/work-items/{work_item_id}/dependencies/{related_work_item_id}/"
        )
