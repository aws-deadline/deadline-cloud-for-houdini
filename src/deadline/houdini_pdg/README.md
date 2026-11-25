# Houdini PDG scheduler for AWS Deadline Cloud (experimental)

This package is experimental. It is a PDG scheduler that cooks a TOP network as one AWS Deadline Cloud job. It has not been tested against a production farm. Do not use it in production scenes.

## How a cook maps to a job

One cook becomes one job:
* Each TOP node whose work items run a command becomes a **step**. Wedges, partitioners, and other nodes that run nothing map to no step.
* Each work item becomes a **task** in its node's step.
* TOP wires become step dependencies. A step also depends on any step whose outputs its scene reads, even when no wire says so.

The job is created suspended. The scheduler releases each task once its work item's data is stored and every step it depends on has succeeded on the farm. Your Houdini session must stay open until the cook ends.

Before anything is uploaded, the scheduler checks the network and refuses what it can't cook. Each refusal names the node and says what to change. [Why a cook is refused](#why-a-cook-is-refused) lists every case.

## Installing

The Houdini submitter installer installs the scheduler with the ROP submitter, but leaves it turned off. To turn it on:

1. Open `deadline_cloud_pdg_scheduler.json` in a text editor. It's in the `deadline_package/pdg_scheduler` folder of the submitter's install folder. The table below gives the default paths.
2. Change `"enable": false` to `"enable": true`, and save the file.
3. Restart Houdini. **Deadline Cloud Scheduler (Experimental)** now appears under Schedulers in a TOP network's Tab menu.

The one file turns the scheduler on for every Houdini version you installed the submitter for. The table gives the default paths for the Houdini submitter installer. If you picked a different install folder, or passed `--prefix`, the file is in that folder instead. If you used the Deadline Cloud submitter installer, which installs the submitters for several applications, the file is under `Submitters/Houdini` in its install folder: `<install folder>/Submitters/Houdini/deadline_package/pdg_scheduler/deadline_cloud_pdg_scheduler.json`.

| OS | User install | System install |
|---|---|---|
| Windows | `%USERPROFILE%\DeadlineCloudForHoudiniSubmitter\deadline_package\pdg_scheduler\deadline_cloud_pdg_scheduler.json` | `C:\Program Files\DeadlineCloudForHoudiniSubmitter\deadline_package\pdg_scheduler\deadline_cloud_pdg_scheduler.json` |
| Linux | `~/DeadlineCloudForHoudiniSubmitter/deadline_package/pdg_scheduler/deadline_cloud_pdg_scheduler.json` | `/opt/DeadlineCloudForHoudiniSubmitter/deadline_package/pdg_scheduler/deadline_cloud_pdg_scheduler.json` |
| macOS | `~/DeadlineCloudForHoudiniSubmitter/deadline_package/pdg_scheduler/deadline_cloud_pdg_scheduler.json` | `/Applications/DeadlineCloudForHoudiniSubmitter/deadline_package/pdg_scheduler/deadline_cloud_pdg_scheduler.json` |

A few more things to know:

* After a system install, you need admin rights to edit the file.
* To turn the scheduler off again, set `"enable": false` and restart Houdini.
* Running the installer again resets the file to `false`. Uninstalling the submitter removes the scheduler.
* In a development checkout, `hatch run install` turns the scheduler on, with no file to edit. See [DEVELOPMENT.md](../../../DEVELOPMENT.md). If you made a `deadline_pdg_for_houdini.json` package file by hand, delete it.

## Cooking

1. Create the scheduler in your TOP network and make it the network's Default TOP Scheduler.
2. Log in with Deadline Cloud Monitor or `deadline auth login`, then pick the farm and queue in the node's **Farm** and **Queue** menus. A pick is saved as your workstation's default. The menus load in the background. Until a list is ready, its menu shows your saved farm or queue and lists **Loading...** under it. If you weren't logged in, log in and click **Update Queue Parameters** to load them again. You can also set them with `deadline config set defaults.farm_id <farm-id>` and `deadline config set defaults.queue_id <queue-id>`. If the farm is in another AWS Region, also run `deadline config set defaults.farm_region <region>` after setting the farm. Then click **Update Queue Parameters**. The node's **Settings** button opens the workstation settings, which don't include the farm and queue. The node has no storage profile menu, and a pick clears the default storage profile, as in the ROP. Set it in a Deadline Cloud ROP or with `deadline config set settings.storage_profile_id <storage-profile-id>`.
3. Start the cook in one of two ways:
   * **Cook** the network, as with any scheduler. The cook is refused if the scene has unsaved changes; or
   * click **Submit** on the node. It saves unsaved changes first, or asks you to, then runs the checks and shows one confirmation with the farm, queue, job name, priority, and working directory. Submit there starts the cook. The job uses the settings stored on the node.

Both ways run the same checks, and the cook runs them again once PDG has generated the work items.

## Why a cook is refused

Before the scheduler uploads anything, it checks your network. If it finds something it can't run on the farm, it stops the cook and tells you why. Nothing is uploaded and no job is created, so a refused cook costs you nothing but a fix.

