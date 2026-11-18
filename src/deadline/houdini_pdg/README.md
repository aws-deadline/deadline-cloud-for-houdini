# Houdini PDG scheduler for AWS Deadline Cloud (experimental)

This package is experimental. It is a PDG scheduler that cooks a TOP network as one AWS Deadline Cloud job. It has not been tested against a production farm. Do not use it in production scenes.

## How a cook maps to a job

One cook becomes one job:
* Each TOP node whose work items run a command becomes a **step**. Wedges, partitioners, and other nodes that run nothing map to no step.
* Each work item becomes a **task** in its node's step.
* TOP wires become step dependencies. A step also depends on any step whose outputs its scene reads, even when no wire says so.

The job is created suspended. The scheduler releases each task once its work item's data is stored and every step it depends on has succeeded on the farm. Your Houdini session must stay open until the cook ends.

Before anything is uploaded, the scheduler checks the network and refuses what it can't cook, for example a node that cooks in your Houdini session between two steps, a step set to cook In-Process, or a step whose results PDG would take from files already on your workstation. Each refusal names the node and says what to change. The node's **Validate Graph** toggle turns the refusals into warnings, except the one for an unsaved scene.

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
