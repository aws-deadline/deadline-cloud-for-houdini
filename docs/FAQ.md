## How can I fail Mantra renders immediately if textures are missing?

There is a hidden feature in Mantra that can be used to fail the render immediately if textures are missing. To activate it:
1. Right click the Mantra node and go to "Parameters and channels" > "Edit Parameter interface"
2. In the "Create Parameters" section, select "Render Properties" and filter for "vm_abort_missing_texture"
3. Right click "Abort on missing texture (vm_abort_missing_texture)" and click "Install Parameter"
4. Click "Accept"
5. Left click the Mantra node and navigate to "Rendering" > "Render" in the parameter editor
6. Select the "Abort on missing texture" checkbox

## Why do I get "incomplete asset definitions" errors while rendering?

Jobs from this submitter that run in your farm may produce errors in the logs that look like:
```
The following node types are using incomplete asset definitions:
  Driver/deadline_cloud
```

These errors are safe to ignore. This submitter exists as a node in your Houdini scene. When a worker in your farm loads the scene, the scene still contains the Deadline Cloud node, but the worker may not have the submitter installed. Because the worker does not have the files needed to run the Deadline Cloud node, it logs "incomplete asset definition" errors. The Deadline Cloud node itself is not rendered as part of the job though, so these errors can be ignored.