The node's error tab lists each problem with the node at fault and what to change. The log has a matching line, for example `Refused, write_batched (rop_batch): ...`. The name in brackets matches the Reason column below. Below, a node is a TOP node in your network, and a step is a step in the Deadline Cloud job, as in [How a cook maps to a job](#how-a-cook-maps-to-a-job).

The scheduler checks your network at three moments:

* When you click **Cook** or **Submit**, it looks at the nodes, how they're wired, and whether the scene is saved. **Submit** also generates the work items and checks them.
* Once PDG has generated the work items, it checks them all again before it creates the job. If a node goes over a task limit while PDG is still generating, it stops the cook right away.
* If PDG ends the cook without sending it a single work item, it tells you why, for example that the items came from cache.

| Reason | Example | Why it's refused | How to fix it |
|---|---|---|---|
| `unsaved_scene` | You cook a scene that has never been saved, or that has unsaved changes. | The farm's workers load the saved `.hip`, so they'd find no file, or cook the scene as it was last saved instead of the one you see. | Save the scene and cook again. **Submit** offers to save it for you. |
| `dynamic_node` | A node's **Generate When** is set to **Each Upstream Item is Cooked**. | The node makes its work items while the cook runs, so the job can't describe its tasks before it is submitted. The nodes below it are dynamic for the same reason, and only the node that causes it is reported. We only support cooks whose tasks are known upfront. | Set **Generate When** to **All Upstream Items are Generated**, or cook the network with another scheduler. |
| `in_process_step` | A node that becomes a step, because it runs a command, such as a ROP Geometry Output, has its **Cook Type** (`pdg_cooktype`) set to **In-Process**. | The node should run on the farm as a step of the job. With Cook Type set to In-Process, PDG cooks its work items in your Houdini session instead and never sends them to the scheduler, so the step's tasks would never run. | Set **Cook Type** to **Out-of-Process**. |
| `in_process_node` | A node that doesn't become a step, such as a Python Script set to **Cook (In-Process)**, sits below a step and writes a file that a step further down reads. | A node that doesn't run a command never becomes a step, so it always runs in your Houdini session. That's fine unless it sits below a step and touches that step's files: it reads a file a step writes on the farm, or it writes a file a step reads. Your workstation doesn't have the files written on the farm, and the farm never gets the files your workstation writes. A node with no step above it, or one that only passes work items along, is fine. | Move the file work into a node that runs on a worker, such as a ROP Fetch or a Python Script set to **Out-of-Process**. |
| `runtime_file_unproduced` | A node reads a file that no node in the cook writes, and that isn't on disk yet. | Something in your Houdini session would have to write it during the cook, and nothing can carry it to a worker. | Make the node that writes it run on a worker, or create the file before you cook so the job takes it along. |
| `rop_batch` | A ROP node has **Frames per Batch** (`framesperbatch`) above 1, or **All Frames in One Batch** (`batchall`) turned on. | We don't support batch items for Houdini PDG yet, so the cook is refused. | Turn off **All Frames in One Batch** and set **Frames per Batch** to 1. You need both, because `batchall` ignores `framesperbatch`. Each frame then loads the scene on its own, so if the work per frame is short, another scheduler may be faster. |
| `multiple_schedulers` | A node's `topscheduler` parm sends its work items to a Local Scheduler. | One cook is one job, so that node's work would run outside the job. | Clear the node's `topscheduler` parm so it uses the network's scheduler. |
| `service_node` | A node's **Cook Type** is **Service**, or the network uses a command server, such as a Houdini, Maya or Nuke server. | Each task runs in its own session on its own worker, and this scheduler doesn't start long-running services. Nothing could connect to a command server, and its task would hold a worker until it timed out. | For a Service node, set `pdg_cooktype` to **Out-of-Process**. For a command server, cook the network with another scheduler. Switching a command server to Out-of-Process only hides the problem. |
| `quota_exceeded` | A node has more than 10,000 work items. | The job would exceed a Deadline Cloud limit. The defaults are 10,000 tasks per job and per step, 200 steps per job, 128 dependencies per step, and 32 steps that depend on one step. | Make the cook smaller, or request a quota increase for AWS Deadline Cloud in AWS Service Quotas. |
| `output_outside_cook` | A node writes to your system's temp folder, and the node below reads from there. | The node writes its files to a folder the worker doesn't upload, so the node below can't find them. Workers upload only what a node writes in the **Working Directory** or in a folder under **Output Directories** on the **Job Attachments** tab. | Write under the working directory, usually `$HIP`, or add the folder to **Output Directories**. Write the path with `$HIP`, `$PDG_DIR` or `$PDG_TEMP` rather than typing out your workstation's folder, because that folder doesn't exist on the worker. |
| `local_file_unreachable` | A File node reads a path outside the working directory that doesn't exist. | No node writes the file, so the job has nothing to bring, and the task would open a missing file. | Point the path under `$HIP` or the working directory, or create the file before you cook so the job takes it along. |
| `attribute_as_variable` | An output path uses `$wedgeindex` for the `wedgeindex` work item attribute. | `$wedgeindex` is a Houdini variable, not the attribute. If the attribute is exported, it's set only on the worker, so the worker writes a different path than the one PDG expects. If it isn't exported, it's empty everywhere, and every work item writes the same file. | Use `` `@wedgeindex` `` instead, so the value is filled in when the work items are generated. |
| `step_without_items` | You cook before PDG has finished generating a node's work items, or a node depends on a node that has no work items in this cook. | The job would be missing most of its tasks, or nothing would produce the node's input files. | Press **Generate** before **Cook**, or cook again. If the node depends on nodes outside the cook, cook those too, or dirty them so they cook again. |
| `cached_outputs` | A node's output files are already next to the scene from an earlier cook. | With **Cache Mode** set to **Automatic** or **Read Files**, PDG treats those work items as done and never sends them to the scheduler. Their tasks would never run, and the nodes below would read your local files instead. | Set the node's **Cache Mode** (`pdg_cachemode`) to **Write Files**, or delete its old results first with **Delete Selected Node Results From Disk**. |
| `no_farm_work` | You cook only a Wedge node, or only nodes that run in your Houdini session. | Nothing would run on a worker, so the job would have no steps, and Deadline Cloud would reject it. | Cook a node that runs on a worker, such as a ROP Fetch or a Python Script set to **Cook (Out-of-Process)**. If the whole network runs in your session, cook it with another scheduler. |

