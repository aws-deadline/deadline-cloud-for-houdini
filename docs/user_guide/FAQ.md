# Frequently Asked Questions

## Why do I get "incomplete asset definitions" errors while rendering?

Jobs from this submitter that run in your farm may produce errors in the logs that look like:

```
The following node types are using incomplete asset definitions:
  Driver/deadline_cloud
```

These errors are safe to ignore. The AWS Deadline Cloud submitter exists as a node in your Houdini scene. When a worker in your farm loads the scene, the scene still contains the Deadline Cloud node, but the worker may not have the submitter installed. Because the worker does not have the files needed to run the Deadline Cloud node, it logs "incomplete asset definition" errors. Since the Deadline Cloud node itself is not rendered as part of the job, these errors can be ignored.

## Does the AWS Deadline Cloud submitter support USD export render workflows using Husk?

The Houdini submitter does not directly support export workflows using Husk at this time. Jobs created through the submitter will always run the adaptor which uses `hython` and therefore a Houdini engine license for the duration of the render. If you want to render an exported USD scene using just Husk and a Hydra render delegate you can use an example [job bundle](https://github.com/aws-deadline/deadline-cloud-samples/tree/mainline/job_bundles/houdini_husk_usd_render). This approach is useful to render USD scenes with only a render license (for example, Karma) without needing a Houdini engine license for the entire render. For more information on rendering USD scenes with Husk on AWS Deadline Cloud see the [Husk rendering and USD workflows](submitter/husk-rendering.md) documentation.