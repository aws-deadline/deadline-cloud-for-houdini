# Frequently Asked Questions

## Why do I get "incomplete asset definitions" errors while rendering?

Jobs from this submitter that run in your farm may produce errors in the logs that look like:

```
The following node types are using incomplete asset definitions:
  Driver/deadline_cloud
```

These errors are safe to ignore. The AWS Deadline Cloud submitter exists as a node in your Houdini scene. When a worker in your farm loads the scene, the scene still contains the Deadline Cloud node, but the worker may not have the submitter installed. Because the worker does not have the files needed to run the Deadline Cloud node, it logs "incomplete asset definition" errors. Since the Deadline Cloud node itself is not rendered as part of the job, these errors can be ignored.