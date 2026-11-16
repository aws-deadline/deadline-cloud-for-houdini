# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""A fake Deadline Cloud client for one queue, whose SearchJobs filters a list of jobs.

The tests of the job search and of canceling a refused cook's jobs use it. SearchJobs
applies the filters as the service does, so a test checks the filter the code builds,
not a canned reply. UpdateJob only records the call.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

#: The farm and queue the fake answers for. A request for any other fails the test.
FARM_ID: str = "farm-tests"
QUEUE_ID: str = "queue-tests"

# The most results the service returns in one page of SearchJobs.
_SERVICE_PAGE_LIMIT: int = 100


class SearchableQueue:
    """A Deadline Cloud client whose SearchJobs answers from a list of jobs on one queue.

    It applies the filters as the service does: a date filter on the creation time,
    parameter filters that compare a job parameter's value, and AND and OR groups. Each
    page holds at most ``page_size`` jobs, and names the next page's offset while more
    jobs are left. UpdateJob calls are recorded in ``updates``.
    """

    def __init__(self, jobs: List[Dict[str, Any]], page_size: int = _SERVICE_PAGE_LIMIT) -> None:
        self._jobs: List[Dict[str, Any]] = jobs
        self._page_size: int = page_size
        self.searches: int = 0
        self.updates: List[Dict[str, str]] = []

    def search_jobs(
        self,
        *,
        farmId: str,
        queueIds: List[str],
        filterExpressions: Dict[str, Any],
        itemOffset: int,
        pageSize: int,
    ) -> Dict[str, Any]:
        """One page of the jobs on the test queue that the filters match."""
        # Every match on the queue, then the page of them the offset asks for. The service
        # refuses a page of more than 100.
        self.searches += 1
        assert (farmId, queueIds) == (FARM_ID, [QUEUE_ID])
        assert 1 <= pageSize <= _SERVICE_PAGE_LIMIT
        found: List[Dict[str, Any]] = [j for j in self._jobs if _matches(filterExpressions, j)]
        size: int = min(pageSize, self._page_size)
        page: Dict[str, Any] = {
            "jobs": found[itemOffset : itemOffset + size],
            "totalResults": len(found),
        }
        if itemOffset + size < len(found):
            page["nextItemOffset"] = itemOffset + size
        return page

    def update_job(self, **kwargs: str) -> Dict[str, Any]:
        """Record the update."""
        self.updates.append(kwargs)
        return {}


def searchable_job(job_id: str, created: datetime, *, name: str, cook_id: str) -> Dict[str, Any]:
    """A job as SearchJobs lists it, with the two parameters every PDG job carries."""
    return {
        "jobId": job_id,
        "name": name,
        "createdAt": created,
        "jobParameters": {"CookName": {"string": name}, "PdgCookId": {"string": cook_id}},
    }


def _matches(group: Dict[str, Any], job: Dict[str, Any]) -> bool:
    """Whether the job meets a SearchJobs filter group."""
    results: List[bool] = [_meets(f, job) for f in group["filters"]]
    return all(results) if group["operator"] == "AND" else any(results)


def _meets(expression: Dict[str, Any], job: Dict[str, Any]) -> bool:
    """Whether the job meets one SearchJobs filter."""
    if "groupFilter" in expression:
        return _matches(expression["groupFilter"], job)
    # The tests' searches use only these comparisons, so any other fails the test.
    if "dateTimeFilter" in expression:
        date: Dict[str, Any] = expression["dateTimeFilter"]
        assert (date["name"], date["operator"]) == ("CREATED_AT", "GREATER_THAN_EQUAL_TO")
        return bool(job["createdAt"] >= date["dateTime"])
    parameter: Dict[str, Any] = expression["parameterFilter"]
    assert parameter["operator"] == "EQUAL"
    value: Optional[Dict[str, str]] = job["jobParameters"].get(parameter["name"])
    return value is not None and value["string"] == parameter["value"]
