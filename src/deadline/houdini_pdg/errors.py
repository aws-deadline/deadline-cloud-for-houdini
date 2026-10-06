# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Errors the PDG scheduler raises, grouped by what the caller should do.

* **Refusal.** :class:`GraphRefusedError`: the graph cannot be cooked as one job.
  Nothing was submitted.
* **Cannot drive safely.** :class:`TaskMapIncompleteError`,
  :class:`TaskSuspensionError`, :class:`PayloadTransferError`: the job exists, but
  continuing would run a task without the data it needs.
* **Operational.** :class:`CookSetupError`, :class:`JobSubmissionError`,
  :class:`QuotaExceededError`, :class:`OutputManifestError`: a misconfiguration,
  an API refusal, or an unreadable artifact.
"""

from __future__ import annotations

from .validation import Verdict


class PDGSchedulerError(Exception):
    """Base for every error the PDG scheduler raises on purpose."""


class GraphRefusedError(PDGSchedulerError):
    """Validation refused the graph before any upload.

    Carries the whole :class:`~deadline.houdini_pdg.validation.Verdict`, so the
    artist sees every refusal, not only the first.
    """

    def __init__(self, verdict: Verdict) -> None:
        self.verdict = verdict
        lines = [f"{r.subject}: {r.detail}" for r in verdict.refusals]
        super().__init__(f"The graph was refused ({len(lines)} refusal(s)): " + "; ".join(lines))


class TaskMapIncompleteError(PDGSchedulerError):
    """A task in the job could not be matched to its farm ID.

    The ID names both the payload's S3 key and the task to activate, so a cook
    cannot proceed without it.
    """


class TaskSuspensionError(PDGSchedulerError):
    """A dependent step could not be suspended.

    An unsuspended task runs as soon as its step dependencies finish, which can be
    before its payload exists.
    """


class PayloadTransferError(PDGSchedulerError):
    """A work item payload could not be stored, fetched, or decoded."""


class CookSetupError(PDGSchedulerError):
    """A setting prevents the cook from starting. The message names what to change."""


class JobSubmissionError(PDGSchedulerError):
    """``CreateJob`` failed for a reason that retrying will not fix."""


class QuotaExceededError(JobSubmissionError):
    """Submission crossed a Deadline Cloud service quota.

    The remedy differs from its parent's: request an increase in Service Quotas,
    or make the cook smaller.
    """


class OutputManifestError(PDGSchedulerError):
    """The output record a worker published cannot be parsed."""


_SCHEDULER = "Deadline Cloud Scheduler (Experimental)"


def artist_message(error: BaseException) -> str:
    """The text to show on the scheduler node for an error, in words an artist can act on.

    Errors raised on purpose already say what to change. Anything else is a bug,
    so the message points to the console, where the traceback is logged.
    """
    if isinstance(error, NotImplementedError):
        return (
            f"{_SCHEDULER} cannot cook yet. This build is a preview, and cooking is not "
            "implemented. To cook this network, choose another scheduler."
        )
    if isinstance(error, PDGSchedulerError):
        return f"{_SCHEDULER}: {error}"
    return (
        f"{_SCHEDULER} hit an unexpected error: {type(error).__name__}: {error}. "
        "Details are in the Houdini console."
    )