`in_process_step` and `in_process_node` are easy to mix up. `in_process_step` is about a node that becomes a step but is set to run in your Houdini session. `in_process_node` is about a node that never becomes a step, and touches the files of a step above it. A node can get one of these refusals, never both.

Deadline Cloud can also turn the job down with a quota error when the scheduler creates it. The cook then ends as refused, and nothing is left on the farm.

### Warnings that don't stop the cook

Some things are worth knowing about but don't stop the cook. The scheduler puts these on the node's warning tab and in the log:

* Your scene isn't inside the working directory or an output folder. Any node that writes a path relative to `$HIP` then writes somewhere the worker doesn't upload.
* A node at the end of the network writes outside the working directory and the output folders. Nothing below needs the files, so the cook succeeds, but that node's files are lost.
* A node reads a file using a typed-out path to your workstation's working directory. It works only while PDG hands it the file from the node above.
* A node reads files from outside the working directory. They're uploaded with every cook, so a large one costs time.
* A node in your Houdini session, below a node that runs on the farm, has Python code that opens files. The scheduler can't tell what the code does, so it warns you instead of refusing.
* A node that runs on the farm has no work items and wasn't part of what you cooked, so the job leaves it out.
* A check couldn't run, or the scene's file references couldn't be read. The cook went ahead without that check, and the Houdini console has the reason.

### The Validate Graph toggle

Turning off the node's **Validate Graph** toggle turns every refusal above into a warning, and the cook goes ahead, except `unsaved_scene`, which always stops the cook. A quota error from Deadline Cloud itself still stops it too. Use the toggle only when you know a refusal doesn't apply to your network.

## When a cook fails

Each cook writes a folder under `<working directory>/pdg_debug/`. It holds the log, `cook.json`, `snapshot.json`, `scene_reads.json`, and the job bundle as submitted. The debug level **Graph dump and item details** adds `graph.json` and a description of the first work items of each node. The log includes the last lines of the worker log for the first few failed tasks.

## Getting the results

The scheduler does not download outputs to your workstation. Steps hand files to each other on the farm, through the working directory and the output directories listed on the node's Job Attachments tab. The node's messages call an output directory an output folder. To fetch a cook's results, use Deadline Cloud Monitor or `deadline job download-output`.

## Known issues

* A login that expires during a long cook is untested. If the cook stops reporting progress, log in again and cook again.

## Layout

| Module | What it does |
|---|---|
| `graph.py` | Maps a TOP network to the shape of one job |
| `scene_reads.py` | Finds the files each step's scene reads, and which step writes them |
| `validation.py` | Refuses graphs the scheduler cannot cook, before anything is uploaded |
| `submitter.py` | Builds and submits the job through the same path as the ROP submitter |
| `task_storage.py` | Stores work item payloads and output records in S3 |
| `farm_job.py` | Releases each task when its inputs exist, and polls for results |
| `cook.py` | Runs one cook, from PDG's start callback to its stop callback |
| `diagnostics.py` | Writes each cook's debug directory and log |
| `houdini/` | Reads PDG and Houdini objects, runs the checks before a cook starts, and runs the node's Submit button and its Farm and Queue menus |
| `houdini_plugin/` | The scheduler registration, and the HDA that gives the node its parms, icon, help, and Tab menu entry. The installer copies it into the install folder's `pdg_scheduler` folder, which goes on `hpath` when you turn the scheduler on |

The worker side lives in `src/deadline/houdini_adaptor/pdg_actions.py`. The Houdini adaptor starts Houdini once per worker session. A stock PDG job script cooks in that Houdini, with the item's scene loaded fresh, and any other command runs through a shell.

Only `houdini/host.py` and the registration module import `hou` or `pdg`. Everything else imports without Houdini, so the unit tests run anywhere.
